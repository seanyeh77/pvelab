"""VMs made by hand (PVE's web UI) on one of pvelab's nets: what they have (their network cards, cloud-init
address and user, the addresses their guest agent reports), finding them (strays: pvelab list names them),
and the checks pvelab adopt makes before it records one as pvelab's.

pvelab's nets here are the numbered nets of nets.all_nets() (its own blocks, SDN labN, PVE vnets it took
over); not the DHCP bridge (vmbr1) nor an admin's bridges (vmbr0): only the numbered nets have pvelab's
rules, addresses and owners."""

import ipaddress
import re
from collections import namedtuple

from . import accounts, config, nets
from .guest import guest_json
from .helptext import bad_usage
from .inventory import is_managed, on_net
from .isolation import shared_mac
from .owners import is_shared_vm, key_problem, shared_name, without_net
from .util import die

Stray = namedtuple("Stray", "vmid name net nic")
NIC = re.compile(r"net(\d+)")
MAC = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}")


def nics(conf):
    """[(key, bridge, mac)] of a VM config's network cards net0, net1, ... in number order (bridge= and the
    MAC of MODEL=MAC, lower case; None when the value has none)."""
    out = []
    for key in sorted((k for k in conf if NIC.fullmatch(k)), key=lambda k: int(k[3:])):
        opts = dict(p.partition("=")[::2] for p in str(conf[key]).split(",") if p)
        mac = next((v for k, v in opts.items() if k != "bridge" and MAC.fullmatch(v)), None)
        out.append((key, opts.get("bridge") or None, mac.lower() if mac else None))
    return out


def pvelab_bridges(found=None):
    """{bridge: net number} of pvelab's nets (found: nets.all_nets(), if read)."""
    return {config.bridge(n): n for n in (nets.all_nets() if found is None else found)}


def strays(inv, found=None):
    """The VMs of inv (inventory()) that are not pvelab's and have a card on one of its nets - the first such
    card, net0 first - in VMID order. Templates are left out."""
    bridges, out = pvelab_bridges(found), []
    for vmid, c in sorted(inv.items()):
        if not c or is_managed(c) or c.get("template") == "1":
            continue
        for key, br, _ in nics(c):
            if br in bridges:
                out.append(Stray(vmid, c.get("name") or str(vmid), bridges[br], key))
                break
    return out


def adopt_arg(s, inv):
    """How pvelab adopt names stray s: its name when no other VM of inv has it and VM arguments read it as a
    name, else its VMID."""
    name = (inv.get(s.vmid) or {}).get("name") or ""
    if name and accounts.vm_name_problem(name) is None and \
            sum(1 for c in inv.values() if (c or {}).get("name") == name) == 1:
        return name
    return str(s.vmid)


def stray_line(s, inv, shared):
    """pvelab list's line for stray s: pvelab leaves it alone, and the command that hands it over. shared: the
    net numbers that have their shared VM already."""
    head = f"{s.name} ({s.vmid}) on {nets.net_name(s.net)}"
    if s.nic != "net0":
        return (f"{head} ({s.nic}): not made by pvelab, so pvelab leaves it alone; pvelab adopt takes a VM whose "
                "net0 is on its net")
    whose = "--owner NAME" if s.net in shared else "--shared (or --owner NAME)"
    return f"{head}: not made by pvelab, so pvelab leaves it alone; pvelab adopt {adopt_arg(s, inv)} {whose} hands it over"


# --- the checks pvelab adopt makes ---

def fixed_address(conf):
    """(address, prefix length, gateway or None) of cloud-init's ipconfig0 ip=A/P[,gw=G]; None without a fixed
    address (ip=dhcp, no ip=, no ipconfig0)."""
    opts = dict(p.partition("=")[::2] for p in (conf.get("ipconfig0") or "").split(",") if p)
    m = re.fullmatch(r"(\d+\.\d+\.\d+\.\d+)/(\d+)", opts.get("ip", ""))
    return (m.group(1), int(m.group(2)), opts.get("gw") or None) if m else None


