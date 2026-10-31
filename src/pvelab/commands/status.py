"""pvelab status [--net N] [--since 1h] [-f]: the hosts and their cluster links, who is logged in to
the student VMs, and login attempts (sshd and the web UI of the hosts, sshd of exposed VMs).
Read-only and without the lock: it runs while a create works. -f redraws it in place, like top."""

import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

from ..args import opt, valid_net
from ..config import (GUEST_USER, INTERNET_PROBE, KNOWN_EVERY, LOGIN_IP_HIGH, read_text, SDN_ZONE,
                      STATUS_EVERY)
from ..credentials import cred_rows
from ..expose import forwarded_on, is_exposed
from ..guest import guest_json
from ..helptext import bad_usage
from ..inventory import managed, net_of
from ..joining import COROSYNC_CONF
from ..logins import is_internal, parse, SSH_LOG, stamp, tally, WANTED
from ..nodes import is_local, members, multi, on_node
from ..resources import pvesh_json, vm_stats
from ..ui import fit, paint, some
from ..util import LabError, no_stdin, parallel

OVERLAP = 60          # s: each journal read starts this long before the last one ended (lines seen twice are dropped)
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(v):
    """'1h', '30m', '2d', '90s' -> seconds, or bad_usage."""
    m = re.fullmatch(r"(\d+)([smhd])", v)
    if not m or int(m.group(1)) == 0:
        bad_usage("--since takes a time like 30m, 1h or 2d")
    return int(m.group(1)) * UNITS[m.group(2)]


def span(secs):
    """3600 -> '1h', 5400 -> '90m': the window as the user wrote it."""
    for u in ("d", "h", "m"):
        if secs % UNITS[u] == 0:
            return f"{secs // UNITS[u]}{u}"
    return f"{secs}s"


# --- what the commands print ---

def sections(text):
    """Output of a status script: '@name rest' starts section name; {name: [rest, lines...]}."""
    out, cur = {}, None
    for line in (text or "").splitlines():
        if line.startswith("@"):
            name, _, rest = line[1:].partition(" ")
            cur = out.setdefault(name, [])
            if rest:
                cur.append(rest)
        elif cur is not None:
            cur.append(line)
    return out


def parse_corosync_nodes(text):
    """corosync.conf -> {nodeid: name} of its nodelist."""
    out = {}
    for block in re.findall(r"node\s*\{([^{}]*)\}", text or ""):
        name, nid = re.search(r"name:\s*(\S+)", block), re.search(r"nodeid:\s*(\d+)", block)
        if name and nid:
            out[int(nid.group(1))] = name.group(1)
    return out


def parse_links(lines):
    """`corosync-cfgtool -n` -> {nodeid: True if one of its links is connected}. It lists only the nodes
    this host reaches; any one missing is down."""
    out, nid = {}, None
    for line in lines:
        if m := re.match(r"\s*nodeid:?\s*(\d+)", line):
            nid = int(m.group(1))
            out[nid] = False
        elif nid is not None and re.search(r"\bLINK\b", line) and re.search(r"\bconnected\b", line):
            out[nid] = True
    return out


def parse_cluster(lines):
    """`pvecm status` -> {quorate, votes, expected, qdevice}, or None (no cluster)."""
    text = "\n".join(lines)
    q = re.search(r"^Quorate:\s*(\w+)", text, re.M)
    if not q:
        return None

    def num(key):
        m = re.search(rf"^{key}:\s*(\d+)", text, re.M)
        return int(m.group(1)) if m else None
    flags = re.search(r"^Flags:(.*)$", text, re.M)
    return {"quorate": q.group(1).lower() == "yes", "votes": num("Total votes"), "expected": num("Expected votes"),
            "qdevice": bool(flags and "Qdevice" in flags.group(1).split())}


def parse_rtt(text):
    """The last line of `ping -q` -> the average round trip in ms, or None (no answer)."""
    m = re.search(r"= [\d.]+/([\d.]+)/", text or "")
    return float(m.group(1)) if m else None


