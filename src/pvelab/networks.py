"""NAT bridges vmbrN (10.10.N.0/24, fixed IPs) and the DHCP bridge vmbr1."""

import glob
import json
import os
import re
import shlex
import subprocess

from .config import DHCP_BRIDGE, DHCP_NET, IFACES, IP_FIRST, LEASES, UPLINK
from .credentials import cred_rows
from .expose import is_exposed, set_expose
from .ifaces import block_re, bridge_is_ours
from .inventory import is_managed, read_conf
from .util import die, ok, run, warn


def net_bridge(net):
    return DHCP_BRIDGE if net == "dhcp" else f"vmbr{net}"


def net_label(net):
    return f"{DHCP_BRIDGE}(dhcp)" if net == "dhcp" else f"net{net}"


def net_in_use(n):
    """Why net n (vmbrN, 10.10.N.0/24) is taken, or None if it is free."""
    br, sub = f"vmbr{n}", f"10.10.{n}"
    if link_exists(br):
        return f"{br} already exists"
    if re.search(rf"^(auto|iface) {br}( |$)", open(IFACES).read(), re.M):
        return f"{br} is configured in {IFACES}"
    if re.search(rf"^{re.escape(sub)}\.0/24", run("ip", "route"), re.M):
        return f"{sub}.0/24 is already routed on this host"
    for f in glob.glob("/etc/pve/nodes/*/qemu-server/*.conf") + glob.glob("/etc/pve/nodes/*/lxc/*.conf"):
        if re.search(rf"bridge={br}(,|$)", open(f).read(), re.M):
            return f"guest {os.path.basename(f)[:-5]} has a network card on {br}"
    if any(r["net"] == n for r in cred_rows().values()):
        return f"pvelab has VMs recorded on net{n}"
    return None


def foreign_use(n):
    """Why vmbrN / 10.10.N.0/24 is used by something that is NOT pvelab's, or None.
    A net pvelab made itself (with or without VMs) is fine to reuse."""
    br, sub = f"vmbr{n}", f"10.10.{n}"
    ours = bridge_is_ours(n)
    if not ours:
        if link_exists(br):
            return f"{br} exists and was not made by pvelab"
        if re.search(rf"^(auto|iface) {br}( |$)", open(IFACES).read(), re.M):
            return f"{br} is configured in {IFACES} (not by pvelab)"
        if re.search(rf"^{re.escape(sub)}\.0/24", run("ip", "route"), re.M):
            return f"{sub}.0/24 is already routed on this host"
    for f in glob.glob("/etc/pve/nodes/*/qemu-server/*.conf") + glob.glob("/etc/pve/nodes/*/lxc/*.conf"):
        vmid = os.path.basename(f)[:-5]
        text = open(f).read()
        if re.search(rf"bridge={br}(,|$)", text, re.M) and not is_managed(read_conf(vmid) if "/qemu-server/" in f else None):
            return f"guest {vmid} (not a pvelab VM) has a network card on {br}"
    return None


def free_net():
    for n in range(2, 255):
        if n != 10 and not net_in_use(str(n)):
            return str(n)
    die("no free net number left (2-254)")


def valid_net(v):
    if not v.isdigit():
        die("--net must be a number")
    n = int(v)
    if not 2 <= n <= 254:
        die("--net must be 2..254")
    if n == 10:
        die("--net 10 would overlap vmbr1's 10.10.10.0/24")
    return str(n)


def link_exists(dev):
    return ok("ip", "link", "show", dev)


def ensure_dhcp_bridge():
    """vmbr1: NAT bridge with dnsmasq DHCP for the templates and non --net students.
    (On OpenStack, port security drops guest MACs on vmbr0, so guests need NAT.)"""
    if not link_exists(DHCP_BRIDGE) and not re.search(rf"^(auto|iface) {DHCP_BRIDGE}( |$)",
                                                      open(IFACES).read(), re.M):
        print(f">> creating NAT bridge {DHCP_BRIDGE} ({DHCP_NET}.1/24)")
        run("cp", "-a", IFACES, IFACES + ".lab-bak")
        with open(IFACES, "a") as f:
            f.write(f"\nauto {DHCP_BRIDGE}\niface {DHCP_BRIDGE} inet static\n"
                    f"        address {DHCP_NET}.1/24\n"
                    f"        bridge-ports none\n        bridge-stp off\n        bridge-fd 0\n"
                    f"        post-up   echo 1 > /proc/sys/net/ipv4/ip_forward\n"
                    f"        post-up   iptables -t nat -A POSTROUTING -s {DHCP_NET}.0/24 -o {UPLINK} -j MASQUERADE\n"
                    f"        post-up   iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n"
                    f"        post-down iptables -t nat -D POSTROUTING -s {DHCP_NET}.0/24 -o {UPLINK} -j MASQUERADE\n"
                    f"        post-down iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n")
        run("ifup", DHCP_BRIDGE)       # only this bridge
    conf = f"/etc/dnsmasq.d/{DHCP_BRIDGE}.conf"
    if not os.path.exists(conf):
        print(f">> installing dnsmasq for DHCP/DNS on {DHCP_BRIDGE}")
        subprocess.run(["apt-get", "install", "-y", "-q", "dnsmasq"], stdout=subprocess.DEVNULL,
                       env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}, check=True)
        with open(conf, "w") as f:
            f.write(f"interface={DHCP_BRIDGE}\nbind-interfaces\nexcept-interface=lo\n"
                    f"dhcp-range={DHCP_NET}.100,{DHCP_NET}.200,255.255.255.0,12h\n"
                    f"dhcp-option=option:router,{DHCP_NET}.1\n"
                    f"dhcp-option=option:dns-server,{DHCP_NET}.1\n")
        os.chmod(conf, 0o644)
        run("systemctl", "restart", "dnsmasq")


