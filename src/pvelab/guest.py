"""Talking to the QEMU guest agent inside VMs (qm guest ...)."""

import json
import subprocess
import time

from .config import AGENT_WAIT
from .nodes import node_of, on_node
from .util import LabError, no_stdin, parallel


def guest_json(*cmd, data=None, timeout=60, node=None):
    """The JSON answer of `qm guest <cmd>`, or None (agent not answering, failed, or no JSON). It runs on
    the VM's node (node: the caller knows it; else looked up): cmd[1] is always the VMID. data (bytes, for
    --pass-stdin) flows through ssh too."""
    try:
        argv = on_node(node or node_of(cmd[1]), "qm", "guest", *map(str, cmd))    # LabError: its node is offline
        r = subprocess.run(argv, input=data, stdin=no_stdin(argv, data), capture_output=True, timeout=timeout)
        return json.loads(r.stdout.decode() or "{}") if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, ValueError, LabError):
        return None


def last_line(text):
    """The last line of text that says something ('' if none): not empty, and not a line of a key's
    randomart box (+---[SHA256]---+, |o.. |) that cloud-init prints."""
    return next((l.strip() for l in reversed((text or "").replace("\r", "\n").split("\n"))
                 if l.strip() and l.strip()[0] not in "+|"), "")


def agent_out(vmid, *cmd, wait=20, timeout=25, node=None):
    """The output of cmd run in VM vmid through the guest agent ('' if it printed nothing), or None (no
    answer). wait: how long the agent lets cmd run; timeout: how long we wait for the agent; node: the
    VM's node, if the caller knows it."""
    r = guest_json("exec", vmid, "--timeout", wait, "--", *cmd, timeout=timeout, node=node)
    return None if r is None else r.get("out-data", "")


def users_in(text):
    """`who` output -> the sorted user names logged in (each once)."""
    return sorted({l.split()[0] for l in (text or "").splitlines() if l.split()})


def who_on(ids):
    """{vmid: [users logged in], or None if its agent did not answer} of the running VMs ids."""
    def who(i):
        out = agent_out(i, "who", wait=10, timeout=30)
        return None if out is None else users_in(out)
    return parallel(who, ids) if ids else {}


def logged_in(ids, names):
    """({name: [users]} of the running VMs ids with someone logged in, [names of those whose agent did not
    answer]); names: {vmid: name}."""
    found = who_on(ids)
    return ({names[i]: u for i, u in found.items() if u}, [names[i] for i, u in found.items() if u is None])


def agent_up(vmid, node=None):
    """Whether VM vmid's guest agent answers; node: the VM's, if the caller knows it."""
    return guest_json("cmd", vmid, "ping", timeout=15, node=node) is not None


def wait_agent(vmid, node, said="", see="its console"):
    """Wait (up to AGENT_WAIT) until VM vmid's guest agent answers - it has booted far enough; else
    LabError: said + 'no guest agent after N min (still booting? see <see>)'."""
    t0 = time.time()
    while not agent_up(vmid, node=node):
        if time.time() - t0 > AGENT_WAIT:
            raise LabError(f"{said}no guest agent after {AGENT_WAIT // 60} min (still booting? see {see})")
        time.sleep(3)
