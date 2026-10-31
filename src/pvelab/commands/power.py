"""pvelab start / stop / reboot"""

import time

from ..config import AGENT_WAIT
from ..guest import agent_up
from ..inventory import targets, vm_names, vm_statuses
from ..nodes import qm_argv
from ..scripts import board_hosts
from ..ui import Board, paint
from ..util import LabError, parallel, run

TITLES = {"start": "start", "stop": "stop (clean shutdown)", "reboot": "reboot"}


def cmd_power(action, args):
    """Every VM at once, each on its own line; start and reboot wait (up to AGENT_WAIT) until the VM
    has booted far enough for its guest agent to answer."""
    ids = targets(args)
    status, names = vm_statuses(ids), vm_names(ids)
    with Board(f"{TITLES[action]}: {len(ids)} VM(s)", names, board_hosts(ids)) as board:
        def one(vmid):
            st = status[vmid]
            try:
                if action == "stop":
                    if st == "stopped":
                        return board.ok(vmid, "already stopped")
                    board.stage(vmid, "shutting down")
                    run(*qm_argv(vmid, "shutdown", vmid, "--timeout", "60", "--forceStop", "1"))
                    return board.ok(vmid, "stopped")
                if action == "start" and st == "running":
                    return board.ok(vmid, "already running")
                if action == "reboot" and st == "running":
                    board.stage(vmid, "rebooting")
                    run(*qm_argv(vmid, "reboot", vmid, "--timeout", "60"))
                else:
                    board.stage(vmid, "starting")
                    run(*qm_argv(vmid, "start", vmid))
                board.stage(vmid, "booting")
                t0 = time.time()
                while not agent_up(vmid):
                    if time.time() - t0 > AGENT_WAIT:
                        return board.fail(vmid, f"started, but no guest agent after {AGENT_WAIT // 60} min "
                                                "(still booting? see the console in the web UI)")
                    time.sleep(3)
                board.ok(vmid, "running" if action == "start" else "rebooted, running")
            except LabError as e:
                board.fail(vmid, str(e))
        parallel(one, ids, workers=8)
    done, failed, total = board.counts()
    if failed:
        print(paint(f"{failed} of {total} VM(s) did not {action}: see their lines above", "red"))
