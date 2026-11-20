"""PVE SDN: one vnet labN per net in a VXLAN zone (pvelab's default zone, or one the net was made in) - the
same layer-2 network on every host of the zone. SDN makes the bridge on each node; the gateway (the net's
first address, NAT, pvelab's rules) is pvelab's own stanza for labN in /etc/network/interfaces, on the
node that runs pvelab for the net."""

import re
import time
from contextlib import contextmanager

from .config import bridge as config_bridge, net_bridge, SDN_TAG_BASE, SDN_ZONE, taken_vnets, UPLINK
from . import nets, zones
from .ifaces import make_rules_add_once
from .nodes import online_nodes, run_on, this_node
from .resources import pvesh_json
from .util import die, LabError, parallel, run, warn


_token = None                  # the SDN lock's token while a session() is open
_released = False              # apply() released it; the next change takes the lock again
_held_back = False             # after the release someone else changed SDN: pvelab's changes stay pending


def _take():
    return run("pvesh", "create", "/cluster/sdn/lock", "--allow-pending", "1", quiet=True).strip().strip('"')


def _lock():
    """--lock-token args for an SDN change. After apply() released the lock (e.g. undoing a vnet then), it is
    taken again - unless someone else made SDN changes meanwhile: an apply would push theirs too, so the
    lock is let go, the change is only recorded (pending) and apply() does nothing. If the lock cannot be
    taken (someone else holds it), it is the same: no error here, the undo goes on without it."""
    global _token, _released, _held_back
    if _token and _released and not _held_back:
        try:
            token = _take()                       # held first: nothing new can come in during the check
        except LabError as e:
            _held_back = True
            warn(f"the SDN lock could not be taken again ({e}); pvelab's SDN undo may be incomplete: "
                 "check Datacenter \u2192 SDN for a vnet labN left over.")
            return []
        _token, _released = token, False
        items = foreign_pending()
        if items:
            run("pvesh", "delete", "/cluster/sdn/lock", "--lock-token", _token, check=False, quiet=True)
            _released, _held_back = True, True
            warn(f"SDN has changes that are not pvelab's: {', '.join(items)}. pvelab's undo is left pending, "
                 "not applied; apply it in Datacenter \u2192 SDN once those are sorted.")
    if _held_back:
        return []
    return ["--lock-token", _token] if _token else []


def pending(listings):
    """One line per item with a pending change that is not pvelab's own (zone SDN_ZONE or one this run
    made, pvelab's vnets labN, their subnets). listings: {"zones"|"vnets"|"subnets"|"controllers": pvesh --pending 1 rows}."""
    ours = {r.get("vnet") for r in listings.get("vnets", [])
            if nets.pvelabs_own(str(r.get("vnet")), r.get("zone"))} | set(taken_vnets())
    out = []
    for kind, key in (("zone", "zone"), ("vnet", "vnet"), ("subnet", "subnet"), ("controller", "controller")):
        for r in listings.get(kind + "s", []):
            if not r.get("state"):
                continue
            name = r.get(key) or r.get("id") or "?"
            if kind == "zone" and (name == SDN_ZONE or name in _ours):
                continue
            if kind == "vnet" and name in ours:
                continue
            if kind == "subnet" and r.get("vnet") in ours:
                continue
            out.append(f"{kind} {name} ({r['state']})")
    return out


def foreign_pending():
    """Pending SDN changes in the cluster that are not pvelab's, one line each."""
    vnets = pvesh_json("/cluster/sdn/vnets", "--pending", "1")
    subs = parallel(lambda v: pvesh_json(f"/cluster/sdn/vnets/{v}/subnets", "--pending", "1"),
                    [v["vnet"] for v in vnets if v.get("vnet")])
    # a subnet row may lack its vnet: add it
    rows = [dict(r, vnet=r.get("vnet", v)) for v, rs in subs.items() for r in rs]
    return pending({"zones": pvesh_json("/cluster/sdn/zones", "--pending", "1"), "vnets": vnets,
                    "subnets": rows, "controllers": pvesh_json("/cluster/sdn/controllers", "--pending", "1")})


def refuse_foreign():
    items = foreign_pending()
    if items:
        die(f"SDN has changes that are not applied yet: {', '.join(items)}. "
            "Apply or roll them back (Datacenter \u2192 SDN), then run pvelab again.")


_snat_before = set()          # PVE's SNAT ranges when the session began (apply drops those gone since)