# The tty field may hold spaces ('sshd pts/0' for a logind session without a tty, coreutils 9.4+), and
# the date is ISO in a UTF-8 locale but 'Oct  1 14:55' in the C locale (if C.UTF-8 is missing).
WHO = re.compile(r"^(\S+)\s.*?(\d{4}-\d\d-\d\d \d\d:\d\d|[A-Z][a-z]{2} +\d{1,2} \d\d:\d\d)(?:\s+\((.*)\))?")


def who_time(text, now):
    """A `who` date, ISO or 'Oct  1 14:55' (no year: the last one not after now) -> datetime."""
    if text[0].isdigit():
        return datetime.strptime(text, "%Y-%m-%d %H:%M")
    t = datetime.strptime(f"{now.year} {' '.join(text.split())}", "%Y %b %d %H:%M")
    return t if t <= now else t.replace(year=t.year - 1)


def parse_who(lines, now):
    """`who` lines and the VM's time ('YYYY-MM-DD HH:MM') -> [(user, from, minutes)]."""
    out, now = [], datetime.strptime(now, "%Y-%m-%d %H:%M")
    for line in lines:
        if m := WHO.match(line):
            try:
                mins = now - who_time(m.group(2), now)
            except ValueError:               # a month name not in English, Feb 29 of another year
                continue
            src = (m.group(3) or "").removeprefix("::ffff:")
            out.append((m.group(1), src if src and not src.startswith(":") else "console",
                        max(0, int(mins.total_seconds() // 60))))
    return out


# --- reading ---

class Journal:
    """The login lines of one host or VM within the window, read a bit at a time: each read asks only for
    what came since the one before (minus OVERLAP), with the host's or VM's own clock."""

    def __init__(self):
        self.lines, self.last, self.now = set(), None, None

    def since(self, window):
        return f"@{int(self.last)}" if self.last else f"-{window}s"

    def add(self, lines, now, window):
        self.lines = {l for l in self.lines | set(lines) if stamp(l) >= now - window}
        self.last, self.now = now - OVERLAP, now

    def events(self):
        return parse(sorted(self.lines, key=stamp))


def sh_on(node, script, timeout=30):
    """Output of `sh -c script` on node (here or over ssh), or None (unreachable, timed out)."""
    try:
        argv = on_node(node, "sh", "-c", script)
        r = subprocess.run(argv, stdin=no_stdin(argv, None), capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (subprocess.TimeoutExpired, LabError, OSError):
        return None


def host_script(node, peers, since):
    """One host's part: its clock, the round trip to each other host, its corosync links, the login
    lines of sshd and pvedaemon since `since`, and (here) pvecm status."""
    s = ["echo @now $(date +%s)"]
    s += [f'echo "@rtt {ip} $(ping -nq -c 3 -i 0.2 -W 1 {ip} 2>/dev/null | tail -n 1)"' for ip in peers]
    if peers:
        s.append("echo @links; corosync-cfgtool -n 2>/dev/null")
    if is_local(node):
        s.append("echo @pvecm; pvecm status 2>/dev/null")
    s.append(f"echo @log; {SSH_LOG} -t pvedaemon --since={since} | grep -E '{WANTED}'")
    return "; ".join(s) + "; true"


def vm_script(since=None):
    """One VM's part, one agent call: who is logged in, its ssh connections, whether it reaches the
    internet, and (exposed VMs, since given) its sshd login lines."""
    s = ["echo @now $(date +%s)", "echo @date $(date '+%Y-%m-%d %H:%M')", "echo @who; LC_ALL=C.UTF-8 who",
         "echo @ssh; ss -Htn state established '( sport = :22 )'",
         f"echo @net; timeout 3 bash -c 'exec 3<>/dev/tcp/{INTERNET_PROBE}' 2>/dev/null && echo up || echo down"]
    if since:
        s.append(f"echo @log; {SSH_LOG} --since={since} | grep -E '{WANTED}'")
    return "; ".join(s) + "; true"


def known_script():
    """Logins of the last 7 days (who is the student, and from where)."""
    return f"{SSH_LOG} --since=-7d | grep -F 'Accepted '; true"


def agent_out(vmid, script, timeout=25):
    """Output of script in VM vmid through the guest agent, or None (no answer)."""
    r = guest_json("exec", vmid, "--timeout", timeout - 5, "--", "sh", "-c", script, timeout=timeout)
    return None if r is None else r.get("out-data", "")


class Status:
    """What status shows, read again by each refresh(); keeps the journals and each VM's known IPs."""

    def __init__(self, net=None, window=3600):
        self.net, self.window = net, window
        self.host_logs, self.vm_logs, self.known = {}, {}, {}      # node / vmid -> Journal; vmid -> (t, [(ts, ip)])
        self.zone, self.zone_t = None, 0

    def refresh(self):
        now = time.time()
        rows, man, mem = cred_rows(), managed(), members()
        nets = {i: net_of(i, rows, c) for i, c in man.items()}
        vms = {i: c for i, c in man.items() if self.net is None or nets[i] == self.net}
        exposed = forwarded_on({n for n in {nets[i] for i in vms} - {"dhcp"} if is_exposed(n)}) & set(vms)
        stats = vm_stats()
        running = [i for i in vms if stats.get(i, {}).get("status") == "running"]
        if now - self.zone_t >= KNOWN_EVERY:      # the SDN peers change only with node add
            zones = pvesh_json("/cluster/sdn/zones")
            self.zone = next((z.get("peers") for z in zones if z.get("zone") == SDN_ZONE), None)
            self.zone_t = now
        for n in mem:
            self.host_logs.setdefault(n, Journal())
        for i in exposed:
            self.vm_logs.setdefault(i, Journal())
        due = [i for i in exposed if i in running and now - self.known.get(i, (0, []))[0] >= KNOWN_EVERY]
        jobs = ([("host", n) for n, m in mem.items() if m["online"]] + [("vm", i) for i in running]
                + [("known", i) for i in due])
        got = parallel(lambda job: self.job(job, mem, exposed), jobs, workers=16)
        hosts = {n: dict(m, reached=False, links=None, rtt={}) for n, m in mem.items()}
        cluster = None
        for (kind, key), out in got.items():
            if kind == "host" and out is not None:
                hosts[key].update(reached=True, links=out["links"], rtt=out["rtt"])
                cluster = out["cluster"] if is_local(key) else cluster
            elif kind == "known" and out is not None:
                self.known[key] = (now, out)
        host_ips = {m["ip"] for m in mem.values() if m["ip"]}
        logins = {}                          # stale: the time of the last read, if this one failed
        for n, j in self.host_logs.items():
            if j.now:
                logins[("host", n)] = dict(tally(j.events(), j.now - self.window, self.window, host_ips),
                                           stale=None if got.get(("host", n)) else j.now)
        for i in exposed:
            j = self.vm_logs[i]
            if j.now:
                start, user = j.now - self.window, rows.get(i, {}).get("user") or GUEST_USER
                known = {ip for ts, ip in self.known.get(i, (0, []))[1] if ts < start}
                logins[("vm", i)] = dict(tally(j.events(), start, self.window, host_ips, user, known),
                                         stale=None if (got.get(("vm", i)) or {}).get("agent") else j.now)
        return {"time": now, "window": self.window, "net": self.net, "hosts": hosts, "cluster": cluster,
                "names": parse_corosync_nodes(read_text(COROSYNC_CONF)), "zone": self.zone, "multi": multi(),
                "vms": [{"vmid": i, "name": c.get("name", str(i)), "node": c.get("_node", ""),
                         "status": stats.get(i, {}).get("status", "?"), **(got.get(("vm", i)) or {})}
                        for i, c in sorted(vms.items(), key=lambda ic: ic[1].get("name", ""))],
                "exposed": sorted(exposed), "logins": logins, "host_ips": host_ips}

    def job(self, job, mem, exposed):
        """One read, run in parallel with the others: a host, a VM, or a VM's known IPs."""
        kind, key = job
        if kind == "host":
            j = self.host_logs[key]
            peers = [m["ip"] for n, m in mem.items() if n != key and m["ip"]] if len(mem) > 1 else []
            out = sh_on(key, host_script(key, peers, j.since(self.window)))
            sec = sections(out)
            if out is None or not sec.get("now"):
                return None
            j.add(sec.get("log", []), int(sec["now"][0]), self.window)
            rtt = {line.split()[0]: parse_rtt(line) for line in sec.get("rtt", []) if line.split()}
            return {"links": parse_links(sec["links"]) if "links" in sec else None, "rtt": rtt,
                    "cluster": parse_cluster(sec.get("pvecm", []))}
        if kind == "known":
            out = agent_out(key, known_script())
            user = cred_rows().get(key, {}).get("user") or GUEST_USER
            return None if out is None else [(e.ts, e.ip) for e in parse(out.splitlines())
                                             if e.kind == "accept" and e.user == user]
        j = self.vm_logs.get(key)
        sec = sections(agent_out(key, vm_script(j.since(self.window) if j else None)))
        if not sec.get("now"):
            return {"agent": False}
        if j:
            j.add(sec.get("log", []), int(sec["now"][0]), self.window)
        return {"agent": True, "sessions": parse_who(sec.get("who", []), (sec.get("date") or ["1970-01-01 00:00"])[0]),
                "ssh": len([l for l in sec.get("ssh", []) if l.strip()]), "internet": (sec.get("net") or ["?"])[0]}


# --- showing ---

def hm(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"


def ips(counter, n=4, high=None):
    """'77.239.124.41 310, 77.239.124.69 213 +3'; with high, IPs at LOGIN_IP_HIGH+ are yellow."""
    top = counter.most_common()
    shown = [paint(f"{ip} {k}", "yellow" if high and k >= LOGIN_IP_HIGH else None) for ip, k in top[:n]]
    return ", ".join(shown) + (f" +{len(top) - n}" if len(top) > n else "")


def host_lines(snap):
    out = [paint("Hosts", "bold")]
    names = snap["names"]
    ids = {v: k for k, v in names.items()}
    width = max(len(n) for n in snap["hosts"])
    for node, h in sorted(snap["hosts"].items(), key=lambda nh: (not is_local(nh[0]), nh[0])):
        if not h["online"]:
            out.append(f"  {node:<{width}}  " + paint("offline", "red"))
            continue
        if not h["reached"]:
            out.append(f"  {node:<{width}}  online, " + paint("not answering (ssh)", "yellow"))
            continue
        links = []
        for peer, p in sorted(snap["hosts"].items()):
            if peer == node:
                continue
            nid = ids.get(peer)                  # its corosync node id
            up = None if h["links"] is None or nid is None else h["links"].get(nid, False)
            rtt = h["rtt"].get(p["ip"])
            state = paint("link ?", "yellow") if up is None else "link up" if up else paint("link down", "red")
            links.append(f"{peer}: {state}" + (f", {rtt:.1f} ms" if rtt is not None else ", " + paint("no ping reply (ICMP blocked?)", "yellow")))
        out.append(f"  {node:<{width}}  " + paint("online", "green") + ("   " + "   ".join(links) if links else ""))
    c = snap["cluster"]
    if c is None:
        out.append("  cluster: none" + (" (one host)" if len(snap["hosts"]) == 1 else ""))
    else:
        out.append("  cluster: " + (paint("quorate", "green") if c["quorate"] else paint("NOT quorate", "red"))
                   + f", votes {c['votes']} of {c['expected']} expected, "
                   + ("QDevice set up" if c["qdevice"] else "no QDevice"))
    if snap["zone"]:
        out.append(f"  VXLAN zone {SDN_ZONE}: peers {snap['zone'].replace(',', ', ')}; "
                   "VXLAN: not checked (pvelab node add HOST checks UDP 4789)")
    return out


def vm_lines(snap):
    title = "Students" + (f" (net{snap['net']})" if snap["net"] and snap["net"] != "dhcp" else
                          " (vmbr1)" if snap["net"] else "")
    out = [paint(title, "bold")]
    vms = snap["vms"]
    if not vms:
        return out + ["  no student VMs"]
    hostcol = snap["multi"]
    width = max(len(v["name"]) for v in vms)
    idle, offline, stopped, dead, gone, unknown = [], [], [], [], [], []
    for v in vms:
        if v["status"] == "stopped":
            stopped.append(v["name"])
        elif v["status"] != "running":       # 'unknown': PVE has no news of it, e.g. its host is offline
            host = snap["hosts"].get(v["node"])
            (gone if host and not host["online"] else unknown).append(v["name"])
        elif not v.get("agent"):
            dead.append(v["name"])
        elif not v["sessions"] and not v["ssh"]:
            idle.append(v["name"])
            if v["internet"] != "up":
                offline.append(v["name"])
        else:
            who = "; ".join(f"{u} from {src} {hm(m)}" for u, src, m in v["sessions"]) or "nobody logged in"
            net = "internet ok" if v["internet"] == "up" else paint("no internet", "yellow")
            host = f"{v['node']:<10} " if hostcol else ""
            out.append(f"  {v['name']:<{width}}  {host}{who}   ssh connections: {v['ssh']}   {net}")
    if idle:
        out.append(f"  {len(idle)} VM{'s' if len(idle) != 1 else ''}: nobody logged in"
                   + (paint(f" (no internet: {some(offline)})", "yellow") if offline else ""))
    if stopped:
        out.append(paint(f"  stopped ({len(stopped)}): {some(stopped)}", "dim"))
    if dead:
        out.append(paint(f"  agent not answering ({len(dead)}): {some(dead)}", "yellow"))
    if gone:
        out.append(paint(f"  host offline ({len(gone)}): {some(gone)}", "red"))
    if unknown:
        out.append(paint(f"  status unknown ({len(unknown)}): {some(unknown)}", "yellow"))
    return out


def login_lines(snap):
    out = [paint(f"Login attempts, last {span(snap['window'])}", "bold")]
    names = {v["vmid"]: v["name"] for v in snap["vms"]}
    hosts = snap["host_ips"]
    alarms, many, quiet = [], False, []
    for (kind, key), t in sorted(snap["logins"].items(),         # this host, the others, the VMs
                                  key=lambda kt: (kt[0][0] != "host", not is_local(kt[0][1]), str(kt[0][1]))):
        who = key if kind == "host" else names.get(key, str(key))
        label = f"{who} ssh" if kind == "host" else f"{who} ssh (exposed)"
        if t.get("stale"):                   # not read this time: the numbers of the last read
            label += paint(f" (as of {time.strftime('%H:%M', time.localtime(t['stale']))}, not answering)", "yellow")
        if not t["fails"] and kind != "host" and not t.get("stale"):
            quiet.append(who)                # VMs with nothing to show: one line for all of them
        elif not t["fails"]:
            out.append(f"  {label}: no failed logins")
        else:
            rate = t["fails"] * 3600 / max(snap["window"], 1)
            out.append(paint(f"  {label}: {t['fails']} failed ({rate:.0f}/h)", "red" if t["rate_high"] else None))
            if t["outside"]:
                out.append(f"    outside: {ips(t['outside'], high=True)}")
            if t["inside"]:
                out.append(f"    inside: {ips(t['inside'])}")
            if t["own"]:
                out.append(f"    wrong password (student): {ips(t['own'])}")
            if t["users"]:
                out.append(f"    users tried: {', '.join(u for u, _ in t['users'].most_common(6))}"
                           + (f" +{len(t['users']) - 6}" if len(t["users"]) > 6 else ""))
        many = many or any(k >= LOGIN_IP_HIGH for k in t["outside"].values())
        if kind == "host":
            web = t["web"]
            outside = {ip: k for ip, k in web.items() if not is_internal(ip, hosts)}
            text = ("no failed logins" if not web else f"{sum(web.values())} failed: "
                    + ", ".join(f"{ip} {k}" + ("" if ip in outside else " (inside)") for ip, k in web.most_common(4))
                    + (f" +{len(web) - 4}" if len(web) > 4 else ""))
            out.append(f"  {who} web UI (8006): {text}")
            many = many or any(k >= LOGIN_IP_HIGH for k in outside.values())
        for ip, k, user in t["guessed"]:
            alarms.append(f"  ! {who}: {k} failures from {ip}, then a login as {user}: maybe a guessed password")
        for ip, user in t["odd"]:
            alarms.append(f"  ! {who}: login as {user} from outside ({ip})")
    if quiet:
        out.append(paint(f"  {len(quiet)} exposed VM{'s' if len(quiet) != 1 else ''}: no failed logins", "dim"))
    if len(out) == 1:
        out.append("  (none read yet)")
    out += [paint(a, "red") for a in alarms]
    if many:
        out.append(paint("  hint: many attempts from outside. Possible steps: root by ssh key only "
                         "(PermitRootLogin prohibit-password), a security group rule for TCP 22 and 8006, "
                         "or fail2ban. status changes nothing.", "dim"))
    return out


def render(snap):
    """All of status as lines (colored on a terminal)."""
    head = paint(f"pvelab status  {time.strftime('%H:%M:%S', time.localtime(snap['time']))}", "dim")
    return [head, *host_lines(snap), "", *vm_lines(snap), "", *login_lines(snap)]


def frame(lines, cols, rows):
    """lines cut to the window: at most rows-1 lines (the last says how many more) of cols-1 characters."""
    room = max(1, rows - 1)
    if len(lines) > room:
        lines = lines[:room - 1] + [paint(f"… {len(lines) - room + 1} more lines (a bigger window shows them)", "dim")]
    return [fit(line, cols - 1) for line in lines]


def follow(st):
    """-f: read every STATUS_EVERY s, redraw every second (the window size too) until Ctrl-C."""
    tty = sys.stdout.isatty()
    out, snap, due = sys.stdout, None, 0
    if tty:
        out.write("\033[?25l\033[H\033[2J" + paint("reading the hosts and VMs ...", "dim"))
        out.flush()
    try:
        while True:
            if time.time() >= due:
                snap = st.refresh()
                due = time.time() + STATUS_EVERY
                if not tty:                  # a pipe or log: each reading once, plain
                    print("\n".join(render(snap)) + "\n", flush=True)
            if tty:
                size = shutil.get_terminal_size((100, 30))
                lines = frame(render(snap) + [paint(f"every {STATUS_EVERY}s - Ctrl-C to stop", "dim")],
                              size.columns, size.lines)
                out.write("\033[H" + "".join(line + "\033[K\n" for line in lines) + "\033[J")
                out.flush()
            time.sleep(1 if tty else max(0.0, due - time.time()))
    except KeyboardInterrupt:
        pass
    finally:
        if tty:
            out.write("\033[?25h")
            out.flush()


def cmd_status(args):
    net, window, keep = None, 3600, False
    it = iter(args)
    for a in it:
        if a in ("-f", "--follow"):
            keep = True
        elif (v := opt(a, it, "--net")) is not None:
            net = "dhcp" if v == "dhcp" else valid_net(v)
        elif (v := opt(a, it, "--since")) is not None:
            window = parse_since(v)
        else:
            bad_usage(f"unknown option '{a}'")
    st = Status(net, window)
    if keep:
        return follow(st)
    for line in render(st.refresh()):
        print(line)

