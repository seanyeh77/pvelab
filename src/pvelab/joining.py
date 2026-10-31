"""pvelab node add ADDR: check a PVE host (read-only), make the cluster if there is none, join the host,
and prepare it for pvelab. The steps (also in `pvelab help node`):
  0. a rerun for a host that is a member already skips to 4
  1. check the new host, read-only (CHECKS)       2. make the cluster if there is none (a typed yes)
  3. join the host: pvecm add there (a typed yes)  4. verify: the host online, the cluster quorate
  5. prepare the host for pvelab                   6. the note about quorum with two hosts

pvelab never reads or stores a password: ssh, sudo and `pvecm add` ask on the real terminal. The new
host is reached with one ssh connection (a ControlMaster) as the user the instructor names; after the
join, pvelab uses the cluster's own root ssh (nodes.run_on) like for any other node."""

import ipaddress
import os
import pty
import re
import secrets
import select
import shlex
import socket
import subprocess
import sys
import termios
import time
import tty

from . import sdn
from .config import bridge, is_sdn, LAB_DIR
from .ifaces import our_nets
from .isolation import ensure_nft_unit, host_table, sync_host
from .nodes import here_first, in_cluster, is_local, is_online, members, parse_version, quorate, this_node
from .templates import ensure_snippet
from . import ui
from .ui import check_line, Ok, paint, Plan, Spinner, step
from .util import concurrently, confirm_yes, die, LabError, run, wait_for

CLUSTER = "pvelab"                            # the cluster's name when pvelab makes it
JOIN_CTL = f"{LAB_DIR}/ssh-join-%C"           # the connection to the new host during node add
TCP_PORTS = (22, 8006)                        # ssh, the web UI / API (the join uses it)
UDP_PORTS = (4789, *range(5405, 5413))        # VXLAN, corosync
UDP_SECS = 4                                  # how long a UDP listener waits for the token
VERIFY_SECS = 120
JOIN_TRIES = 3                                # root password of this host, typed into pvecm add