@contextmanager
def session():
    """Hold PVE's SDN lock while pvelab changes SDN; refuses when someone else has pending changes. The lock
    is held first: nothing new can come in between the check and pvelab's changes."""
    global _token, _released, _held_back
    token = _take()
    try:
        refuse_foreign()
    except BaseException:
        run("pvesh", "delete", "/cluster/sdn/lock", "--lock-token", token, check=False, quiet=True)
        raise
    global _snat_before
    _token, _released, _held_back = token, False, False
    _snat_before = snat_ranges()
    try:
        yield _token
    finally:
        if not _released:                         # apply() released it itself, else it is still held
            run("pvesh", "delete", "/cluster/sdn/lock", "--lock-token", _token, check=False, quiet=True)
        _token, _released, _held_back = None, False, False
        _ours.clear()                             # a zone left pending is someone's change to the next run


RELOAD_WARNING = ("applying SDN reloads the network of every host: users on other SDN nets may lose the "
                  "connection for a few seconds")


def vnet(n):
    """Net n's vnet: a PVE vnet pvelab took over (vnet1), else its own labN."""
    br = config_bridge(n)
    return net_bridge(n, True) if br == net_bridge(n, False) else br


def conflict(n, subnet=True):
    """Why SDN has net n's vnet name or subnet already (someone else's, or left over), or None."""
    if vnet(n) in nets.sdn_vnets():                  # any zone: vnet names are cluster-wide
        return f"SDN vnet {vnet(n)} exists already"
    net = nets.cidr(n)
    if subnet and any(other == net for _, _, other in nets.sdn_subnets()):
        return f"SDN has a subnet {net} already"
    return None


def node_ips():
    """The VXLAN peers: every cluster node's address, or this node's own when it is not in a cluster."""
    return sorted(zones.addresses().values())


def zone_mtu():
    """VXLAN takes 50 bytes of each packet: 1500 inside where the uplink allows it."""
    with open(f"/sys/class/net/{UPLINK}/mtu") as f:
        return min(1500, int(f.read()) - 50)


_ours = set()              # zones this run made: their pending change is pvelab's own


def ensure_zone(z, nodes=None):
    """Make VXLAN zone z (peers: its hosts' addresses; on nodes, or every host) unless it exists; an
    existing zone is left as it is. Whether it was made."""
    if z in _ours or z in zones.zones():
        return False
    addrs = zones.addresses()
    peers = sorted(ip for h, ip in addrs.items() if not nodes or h in nodes)
    run("pvesh", "create", "/cluster/sdn/zones", "--type", "vxlan", "--zone", z, "--peers", ",".join(peers),
        "--mtu", zone_mtu(), *(["--nodes", ",".join(nodes)] if nodes else []), "--ipam", "pve", *_lock())
    _ours.add(z)
    return True


def add_vnet(n):
    z = zones.net_zone(n)
    ensure_zone(z, zones.chosen_nodes(n))
    run("pvesh", "create", "/cluster/sdn/vnets", "--vnet", vnet(n), "--zone", z,
        "--tag", SDN_TAG_BASE + int(n), "--alias", nets.net_name(n), *_lock())
    create_subnet(vnet(n), nets.cidr(n), nets.gateway(n))


def set_zone(z, nodes=None, peers=None):
    """Zone z's hosts (nodes; [] means every host) and/or VXLAN peers. Takes effect with apply()."""
    args = [] if nodes is None else ["--nodes", ",".join(nodes)] if nodes else ["--delete", "nodes"]
    args += ["--peers", ",".join(peers)] if peers is not None else []
    run("pvesh", "set", f"/cluster/sdn/zones/{z}", *args, *_lock())


def delete_zone(z):
    run("pvesh", "delete", f"/cluster/sdn/zones/{z}", *_lock())


def sync_peers(before):
    """Every VXLAN zone whose peers were its hosts' addresses before ({host: IPv4}) gets those of now;
    hand-tuned peers are left, with a warning each. Whether any zone changed (the caller applies)."""
    changes, drift = zones.peer_plan(before, zones.addresses())
    for z, peers in changes.items():
        set_zone(z, peers=peers)
    for line in drift:
        warn(line)
    return bool(changes)


def set_alias(n):
    """Net n's vnet alias: its name (after a rename). Takes effect with apply()."""
    set_vnet_alias(vnet(n), nets.net_name(n))


def replace_subnet(n):
    """Net n's vnet with its subnet for the net's current range and gateway. Takes effect with apply()."""
    replace_vnet_subnet(vnet(n), nets.cidr(n), nets.gateway(n))


def remove_vnet(n):
    """Delete net n's vnet (and the subnets subnets.cfg has on it); also what an unfinished add_vnet left.
    Takes effect with apply()."""
    delete_vnet(vnet(n), check=False)


