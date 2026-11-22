"""VMs made by hand (PVE's web UI) on one of pvelab's nets: what they have (their network cards, cloud-init
address and user, the addresses their guest agent reports), finding them (strays: pvelab list names them),
and the checks pvelab adopt makes before it records one as pvelab's.

pvelab's nets here are the numbered nets of nets.all_nets() (its own blocks, SDN labN, PVE vnets it took
over); not the DHCP bridge (vmbr1) nor an admin's bridges (vmbr0): only the numbered nets have pvelab's
rules, addresses and owners."""

import ipaddress
import re
from collections import Counter, namedtuple

from . import accounts, config, nets, zones
from .guest import guest_json
from .helptext import bad_usage
from .ifaces import bridge_is_ours
from .inventory import is_managed, MAC, net0, nic, on_net
from .isolation import shared_mac
from .lifecycle import ip_config
from .owners import is_shared_vm, key_problem, shared_name, without_net
from .util import die

Stray = namedtuple("Stray", "vmid name net nic arg")     # arg: how pvelab adopt names it
NIC = re.compile(r"net(\d+)")


def nics(conf):
    """[(key, bridge, mac)] of a VM config's network cards net0, net1, ... in number order (see inventory.nic)."""
    return [(key, *nic(conf[key])) for key in sorted((k for k in conf if NIC.fullmatch(k)), key=lambda k: int(k[3:]))]


def pvelab_bridges(found=None):
    """{bridge: net number} of pvelab's nets (found: nets.all_nets(), if read)."""
    text = config.read_text(config.IFACES)          # read once for all the nets
    return {config.bridge(n, text): n for n in (nets.all_nets() if found is None else found)}


def on_net_host(n, node):
    """Whether a VM on host node is on net n's network: the net is a bridge on node (vmbrN, a taken-over vnet
    of a simple zone - a bridge of that name on another host is another network), or an SDN net whose zone
    has node."""
    on = zones.net_hosts(n)
    return on is None or node in on


def host_problem(n, node, name):
    """Why VM name, on host node, is not on net n's network (see on_net_host), or None."""
    if on_net_host(n, node):
        return None
    from .placement import bridge_host       # placement reads resources: imported when needed
    net = nets.net_name(n)
    if pin := bridge_host(n):
        return (f"{name} is on {node}; {net} is a bridge on {pin} only: move it there (qm migrate), then run "
                "pvelab adopt again")
    return (f"{name} is on {node}; {net}'s zone {zones.net_zone(n)} is on {', '.join(zones.net_hosts(n))} only: "
            "move it to one of those hosts (qm migrate), then run pvelab adopt again")


def strays(inv, found=None, only_net=None):
    """The VMs of inv (inventory()) that are not pvelab's and have a card on one of its nets (only_net, if
    given) - the first such card, net0 first - in VMID order. Templates are left out, and so are VMs on a host
    the net is not on (a bridge of the same name there is another network) and VMs on a net whose gateway is
    on another host (adopt is run there). Each with its arg: its name when no other VM of inv has it and VM
    arguments read it as a name (not digits only: a VMID), else its VMID."""
    hand = {vmid: c for vmid, c in sorted(inv.items()) if c and not is_managed(c) and c.get("template") != "1"}
    if not hand:                             # nothing made by hand: no nets read
        return []
    bridges, out, on_host, here = pvelab_bridges(found), [], {}, {}
    names = Counter(c.get("name") for c in inv.values() if c)
    for vmid, c in hand.items():
        key, n = next(((key, bridges[br]) for key, br, _ in nics(c) if br in bridges), (None, None))
        if n is None or only_net is not None and n != only_net:
            continue
        if n not in here:
            here[n] = bridge_is_ours(n)
        where = (n, c.get("_node"))
        if here[n] and where not in on_host:
            on_host[where] = on_net_host(*where)
        if here[n] and on_host[where]:
            name = c.get("name") or ""
            arg = (name if name and not name.isdigit() and names[name] == 1 and accounts.vm_name_problem(name) is None
                   else str(vmid))
            out.append(Stray(vmid, name or str(vmid), n, key, arg))
    return out


def adoptable(inv, found=None):
    """{arg: config} of the strays pvelab adopt takes (their net0 on pvelab's net)."""
    return {s.arg: inv[s.vmid] for s in strays(inv, found) if s.nic == "net0"}


