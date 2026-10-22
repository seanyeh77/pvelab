"""Entry point: argument dispatch, root / PVE checks, the lock."""

import fcntl
import os
import subprocess
import sys

from .commands.access import cmd_block, cmd_expose
from .commands.create import cmd_create
from .commands.destroy import cmd_destroy
from .commands.listing import cmd_list
from .commands.power import cmd_power
from .commands.resize import cmd_resize
from .commands.run import cmd_run
from .commands.snapshots import cmd_rollback, cmd_snapshot
from .commands.ssh import cmd_ssh
from .commands.templates import cmd_pull, cmd_templates
from .commands.upgrade import cmd_upgrade
from .config import CRED, LAB_DIR
from . import helptext
from .helptext import USAGE, command_help
from .util import LabError, die, elevate, ok


def main():
    sys.stdout.reconfigure(line_buffering=True)      # keep order with qm output under tee
    args = sys.argv[1:]
    cmd = args.pop(0) if args else ""
    commands = {"create": cmd_create, "list": cmd_list, "ls": cmd_list, "templates": cmd_templates,
                "pull": cmd_pull, "destroy": cmd_destroy,
                "expose": cmd_expose, "rollback": cmd_rollback, "snapshot": cmd_snapshot,
                "run": cmd_run, "resize": cmd_resize, "block": cmd_block, "unblock": lambda a: cmd_block(a, on=False)}
    if cmd in ("help", "-h", "--help"):
        print(command_help(args[0]) if args and command_help(args[0]) else USAGE)
        return
    if cmd not in (*commands, "start", "stop", "reboot", "ssh", "upgrade"):
        sys.exit(USAGE if not cmd else f"pvelab: unknown command '{cmd}'\n\n{USAGE}")
    if "-h" in args or "--help" in args:
        print(command_help(cmd))
        return
    helptext.CMD = cmd
    if cmd == "upgrade":
        return cmd_upgrade()
    if os.geteuid() != 0:
        elevate()
    if not os.path.isdir("/etc/pve") or not ok("which", "qm"):
        die("not a Proxmox VE host")
    if not os.path.isdir("/etc/pve/local"):       # pmxcfs not mounted, or no node dir for this hostname
        host = os.uname().nodename
        die(f"/etc/pve is not usable for node '{host}' (pve-cluster not running?).\n"
            f"  check: systemctl status pve-cluster; getent hosts {host}\n"
            f"  the hostname must resolve to this host's IP in /etc/hosts; after renaming a node,\n"
            f"  its VMs stay under /etc/pve/nodes/<old name>/")
    os.umask(0o077)
    os.makedirs(LAB_DIR, exist_ok=True)
    os.chmod(LAB_DIR, 0o700)
    if not os.path.exists(CRED):
        open(CRED, "w").close()
    os.chmod(CRED, 0o600)
    lock = open(f"{LAB_DIR}/.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die("another lab.sh/pvelab is running")
    if cmd in ("start", "stop", "reboot"):
        cmd_power(cmd, args)
    elif cmd == "ssh":
        cmd_ssh(args, lock)
    else:
        commands[cmd](args)


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
