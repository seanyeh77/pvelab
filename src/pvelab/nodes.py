"""The PVE hosts (nodes) of the cluster, and running a command on the node that has a VM.

`qm` acts only on VMs of the node it runs on. pvelab runs on one node and reaches the others with
`ssh root@<node>`, as PVE itself does (after a join, the nodes' root accounts trust each other).
With one node, or no cluster, every command runs here exactly as before."""

import glob
import json
import os
import shlex

from .config import MEMBERS, NODES_DIR, SSH_CTL
from .util import die, run, uplink_ip


def this_node():
    """This node's name, as PVE has it: the hostname without its domain part."""
    return os.uname().nodename.split(".")[0]


def parse_members(text):
    """/etc/pve/.members (JSON) -> {name: {"ip": str or None, "online": bool}}, or {} (no cluster, or
    unreadable). pmxcfs leaves out "ip" for a node whose address it does not know yet (offline, just joined)."""
    try:
        nodes = json.loads(text)["nodelist"]
        return {n: {"ip": v.get("ip"), "online": bool(v.get("online"))} for n, v in nodes.items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def members():
    """{name: {"ip": str, "online": bool}} of the cluster's nodes. No cluster (or .members can't be
    read): just this node."""
    try:
        with open(MEMBERS) as f:
            out = parse_members(f.read())
        if out:
            return out
    except OSError:
        pass
    return {this_node(): {"ip": uplink_ip(), "online": True}}


def node_names():
    return sorted(members())


def multi():
    """More than one node: the host column and questions show only then."""
    return len(members()) > 1


def conf_files(vmid="*"):
    """The config files of VM vmid (default: every VM) on the cluster's nodes. Only nodes in members(): a
    directory left from an old hostname is not a node (one host: only its own VMs, as before)."""
    return sorted(f for n in members() for f in glob.glob(f"{NODES_DIR}/{n}/qemu-server/{vmid}.conf"))


def node_of(vmid):
    """The node VM vmid lives on (from its config file), or None if there is no such VM."""
    found = conf_files(vmid)
    return found[0].split("/")[-3] if found else None


def is_local(node):
    return node is None or node == this_node()


def ssh_argv(node):
    """ssh to root on node, with PVE's own options (PVE::SSHInfo) and one shared connection."""
    m = members().get(node)
    if not m or not m["online"] or not m["ip"]:
        die(f"host {node} is offline; its VMs can't be reached")
    known = f"{NODES_DIR}/{node}/ssh_known_hosts"
    try:
        os.makedirs(os.path.dirname(SSH_CTL), exist_ok=True)     # where the shared connection's socket goes
    except OSError:
        pass
    return ["ssh", "-e", "none", "-o", "BatchMode=yes", "-o", f"HostKeyAlias={node}",
            *(["-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=none"]
              if os.path.isfile(known) else []),
            "-o", f"ControlPath={SSH_CTL}", "-o", "ControlMaster=auto", "-o", "ControlPersist=60",
            "-T", f"root@{m['ip']}"]


def on_node(node, *cmd):
    """The argv that runs cmd on node: cmd itself here, else over ssh. ssh joins its arguments into
    one shell line, so cmd is quoted into one string (sh -c SCRIPT stays one argument)."""
    if is_local(node):
        return list(cmd)
    return [*ssh_argv(node), "--", shlex.join(map(str, cmd))]


def run_on(node, *cmd, **kw):
    """util.run of cmd on node (data= is its stdin, over ssh too)."""
    return run(*on_node(node, *cmd), **kw)


def qm_argv(vmid, *args):
    """`qm args` on the node that has VM vmid: run(*qm_argv(vmid, "start", vmid))."""
    return on_node(node_of(vmid), "qm", *args)
