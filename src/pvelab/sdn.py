"""PVE SDN: pvelab's VXLAN zone, with one vnet labN per net - the same layer-2 network on every node
of the cluster. SDN makes the bridge on each node; the gateway (10.10.N.1, NAT, pvelab's rules) is
pvelab's own stanza for labN in /etc/network/interfaces, on the node that runs pvelab for the net."""

import re
import socket
import time

from .config import net_bridge, read_text, SDN_SUBNETS, SDN_TAG_BASE, SDN_VNETS, SDN_ZONE, subnet, UPLINK
from .ifaces import make_rules_add_once
from .resources import pvesh_json
from .util import die, run, uplink_ip


def vnet(n):
    return net_bridge(n, True)


def subnet_id(n):
    return f"{SDN_ZONE}-{subnet(n)}.0-24"


def conflict(n):
    """Why SDN has net n's vnet name or subnet already (someone else's, or left over), or None."""
    if re.search(rf"^vnet: {vnet(n)}$", read_text(SDN_VNETS), re.M):
        return f"SDN vnet {vnet(n)} exists already"
    if re.search(rf"^subnet: \S+-{re.escape(subnet(n))}\.0-24$", read_text(SDN_SUBNETS), re.M):
        return f"SDN has a subnet {subnet(n)}.0/24 already"
    return None


def node_ips():
    """The VXLAN peers: every cluster node's address, or this node's own when it is not in a cluster."""
    names = re.findall(r"ring0_addr:\s*(\S+)", read_text("/etc/pve/corosync.conf"))
    if names:
        return sorted(socket.gethostbyname(a) for a in names)
    return [uplink_ip() or die(f"no IPv4 address on {UPLINK} for SDN's VXLAN")]


def zone_mtu():
    """VXLAN takes 50 bytes of each packet: 1500 inside where the uplink allows it."""
    with open(f"/sys/class/net/{UPLINK}/mtu") as f:
        return min(1500, int(f.read()) - 50)


def ensure_zone():
    peers = ",".join(node_ips())
    zone = next((z for z in pvesh_json("/cluster/sdn/zones") if z.get("zone") == SDN_ZONE), None)
    if zone is None:
        run("pvesh", "create", "/cluster/sdn/zones", "--type", "vxlan", "--zone", SDN_ZONE, "--peers", peers,
            "--mtu", zone_mtu())
    elif zone.get("peers") != peers:             # a node joined or left the cluster
        run("pvesh", "set", f"/cluster/sdn/zones/{SDN_ZONE}", "--peers", peers)


def add_vnet(n):
    ensure_zone()
    run("pvesh", "create", "/cluster/sdn/vnets", "--vnet", vnet(n), "--zone", SDN_ZONE,
        "--tag", SDN_TAG_BASE + int(n), "--alias", f"pvelab net{n}")
    run("pvesh", "create", f"/cluster/sdn/vnets/{vnet(n)}/subnets", "--type", "subnet",
        "--subnet", f"{subnet(n)}.0/24", "--gateway", f"{subnet(n)}.1")


def remove_vnet(n):
    """Delete net n's vnet (and its subnet); also what an unfinished add_vnet left. Takes effect with apply()."""
    run("pvesh", "delete", f"/cluster/sdn/vnets/{vnet(n)}/subnets/{subnet_id(n)}", check=False, quiet=True)
    run("pvesh", "delete", f"/cluster/sdn/vnets/{vnet(n)}", check=False, quiet=True)


def apply():
    """Apply the SDN configuration on every node (PVE reloads their whole network config) and wait."""
    make_rules_add_once()                        # the reload runs every post-up line again
    upid = run("pvesh", "set", "/cluster/sdn", quiet=True).strip().splitlines()[-1]
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