def stray_line(s, shared):
    """pvelab list's line for stray s: pvelab leaves it alone, and the command that hands it over. shared: the
    net numbers that have their shared VM already."""
    head = f"{s.name} ({s.vmid}) on {nets.net_name(s.net)}"
    alone = "not made by pvelab, so pvelab leaves it alone; "
    if s.nic != "net0":
        return f"{head} ({s.nic}): {alone}pvelab adopt takes a VM whose net0 is on its net"
    whose = "--owner NAME" if s.net in shared else "--shared (or --owner NAME)"
    return f"{head}: {alone}pvelab adopt {s.arg} {whose} hands it over"


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


def has_vendor_data(conf):
    """Whether a VM config's cloud-init has pvelab's vendor-data (cicustom vendor=local:snippets/SNIPPET, as its
    templates give their clones): it keeps the SSH host keys when cloud-init runs again (rename, net set)."""
    return f"vendor=local:snippets/{config.SNIPPET}" in str(conf.get("cicustom") or "").split(",")


def guest_addresses(conf):
    """The IPv4 addresses VM conf's guest agent reports on its net0 (by its MAC), or None: no answer, or that
    card not reported, or no IPv4 on it."""
    mac = net0(conf)[1]
    for i in guest_json("cmd", conf["_vmid"], "network-get-interfaces", timeout=5, node=conf.get("_node")) or []:
        if isinstance(i, dict) and str(i.get("hardware-address", "")).lower() == mac:
            found = [a["ip-address"] for a in i.get("ip-addresses", [])
                     if a.get("ip-address-type") == "ipv4" and a.get("ip-address")]
            return found or None
    return None


def _ip(v):
    """v as an IP address, or None."""
    try:
        return ipaddress.ip_address(v)
    except ValueError:
        return None


def _in(ip, cidr):
    """Whether ip (an _ip()) is an address of the network cidr."""
    return ip is not None and ip in cidr


def pick_ip(conf, n, ip_opt, rows, inv):
    """(address, "ipconfig0" | "--ip"): the address pvelab records for VM conf on net n - its cloud-init
    ipconfig0's when fixed (in the net's range, with its prefix and gateway), else --ip; or die. rows:
    cred_rows(), inv: inventory() (no two VMs of the net's network get one address: pvelab's of the net, and
    those with a card on its bridge on a host the net is on - see on_net_host)."""
    vmid, name, net, cidr, gw = conf["_vmid"], conf["name"], nets.net_name(n), nets.cidr(n), nets.gateway(n)
    prefix = cidr.prefixlen
    boot, again = "cloud-init sets it at the next boot", ", then run pvelab adopt again"

    def qm_set(addr):
        return f"qm set {vmid} --ipconfig0 {ip_config(n, addr)}"
    if fixed := fixed_address(conf):
        a, p, g = fixed
        ip = _ip(a)
        if not _in(ip, cidr):
            die(f"{name}'s ipconfig0 gives {a}/{p}, outside {net} ({cidr}): give it an address of {net} "
                f"({qm_set('ADDR')}; {boot}){again}")
        if p != prefix:
            die(f"{name}'s ipconfig0 gives {a}/{p}, {net} is /{prefix}: {qm_set(a)} ({boot}){again}")
        if _ip(g) != gw:
            die(f"{name}'s ipconfig0 has the gateway {g or 'none'}, {net}'s is {gw}: {qm_set(a)} ({boot}){again}")
        if ip_opt is not None and _ip(ip_opt) != ip:
            die(f"--ip {ip_opt}, but {name}'s ipconfig0 gives {a}: leave --ip out, or change ipconfig0 first")
        source, fix = "ipconfig0", f"give {name} another address ({qm_set('ADDR')}; {boot}){again}"
    elif ip_opt is None:
        seen = [x for x in guest_addresses(conf) or [] if _in(_ip(x), cidr)]
        die(f"{name} has no fixed address in its config (ipconfig0: {conf.get('ipconfig0') or 'none'}): pvelab needs "
            f"the address it has on {net} - give it with --ip ADDR (recorded only; nothing in the VM changes)"
            + (f"; its guest agent reports {', '.join(seen)}" if seen else ""))
    else:
        ip = _ip(ip_opt)
        if not _in(ip, cidr):
            bad_usage(f"--ip {ip_opt}: an address of {net} ({cidr})")
        a, source, fix = str(ip), "--ip", f"give the address {name} has on {net} with --ip"
    if ip == gw:
        die(f"{a} is {net}'s gateway: {fix}")
    if not nets.usable(ip, cidr):
        die(f"{a} is no host address of {net} ({cidr}): {fix}")
    br = config.bridge(n)
    other = next((r["name"] for v, r in rows.items() if v != vmid and r.get("net") == n and r.get("ip") == a), None)
    other = other or next((c.get("name") or str(v) for v, c in inv.items() if v != vmid and c
                           and any(b == br for _, b, _ in nics(c)) and (fixed_address(c) or ("",))[0] == a
                           and on_net_host(n, c.get("_node"))), None)
    if other:
        die(f"{a} is {other}'s address already: two VMs can't share it - change {name}'s address first")
    return a, source


