"""pvelab start / stop / reboot"""

from ..inventory import targets, vm_names, vm_statuses
from ..lifecycle import boot, shutdown
from ..nodes import node_of
from ..ui import each

TITLES = {"start": "start", "stop": "stop (clean shutdown)", "reboot": "reboot"}


def cmd_power(action, args):
    """Every VM at once, each on its own line; start and reboot wait (up to AGENT_WAIT) until the VM
    has booted far enough for its guest agent to answer."""
    ids = targets(args)
    status, names = vm_statuses(ids), vm_names(ids)

    def one(vmid, board):
        st, node = status[vmid], node_of(vmid)      # its node, looked up once (not on every poll)
        if action == "stop":
            if st == "stopped":
                return "already stopped"
            board.stage(vmid, "shutting down")
            shutdown(vmid, node, 60)
            return "stopped"
        if action == "start" and st == "running":
            return "already running"
        if action == "reboot" and st == "running":
            first, stage = ("reboot", "--timeout", "60"), "rebooting"
        else:
            first, stage = ("start",), "starting"
        boot(vmid, node, board, first, stage, said="started, but ", see="the console in the web UI")
        return "running" if action == "start" else "rebooted, running"
    each(f"{TITLES[action]}: {len(ids)} VM(s)", ids, names, one, workers=8,
         failed="{failed} of {total} VM(s) did not " + action)
