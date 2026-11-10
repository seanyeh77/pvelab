"""PVE SDN: pvelab's VXLAN zone, with one vnet labN per net - the same layer-2 network on every node
of the cluster. SDN makes the bridge on each node; the gateway (the net's first address, NAT, pvelab's rules) is
pvelab's own stanza for labN in /etc/network/interfaces, on the node that runs pvelab for the net."""

import re
import socket
import time
from contextlib import contextmanager

from .config import COROSYNC_CONF, net_bridge, read_text, SDN_TAG_BASE, SDN_ZONE, UPLINK
from . import nets
from .ifaces import make_rules_add_once
from .nodes import members, multi, run_on
from .resources import pvesh_json
from .util import die, LabError, parallel, run, uplink_ip, warn


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
                 f"check Datacenter \u2192 SDN for a {SDN_ZONE} vnet left over.")
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
    """One line per item with a pending change that is not pvelab's own (zone SDN_ZONE, vnets labN in it,
    their subnets). listings: {"zones"|"vnets"|"subnets"|"controllers": pvesh --pending 1 rows}."""
    ours = {r.get("vnet") for r in listings.get("vnets", [])
            if r.get("zone") == SDN_ZONE and re.fullmatch(r"lab\d+", str(r.get("vnet")))}
    out = []
    for kind, key in (("zone", "zone"), ("vnet", "vnet"), ("subnet", "subnet"), ("controller", "controller")):
        for r in listings.get(kind + "s", []):
            if not r.get("state"):
                continue
            name = r.get(key) or r.get("id") or "?"
            if kind == "zone" and name == SDN_ZONE:
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
    _token, _released, _held_back = token, False, False
    try:
        yield _token
    finally:
        if not _released:                         # apply() released it itself, else it is still held
            run("pvesh", "delete", "/cluster/sdn/lock", "--lock-token", _token, check=False, quiet=True)
        _token, _released, _held_back = None, False, False


def vnet(n):
    return net_bridge(n, True)


def subnet_id(n):
    net = nets.cidr(n)
    return f"{SDN_ZONE}-{net.network_address}-{net.prefixlen}"


def conflict(n, subnet=True):
    """Why SDN has net n's vnet name or subnet already (someone else's, or left over), or None."""
    if vnet(n) in nets.sdn_vnets():                  # any zone: vnet names are cluster-wide
        return f"SDN vnet {vnet(n)} exists already"
    net = nets.cidr(n)
    if subnet and any(other == net for _, _, other in nets.sdn_subnets()):
        return f"SDN has a subnet {net} already"
    return None


def node_ips():
    """The VXLAN peers: every cluster node's address (from .members with several nodes, else corosync's),
    or this node's own when it is not in a cluster."""
    if multi():
        return sorted(m["ip"] for m in members().values() if m["ip"])
    names = re.findall(r"ring0_addr:\s*(\S+)", read_text(COROSYNC_CONF))
    if names:
        return sorted(socket.gethostbyname(a) for a in names)
    return [uplink_ip() or die(f"no IPv4 address on {UPLINK} for SDN's VXLAN")]


def zone_mtu():
    """VXLAN takes 50 bytes of each packet: 1500 inside where the uplink allows it."""
    with open(f"/sys/class/net/{UPLINK}/mtu") as f:
        return min(1500, int(f.read()) - 50)


def zone():
    """pvelab's VXLAN zone (pvesh's dict of it), or None if there is none."""
    return next((z for z in pvesh_json("/cluster/sdn/zones") if z.get("zone") == SDN_ZONE), None)


def ensure_zone():
    peers = ",".join(node_ips())
    found = zone()
    if found is None:
        run("pvesh", "create", "/cluster/sdn/zones", "--type", "vxlan", "--zone", SDN_ZONE, "--peers", peers,
            "--mtu", zone_mtu(), *_lock())
    elif found.get("peers") != peers:             # a node joined or left the cluster
        run("pvesh", "set", f"/cluster/sdn/zones/{SDN_ZONE}", "--peers", peers, *_lock())


def add_vnet(n):
    ensure_zone()
    run("pvesh", "create", "/cluster/sdn/vnets", "--vnet", vnet(n), "--zone", SDN_ZONE,
        "--tag", SDN_TAG_BASE + int(n), "--alias", f"pvelab {nets.net_name(n)}", *_lock())
    run("pvesh", "create", f"/cluster/sdn/vnets/{vnet(n)}/subnets", "--type", "subnet",
        "--subnet", str(nets.cidr(n)), "--gateway", str(nets.gateway(n)), *_lock())


def remove_vnet(n):
    """Delete net n's vnet (and its subnet); also what an unfinished add_vnet left. Takes effect with apply()."""
    run("pvesh", "delete", f"/cluster/sdn/vnets/{vnet(n)}/subnets/{subnet_id(n)}", *_lock(), check=False, quiet=True)
    run("pvesh", "delete", f"/cluster/sdn/vnets/{vnet(n)}", *_lock(), check=False, quiet=True)


def apply():
    """Apply the SDN configuration on every node (PVE reloads their whole network config) and wait."""
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