class Remote:
    """The new host, over one ssh connection as user (a sudoer, or root). ssh's own prompts (host key,
    password) show on the terminal; the commands after open() reuse the connection."""

    def __init__(self, addr, user="root", opts=()):
        self.addr, self.user, self.opts = addr, user or "root", list(opts)    # opts: more ssh options

    def ssh(self, *opts):
        return ["ssh", "-o", f"ControlPath={JOIN_CTL}", *self.opts, *opts]

    def open(self, batch=False):
        """Connect; batch: never ask (no key that works: False instead of stopping)."""
        r = subprocess.run(self.ssh("-o", "ControlMaster=yes", "-o", "ControlPersist=600",
                                    *(["-o", "BatchMode=yes"] if batch else []), "-fN", f"{self.user}@{self.addr}"),
                           **({"stdin": subprocess.DEVNULL, "stderr": subprocess.DEVNULL} if batch else {}))
        if r.returncode and not batch:
            die(f"could not ssh to {self.user}@{self.addr}")
        return not r.returncode

    def run(self, cmd, timeout=60):
        """(exit code, output) of shell line cmd there; never asks anything."""
        try:
            r = subprocess.run(self.ssh("-o", "BatchMode=yes", "-T", f"{self.user}@{self.addr}", "--", cmd),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            ui.log(f"$ {cmd[:200]}\n(timed out after {timeout}s)")
            return 124, ""
        ui.log(f"$ {cmd[:200]}\n{r.stdout}(exit {r.returncode})")
        return r.returncode, r.stdout

    def tty(self, cmd):
        """(exit code, output) of shell line cmd there, on a terminal (for sudo's and pvecm's password
        prompts): see relay."""
        return relay(self.ssh("-t", f"{self.user}@{self.addr}", "--", cmd))

    def close(self):
        subprocess.run(self.ssh("-O", "exit", f"{self.user}@{self.addr}"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def relay(argv):
    """Run argv on a pseudo-terminal of its own: what it prints is logged and shown dim and indented
    ('  │ '), so it stands apart from pvelab's own lines; the user's keys go straight to it (password
    prompts work, and stay unechoed). Without a terminal: run plainly. Returns (exit code, output)."""
    if not sys.stdin.isatty():
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        sys.stdout.write(r.stdout)
        ui.log(r.stdout)
        return r.returncode, r.stdout
    pid, fd = pty.fork()
    if pid == 0:                                         # the child: argv on the new terminal
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    out, bol, old = [], True, termios.tcgetattr(0)
    bar = paint("  │ ", "dim")
    try:
        tty.setraw(0)
        while True:
            ready = select.select([fd, 0], [], [])[0]
            if 0 in ready:
                keys = os.read(0, 1024)
                if keys:
                    os.write(fd, keys)
            if fd in ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:                          # the child is gone (EIO)
                    break
                if not data:
                    break
                text = data.decode(errors="replace")
                out.append(text)
                shown = []
                for ch in text:                          # the bar at the start of each line
                    if bol and ch not in "\r\n":
                        shown.append(bar)
                    shown.append(ch)
                    bol = ch == "\n"
                sys.stdout.write("".join(shown))
                sys.stdout.flush()
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old)
        if not bol:
            print()
    rc = os.waitpid(pid, 0)[1]
    text = "".join(out).replace("\r\n", "\n")
    ui.log(text)
    return os.waitstatus_to_exitcode(rc), text


# --- parsers (pure) ---

def parse_getent(text):
    """`getent hosts NAME` -> its addresses."""
    return [line.split()[0] for line in (text or "").splitlines() if line.split()]


def parse_fingerprint(text):
    """`openssl x509 -noout -fingerprint -sha256` -> 'AB:CD:...', or None."""
    m = re.search(r"Fingerprint=([0-9A-Fa-f:]+)", text or "")
    return m.group(1).upper() if m else None


def ranges(ports):
    """[5405, 5406, 5407, 4789] -> '4789, 5405-5407'."""
    out, ports = [], sorted(set(ports))
    for p in ports:
        if out and p == out[-1][1] + 1:
            out[-1][1] = p
        else:
            out.append([p, p])
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in out)


def blocked(proto, to_here, to_there, addr):
    """One short text for the ports that did not get through, by direction ('' when none)."""
    both = set(to_here) & set(to_there)
    parts = [f"{proto} {ranges(both)} blocked both ways"] if both else []
    if set(to_here) - both:
        parts.append(f"{proto} {ranges(set(to_here) - both)} blocked from {addr} to here")
    if set(to_there) - both:
        parts.append(f"{proto} {ranges(set(to_there) - both)} blocked from here to {addr}")
    return "; ".join(parts)


# --- this host's side of the checks (tests replace these) ---

def tcp_ok(ip, port):
    try:
        socket.create_connection((ip, port), timeout=3).close()
        return True
    except OSError:
        return False


def udp_listen(ports):
    """({port: socket}, [ports already in use here]) - a short-lived listener on each free port."""
    socks, busy = {}, []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("", p))
            socks[p] = s
        except OSError:
            s.close()
            busy.append(p)
    return socks, busy


def udp_received(socks, token, secs=1.0):
    """The ports whose listener got 'token:port' (within secs after the sender is done); closes them."""
    got, end = set(), time.time() + secs
    try:
        for p, s in socks.items():
            s.settimeout(max(0.05, end - time.time()))
            try:
                while p not in got:
                    if s.recv(200).decode(errors="replace") == f"{token}:{p}":
                        got.add(p)
            except OSError:                  # nothing (more) came in time
                pass
    finally:
        for s in socks.values():
            s.close()
    return got


