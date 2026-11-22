"""VMs made by hand (PVE's web UI) on one of pvelab's nets: what they have (their network cards, cloud-init
address and user, the addresses their guest agent reports), finding them (strays: pvelab list names them),
and the checks pvelab adopt makes before it records one as pvelab's.

pvelab's nets here are the numbered nets of nets.all_nets() (its own blocks, SDN labN, PVE vnets it took
over); not the DHCP bridge (vmbr1) nor an admin's bridges (vmbr0): only the numbered nets have pvelab's
rules, addresses and owners."""

import functools
import ipaddress
import re
from collections import Counter, namedtuple

from . import accounts, config, nets, zones
from .guest import guest_json
from .helptext import bad_usage
from .ifaces import bridge_is_ours, gateway_elsewhere, our_nets
from .inventory import has_cloudinit, is_managed, MAC, net0, nic, on_net, pve_opts, pve_value
from .isolation import shared_mac
from .lifecycle import ip_config
from .owners import is_shared_vm, key_problem, shared_name, without_net
from .util import die

Stray = namedtuple("Stray", "vmid name net nic arg tagged")     # arg: how pvelab adopt names it; tagged: tag_problem
NIC = re.compile(r"net(\d+)")


def nics(conf):
    """[(key, bridge, mac)] of a VM config's network cards net0, net1, ... in number order (see inventory.nic)."""
    return [(key, *nic(conf[key])) for key in sorted((k for k in conf if NIC.fullmatch(k)), key=lambda k: int(k[3:]))]


def vlan_tag(conf, key="net0"):
    """The VLAN tag (tag=) of a VM config's card key, or None: a tagged card on a pvelab bridge is on that VLAN,
    not on the net (pvelab's nets are untagged)."""
    return pve_opts(conf.get(key)).get("tag") or None


def net0_without(conf, key):
    """VM conf's net0 value without its option key (tag, firewall): what qm set --net0 gives it to drop that."""
    return pve_value({k: v for k, v in pve_opts(conf.get("net0")).items() if k != key})


def on_nets(conf, bridges):
    """[(key, net number)] of a VM config's cards on pvelab's nets (bridges: {bridge: net number}, such as
    pvelab_bridges(); a card with a VLAN tag is not on its bridge's net), in card order."""
    return [(key, bridges[br]) for key, br, _ in nics(conf) if br in bridges and not vlan_tag(conf, key)]


def pvelab_bridges(only_here=False, text=None):
    """{bridge: net number} of pvelab's nets (nets.all_nets()); only_here: only those whose gateway is here
    (ifaces.our_nets: no SDN or zone file read). text: /etc/network/interfaces, if read."""
    text = config.read_text(config.IFACES) if text is None else text     # read once for all the nets
    return {config.bridge(n, text): n for n in (our_nets(text) if only_here else nets.all_nets())}


def on_net_host(n, node):
    """Whether a VM on host node is on net n's network: the net is a bridge on node (vmbrN, a taken-over vnet
    of a simple zone - a bridge of that name on another host is another network), or an SDN net whose zone
    has node."""
    on = zones.net_hosts(n)
    return on is None or node in on


def host_problem(n, node, name):
    """Why VM name, on host node, is not on net n's network (on_net_host is False)."""
    from .placement import bridge_host       # placement reads resources: imported when needed
    net = nets.net_name(n)
    if pin := bridge_host(n):
        return (f"{name} is on {node}; {net} is a bridge on {pin} only: move it there (qm migrate), then run "
                "pvelab adopt again")
    return (f"{name} is on {node}; {net}'s zone {zones.net_zone(n)} is on {', '.join(zones.net_hosts(n))} only: "
            "move it to one of those hosts (qm migrate), then run pvelab adopt again")


def rules_problem(n, conf):
    """Why net n's rules can't follow VM conf (a card of it on n) from this host, or None: n's gateway is on
    another host (adopt is run there), or the VM is on a host n is not on (host_problem)."""
    if not bridge_is_ours(n):
        return (f"{nets.net_name(n)}'s gateway is on {gateway_elsewhere(n) or 'another host'}: its rules (isolation, "
                "ssh forwards, blocks) are there - run pvelab adopt on that host")
    if not on_net_host(n, conf.get("_node")):
        return host_problem(n, conf.get("_node"), conf.get("name") or str(conf.get("_vmid")))
    return None