def ensure_bridge(n):
    br, sub = f"vmbr{n}", f"10.10.{n}"
    if bridge_is_ours(n):
        if not link_exists(br):
            run("ifup", br)
        return
    if link_exists(br) or re.search(rf"^(auto|iface) {br}( |$)", open(IFACES).read(), re.M):
        die(f"{br} already exists and was not created by lab - pick another --net")
    if re.search(rf"^{re.escape(sub)}\.0/24", run("ip", "route"), re.M):
        die(f"{sub}.0/24 is already routed on this host")
    print(f">> creating isolated NAT bridge {br} ({sub}.1/24)")
    rules = [
        f"-t nat {{}} POSTROUTING -s {sub}.0/24 -o {UPLINK} -j MASQUERADE",
        f"-t mangle {{}} FORWARD -i {br} -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu",
        f"-t mangle {{}} FORWARD -o {br} -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu",
        f"{{}} FORWARD -i {br} -o {br} -j ACCEPT",                 # same subnet
        f"{{}} FORWARD -i {br} -o {UPLINK} -j ACCEPT",             # internet
        f"{{}} FORWARD -o {br} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        f"{{}} FORWARD -i {br} -j DROP",                           # other subnets
        f"{{}} FORWARD -o {br} -j DROP",
    ]
    up = "".join(f"        post-up   iptables {r.format('-A')}\n" for r in rules)
    down = "".join(f"        post-down iptables {r.format('-D')}\n" for r in rules)
    run("cp", "-a", IFACES, IFACES + ".lab-bak")
    with open(IFACES, "a") as f:
        f.write(f"\n# lab.sh net {n} begin (managed by lab - removed automatically with its last VM)\n"
                f"auto {br}\niface {br} inet static\n        address {sub}.1/24\n"
                f"        bridge-ports none\n        bridge-stp off\n        bridge-fd 0\n"
                f"        post-up   sysctl -qw net.ipv4.ip_forward=1\n{up}{down}"
                f"# lab.sh net {n} end\n")
    run("ifup", br)                  # only this bridge; vmbr0/vmbr1 untouched


def remove_bridge(n):
    br = f"vmbr{n}"
    if not bridge_is_ours(n):
        warn(f"{br} was not created by lab, leaving it")
        return
    subprocess.run(["ifdown", br])
    # ifdown replays the config saved at ifup time, which may predate "expose": remove those rules here
    if is_exposed(n):
        set_expose(n, False)
    s = open(IFACES).read()
    old = block_re(n).search(s)
    if old:                                  # same for block rules
        for line in old.group(0).splitlines():
            if line.strip().startswith("post-down"):
                subprocess.run(shlex.split(line.strip()[len("post-down"):]), stderr=subprocess.DEVNULL)
    run("cp", "-a", IFACES, IFACES + ".lab-bak")
    s = re.sub(rf"\n?# lab\.sh net {n} begin.*?# lab\.sh net {n} end\n", "", s, flags=re.S)
    with open(IFACES, "w") as f:
        f.write(s)
    if link_exists(br):
        subprocess.run(["ip", "link", "delete", br])
    print(f">> bridge {br} removed")


def next_ip(n, rows):
    used = {r["ip"] for r in rows.values() if r["net"] == n}
    for i in range(IP_FIRST, 255):
        if f"10.10.{n}.{i}" not in used:
            return f"10.10.{n}.{i}"
    die(f"net {n} is full")


def host_dns():
    for line in open("/etc/resolv.conf"):
        if line.startswith("nameserver"):
            return line.split()[1]
    return "1.1.1.1"


def vm_ip(vmid, rows):
    r = rows.get(vmid)
    if r and r["ip"] != "-":
        return r["ip"]
    try:
        out = subprocess.run(["qm", "guest", "cmd", str(vmid), "network-get-interfaces"],
                             capture_output=True, text=True, timeout=5).stdout
        for i in json.loads(out):
            if i.get("name") == "lo":
                continue
            for a in i.get("ip-addresses", []):
                if a.get("ip-address-type") == "ipv4":
                    return a["ip-address"]
    except (subprocess.TimeoutExpired, ValueError):
        pass
    m = re.search(r"^net0: \w+=([0-9A-Fa-f:]+)", run("qm", "config", vmid), re.M)
    if m and os.path.exists(LEASES):             # agent not up yet: DHCP lease
        mac = m.group(1).lower()
        hits = [l.split()[2] for l in open(LEASES) if len(l.split()) > 2 and l.split()[1] == mac]
        if hits:
            return hits[-1]
    return "-"