def udp_send(ip, ports, token, secs=UDP_SECS + 1):
    """Send 'token:port' to each port of ip, a few times a second for secs."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    end = time.time() + secs
    try:
        while time.time() < end:
            for p in ports:
                try:
                    s.sendto(f"{token}:{p}".encode(), (ip, p))
                except OSError:
                    pass
            time.sleep(0.25)
    finally:
        s.close()


def new_token():
    return secrets.token_hex(8)


# --- the other side: perl is on every PVE host, and needs no root for ports above 1024 ---

def perl(script):
    return f"perl -MIO::Socket::INET -MIO::Select -e {shlex.quote(script)}"


def perl_tcp(ip, ports):
    """Prints each of ports that connects to ip."""
    return perl(f'for my $p ({",".join(map(str, ports))}) {{ print "$p\\n" if IO::Socket::INET->new('
                f'PeerAddr => "{ip}", PeerPort => $p, Timeout => 3) }}')


def perl_udp_send(ip, ports, token):
    return perl(f'for (1..8) {{ for my $p ({",".join(map(str, ports))}) {{ my $s = IO::Socket::INET->new('
                f'PeerAddr => "{ip}", PeerPort => $p, Proto => "udp") or next; $s->send("{token}:$p") }} '
                f'select(undef, undef, undef, 0.25) }}')


def perl_udp_listen(ports, secs=UDP_SECS):
    """Listens on ports for secs; prints 'busy P' for a port in use, 'got P TEXT' for what arrives."""
    return perl(f'$| = 1; my $sel = IO::Select->new; my %port; for my $p ({",".join(map(str, ports))}) {{ '
                f'my $s = IO::Socket::INET->new(LocalPort => $p, Proto => "udp"); '
                f'if ($s) {{ $sel->add($s); $port{{fileno $s}} = $p }} else {{ print "busy $p\\n" }} }} '
                f'my $end = time + {secs}; while ((my $left = $end - time) > 0) {{ '
                f'for my $s ($sel->can_read($left)) {{ my $b = ""; $s->recv($b, 200); '
                f'print "got $port{{fileno $s}} $b\\n" }} }}')


# --- the checks: (label, function(remote, local) -> problem text or None, fix) ---
# local: facts about this host (local_facts); a check may add to local["notes"].

def check_hostname(remote, local):
    name = local["name"]                     # what join_steps read from it
    if not name or name == local["addr"]:
        return "can't read its hostname"
    if name in local["names"]:
        return f"its hostname '{name}' is already a host of this cluster (or this host's own name)"
    return Ok(f"hostname {name} (not used in this cluster)")


def check_resolves(remote, local):
    ips = parse_getent(remote.run("getent hosts $(hostname)")[1])
    if local["addr"] not in ips:
        return f"its hostname resolves to {', '.join(ips) or 'nothing'}, not {local['addr']}"
    return Ok(f"{local['name']} resolves to {local['addr']} there")


def check_version(remote, local):
    theirs = parse_version(remote.run("pveversion")[1])
    if not theirs:
        return "pveversion did not answer: is it a Proxmox VE host?"
    if theirs != local["version"]:
        return f"it runs PVE {theirs}, this host PVE {local['version']}"
    return Ok(f"PVE {theirs} (the same as this host)")


def check_guests(remote, local):
    out = remote.run('find /etc/pve/nodes/"$(hostname)"/qemu-server /etc/pve/nodes/"$(hostname)"/lxc '
                     "-name '*.conf' 2>/dev/null")[1]
    ids = sorted(os.path.basename(line)[:-5] for line in out.split() if line.endswith(".conf"))
    return f"it has guests: {' '.join(ids)}" if ids else Ok("no VMs or containers on it")


def check_storage(remote, local):
    # the pool's data volume is there whenever pve/data is active; -tpool only once it holds thin volumes
    if remote.run("test -e /dev/mapper/pve-data_tdata -o -e /dev/mapper/pve-data-tpool")[0]:
        return "it has no LVM-thin pool pve/data (local-lvm)"
    return Ok("storage local-lvm (LVM-thin pve/data)")


def check_kvm(remote, local):
    return "it has no /dev/kvm" if remote.run("test -e /dev/kvm")[0] else Ok("/dev/kvm (VMs run at full speed)")


def check_tcp(remote, local):
    there = [p for p in TCP_PORTS if not tcp_ok(local["addr"], p)]
    back = remote.run(perl_tcp(local["ip"], TCP_PORTS))[1].split()
    here = [p for p in TCP_PORTS if str(p) not in back]
    return blocked("TCP", here, there, local["addr"]) or Ok(f"TCP {ranges(TCP_PORTS)} (ssh, API) both ways")


def check_udp(remote, local):
    """Both ways: the receiver listens a few seconds, the sender sends a random token to each port."""
    addr, token = local["addr"], new_token()
    socks, busy = udp_listen(UDP_PORTS)                       # to here
    try:
        remote.run(perl_udp_send(local["ip"], list(socks), token))
    finally:
        got = udp_received(socks, token)
    here = [p for p in socks if p not in got]
    local["notes"] += [f"UDP {p} is in use here, so it can't be tested: make sure UDP {p} from {addr} is allowed"
                       for p in busy]
    out = concurrently(lambda: remote.run(perl_udp_listen(UDP_PORTS)),      # to there
                       lambda: udp_send(addr, UDP_PORTS, token))[0][1]
    there_busy = {int(p) for p in re.findall(r"^busy (\d+)$", out, re.M)}
    there_got = {int(p) for p, t in re.findall(r"^got (\d+) (\S+)$", out, re.M) if t == f"{token}:{p}"}
    there = [p for p in UDP_PORTS if p not in there_got | there_busy]
    local["notes"] += [f"UDP {p} is in use on {addr}, so it can't be tested: make sure UDP {p} to {addr} is allowed"
                       for p in sorted(there_busy)]
    return blocked("UDP", here, there, addr) or Ok("UDP 4789 (VXLAN), 5405-5412 (corosync) both ways")


def check_time(remote, local):
    t0 = time.time()
    rc, out = remote.run("date +%s.%N")
    mid = (t0 + time.time()) / 2
    try:
        skew = abs(float(out.strip()) - mid)
    except ValueError:
        return "can't read its clock"
    if skew >= 2:
        return f"its clock is {skew:.1f} s off this host's"
    synced = remote.run("timedatectl show -p NTPSynchronized --value")[1].strip() == "yes"
    if not synced or not local["ntp"]:
        return "NTP is not synchronized on " + " and ".join(
            [h for h, ok in ((local["addr"], synced), ("this host", local["ntp"])) if not ok])
    return Ok(f"clocks in sync ({skew:.1f} s apart, NTP on both)")


def check_local(remote, local):
    if local["cluster"] and not local["quorate"]:
        return "this host's cluster is not quorate (pvecm status)"
    return Ok("this host: in a quorate cluster" if local["cluster"] else "this host: no cluster yet (step 2 makes it)")


CHECKS = [
    ("hostname", check_hostname,
     "pick a new name on it before joining: hostnamectl set-hostname NEW, and the same in its /etc/hosts"),
    ("hostname resolves", check_resolves,
     "in its /etc/hosts, the line: ADDR name.domain name (not 127.0.1.1)"),
    ("PVE version", check_version, "on the older host: apt update && apt full-upgrade"),
    ("no guests", check_guests, "back them up with vzdump, then remove them (a joining host must have none)"),
    ("storage local-lvm", check_storage,
     "the host must use PVE's default LVM-thin layout: pvelab uses one storage name on every host"),
    ("/dev/kvm", check_kvm, "enable nested virtualization / CPU passthrough on the hypervisor"),
    ("TCP 22, 8006", check_tcp, "open the blocked TCP ports between the two IPs (OpenStack security group)"),
    ("UDP 4789, 5405-5412", check_udp,
     "open the blocked UDP ports between the two IPs (OpenStack security group: ingress UDP, both hosts)"),
    ("clocks in sync", check_time, "on both hosts: systemctl enable --now chrony"),
    ("this host's cluster", check_local, "bring the cluster's hosts up first (pvecm status)"),
]


def local_facts(job):
    """What the checks compare against: the new host's address and name, this host's name(s), IP, PVE
    version, cluster state, NTP."""
    cluster = in_cluster()
    return {"addr": job["addr"], "name": job["name"], "ip": job["ip"], "names": set(members()) | {this_node()},
            "version": parse_version(run("pveversion", check=False, quiet=True)),
            "cluster": cluster,
            "quorate": quorate() if cluster else None,
            "ntp": run("timedatectl", "show", "-p", "NTPSynchronized", "--value",
                       check=False, quiet=True).strip() == "yes",
            "notes": []}


def run_checks(remote, local):
    """Run every check, each on a spinner line that ends as ✔ (with what was found) or ✘ (the problem,
    and under it the fix); [(label, problem, fix)] of those that failed."""
    failed = []
    for label, fn, fix in CHECKS:
        problem = check_line(label, fn, remote, local)
        if problem:
            failed.append((label, problem, fix.replace("ADDR", local["addr"])))
            print(paint(f"      fix: {failed[-1][2]}", "dim"))
    for note in local["notes"]:
        print(paint(f"    note: {note}", "yellow"))
    return failed


# --- node add, step by step ---

RECOVERY = {
    "checks": "nothing was changed; fix the problems and run `pvelab node add {addr}` again",
    "create": "check `pvecm status` on {this}: if it shows no cluster, run `pvelab node add {addr}` again",
    "confirm": "nothing was changed on {addr}; run `pvelab node add {addr}` again",
    "join": ("{addr} may be half-joined. If `pvecm status` on {addr} shows the cluster, run "
             "`pvelab node add {addr}` again. Otherwise, on {addr} as root:\n"
             "      systemctl stop pve-cluster corosync; pmxcfs -l; rm -f /etc/corosync/* /etc/pve/corosync.conf; "
             "killall pmxcfs; systemctl start pve-cluster\n"
             "    then on {this}: pvecm delnode {name}   (if `pvecm status` lists it)"),
    "verify": "check `pvecm status` on {this} and on {addr}, then run `pvelab node add {addr}` again",
    "prepare": "the join is done; run `pvelab node add {addr}` again (it skips to checking and preparing)",
}
STATE = {
    "checks": "{this}: not changed; {addr}: not changed",
    "create": "{this}: the cluster may be half made; {addr}: not changed",
    "confirm": "{this}: in a cluster; {addr}: not changed",
    "join": "{this}: in the cluster; {addr} ({name}): may be half-joined",
    "verify": "{this}: in the cluster; {addr} ({name}): joined, but not yet seen online and quorate",
    "prepare": "{this} and {addr} ({name}): in the cluster; pvelab's setup on {name} is not finished",
}


def member_name(addr):
    """The cluster member that addr is (by IP or name), or None."""
    return next((n for n, m in members().items() if addr in (n, m["ip"])), None)


def ask_user(addr):
    """The ssh user on the new host, asked on a terminal; Enter takes the user who ran pvelab (sudo)."""
    default = os.environ.get("SUDO_USER") or "root"
    if not sys.stdin.isatty():
        return "root"
    return input(f"SSH user on {addr} (a sudoer, or root) [{default}]: ").strip() or default


def hint(text):
    """What the next prompt is about, so the right password goes into it."""
    print(paint(f"  next: {text}", "yellow"))


def connect(addr):
    """One ssh connection to addr as the user the instructor names, open (the caller closes it)."""
    user = ask_user(addr)
    remote = Remote(addr, user)
    hint(f"the ssh password of {user}@{addr} (not asked if it has your key)")
    remote.open()
    return remote


def create_cluster(job):
    """Step 2: cluster 'pvelab' on this host (the plan was confirmed before)."""
    job["step"] = "create"
    with Spinner(f"creating cluster '{CLUSTER}' on {job['this']}") as line:
        ui.logged("pvecm", "create", CLUSTER, "--link0", job["ip"])
        wait_for("the new cluster's quorum", quorate, 60)
        line.ok(f"cluster '{CLUSTER}' made on {job['this']}, quorate")


def api_fingerprint():
    """The SHA-256 fingerprint of this host's API certificate (pvecm add checks it)."""
    cert = "/etc/pve/local/pveproxy-ssl.pem"
    if not os.path.exists(cert):
        cert = "/etc/pve/local/pve-ssl.pem"
    fp = parse_fingerprint(run("openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", cert))
    return fp or die(f"can't read the fingerprint of {cert}")