def tag_problem(conf, bridges):
    """Why VM conf's net0 is not on the net of its bridge, one of pvelab's (bridges: {bridge: net number}), and the
    fix: it has a VLAN tag; else None."""
    br = net0(conf)[0]
    if br not in bridges or not (tag := vlan_tag(conf)):
        return None
    vmid, name, net = conf.get("_vmid"), conf.get("name"), nets.net_name(bridges[br])
    return (f"{name or f'VM ({vmid})'}'s net0 is on {br} with VLAN tag {tag}, so not on {net}'s network (untagged): "
            f"qm set {vmid} --net0 {net0_without(conf, 'tag')} (the VM is then on {net}, its address one of {net}'s), "
            "then run pvelab adopt again")


def adopt_net(conf, bridges):
    """(n, why): whether pvelab adopt takes VM conf - n: the net of its net0 when that is one of pvelab's
    (bridges: pvelab_bridges()), else None; why: what stops it (None: nothing) - a template, pvelab's already,
    net0 on none of pvelab's nets or with a VLAN tag (n None), rules_problem, PVE's firewall on net0
    (firewall_problem)."""
    vmid, name = conf.get("_vmid"), conf.get("name")
    called = f"{name or 'VM'} ({vmid})"
    if conf.get("template") == "1":
        return None, f"{called} is a template: pvelab adopt takes VMs"
    if "_rec" in conf:
        return None, f"{called} is pvelab's already (pvelab list shows it)"
    if is_managed(conf):
        return None, f"{called} was made by an older pvelab: pvelab migrate records it"
    br = net0(conf)[0]
    if br not in bridges:
        names = [nets.net_name(n) for n in sorted(bridges.values(), key=int)]
        return None, (f"{name or called}'s net0 is on {br or 'no bridge'}, not on one of pvelab's nets "
                      f"({', '.join(names) or 'none yet'}): pvelab adopt takes a VM whose net0 is on its net")
    if why := tag_problem(conf, bridges):
        return None, why
    n = bridges[br]
    return n, rules_problem(n, conf) or firewall_problem(conf)


def strays(inv, only_net=None):
    """The VMs of inv (inventory()) that are not pvelab's and have a card on one of its nets (only_net, if
    given: one of their cards on it) whose gateway is here and that is on their host (on_net_host) - the first
    such card on any of those nets, net0 first, so a VM's line is the same with or without --net - in VMID
    order: adopt takes those whose card is net0 (or prints the fix: PVE's firewall), and the others are named
    (with the fix when their net0 has a VLAN tag: tagged). Each with its arg: its name when no other VM of inv
    has it and VM arguments read it as a name (not digits only: a VMID), else its VMID."""
    hand = {vmid: c for vmid, c in sorted(inv.items()) if c and not is_managed(c) and c.get("template") != "1"}
    if not hand:                             # nothing made by hand: no nets read
        return []
    bridges = pvelab_bridges(only_here=True)     # only the nets whose gateway is here count
    on_host = functools.cache(on_net_host)   # asked only for a card on one of them
    names, out = Counter(c.get("name") for c in inv.values() if c), []
    for vmid, c in hand.items():
        cards = [(key, n) for key, n in on_nets(c, bridges) if on_host(n, c.get("_node"))]
        if not cards or only_net not in (None, *(n for _, n in cards)):
            continue
        key, n = cards[0]
        name = c.get("name") or ""
        arg = (name if name and not name.isdigit() and names[name] == 1 and accounts.vm_name_problem(name) is None
               else str(vmid))
        out.append(Stray(vmid, name or str(vmid), n, key, arg, tag_problem(c, bridges)))
    return out


def adoptable(inv):
    """{arg: config} of the strays pvelab adopt takes (their net0 on pvelab's net)."""
    return {s.arg: inv[s.vmid] for s in strays(inv) if s.nic == "net0"}


