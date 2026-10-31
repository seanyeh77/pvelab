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
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time

from . import sdn
from .config import bridge, is_sdn, LAB_DIR
from .ifaces import our_nets
from .isolation import drop_nft, ensure_nft_unit, is_isolated, net_vms, nft_table, push_nft
from .nodes import is_local, members, this_node
from .templates import ensure_snippet, wait_for
from .util import concurrently, die, LabError, run

CLUSTER = "pvelab"                            # the cluster's name when pvelab makes it
JOIN_CTL = f"{LAB_DIR}/ssh-join-%C"           # the connection to the new host during node add
TCP_PORTS = (22, 8006)                        # ssh, the web UI / API (the join uses it)
UDP_PORTS = (4789, *range(5405, 5413))        # VXLAN, corosync
UDP_SECS = 4                                  # how long a UDP listener waits for the token
VERIFY_SECS = 120
COROSYNC_CONF = "/etc/pve/corosync.conf"      # there only when this host is in a cluster


class Remote:
    """The new host, over one ssh connection as user (a sudoer, or root). ssh's own prompts (host key,
    password) show on the terminal; the commands after open() reuse the connection."""

    def __init__(self, addr, user="root"):
        self.addr, self.user = addr, user or "root"

    def ssh(self, *opts):
        return ["ssh", "-o", f"ControlPath={JOIN_CTL}", *opts]

    def open(self):
        r = subprocess.run(self.ssh("-o", "ControlMaster=yes", "-o", "ControlPersist=600", "-fN",
                                    f"{self.user}@{self.addr}"))
        if r.returncode:
            die(f"could not ssh to {self.user}@{self.addr}")

    def run(self, cmd, timeout=60):
        """(exit code, output) of shell line cmd there; never asks anything."""
        try:
            r = subprocess.run(self.ssh("-o", "BatchMode=yes", "-T", f"{self.user}@{self.addr}", "--", cmd),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, ""
        return r.returncode, r.stdout

    def tty(self, cmd):
        """Exit code of shell line cmd there, on a terminal (for sudo's and pvecm's password prompts)."""
        return subprocess.run(self.ssh("-t", f"{self.user}@{self.addr}", "--", cmd)).returncode

    def close(self):
        subprocess.run(self.ssh("-O", "exit", f"{self.user}@{self.addr}"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- parsers (pure) ---

def parse_version(text):
    """`pveversion` ('pve-manager/9.2.20/abc (running kernel: ...)') -> '9.2', or None."""
    m = re.search(r"pve-manager/(\d+)\.(\d+)", text or "")
    return f"{m.group(1)}.{m.group(2)}" if m else None


def parse_getent(text):
    """`getent hosts NAME` -> its addresses."""
    return [line.split()[0] for line in (text or "").splitlines() if line.split()]


def parse_quorate(text):
    """`pvecm status` -> True / False (Quorate: Yes / No), or None (no cluster)."""
    m = re.search(r"^Quorate:\s*(\w+)", text or "", re.M)
    return None if not m else m.group(1).lower() == "yes"


def parse_fingerprint(text):
    """`openssl x509 -noout -fingerprint -sha256` -> 'AB:CD:...', or None."""
    m = re.search(r"Fingerprint=([0-9A-Fa-f:]+)", text or "")
    return m.group(1).upper() if m else None


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
    return None


def check_resolves(remote, local):
    ips = parse_getent(remote.run("getent hosts $(hostname)")[1])
    if local["addr"] not in ips:
        return f"its hostname resolves to {', '.join(ips) or 'nothing'}, not {local['addr']}"
    return None


def check_version(remote, local):
    theirs = parse_version(remote.run("pveversion")[1])
    if not theirs:
        return "pveversion did not answer: is it a Proxmox VE host?"
    if theirs != local["version"]:
        return f"it runs PVE {theirs}, this host PVE {local['version']}"
    return None


def check_guests(remote, local):
    out = remote.run('find /etc/pve/nodes/"$(hostname)"/qemu-server /etc/pve/nodes/"$(hostname)"/lxc '
                     "-name '*.conf' 2>/dev/null")[1]
    ids = sorted(os.path.basename(line)[:-5] for line in out.split() if line.endswith(".conf"))
    return f"it has guests: {' '.join(ids)}" if ids else None


def check_storage(remote, local):
    # the pool's data volume is there whenever pve/data is active; -tpool only once it holds thin volumes
    if remote.run("test -e /dev/mapper/pve-data_tdata -o -e /dev/mapper/pve-data-tpool")[0]:
        return "it has no LVM-thin pool pve/data (local-lvm)"
    return None


def check_kvm(remote, local):
    return "it has no /dev/kvm" if remote.run("test -e /dev/kvm")[0] else None


def check_tcp(remote, local):
    problems = [f"TCP {p} from here to {local['addr']}" for p in TCP_PORTS if not tcp_ok(local["addr"], p)]
    back = remote.run(perl_tcp(local["ip"], TCP_PORTS))[1].split()
    problems += [f"TCP {p} from {local['addr']} to here" for p in TCP_PORTS if str(p) not in back]
    return "not reachable: " + ", ".join(problems) if problems else None


def check_udp(remote, local):
    """Both ways: the receiver listens a few seconds, the sender sends a random token to each port."""
    addr, token, problems = local["addr"], new_token(), []
    socks, busy = udp_listen(UDP_PORTS)                       # to here
    try:
        remote.run(perl_udp_send(local["ip"], list(socks), token))
    finally:
        got = udp_received(socks, token)
    problems += [f"UDP {p} from {addr} to here" for p in socks if p not in got]
    local["notes"] += [f"UDP {p} is in use here, so it can't be tested: make sure UDP {p} from {addr} is allowed"
                       for p in busy]
    out = concurrently(lambda: remote.run(perl_udp_listen(UDP_PORTS)),      # to there
                       lambda: udp_send(addr, UDP_PORTS, token))[0][1]
    there_busy = {int(p) for p in re.findall(r"^busy (\d+)$", out, re.M)}
    there_got = {int(p) for p, t in re.findall(r"^got (\d+) (\S+)$", out, re.M) if t == f"{token}:{p}"}
    problems += [f"UDP {p} from here to {addr}" for p in UDP_PORTS if p not in there_got | there_busy]
    local["notes"] += [f"UDP {p} is in use on {addr}, so it can't be tested: make sure UDP {p} to {addr} is allowed"
                       for p in sorted(there_busy)]
    return "not reachable: " + ", ".join(problems) if problems else None


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
    return None


def check_local(remote, local):
    if local["cluster"] and not local["quorate"]:
        return "this host's cluster is not quorate (pvecm status)"
    return None


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
    ("TCP 22, 8006 both ways", check_tcp, "open TCP 22 and 8006 between the two IPs (OpenStack security group)"),
    ("UDP 4789, 5405-5412 both ways", check_udp,
     "open UDP 4789 and 5405-5412 between the two IPs (OpenStack security group)"),
    ("clocks in sync", check_time, "on both hosts: systemctl enable --now chrony"),
    ("this host's cluster", check_local, "bring the cluster's hosts up first (pvecm status)"),
]


def local_facts(job):
    """What the checks compare against: the new host's address and name, this host's name(s), IP, PVE
    version, cluster state, NTP."""
    cluster = os.path.exists(COROSYNC_CONF)
    return {"addr": job["addr"], "name": job["name"], "ip": job["ip"], "names": set(members()) | {this_node()},
            "version": parse_version(run("pveversion", check=False, quiet=True)),
            "cluster": cluster,
            "quorate": parse_quorate(run("pvecm", "status", check=False, quiet=True)) if cluster else None,
            "ntp": run("timedatectl", "show", "-p", "NTPSynchronized", "--value",
                       check=False, quiet=True).strip() == "yes",
            "notes": []}


def run_checks(remote, local):
    """Run every check, printing ✔/✘; [(label, problem, fix)] of those that failed."""
    failed = []
    for label, fn, fix in CHECKS:
        try:
            problem = fn(remote, local)
        except (LabError, OSError) as e:
            problem = f"the check itself failed: {e}"
        print(f"  {'✘' if problem else '✔'} {label}" + (f": {problem}" if problem else ""))
        if problem:
            failed.append((label, problem, fix.replace("ADDR", local["addr"])))
    for note in local["notes"]:
        print(f"  note: {note}")
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
    """The ssh user on the new host: asked on a terminal (empty: root)."""
    if not sys.stdin.isatty():
        return "root"
    return input(f"SSH user on {addr} (a sudoer, or empty for root): ").strip() or "root"


def typed_yes(msg):
    """A typed yes on a real terminal - even with LAB_YES=1."""
    print(msg)
    if not sys.stdin.isatty():
        die("this needs a typed yes on a terminal (LAB_YES=1 does not count here); run it interactively")
    if input("Type 'yes' to continue: ").strip() != "yes":
        die("aborted")


def quorate():
    return bool(parse_quorate(run("pvecm", "status", check=False, quiet=True)))


def create_cluster(job):
    """Step 2: cluster 'pvelab' on this host, after a typed yes."""
    typed_yes(f"{job['this']} is not in a cluster. This creates cluster '{CLUSTER}' on {job['this']}: "
              "pve-cluster restarts for a few seconds; running VMs are not touched.")
    job["step"] = "create"
    run("pvecm", "create", CLUSTER, "--link0", job["ip"], capture=False)
    wait_for("the new cluster's quorum", quorate, 60)


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


def join(remote, job):
    """Step 3."""
    job["step"] = "confirm"
    typed_yes(f"This joins {job['addr']} ({job['name']}) to the cluster; the network is applied on all hosts "
              "afterwards.")
    fp = api_fingerprint()
    print(f">> on {job['addr']}: pvecm add {job['ip']} (it asks for the root password of {job['this']})")
    job["step"] = "join"
    rc = remote.tty(join_cmd(remote, job["ip"], fp))
    if rc:
        die(f"pvecm add on {job['addr']} failed ({rc})")


def verify(name):
    """Step 4: the new host online and the cluster quorate, within VERIFY_SECS."""
    wait_for(f"{name} online in a quorate cluster", lambda: members().get(name, {}).get("online") and quorate(),
             VERIFY_SECS)
    print(f"  ✔ {name} is online; the cluster is quorate")


def prepare(name):
    """Step 5, over the cluster's root ssh: snippet, boot unit and tables for isolation, SDN peers, and
    each pvelab SDN net up there."""
    ensure_snippet(name)
    ensure_nft_unit(name)
    nets = [n for n in our_nets() if is_sdn(n)]
    for n in nets:
        if is_isolated(n):
            push_nft(name, n, nft_table(n, net_vms(n), gateway=False))
        else:                                # one left from before an isolate --off while the host was down
            drop_nft(name, n)
    if not nets:
        print("  (pvelab has no SDN nets yet: nothing to apply)")
        return
    print(">> adding the host to the SDN zone, and applying the network on all hosts")
    sdn.ensure_zone()
    sdn.apply()
    for n in nets:                           # each host reloads its network on its own, after apply
        print(f"  {'✔' if sdn.wait_vnet(name, n) else '✘'} {bridge(n)} on {name}")


NOTE = """
Note: with two hosts, when one is down the other has no quorum: /etc/pve is read-only, and VMs
can't be started or changed. The way out, on the host that is up:  pvecm expected 1
Later, a third vote (a QDevice) avoids this:  pvecm qdevice setup <IP>"""


def bad_addr(addr):
    """Why addr is not what node add takes (an IPv4 address), or None. The wizard asks with it too."""
    try:
        ipaddress.IPv4Address(addr)
        return None
    except ValueError:
        return f"node add takes the new host's IPv4 address, like 10.0.0.229, not '{addr}'"


def node_add(addr):
    """Steps 0-6 (see the module's doc). A failure says which step, each host's state, and how to recover."""
    if bad_addr(addr):
        die(bad_addr(addr))
    job = {"addr": addr, "this": this_node(), "ip": members().get(this_node(), {}).get("ip"),
           "name": member_name(addr), "step": "checks"}
    if not job["ip"]:
        die(f"can't tell this host's own IP ({this_node()} in /etc/pve/.members, or on vmbr0)")
    if job["name"] and is_local(job["name"]):
        die(f"{addr} is this host")
    try:
        if job["name"]:                       # step 0: rerun after a join
            print(f"{addr} ({job['name']}) is already in the cluster")
        else:
            job["name"] = addr
            join_steps(job)
        job["step"] = "verify"
        verify(job["name"])
        job["step"] = "prepare"
        prepare(job["name"])
    except (LabError, KeyboardInterrupt) as e:
        step = job["step"]
        if step == "verify" and not members().get(job["name"], {}).get("online"):
            step = "join"                    # listed but never up: the join may have stopped half-way
        die(f"node add: step '{step}' failed: {str(e) or 'interrupted'}\n  state: {STATE[step].format(**job)}\n"
            f"  recovery: {RECOVERY[step].format(**job)}")
    print(f"{job['name']} ({addr}) is ready for pvelab.")
    if len(members()) == 2:
        print(NOTE)


def join_steps(job):
    """Steps 1-3 over one ssh connection, which is always closed. Sets job["name"] (the new host's) and
    job["step"] (the step running)."""
    remote = Remote(job["addr"], ask_user(job["addr"]))
    remote.open()
    try:
        job["name"] = remote.run("hostname")[1].strip() or job["addr"]
        print(f"checking {job['addr']} ({job['name']}); nothing is changed:")
        failed = run_checks(remote, local_facts(job))
        if failed:
            for label, problem, fix in failed:
                print(f"\n✘ {label}: {problem}\n  fix: {fix}")
            die(f"{len(failed)} check(s) failed")
        if not os.path.exists(COROSYNC_CONF):
            create_cluster(job)
        join(remote, job)
    finally:
        remote.close()
