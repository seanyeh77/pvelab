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
from .commands.node import cmd_node
from .commands.power import cmd_power
from .commands.resize import cmd_resize
from .commands.run import cmd_run
from .commands.scripts import cmd_scripts
from .commands.snapshots import cmd_rollback, cmd_snapshot
from .commands.ssh import cmd_ssh
from .commands.status import cmd_status
from .commands.templates import cmd_delete_template, cmd_pull, cmd_save_template, cmd_templates
from .commands.top import cmd_top
from .commands.upgrade import cmd_upgrade, cmd_version, copies_warnings
from .config import CRED, LAB_DIR
from .helptext import USAGE, UsageError, bad_usage, command_help
from .inventory import did_you_mean
from . import VERSION
from .util import LabError, die, elevate, warn
from .templates import ensure_snippet
from .ui import start_log
from . import renaming
from .wizard import wizard


# Commands that need root on a PVE host: name -> (function(args), locking). "read" only looks and
# runs while another pvelab works; "dry" takes --dry-run (the function gets dry=True and only looks);
# "write" takes the lock. The NO_ROOT commands (below) are handled before these.
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
    "status": (cmd_status, "read"),
    "handout": (cmd_handout, "read"),
    "templates": (cmd_templates, "read"),
    "pull": (cmd_pull, "write"),
    "save-template": (cmd_save_template, "write"),
    "delete-template": (cmd_delete_template, "write"),
    "node": (cmd_node, "write"),             # node list only looks: see main()
    "nodes": (cmd_node, "write"),
}
DRY_RUN = [c for c, (_, locking) in COMMANDS.items() if locking == "dry"] + ["node rename"]
NO_ROOT = ("version", "completion", "help", "upgrade", "scripts")


SBIN = ("/usr/local/sbin", "/usr/sbin", "/sbin")    # qm, ifup, iptables, ... (not in every user's / sudo's PATH)


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)      # keep order with qm output under tee
    path = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(path + [d for d in SBIN if d not in path])
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
    if cmd not in (*COMMANDS, *NO_ROOT) and not interactive:
        sys.exit(USAGE if not cmd else f"pvelab: unknown command '{cmd}'"
                 + did_you_mean(cmd, [*COMMANDS, *NO_ROOT]) + f"\n\n{USAGE}")
    if "-h" in args or "--help" in args:
        print(command_help(cmd))
        return
    if cmd == "upgrade":
        return cmd_upgrade()
    if cmd == "scripts":                         # read-only, from the library: no root, no lock
        return cmd_scripts(args)
    run_cmd, locking = COMMANDS.get(cmd, (None, "write"))
    if cmd in ("node", "nodes"):                 # node list: no lock; node upgrade and rename take --dry-run
        locking = {"add": "write", "upgrade": "dry", "rename": "dry"}.get(args[0] if args else "", "read")
    dry = "--dry-run" in args
    if dry and locking != "dry":
        bad_usage(f"--dry-run works with {', '.join(DRY_RUN)}, node upgrade")
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
    lock = None
    renaming_now = cmd in ("node", "nodes") and args[:1] == ["rename"]      # it finishes one itself
    if not dry and not renaming_now and renaming.pending():
        lock = take_lock()                       # a reboot stopped `node rename`: finish it first
        try:
            renaming.resume()
        except LabError as e:
            if locking != "read":
                raise
            warn(str(e))
    if interactive:
        return wizard()
    if locking == "read" or dry:                 # only looks: fine while another pvelab works
        return run_cmd(args)
    lock = lock or take_lock()
    if cmd not in ("node", "nodes"):             # node add, upgrade and rename keep their own log
        start_log(f"{LAB_DIR}/logs", cmd)
    ensure_snippet()                             # hand clones of a template get the current vendor-data
    run_cmd(args)


def take_lock():
    """The lock file, locked (kept open while this pvelab runs); stop if another pvelab has it."""
    lock = open(f"{LAB_DIR}/.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die("another pvelab is running (a create, run, ... not finished yet) - try again when it is done")
    return lock


def pve_check():
    """Stop with what is missing unless this is a working PVE node (all of it readable without root)."""
    if not shutil.which("qm"):
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
    except UsageError as e:                  # what is wrong, then that command's help
        cmd = sys.argv[1] if len(sys.argv) > 1 else ""
        sys.exit(f"pvelab: {e}\n\n{command_help(cmd) or ''}".rstrip())
    except LabError as e:
        sys.exit(f"pvelab: {e}")
    except BrokenPipeError:                  # output piped into head & co. (before OSError: a subclass)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)
    except FileNotFoundError as e:           # a command (or file) that isn't there (before OSError)
        sys.exit(f"pvelab: not found: {e.filename} (is its package installed?)")
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"pvelab: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