def join_cmd(remote, ip, fp):
    """The shell line that joins remote's host to the cluster of ip (sudo unless the user is root).
    It holds no password: sudo and pvecm ask on the terminal."""
    line = "sh -c " + shlex.quote(f"cd / && pvecm add {ip} --link0 {remote.addr} --fingerprint {fp}")
    if remote.user == "root":
        return line
    return f"sudo -p {shlex.quote(f'sudo password for %u on {remote.addr}: ')} {line}"


def wrong_password(output):
    """pvecm add stopped at the login: the root password was wrong, and nothing was changed there."""
    return "401" in output and "authentication failure" in output.lower() and "Join request OK" not in output


def join(remote, job):
    """Step 3: pvecm add there; a wrong root password (nothing changed yet) may be typed again."""
    fp = api_fingerprint()
    job["step"] = "join"
    for left in range(JOIN_TRIES - 1, -1, -1):
        hint((f"the sudo password of {remote.user} on {job['name']}, then " if remote.user != "root" else "")
             + f"the root password of {job['this']} ({job['ip']}) - not {job['name']}'s")
        rc, out = remote.tty(join_cmd(remote, job["ip"], fp))
        if not rc:
            return
        if not wrong_password(out) or not left:
            break
        print(paint(f"  ✘ wrong root password for {job['this']}; nothing was changed on {job['name']}. "
                    f"Try again ({left} left)", "red"))
    if wrong_password(out):
        job["step"] = "confirm"                       # the login failed: nothing changed there
        die(f"the root password of {job['this']} was not accepted {JOIN_TRIES} times")
    die(f"pvecm add on {job['addr']} failed ({rc})")