# --- by vnet id: any vnet of PVE's SDN config (pvelab's labN, or one made in the web UI). Each takes effect
# with apply().

def set_vnet_alias(v, alias):
    run("pvesh", "set", f"/cluster/sdn/vnets/{v}", "--alias", alias, *_lock())


def create_subnet(v, net, gw, snat=False):
    run("pvesh", "create", f"/cluster/sdn/vnets/{v}/subnets", "--type", "subnet", "--subnet", str(net),
        *(["--gateway", str(gw)] if gw else []), *(["--snat", "1"] if snat else []), *_lock())


def delete_subnets(v, check=True):
    """Delete the subnets subnets.cfg has on vnet v."""
    for ident, owner, _ in nets.sdn_subnets():
        if owner == v:
            run("pvesh", "delete", f"/cluster/sdn/vnets/{v}/subnets/{ident}", *_lock(), check=check,
                quiet=not check)


def replace_vnet_subnet(v, net, gw):
    """Vnet v's subnet replaced by net with gateway gw; its SNAT setting is kept (a subnet with other
    settings, or a second subnet, is refused: they would be lost)."""
    if why := nets.subnet_problem(v):
        die(why)
    snat = nets.pve_subnet(v)[2]
    delete_subnets(v)
    create_subnet(v, net, gw, snat)


def delete_vnet(v, check=True):
    delete_subnets(v, check=check)
    run("pvesh", "delete", f"/cluster/sdn/vnets/{v}", *_lock(), check=check, quiet=not check)


def snat_ranges():
    """The ranges PVE masquerades itself: subnets with SNAT in a zone where PVE gives the gateway."""
    return {str(v["cidr"]) for v in nets.pve_vnets() if nets.pve_snat(v)}


def tidy_snat(gone=(), node=None, want=None):
    """PVE's SNAT rules on node (this host by default) once each: a reload runs a changed vnet's post-up
    lines again, and PVE's SNAT line adds its rule every time. gone: ranges that no longer have SNAT
    (a vnet removed or moved): their rules go entirely."""
    node, want, gone = node or this_node(), snat_ranges() if want is None else want, {str(g) for g in gone}
    seen = set()
    for line in run_on(node, "iptables", "-t", "nat", "-S", "POSTROUTING", check=False,
                       quiet=True).splitlines():
        m = re.fullmatch(r"-A (POSTROUTING -s (\S+) -o \S+ -j SNAT --to-source \S+)", line.strip())
        if not m or m.group(2) not in want | gone:
            continue
        if m.group(2) not in want or m.group(1) in seen:
            run_on(node, "iptables", "-t", "nat", "-D", *m.group(1).split(), check=False, quiet=True)
        seen.add(m.group(1))


def apply():
    """Apply the SDN configuration on every node (PVE reloads their whole network config) and wait; then
    PVE's SNAT rules on every host once each, without those of ranges that lost their SNAT since the
    session began."""
    global _released
    lock = _lock()
    if _held_back:
        return                                   # someone else's changes are pending too (_lock warned)
    make_rules_add_once()                        # the reload runs every post-up line again
    out = run("pvesh", "set", "/cluster/sdn", *lock, *(["--release-lock", "1"] if lock else []), quiet=True)
    _released = bool(lock)                       # PVE released the lock with the apply
    lines = out.strip().splitlines()
    upid = lines[-1] if lines else ""
    if not upid.startswith("UPID:"):
        return                                   # no task to wait for
    node, t0 = upid.split(":")[1], time.time()
    while time.time() - t0 < 120:
        st = pvesh_json(f"/nodes/{node}/tasks/{upid}/status") or {}
        if st.get("status") == "stopped":
            if st.get("exitstatus") != "OK":
                die(f"applying SDN failed: {st.get('exitstatus')} (see the task log: {upid})")
            want = snat_ranges()
            parallel(lambda host: tidy_snat(_snat_before - want, host, want), online_nodes())
            return
        time.sleep(1)
    die(f"applying SDN did not finish in 2 min ({upid})")


def wait_vnet(node, n, secs=60):
    """Whether net n's vnet is up on node within secs. apply() waits only for the task that starts each
    node's network reload: PVE (API2/Network/SDN.pm) does not wait for those, so a host may lag behind."""
    t0 = time.time()
    while not run_on(node, "ip", "link", "show", vnet(n), check=False, quiet=True).strip():
        if time.time() - t0 >= secs:
            return False
        time.sleep(2)
    return True
