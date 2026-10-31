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
from .commands.move import cmd_move
from .commands.node import cmd_node_add, cmd_node_list, cmd_node_rename, cmd_node_upgrade
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
from .config import CRED, LAB_DIR, LOG_DIR
from .helptext import USAGE, UsageError, bad_usage, command_help
from . import VERSION
from .util import did_you_mean, LabError, die, elevate, warn
from .templates import ensure_snippet
from .ui import log, start_log
from . import renaming
from .wizard import wizard


# The commands: name -> (function(args), locking). "none" needs no root and no PVE (handled first);
# "read" only looks and runs while another pvelab works; "write" takes the lock and logs to
# LOG_DIR/<name>-<time>.log; "dry" is "write" that also takes --dry-run (then the function gets dry=True,
# only looks, and runs without the lock). A name of two words ("node add") is that subcommand: the
# longest match counts (see command()).
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
    "node": (cmd_node_list, "read"),         # node, node list: the hosts (anything else: its usage)
    "node add": (cmd_node_add, "write"),
    "node rename": (cmd_node_rename, "dry"), # alone: finishes an unfinished rename
    "node upgrade": (cmd_node_upgrade, "dry"),
    "move": (cmd_move, "dry"),
    "upgrade": (lambda args: cmd_upgrade(), "none"),
    "scripts": (cmd_scripts, "none"),        # read-only, from the library
}
ALIASES = {"nodes": "node"}
DRY_RUN = [c for c, (_, locking) in COMMANDS.items() if locking == "dry"]
NO_ROOT = ("version", "completion", "help")
NAMES = [*dict.fromkeys(c.split()[0] for c in COMMANDS), *ALIASES, *NO_ROOT]    # what may come first


def command(cmd, args):
    """The COMMANDS name that cmd and args run: 'node add' before 'node' (aliases resolved)."""
    cmd = ALIASES.get(cmd, cmd)
    return f"{cmd} {args[0]}" if args and f"{cmd} {args[0]}" in COMMANDS else cmd


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
    if cmd not in NAMES and not interactive:
        sys.exit(USAGE if not cmd else f"pvelab: unknown command '{cmd}'"
                 + did_you_mean(cmd, NAMES) + f"\n\n{USAGE}")
    if "-h" in args or "--help" in args:
        print(command_help(cmd))
        return
    name = command(cmd, args)
    run_cmd, locking = COMMANDS.get(name, (None, "write"))
    if locking == "none":
        return run_cmd(args)
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
    lock = None
    renaming_now = name == "node rename"     # it finishes one itself
    if not dry and not renaming_now and renaming.pending():
        lock = take_lock()                       # a reboot stopped `node rename`: finish it first
        try:
            renaming.resume(auto=True)
        except LabError as e:
            if locking != "read":
                raise
            warn(str(e))
    if interactive:
        return wizard()
    if locking == "read" or dry:                 # only looks: fine while another pvelab works
        return run_cmd(args)
    lock = lock or take_lock()
    start_log(LOG_DIR, name.replace(" ", "-"))
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
    except BrokenPipeError:                  # output piped into head & co. (before OSError: a subclass)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)
    except FileNotFoundError as e:           # a command (or file) that isn't there (before OSError)
        sys.exit(f"pvelab: not found: {e.filename} (is its package installed?)")
    except (LabError, OSError, subprocess.CalledProcessError) as e:
        log(f"ERROR: {e}")                   # the command's log says why it stopped, too
        sys.exit(f"pvelab: {e}")
    except KeyboardInterrupt:
        log("ERROR: interrupted (Ctrl-C)")
        sys.exit(130)