def has_cloudinit(conf):
    """Whether a VM config has a cloud-init drive (an ide/sata/scsi disk that is one)."""
    return any(re.fullmatch(r"(ide|sata|scsi)\d+", k) and "cloudinit" in str(v) for k, v in conf.items())


def net0_mac(conf):
    return next((mac for key, _, mac in nics(conf) if key == "net0"), None)


def guest_addresses(vmid, mac, node):
    """The IPv4 addresses the guest agent reports on the card with MAC mac, or None: no answer, or that card
    not reported, or no IPv4 on it."""
    for i in guest_json("cmd", vmid, "network-get-interfaces", timeout=5, node=node) or []:
        if isinstance(i, dict) and str(i.get("hardware-address", "")).lower() == mac:
            found = [a["ip-address"] for a in i.get("ip-addresses", [])
                     if a.get("ip-address-type") == "ipv4" and a.get("ip-address")]
            return found or None
    return None


def _ip(v):
    try:
        return ipaddress.ip_address(v)
    except ValueError:
        return None


def pick_ip(vmid, conf, n, ip_opt, rows, inv):
    """(address, "ipconfig0" | "--ip"): the address pvelab records for VM vmid on net n - its cloud-init
    ipconfig0's when fixed (in the net's range, with its prefix and gateway), else --ip; or die. rows:
    cred_rows(), inv: inventory() (no two VMs of the net get one address)."""
    name, net, cidr, gw = conf.get("name") or str(vmid), nets.net_name(n), nets.cidr(n), nets.gateway(n)
    prefix = cidr.prefixlen
    again = "cloud-init sets it at the next boot), then run pvelab adopt again"
    if fixed := fixed_address(conf):
        a, p, g = fixed
        if _ip(a) is None or _ip(a) not in cidr:
            die(f"{name}'s ipconfig0 gives {a}/{p}, outside {net} ({cidr}): give it an address of {net} "
                f"(qm set {vmid} --ipconfig0 ip=ADDR/{prefix},gw={gw}; {again}")
        if p != prefix:
            die(f"{name}'s ipconfig0 gives {a}/{p}, {net} is /{prefix}: qm set {vmid} --ipconfig0 "
                f"ip={a}/{prefix},gw={gw} ({again}")
        if _ip(g) != gw:
            die(f"{name}'s ipconfig0 has the gateway {g or 'none'}, {net}'s is {gw}: qm set {vmid} --ipconfig0 "
                f"ip={a}/{prefix},gw={gw} ({again}")
        if ip_opt is not None and _ip(ip_opt) != _ip(a):
            die(f"--ip {ip_opt}, but {name}'s ipconfig0 gives {a}: leave --ip out, or change ipconfig0 first")
        source, fix = "ipconfig0", (f"give {name} another address (qm set {vmid} --ipconfig0 "
                                    f"ip=ADDR/{prefix},gw={gw}; {again}")
    elif ip_opt is None:
        seen = guest_addresses(vmid, net0_mac(conf), conf.get("_node")) or []
        seen = [x for x in seen if _ip(x) is not None and _ip(x) in cidr]
        die(f"{name} has no fixed address in its config (ipconfig0: {conf.get('ipconfig0') or 'none'}): pvelab needs "
            f"the address it has on {net} - give it with --ip ADDR (recorded only; nothing in the VM changes)"
            + (f"; its guest agent reports {', '.join(seen)}" if seen else ""))
    else:
        if _ip(ip_opt) is None or _ip(ip_opt) not in cidr:
            bad_usage(f"--ip {ip_opt}: an address of {net} ({cidr})")
        a, source, fix = str(_ip(ip_opt)), "--ip", f"give the address {name} has on {net} with --ip"
    if _ip(a) == gw:
        die(f"{a} is {net}'s gateway: {fix}")
    if not nets.usable(_ip(a), cidr):
        die(f"{a} is no host address of {net} ({cidr}): {fix}")
    br = config.bridge(n)
    other = next((r["name"] for v, r in rows.items() if v != vmid and r.get("net") == n and r.get("ip") == a), None)
    other = other or next((c.get("name") or str(v) for v, c in inv.items() if v != vmid and c
                           and any(b == br for _, b, _ in nics(c)) and (fixed_address(c) or ("",))[0] == a), None)
    if other:
        die(f"{a} is {other}'s address already: two VMs can't share it - change {name}'s address first")
    return a, source


