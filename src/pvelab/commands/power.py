"""pvelab start / stop / reboot"""

from ..inventory import targets, vm_names, vm_statuses
from ..util import run


def cmd_power(action, args):
    ids = targets(args)
    status, names = vm_statuses(ids), vm_names(ids)
    for vmid in ids:
        name, st = names[vmid], status[vmid]
        if action == "start":
            if st == "running":
                print(f"{name}: already running")
                continue
            run("qm", "start", vmid)
            print(f"{name}: started")
        elif action == "stop":
            if st == "stopped":
                print(f"{name}: already stopped")
                continue
            run("qm", "shutdown", vmid, "--timeout", "60", "--forceStop", "1")
            print(f"{name}: stopped")
        else:
            if st != "running":
                print(f"{name}: not running, starting")
                run("qm", "start", vmid)
                continue
            run("qm", "reboot", vmid, "--timeout", "60")
            print(f"{name}: rebooted")