def stray_line(s, shared):
    """pvelab list's line for stray s: pvelab leaves it alone, and the command that hands it over. shared: the
    net numbers that have their shared VM already."""
    head = f"{s.name} ({s.vmid}) on {nets.net_name(s.net)}"
    alone = "not made by pvelab, so pvelab leaves it alone; "
    if s.nic != "net0":                      # net0 tagged: why and the fix
        return f"{head} ({s.nic}): {alone}" + (s.tagged or "pvelab adopt takes a VM whose net0 is on its net")
    whose = "--owner NAME" if s.net in shared else "--shared (or --owner NAME)"
    return f"{head}: {alone}pvelab adopt {s.arg} {whose} hands it over"


# --- the checks pvelab adopt makes ---

def fixed_address(conf):
    """(address, prefix length, gateway or None) of cloud-init's ipconfig0 ip=A/P[,gw=G]; None without a fixed
    address (ip=dhcp, no ip=, no ipconfig0)."""
    opts = pve_opts(conf.get("ipconfig0"))
    m = re.fullmatch(r"(\d+\.\d+\.\d+\.\d+)/(\d+)", opts.get("ip", ""))
    return (m.group(1), int(m.group(2)), opts.get("gw") or None) if m else None


def disks(conf):
    """A VM config's disks (ide/sata/scsi/virtio N that are no CD-ROM and no cloud-init drive), in key order."""
    return sorted(k for k, v in conf.items() if re.fullmatch(r"(ide|sata|scsi|virtio)\d+", k)
                  and "media=cdrom" not in str(v) and "cloudinit" not in str(v))


def has_vendor_data(conf):
    """Whether a VM config's cloud-init has pvelab's vendor-data (cicustom vendor=local:snippets/SNIPPET, as its
    templates give their clones): it keeps the SSH host keys when cloud-init runs again (rename, net set)."""
    return f"vendor=local:snippets/{config.SNIPPET}" in str(conf.get("cicustom") or "").split(",")


def guest_addresses(conf):
    """The IPv4 addresses VM conf's guest agent reports on its net0: on every interface with its MAC (a bond,
    bridge or VLAN on it has that MAC too, the card itself then often none), or None: no answer, or no IPv4 on
    any of them."""
    mac, found = net0(conf)[1], []
    for i in guest_json("cmd", conf["_vmid"], "network-get-interfaces", timeout=5, node=conf.get("_node")) or []:
        if isinstance(i, dict) and str(i.get("hardware-address", "")).lower() == mac:
            found += [a["ip-address"] for a in i.get("ip-addresses", [])
                      if a.get("ip-address-type") == "ipv4" and a.get("ip-address") and a["ip-address"] not in found]
    return found or None


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
    """(address, "ipconfig0" | "config" | "--ip"): the address pvelab records for VM conf on net n - its
    cloud-init ipconfig0's when fixed (in the net's range, with its prefix and gateway), else --ip; or die.
    Without a cloud-init drive nothing applies ipconfig0: its address is only the default of --ip ("config").
    rows: cred_rows(), inv: inventory() (no two VMs of the net's network get one address: pvelab's of the net,
    and those with a card on its bridge on a host the net is on - see on_net_host)."""
    vmid, name, net, cidr, gw = conf["_vmid"], conf["name"], nets.net_name(n), nets.cidr(n), nets.gateway(n)
    prefix = cidr.prefixlen
    boot, again = "cloud-init sets it at the next boot", ", then run pvelab adopt again"
    given = f"give the address {name} has on {net} with --ip"
    recorded = "(recorded only; nothing in the VM changes)"

    def qm_set(addr):
        return f"qm set {vmid} --ipconfig0 {ip_config(n, addr)}"
    fixed, source = fixed_address(conf), "--ip"
    if fixed and not has_cloudinit(conf):
        if ip_opt is None:
            if not _in(_ip(fixed[0]), cidr):
                die(f"{name}'s ipconfig0 gives {fixed[0]}, outside {net} ({cidr}), and no cloud-init drive applies "
                    f"it: {given} ADDR {recorded}")
            ip_opt, source = fixed[0], "config"
        fixed = None
    if fixed:
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
            f"the address it has on {net} - give it with --ip ADDR {recorded}"
            + (f"; its guest agent reports {', '.join(seen)}" if seen else ""))
    else:
        ip = _ip(ip_opt)
        if not _in(ip, cidr):
            bad_usage(f"--ip {ip_opt}: an address of {net} ({cidr})")
        a, fix = str(ip), given
    if ip == gw:
        die(f"{a} is {net}'s gateway: {fix}")
    if not nets.usable(ip, cidr):
        die(f"{a} is no host address of {net} ({cidr}): {fix}")
    br = net0(conf)[0]
    other = next((r["name"] for v, r in rows.items() if v != vmid and r.get("net") == n and r.get("ip") == a), None)
    other = other or next((c.get("name") or str(v) for v, c in inv.items() if v != vmid and c
                           and on_nets(c, {br: n}) and (fixed_address(c) or ("",))[0] == a
                           and on_net_host(n, c.get("_node"))), None)
    if other:
        die(f"{a} is {other}'s address already: two VMs can't share it - change {name}'s address first")
    return a, source