def check_agent(vmid, conf, ip, source, node, running):
    """Whether the guest has ip: dies when its agent reports other IPv4 addresses on net0; a warning (text)
    when it can't be asked and ip came from --ip; else None."""
    name = conf.get("name") or str(vmid)
    if running and (seen := guest_addresses(vmid, net0_mac(conf), node)) is not None:
        if ip not in seen:
            die(f"{name}'s guest agent reports {', '.join(seen)} on net0, not {ip}: pvelab's rules would follow an "
                "address it does not have. Fix its address (or --ip), then run pvelab adopt again")
        return None
    if source != "--ip":
        return None
    why = "its guest agent did not answer" if running else f"{name} is not running"
    return f"{ip} not checked ({why}): pvelab's rules follow the address recorded"


def shared_problem(vmid, conf, n, inv):
    """Why VM vmid can't be net n's shared VM, or None: the net has one, or (an SDN net) its net0 lacks the
    fixed MAC VMs on other hosts reach it by."""
    name, net = conf.get("name") or str(vmid), nets.net_name(n)
    if other := next((c.get("name") or str(v) for v, c in on_net(n, inv).items() if v != vmid and is_shared_vm(c)),
                     None):
        return (f"{other} is {net}'s shared VM already (one per net: they share a fixed MAC) - --owner NAME adopts "
                f"{name} as an owner's VM")
    smac, mac = shared_mac(n), net0_mac(conf)
    if config.is_sdn(n) and mac != smac:
        net0 = conf.get("net0", "")
        new = MAC.sub(smac, net0, count=1) if mac else f"{net0},macaddr={smac}"
        return (f"{name}'s net0 has the MAC {mac}; {net}'s shared VM needs the fixed MAC {smac} (VMs on other hosts "
                f"reach it by that). Shut it down, then: qm set {vmid} --net0 {new}; start it and run pvelab adopt "
                "again (a guest whose network config names the old MAC needs that changed too)")
    return None


def owner_key(arg, n, conf):
    """--owner arg's owner key on net n (the net's name dropped), checked as pvelab owner checks KEY."""
    if why := accounts.vm_name_problem(arg):
        bad_usage(f"--owner: {why}")
    key = without_net(arg, nets.net_name(n))
    if why := key_problem(key, nets.pvelab_names()):
        bad_usage(f"--owner {arg}: {why}")
    if (name := conf.get("name")) == shared_name(n):
        die(f"{name} is named as {nets.net_name(n)}'s shared VM: --shared, or give it another name first "
            f"(qm set {conf.get('_vmid')} --name NEW)")
    return key


def login(conf, user_opt):
    """The login pvelab records: cloud-init's user (ciuser; --user must be it), else --user."""
    name = conf.get("name") or str(conf.get("_vmid"))
    if ciuser := conf.get("ciuser"):
        if user_opt is not None and user_opt != ciuser:
            die(f"{name}'s cloud-init user is {ciuser}: pvelab logs in as that (passwd, handout) - leave --user out "
                f"or give --user {ciuser}")
        return ciuser
    if user_opt is None:
        die(f"{name} has no cloud-init user (ciuser): --user USER names the login pvelab hands out")
    if why := accounts.user_problem(user_opt):
        bad_usage(f"--user: {why}")
    return user_opt
