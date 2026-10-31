"""Talking to the QEMU guest agent inside VMs (qm guest ...)."""

import json
import subprocess

from .nodes import node_of, on_node
from .util import LabError, no_stdin


def guest_json(*cmd, data=None, timeout=60):
    """The JSON answer of `qm guest <cmd>`, or None (agent not answering, failed, or no JSON). It runs on
    the VM's node: cmd[1] is always the VMID. data (bytes, for --pass-stdin) flows through ssh too."""
    try:
        argv = on_node(node_of(cmd[1]), "qm", "guest", *map(str, cmd))      # LabError: its node is offline
        r = subprocess.run(argv, input=data, stdin=no_stdin(argv, data), capture_output=True, timeout=timeout)
        return json.loads(r.stdout.decode() or "{}") if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, ValueError, LabError):
        return None


def agent_up(vmid):
    return guest_json("cmd", vmid, "ping", timeout=15) is not None
