"""pvelab start / stop / reboot"""

import time

from ..config import AGENT_WAIT
from ..guest import agent_up
from ..inventory import targets, vm_names, vm_statuses
from ..nodes import node_of, on_node
from ..ui import each
from ..util import LabError, run

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
            run(*on_node(node, "qm", "shutdown", vmid, "--timeout", "60", "--forceStop", "1"))
            return "stopped"
        if action == "start" and st == "running":
            return "already running"
        if action == "reboot" and st == "running":
            board.stage(vmid, "rebooting")
            run(*on_node(node, "qm", "reboot", vmid, "--timeout", "60"))
        else:
            board.stage(vmid, "starting")
            run(*on_node(node, "qm", "start", vmid))
        board.stage(vmid, "booting")
        t0 = time.time()
        while not agent_up(vmid, node=node):
            if time.time() - t0 > AGENT_WAIT:
                raise LabError(f"started, but no guest agent after {AGENT_WAIT // 60} min "
                               "(still booting? see the console in the web UI)")
            time.sleep(3)
        return "running" if action == "start" else "rebooted, running"
    each(f"{TITLES[action]}: {len(ids)} VM(s)", ids, names, one, workers=8,
         failed="{failed} of {total} VM(s) did not " + action)