def verify(name):
    """Step 4 (first part): the new host online and the cluster quorate, within VERIFY_SECS."""
    with Spinner(f"waiting for {name} to be online in a quorate cluster") as line:
        wait_for(f"{name} online in a quorate cluster", lambda: is_online(name) and quorate(), VERIFY_SECS)
        line.ok(f"{name} is online; the cluster is quorate")


def prepare(name):
    """Step 4: over the cluster's root ssh: snippet, boot unit and tables for isolation, SDN peers, and
    each pvelab SDN net up there."""
    with Spinner(f"preparing {name} for pvelab (vendor-data, isolation)") as line:
        ensure_snippet(name)
        ensure_nft_unit(name)
        nets = [n for n in our_nets() if is_sdn(n)]
        for n in nets:                       # not isolated: drop one left from an isolate --off while it was down
            sync_host(name, n, host_table(n))
        line.ok(f"{name}: vendor-data and isolation ready")
    if not nets:
        print(paint("    pvelab has no SDN nets yet: no network to apply", "dim"))
        return
    with Spinner("adding the host to the SDN zone, applying the network on all hosts") as line:
        sdn.ensure_zone()
        sdn.apply()
        line.ok("SDN zone updated and applied on all hosts")
    for n in nets:                           # each host reloads its network on its own, after apply
        with Spinner(f"{bridge(n)} on {name}") as line:
            if not sdn.wait_vnet(name, n):
                line.fail(f"not up on {name} after 60 s (see `pvesh get /cluster/sdn` and {name}'s ifreload)")


