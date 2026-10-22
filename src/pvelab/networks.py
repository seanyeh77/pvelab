"""NAT bridges vmbrN (10.10.N.0/24, fixed IPs) and the DHCP bridge vmbr1."""

import glob
import json
import os
import re
import subprocess

from .blocks import apply_blocks
from .config import DHCP_BRIDGE, DHCP_NET, IFACES, IP_FIRST, LEASES, UPLINK
from .credentials import cred_rows
from .expose import set_expose
from .ifaces import add_net_block, bridge_is_ours, has_section, remove_net_block, save_ifaces
from .inventory import is_managed, read_conf, vm_mac
from .isolation import set_isolation
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



def link_exists(dev):
    return ok("ip", "link", "show", dev)


def ensure_dhcp_bridge():
    """vmbr1: NAT bridge with dnsmasq DHCP for the templates and non --net students.
    (On OpenStack, port security drops guest MACs on vmbr0, so guests need NAT.)"""
    if not link_exists(DHCP_BRIDGE) and not re.search(rf"^(auto|iface) {DHCP_BRIDGE}( |$)",
                                                      open(IFACES).read(), re.M):
        print(f">> creating NAT bridge {DHCP_BRIDGE} ({DHCP_NET}.1/24)")
        block = (f"\nauto {DHCP_BRIDGE}\niface {DHCP_BRIDGE} inet static\n"
                 f"        address {DHCP_NET}.1/24\n"
                 f"        bridge-ports none\n        bridge-stp off\n        bridge-fd 0\n"
                 f"        post-up   echo 1 > /proc/sys/net/ipv4/ip_forward\n"
                 f"        post-up   iptables -t nat -A POSTROUTING -s {DHCP_NET}.0/24 -o {UPLINK} -j MASQUERADE\n"
                 f"        post-up   iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n"
                 f"        post-down iptables -t nat -D POSTROUTING -s {DHCP_NET}.0/24 -o {UPLINK} -j MASQUERADE\n"
                 f"        post-down iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n")
        save_ifaces(open(IFACES).read() + block)
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
    add_net_block(n, f"auto {br}\niface {br} inet static\n        address {sub}.1/24\n"
                     f"        bridge-ports none\n        bridge-stp off\n        bridge-fd 0\n"
                     f"        post-up   sysctl -qw net.ipv4.ip_forward=1\n{up}{down}")
    run("ifup", br)                  # only this bridge; vmbr0/vmbr1 untouched


SETTERS = {"expose": set_expose, "block": apply_blocks, "isolate": set_isolation}   # (net, on): each kind
ALWAYS_ON = {"block"}                        # mining-port rules: on for every net


def remove_bridge(n):
    br = f"vmbr{n}"
    if not bridge_is_ours(n):
        warn(f"{br} was not created by lab, leaving it")
        return
    subprocess.run(["ifdown", br])
    # ifdown replays the config saved at ifup time, which may predate later sections: undo those here
    for setter in SETTERS.values():
        setter(n, False)
    remove_net_block(n)
    if link_exists(br):
        subprocess.run(["ip", "link", "delete", br])
    print(f">> bridge {br} removed")


def refresh_net(n):
    """Rebuild every section that follows net n's VM list (ssh forwards, block rules, isolation)
    after VMs were added or removed - those that are on. Block rules are always on (mining ports)."""
    if bridge_is_ours(n):
        for kind, setter in SETTERS.items():
            if kind in ALWAYS_ON or has_section(kind, n):
                setter(n, True)


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


def vm_ip(vmid, rows, running=True):
    """The VM's IPv4: recorded (--net), else from its guest agent (running VMs only), else its DHCP lease."""
    r = rows.get(vmid)
    if r and r["ip"] != "-":
        return r["ip"]
    if running:                                  # a stopped VM has no agent to ask
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
    mac = vm_mac(vmid)
    if mac and os.path.exists(LEASES):           # agent not up yet: DHCP lease
        hits = [l.split()[2] for l in open(LEASES) if len(l.split()) > 2 and l.split()[1] == mac]
        if hits:
            return hits[-1]
    return "-"
