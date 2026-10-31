"""The PVE hosts (nodes) of the cluster, and running a command on the node that has a VM.

`qm` acts only on VMs of the node it runs on. pvelab runs on one node and reaches the others with
`ssh root@<node>`, as PVE itself does (after a join, the nodes' root accounts trust each other).
With one node, or no cluster, every command runs here exactly as before."""

import glob
import json
import os
import shlex
import subprocess
import threading

from .config import MEMBERS, NODES_DIR, SSH_CTL
from .util import die, LabError, run, uplink_ip, write_file


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


def guest_conf_files(vmid="*", node="*"):
    """The config files of guest vmid (default: every guest), VMs and containers, in every node directory
    (node: in that one's) - also one left from an old hostname: its VMIDs are taken all the same."""
    return [f for kind in ("qemu-server", "lxc") for f in glob.glob(f"{NODES_DIR}/{node}/{kind}/{vmid}.conf")]


def used_vmids():
    """Every VMID in use in the cluster (VMs and containers), from one look at the config files."""
    return {int(n) for n in (os.path.basename(f)[:-5] for f in guest_conf_files()) if n.isdigit()}


def node_of(vmid):
    """The node VM vmid lives on (from its config file), or None if there is no such VM."""
    found = conf_files(vmid)
    return found[0].split("/")[-3] if found else None


def board_hosts(ids):
    """{vmid: host} for a board's host column, or None with one host."""
    return {i: node_of(i) for i in ids} if multi() else None


def is_local(node):
    return node is None or node == this_node()


def online_nodes():
    """The cluster's hosts that are online now, sorted."""
    return [node for node, m in sorted(members().items()) if m["online"]]


def offline_nodes():
    """The cluster's hosts that are offline now, sorted."""
    return [node for node, m in sorted(members().items()) if not m["online"]]


def is_online(node):
    """Whether node is a host of the cluster that is online now."""
    return bool(members().get(node, {}).get("online"))


def here_first(names, last=False):
    """names sorted with this host first (last=True: last), the others by name."""
    return sorted(names, key=lambda n: (is_local(n) if last else not is_local(n), n))


def ssh_argv(node, shared=True):
    """ssh to root on node, with PVE's own options (PVE::SSHInfo) and one shared connection. shared=False:
    a connection of its own, with timeouts (one kept over a reboot of node would hang)."""
    m = members().get(node)
    if not m or not m["online"] or not m["ip"]:
        die(f"host {node} is offline; its VMs can't be reached")
    try:
        os.makedirs(os.path.dirname(SSH_CTL), exist_ok=True)     # where the shared connection's socket goes
    except OSError:
        pass
    conn = (["-o", f"ControlPath={SSH_CTL}", "-o", "ControlMaster=auto", "-o", "ControlPersist=60"] if shared
            else ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10"])
    return ["ssh", "-e", "none", "-o", "BatchMode=yes", *(known_hosts_opts(node) or ["-o", f"HostKeyAlias={node}"]),
            *conn, "-T", f"root@{m['ip']}"]


def known_hosts_opts(node):
    """ssh options that check node's host key against the cluster's copy of it (PVE keeps it in
    /etc/pve/nodes/NODE/ssh_known_hosts, under the alias NODE), or [] when there is none."""
    known = f"{NODES_DIR}/{node}/ssh_known_hosts"
    return (["-o", f"HostKeyAlias={node}", "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=none"]
            if os.path.isfile(known) else [])


def drop_master(node):
    """Close the shared ssh connection to node (after a reboot it would hang on the old one)."""
    if is_local(node):
        return
    try:
        argv = ssh_argv(node)
        subprocess.run([*argv[:-1], "-O", "exit", argv[-1]], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    except (LabError, OSError, subprocess.TimeoutExpired):     # offline: no connection to close
        pass


def on_node(node, *cmd):
    """The argv that runs cmd on node: cmd itself here, else over ssh. ssh joins its arguments into
    one shell line, so cmd is quoted into one string (sh -c SCRIPT stays one argument)."""
    if is_local(node):
        return list(cmd)
    return [*ssh_argv(node), "--", shlex.join(map(str, cmd))]


def run_on(node, *cmd, **kw):
    """util.run of cmd on node (data= is its stdin, over ssh too)."""
    return run(*on_node(node, *cmd), **kw)


def shell(node, script, timeout=60, show=None, data=None, stderr=True):
    """(exit code, output) of shell line script on node: here, or over the cluster's root ssh. stderr: in
    the output with stdout (False: dropped). Each line goes to the log, or to show(line) (a board's tail,
    which logs it). data: text for its stdin. Killed after timeout s (exit 124); an offline node gives 255."""
    from . import ui                         # here: ui imports this module
    ui.log(f"{node}$ {script[:300]}")
    try:
        argv = on_node(node, "sh", "-c", script)
    except LabError as e:                    # offline, or no address
        ui.log(f"{node}: {e}")
        return 255, str(e)
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT if stderr else subprocess.DEVNULL,
                         text=True, errors="replace")
    killed = []
    timer = threading.Timer(timeout, lambda: (killed.append(1), p.kill()))
    timer.start()
    out = []
    try:
        if data is not None:
            p.stdin.write(data)
            p.stdin.close()
        for line in p.stdout:
            out.append(line)
            if show:
                show(line)
            elif line.strip():
                ui.log(f"{node} | {line.rstrip()}")
        rc = p.wait()
    finally:
        timer.cancel()
        if p.poll() is None:
            p.kill()
            p.wait()
    if killed:
        ui.log(f"{node}: timed out after {timeout}s")
        return 124, "".join(out)
    return rc, "".join(out)


def write_on(node, path, text, mode=0o644):
    """Write text to path on node (its directory made if needed) unless it holds exactly that already;
    True if it was (re)written. On another node: a new file moved over the old one."""
    if is_local(node):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return write_file(path, text, mode)
    if run_on(node, "cat", path, check=False, quiet=True) == text:
        return False
    new, d = shlex.quote(path + ".pvelab-new"), shlex.quote(os.path.dirname(path))
    run_on(node, "sh", "-c", f"mkdir -p {d} && cat > {new} && chmod {mode:o} {new} && mv {new} {shlex.quote(path)}",
           data=text)
    return True


def qm_argv(vmid, *args):
    """`qm args` on the node that has VM vmid: run(*qm_argv(vmid, "start", vmid))."""
    return on_node(node_of(vmid), "qm", *args)