def note(names):
    """The two-host quorum warning, naming the hosts."""
    a, b = names
    return paint(f"""
Note: with two hosts, each needs the other to change anything. When {a} or {b} is down, the other
one's /etc/pve is read-only: VMs can't be made, started or changed there (running ones keep running).
  The way out, on the host that is up:   pvecm expected 1
  For good, a third vote (QDevice):      pvecm qdevice setup <IP of a small Debian VM>""", "yellow")


def bad_addr(addr):
    """Why addr is not what node add takes (an IPv4 address), or None. The wizard asks with it too."""
    try:
        ipaddress.IPv4Address(addr)
        return None
    except ValueError:
        return f"node add takes the new host's IPv4 address, like 10.0.0.229, not '{addr}'"


def node_add(addr):
    """Steps 0-4 (see the module's doc). A failure says which step, each host's state, and how to recover.
    Every command's output goes to this command's log (ui.LOG); the screen shows one line per step."""
    if (why := bad_addr(addr)):
        die(why)
    job = {"addr": addr, "this": this_node(), "ip": members().get(this_node(), {}).get("ip"),
           "name": member_name(addr), "step": "checks"}
    if not job["ip"]:
        die(f"can't tell this host's own IP ({this_node()} in /etc/pve/.members, or on vmbr0)")
    if job["name"] and is_local(job["name"]):
        die(f"{addr} is this host")
    rerun = bool(job["name"])
    try:
        if rerun:                             # step 0: rerun after a join
            print(f"{addr} ({job['name']}) is already in the cluster: checking it and preparing it again")
            step(1, 2, f"verify {job['name']}")
        else:
            job["name"] = addr
            join_steps(job)
        job["step"] = "verify"
        verify(job["name"])
        job["step"] = "prepare"
        if rerun:
            step(2, 2, f"prepare {job['name']}")
        prepare(job["name"])
    except (LabError, KeyboardInterrupt) as e:
        at = job["step"]
        if at == "verify" and not is_online(job["name"]):
            at = "join"                      # listed but never up: the join may have stopped half-way
        die(f"node add: step '{at}' failed: {str(e) or 'interrupted'}\n  state: {STATE[at].format(**job)}\n"
            f"  recovery: {RECOVERY[at].format(**job)}\n  log: {ui.LOG}")
    done(job)


