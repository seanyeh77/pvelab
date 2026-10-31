"""Nets: 10.10.N.0/24 with fixed IPs behind NAT - an SDN vnet labN (one network across the cluster's
nodes) where PVE has SDN, else a bridge vmbrN on this node - and the DHCP bridge vmbr1."""

import os
import re
import shutil
import subprocess

from .blocks import apply_blocks
from . import sdn
from .config import (bridge, DHCP_BRIDGE, DHCP_NET, IFACES, IP_FIRST, is_sdn, LEASES, net_bridge,
                     subnet, UPLINK)
from .credentials import cred_rows
from .expose import set_expose
from .guest import guest_json
from .ifaces import (add_net_block, block_begin, block_end, bridge_is_ours, has_section, iptables, post_lines,
                     remove_net_block, save_ifaces)
from .inventory import is_managed, read_conf, vm_mac
from .isolation import gw_mac, set_isolation
from .nodes import guest_conf_files, multi
from .util import die, LabError, ok, run, warn, write_file


def net_label(net):
    return f"{DHCP_BRIDGE}(dhcp)" if net == "dhcp" else f"net{net}"


def net_in_use(n):
    """Why net n (its bridge, 10.10.N.0/24) is taken, or None if it is free."""
    br, sub = bridge(n), subnet(n)
    if link_exists(br):
        return f"{br} already exists"
    if re.search(rf"^(auto|iface) {br}( |$)", open(IFACES).read(), re.M):
        return f"{br} is configured in {IFACES}"
    if re.search(rf"^{re.escape(sub)}\.0/24", run("ip", "route"), re.M):
        return f"{sub}.0/24 is already routed on this host"
    if why := sdn.conflict(n):
        return why
    for f in guest_conf_files():
        if re.search(rf"bridge={br}(,|$)", open(f).read(), re.M):
            return f"guest {os.path.basename(f)[:-5]} has a network card on {br}"
    if any(r["net"] == n for r in cred_rows().values()):
        return f"pvelab has VMs recorded on net{n}"
    return None


def foreign_use(n):
    """Why net n's bridge / 10.10.N.0/24 is used by something that is NOT pvelab's, or None.
    A net pvelab made itself (with or without VMs) is fine to reuse."""
    br, sub = bridge(n), subnet(n)
    ours = bridge_is_ours(n)
    if not ours:
        if link_exists(br):
            return f"{br} exists and was not made by pvelab"
        if re.search(rf"^(auto|iface) {br}( |$)", open(IFACES).read(), re.M):
            return f"{br} is configured in {IFACES} (not by pvelab)"
        if re.search(rf"^{re.escape(sub)}\.0/24", run("ip", "route"), re.M):
            return f"{sub}.0/24 is already routed on this host"
        if why := sdn.conflict(n):
            return why
    for f in guest_conf_files():
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


TCPMSS = "-p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"     # MTU-safe TCP through NAT


def bridge_stanza(br, sub, rules, sdn_made=False):
    """interfaces stanza of a NAT bridge for sub.0/24 (host .1): forwarding and masquerading on,
    plus iptables rules ("[-t TABLE] CHAIN ...") added when it comes up, removed when it goes down.
    sdn_made: SDN makes the bridge itself (its own stanza); this one only adds the gateway to it - with
    several hosts, with the gateway's fixed MAC (gw_mac; one host: as before, ensure_gw_mac adds it later)."""
    up, down = iptables([f"-t nat POSTROUTING -s {sub}.0/24 -o {UPLINK} -j MASQUERADE"] + rules)
    stanza = f"iface {br} inet static\n        address {sub}.1/24\n"
    if sdn_made:
        if multi():
            stanza += f"        hwaddress {gw_mac(sub.split('.')[-1])}\n"
    else:
        stanza = f"auto {br}\n{stanza}        bridge-ports none\n        bridge-stp off\n        bridge-fd 0\n"
    return stanza + post_lines(["sysctl -qw net.ipv4.ip_forward=1"] + up, down)


