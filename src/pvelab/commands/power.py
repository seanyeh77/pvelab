"""pvelab start / stop / reboot"""

from ..downs import RESUMABLE, reasons
from ..inventory import targets, vm_names, vm_statuses
from ..lifecycle import boot, shutdown
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
            shutdown(vmid, node, 60)
            return "stopped"
        if action == "start" and st == "running":
            if (q := qmp_status(vmid, node)) not in RESUMABLE:
                return "already running"
            board.stage(vmid, f"resuming ({q})")         # paused: a failed disk write, or suspended
            run(*on_node(node, "qm", "resume", vmid), check=False, quiet=True)
            if (q := qmp_status(vmid, node)) in RESUMABLE:
                raise LabError(f"still {q}: " + reasons({vmid: (q, node)})[vmid][0])
            return "resumed, running"
        if action == "reboot" and st == "running":
            first, stage = ("reboot", "--timeout", "60"), "rebooting"
        else:
            first, stage = ("start",), "starting"
        boot(vmid, node, board, first, stage, said="started, but ", see="the console in the web UI")
        return "running" if action == "start" else "rebooted, running"
    each(f"{TITLES[action]}: {len(ids)} VM(s)", ids, names, one, workers=8,
         failed="{failed} of {total} VM(s) did not " + action)


def qmp_status(vmid, node):
    """QEMU's own state of a running VM (running, paused, io-error, ...): qm list says only running."""
    out = run(*on_node(node, "qm", "status", vmid, "--verbose"), check=False, quiet=True)
    return next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("qmpstatus:")), "running")
