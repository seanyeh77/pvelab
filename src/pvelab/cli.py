"""Entry point: argument dispatch, root / PVE checks, the lock."""

import fcntl
import os
import shutil
import subprocess
import sys
from functools import partial

from .commands.access import cmd_block, cmd_expose, cmd_isolate
from .commands.complete import cmd_complete, cmd_completion
from .commands.create import cmd_create
from .commands.destroy import cmd_destroy
from .commands.handout import cmd_handout
from .commands.listing import cmd_list
from .commands.power import cmd_power
from .commands.resize import cmd_resize
from .commands.run import cmd_run
from .commands.snapshots import cmd_rollback, cmd_snapshot
from .commands.ssh import cmd_ssh
from .commands.templates import cmd_pull, cmd_templates
from .commands.top import cmd_top
from .commands.upgrade import cmd_upgrade, cmd_version, copies_warnings
from .config import CRED, LAB_DIR
from . import helptext
from .helptext import USAGE, bad_usage, command_help
from .inventory import did_you_mean
from . import VERSION
from .util import LabError, die, elevate, warn
from .wizard import wizard


# Commands that need root on a PVE host: name -> (function(args), locking). "read" only looks and
# runs while another pvelab works; "dry" takes --dry-run (the function gets dry=True and only looks);
# "write" takes the lock. version / completion / help / upgrade are handled before these.
COMMANDS = {
    "create": (cmd_create, "write"),
    "list": (cmd_list, "read"),
    "ls": (cmd_list, "read"),
    "start": (partial(cmd_power, "start"), "write"),
    "stop": (partial(cmd_power, "stop"), "write"),
    "reboot": (partial(cmd_power, "reboot"), "write"),
    "destroy": (cmd_destroy, "dry"),
    "resize": (cmd_resize, "dry"),
    "ssh": (cmd_ssh, "read"),
    "rollback": (cmd_rollback, "dry"),
    "snapshot": (cmd_snapshot, "write"),
    "run": (cmd_run, "write"),
    "expose": (cmd_expose, "write"),
    "isolate": (cmd_isolate, "write"),
    "block": (cmd_block, "write"),
    "unblock": (partial(cmd_block, on=False), "write"),
    "top": (cmd_top, "read"),
    "handout": (cmd_handout, "read"),
    "templates": (cmd_templates, "read"),
    "pull": (cmd_pull, "write"),
}
DRY_RUN = [c for c, (_, locking) in COMMANDS.items() if locking == "dry"]
NO_ROOT = ("version", "completion", "help", "upgrade")


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)      # keep order with qm output under tee
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args.pop(0) if args else ""
    if cmd == "__complete":                     # bash tab completion: quick, never asks anything
        return cmd_complete(args)
    if cmd == "completion":
        return cmd_completion()
    if cmd in ("version", "--version", "-V"):
        return cmd_version()
    if cmd in ("help", "-h", "--help"):
        print((args and command_help(args[0])) or USAGE)
        return
    interactive = not cmd and sys.stdin.isatty() and sys.stdout.isatty()
    if cmd not in (*COMMANDS, "upgrade") and not interactive:
        sys.exit(USAGE if not cmd else f"pvelab: unknown command '{cmd}'"
                 + did_you_mean(cmd, [*COMMANDS, *NO_ROOT]) + f"\n\n{USAGE}")
    if "-h" in args or "--help" in args:
        print(command_help(cmd))
        return
    helptext.CMD = cmd
    if cmd == "upgrade":
        return cmd_upgrade()
    run_cmd, locking = COMMANDS.get(cmd, (None, "write"))
    dry = "--dry-run" in args
    if dry and locking != "dry":
        bad_usage(f"--dry-run works with {', '.join(DRY_RUN)}")
    if dry:
        args = [a for a in args if a != "--dry-run"]
        run_cmd = partial(run_cmd, dry=True)
    pve_check()                                  # before sudo: no point asking on another machine
    if os.geteuid() != 0:
        elevate()
    if sys.stderr.isatty() and not VERSION.startswith("dev") and not os.environ.get("PVELAB_WIZARD_STEP"):
        for line in copies_warnings():
            warn(line)
    os.umask(0o077)
    os.makedirs(LAB_DIR, exist_ok=True)
    os.chmod(LAB_DIR, 0o700)
    if not os.path.exists(CRED):
        open(CRED, "w").close()
    os.chmod(CRED, 0o600)
    if interactive:
        return wizard()
    if locking == "read" or dry:                 # only looks: fine while another pvelab works
        return run_cmd(args)
    lock = open(f"{LAB_DIR}/.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die("another pvelab is running (a create, run, ... not finished yet) - try again when it is done")
    run_cmd(args)


def pve_check():
    """Stop with what is missing unless this is a working PVE node (all of it readable without root)."""
    if not shutil.which("qm", path=os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])):
        die("not a Proxmox VE host: there is no 'qm' command (package qemu-server)")
    if not os.path.isdir("/etc/pve/local"):       # pmxcfs not mounted, or no node dir for this hostname
        host = os.uname().nodename
        die(f"/etc/pve is not usable for node '{host}' (pve-cluster not running?).\n"
            f"  check: systemctl status pve-cluster; getent hosts {host}\n"
            f"  the hostname must resolve to this host's IP in /etc/hosts; after renaming a node,\n"
            f"  its VMs stay under /etc/pve/nodes/<old name>/")


def entry():
    """Console entry point: main() with errors as one line instead of a traceback."""
    try:
        main()
    except LabError as e:
        sys.exit(f"pvelab: {e}")
    except BrokenPipeError:                  # output piped into head & co. (before OSError: a subclass)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"pvelab: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