def done(job):
    """The end: the host table, what to do next, the quorum note with two hosts, the log."""
    from .commands.node import node_list
    print(paint(f"\n{job['name']} ({job['addr']}) is ready for pvelab.\n", "green"))
    node_list()
    names = here_first(members())
    print(paint(f"\nNext: pvelab create 20 --net auto   (students are spread over {', '.join(names)})", "bold"))
    if len(names) == 2:
        print(note(names))
    print(paint(f"\nlog: {ui.LOG}", "dim"))


def join_steps(job):
    """Steps 1-3 over one ssh connection, which is always closed. Sets job["name"] (the new host's) and
    job["step"] (the step running)."""
    remote = connect(job["addr"])
    try:
        job["name"] = remote.run("hostname")[1].strip() or job["addr"]
        make = not in_cluster()
        n = 4 if make else 3
        step(1, n, f"check {job['addr']} ({job['name']}) - nothing is changed")
        failed = run_checks(remote, local_facts(job))
        if failed:
            die(f"{len(failed)} check(s) failed: fix {'it' if len(failed) == 1 else 'them'} as shown above")
        job["step"] = "confirm"
        plan = Plan()
        if make:
            plan.do(f"create cluster '{CLUSTER}' on {job['this']} (pve-cluster restarts for a few seconds; "
                    "running VMs are not touched)")
        plan.do(f"join {job['name']} ({job['addr']}) to the cluster")
        plan.do(f"prepare {job['name']} for pvelab, and apply the network on all hosts")
        print()
        plan.ask()
        confirm_yes("", typed=True)
        if make:
            step(2, n, "cluster")
            create_cluster(job)
        step(n - 1, n, f"join {job['name']}")
        join(remote, job)
    finally:
        remote.close()
    step(n, n, f"verify and prepare {job['name']}")