def check_agent(conf, ip, source, status):
    """Whether VM conf has ip: dies when its guest agent reports other IPv4 addresses on net0; when the agent
    gives nothing and ip came from --ip, a warning (text) saying why (status: a function giving the VM's state,
    asked only then); else None."""
    if (seen := guest_addresses(conf)) is not None:
        if ip not in seen:
            die(f"{conf['name']}'s guest agent reports {', '.join(seen)} on net0, not {ip}: pvelab's rules would "
                "follow an address it does not have. Fix its address (or --ip), then run pvelab adopt again")
        return None
    if source != "--ip":
        return None
    why = "its guest agent did not answer" if status() == "running" else f"{conf['name']} is not running"
    return f"{ip} not checked ({why}): pvelab's rules follow the address recorded"


def shared_problem(conf, n, inv):
    """Why VM conf can't be net n's shared VM, or None: the net has one, or (an SDN net) its net0 lacks the
    fixed MAC VMs on other hosts reach it by."""
    vmid, name, net = conf["_vmid"], conf["name"], nets.net_name(n)
    if other := next((c.get("name") or str(v) for v, c in on_net(n, inv).items()
                      if v != vmid and is_shared_vm(c, n)), None):
        return (f"{other} is {net}'s shared VM already (one per net: they share a fixed MAC) - --owner NAME adopts "
                f"{name} as an owner's VM")
    smac, mac = shared_mac(n), net0(conf)[1]
    if config.is_sdn(n) and mac != smac:
        card = conf.get("net0", "")
        new = MAC.sub(smac, card, count=1) if mac else f"{card},macaddr={smac}"
        return (f"{name}'s net0 has the MAC {mac}; {net}'s shared VM needs the fixed MAC {smac} (VMs on other hosts "
                f"reach it by that). Shut it down, then: qm set {vmid} --net0 {new}; start it and run pvelab adopt "
                "again (a guest whose network config names the old MAC needs that changed too)")
    return None


def firewall_problem(conf):
    """Why VM conf's net0 stops pvelab's rules from following it, or None: PVE's firewall on that card
    (firewall=1) puts a firewall bridge between it and the net's bridge, whose port there (fwpr<VMID>p0) is
    not the VM's tap, which pvelab's isolation matches (isolation.tap)."""
    card, vmid = str(conf.get("net0") or ""), conf["_vmid"]
    opts = card.split(",")
    if "firewall=1" not in opts:
        return None
    new = ",".join(o for o in opts if o != "firewall=1")
    return (f"{conf.get('name') or vmid}'s net0 has PVE's firewall on (firewall=1): its port on {net0(conf)[0]} is "
            f"then fwpr{vmid}p0, not tap{vmid}i0, so pvelab's isolation would not know it. qm set {vmid} --net0 "
            f"{new} (PVE applies it at once while it runs, with network hotplug on - its default; else at its next "
            "start), then run pvelab adopt again")


def owner_key(arg, n, conf):
    """--owner arg's owner key on net n (the net's name dropped), checked as pvelab owner checks KEY."""
    if why := accounts.vm_name_problem(arg):
        bad_usage(f"--owner: {why}")
    key = without_net(arg, nets.net_name(n))
    if why := key_problem(key, nets.pvelab_names()):
        bad_usage(f"--owner {arg}: {why}")
    if (name := conf.get("name")) == shared_name(n):
        die(f"{name} is named as {nets.net_name(n)}'s shared VM: --shared, or give it another name first "
            f"(qm set {conf['_vmid']} --name NEW)")
    return key


def login(conf, user_opt):
    """The login pvelab records: cloud-init's user (ciuser; --user must be it), else --user."""
    name = conf["name"]
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