def ensure_dhcp_bridge():
    """vmbr1: NAT bridge with dnsmasq DHCP for VMs made without --net (create makes it when needed).
    (On OpenStack, port security drops guest MACs on vmbr0, so guests need NAT.)"""
    if not link_exists(DHCP_BRIDGE):
        if not re.search(rf"^(auto|iface) {DHCP_BRIDGE}( |$)", open(IFACES).read(), re.M):
            print(f">> creating NAT bridge {DHCP_BRIDGE} ({DHCP_NET}.1/24)")
            stanza = bridge_stanza(DHCP_BRIDGE, DHCP_NET, [f"-t mangle FORWARD {TCPMSS}"])
            save_ifaces(open(IFACES).read() + "\n" + stanza)
        run("ifup", DHCP_BRIDGE)       # only this bridge; also when it is configured but not up
    conf = f"/etc/dnsmasq.d/{DHCP_BRIDGE}.conf"
    want = (f"interface={DHCP_BRIDGE}\nbind-interfaces\nexcept-interface=lo\n"
            f"dhcp-range={DHCP_NET}.100,{DHCP_NET}.200,255.255.255.0,12h\n"
            f"dhcp-option=option:router,{DHCP_NET}.1\n"
            f"dhcp-option=option:dns-server,{DHCP_NET}.1\n")
    if not shutil.which("dnsmasq"):
        print(f">> installing dnsmasq for DHCP/DNS on {DHCP_BRIDGE}")
        subprocess.run(["apt-get", "install", "-y", "-q", "dnsmasq"], stdout=subprocess.DEVNULL,
                       env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}, check=True)
    if write_file(conf, want) or not ok("systemctl", "is-active", "--quiet", "dnsmasq"):   # also when an earlier start failed
        run("systemctl", "restart", "dnsmasq")


def is_new_net(n):
    """create makes net n (and sets its isolation) only if pvelab has no bridge for it yet."""
    return not bridge_is_ours(n)


def net_rules(br):
    return [f"-t mangle FORWARD -i {br} {TCPMSS}",
            f"-t mangle FORWARD -o {br} {TCPMSS}",
            f"FORWARD -i {br} -o {br} -j ACCEPT",                     # same subnet
            f"FORWARD -i {br} -o {UPLINK} -j ACCEPT",                 # internet
            f"FORWARD -o {br} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            f"FORWARD -i {br} -j DROP",                               # other subnets
            f"FORWARD -o {br} -j DROP"]


def check_free(br, sub):
    if link_exists(br) or re.search(rf"^(auto|iface) {br}( |$)", open(IFACES).read(), re.M):
        die(f"{br} already exists and was not created by lab - pick another --net")
    if re.search(rf"^{re.escape(sub)}\.0/24", run("ip", "route"), re.M):
        die(f"{sub}.0/24 is already routed on this host")


def ensure_bridge(n):
    br, sub = bridge(n), subnet(n)
    if bridge_is_ours(n):
        if not link_exists(br):
            run("ifup", br)
        return
    check_free(br, sub)
    if is_sdn(n):
        try:
            make_sdn_net(n, br, sub)
            return
        except LabError as e:
            br = net_bridge(n, False)
            warn(f"SDN vnet for net{n} failed ({e}) - using a bridge {br} on this node instead")
            check_free(br, sub)
    print(f">> creating isolated NAT bridge {br} ({sub}.1/24)")
    add_net_block(n, bridge_stanza(br, sub, net_rules(br)))
    run("ifup", br)                  # only this bridge; vmbr0/vmbr1 untouched


def make_sdn_net(n, br, sub):
    """Net n as SDN vnet br (on every node) with its gateway here; on failure, undo all of it."""
    print(f">> creating SDN vnet {br} ({sub}.0/24, VXLAN zone across the nodes) with NAT gateway {sub}.1 here")
    add_net_block(n, bridge_stanza(br, sub, net_rules(br), sdn_made=True))   # before apply brings br up
    applied = False
    try:
        sdn.add_vnet(n)
        applied = True                         # from here on the nodes may have it
        sdn.apply()
        if f"inet {sub}.1/" not in run("ip", "-4", "addr", "show", br, check=False, quiet=True):
            die(f"{br} came up without its gateway {sub}.1")
    except BaseException:
        remove_bridge(n, quiet=True)
        if applied:
            try:
                sdn.apply()
            except LabError as e:
                warn(f"undoing the SDN vnet {br}: {e}")
        raise