def check_agent(conf, n, ip, source, status):
    """Whether VM conf (on net n) has ip: dies when its guest agent reports other IPv4 addresses on net0 (the fix
    by where ip came from: ipconfig0, or --ip / config); when the agent gives nothing and ip did not come from a
    cloud-init ipconfig0, a warning
    (text) saying why (status: a function giving the VM's state, asked only then); else None."""
    if (seen := guest_addresses(conf)) is not None:
        if ip not in seen:
            head = (f"{conf['name']}'s guest agent reports {', '.join(seen)} on net0, not {ip}"
                    + (" (its ipconfig0's)" if source == "ipconfig0" else "")
                    + ": pvelab's rules would follow an address it does not have. ")
            if source != "ipconfig0":
                die(head + "Give the address it has with --ip, then run pvelab adopt again")
            has = next((a for a in seen if _in(_ip(a), nets.cidr(n))), "ADDR")
            die(head + f"Give ipconfig0 the address it has (qm set {conf['_vmid']} --ipconfig0 {ip_config(n, has)}; "
                "cloud-init applies it at its next reboot), or fix the guest's own network config, then run pvelab "
                "adopt again")
        return None
    if source == "ipconfig0":                # cloud-init applies it: the guest's
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
    not the VM's tap, which pvelab's isolation matches (isolation.tap). conf has a net0 (adopt_net asks)."""
    vmid = conf["_vmid"]
    if pve_opts(conf["net0"]).get("firewall") != "1":
        return None
    new = net0_without(conf, "firewall")
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
    """The login pvelab records: cloud-init's user (ciuser; --user must be it), else --user. Without a
    cloud-init drive nothing applies ciuser: it is only the default of --user. A cloud-init drive without
    ciuser is refused: cloud-init would give every password pvelab writes (passwd, snapshots) to the image's
    default user."""
    name, ciuser = conf["name"], conf.get("ciuser")
    if user_opt is not None and (why := accounts.user_problem(user_opt)):
        bad_usage(f"--user: {why}")
    if has_cloudinit(conf):
        if not ciuser:
            user = user_opt or "LOGIN"
            die(f"{name} has a cloud-init drive but no cloud-init user (ciuser): the passwords pvelab sets (pvelab "
                f"passwd, its snapshots) would reach the image's default user, not {user}. qm set {conf.get('_vmid')} "
                f"--ciuser {user} (a changed cloud-init config makes cloud-init run again at the next boot: new SSH "
                "host keys, unless pvelab's vendor-data is set), then run pvelab adopt again")
        if user_opt is not None and user_opt != ciuser:
            die(f"{name}'s cloud-init user is {ciuser}: pvelab logs in as that (passwd, handout) - leave --user out "
                f"or give --user {ciuser}")
        return ciuser
    if user_opt is None:
        if ciuser:
            return ciuser
        die(f"{name} has no cloud-init user (ciuser): --user USER names the login pvelab hands out")
    return user_opt
