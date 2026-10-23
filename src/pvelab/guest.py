"""Talking to the QEMU guest agent inside VMs (qm guest ...)."""

import json
import subprocess


def agent_up(vmid):
    try:
        return subprocess.run(["qm", "guest", "cmd", str(vmid), "ping"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def guest_json(*cmd, data=None, timeout=60):
    """The JSON answer of `qm guest <cmd>`, or None (agent not answering, failed, or no JSON)."""
    try:
        r = subprocess.run(["qm", "guest", *map(str, cmd)], input=data, capture_output=True, timeout=timeout)
        return json.loads(r.stdout.decode() or "{}") if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, ValueError):
        return None