def ensure_gw_mac(n):
    """Give SDN net n's gateway its fixed MAC (gw_mac), now and in its stanza, if an older net lacks it.
    Run when the net first gets VMs on another host; a vmbrN net is on one host and is never touched."""
    if not is_sdn(n) or not bridge_is_ours(n):
        return
    s, br, mac = open(IFACES).read(), bridge(n), gw_mac(n)
    line = f"        address {subnet(n)}.1/24\n"
    block = re.search(rf"^{re.escape(block_begin(n))}.*?^{re.escape(block_end(n))}", s, re.M | re.S)
    if not block or "hwaddress " in block.group(0) or line not in block.group(0):
        return
    print(f">> {br}: the gateway {subnet(n)}.1 gets the fixed MAC {mac} (VMs on other hosts reach only it)")
    save_ifaces(s.replace(block.group(0), block.group(0).replace(line, line + f"        hwaddress {mac}\n", 1)))
    run("ip", "link", "set", br, "address", mac)


def vnet_up(host, n, nothing, then=""):
    """Wait (up to 1 min) until SDN net n's vnet is up on host; else LabError. nothing: what was not done
    ("no VM was made"); then: what to do after the ifreload on host."""
    if not sdn.wait_vnet(host, n):
        die(f"{bridge(n)} is not up on {host} after 1 min; {nothing}. On {host}: ifreload -a{then}")


def nets_ready_on(host, nets, nothing):
    """Make another host ready for VMs of the SDN nets: each gateway's fixed MAC (VMs on another host reach
    the gateway by it), each vnet up there."""
    for n in nets:
        ensure_gw_mac(n)
        vnet_up(host, n, nothing)


SETTERS = {"expose": set_expose, "block": apply_blocks, "isolate": set_isolation}   # (net, on): each kind


def remove_bridge(n, quiet=False):
    """Remove net n's bridge and rules. An SDN vnet is gone from the nodes after sdn.apply(), which the
    caller runs once for all the nets it removes; returns whether there is one to apply.
    quiet: undoing an unfinished net (no messages)."""
    br, made_by_sdn = bridge(n), is_sdn(n)
    if not bridge_is_ours(n):
        warn(f"{br} was not created by lab, leaving it")
        return False
    subprocess.run(["ifdown", br], stderr=subprocess.DEVNULL if quiet else None)
    # ifdown replays the config saved at ifup time, which may predate later sections: undo those here
    for setter in SETTERS.values():
        setter(n, False)
    remove_net_block(n)
    if made_by_sdn:
        sdn.remove_vnet(n)
    elif link_exists(br):
        subprocess.run(["ip", "link", "delete", br])
    if not quiet:
        print(f">> {'SDN vnet' if made_by_sdn else 'bridge'} {br} removed")
    return made_by_sdn


def refresh_net(n):
    """Rebuild every section that follows net n's VM list (ssh forwards, block rules, isolation)
    after VMs were added or removed - those that are on. Block rules are always on (mining ports)."""
    if bridge_is_ours(n):
        for kind, setter in SETTERS.items():
            if kind == "block" or has_section(kind, n):       # block: mining-port rules on every net
                setter(n, True)


def next_ip(n, rows):
    used = {r["ip"] for r in rows.values() if r["net"] == n}
    for i in range(IP_FIRST, 255):
        if f"{subnet(n)}.{i}" not in used:
            return f"{subnet(n)}.{i}"
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
        for i in guest_json("cmd", vmid, "network-get-interfaces", timeout=5) or []:
            if i.get("name") == "lo":
                continue
            for a in i.get("ip-addresses", []):
                if a.get("ip-address-type") == "ipv4":
                    return a["ip-address"]
    mac = vm_mac(vmid)
    if mac and os.path.exists(LEASES):           # agent not up yet: DHCP lease
        hits = [l.split()[2] for l in open(LEASES) if len(l.split()) > 2 and l.split()[1] == mac]
        if hits:
            return hits[-1]
    return "-"
