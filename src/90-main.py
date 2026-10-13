# ================================================================ upgrade, sudo, main
def cmd_upgrade():
    """Download pvelab again from UPGRADE_URL and replace this file (sudo only if needed)."""
    me = os.path.realpath(__file__)
    if not os.access(me, os.W_OK) or not os.access(os.path.dirname(me), os.W_OK):
        if os.geteuid() != 0:
            elevate()
        die(f"cannot write {me}")
    print(f">> downloading {UPGRADE_URL}")
    try:
        new = http_open(UPGRADE_URL, timeout=60).read()
    except OSError as e:
        die(f"download failed: {e}")
    if not new.startswith(b"#!/usr/bin/env python3"):
        die(f"the download is not pvelab (an HTML or error page? check that {UPGRADE_URL} is the raw file)")
    try:
        compile(new, "pvelab", "exec")
    except SyntaxError as e:
        die(f"the download is not valid Python ({e}) - not installed")
    old = open(me, "rb").read()
    if new == old:
        print(f">> {me} is already up to date ({hashlib.sha256(old).hexdigest()[:12]})")
        return
    tmp = f"{me}.new"
    with open(tmp, "wb") as f:
        f.write(new)
    os.chmod(tmp, os.stat(me).st_mode & 0o7777)
    os.replace(tmp, me)
    print(f">> {me} updated: {hashlib.sha256(old).hexdigest()[:12]} -> {hashlib.sha256(new).hexdigest()[:12]}")


def elevate():
    """Not root: after asking, re-run this same command through sudo (LAB_* settings kept)."""
    cmd = " ".join(["pvelab", *sys.argv[1:]])
    if not shutil.which("sudo"):
        die(f"needs root: run '{cmd}' as root")
    if not sys.stdin.isatty():
        die(f"needs root: run 'sudo {cmd}'")
    print(f"pvelab: '{cmd}' needs root privileges (VMs, bridges, /etc/pve).", file=sys.stderr)
    if input("Run it with sudo? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        die("aborted")
    env = [f"{k}={v}" for k, v in os.environ.items() if k.startswith("LAB_")]
    os.execvp("sudo", ["sudo", "env", *env, sys.executable, os.path.abspath(__file__), *sys.argv[1:]])


def main():
    sys.stdout.reconfigure(line_buffering=True)      # keep order with qm output under tee
    args = sys.argv[1:]
    cmd = args.pop(0) if args else ""
    commands = {"create": cmd_create, "list": cmd_list, "ls": cmd_list, "templates": cmd_templates,
                "pull": cmd_pull, "destroy": cmd_destroy,
                "expose": cmd_expose, "rollback": cmd_rollback, "snapshot": cmd_snapshot,
                "run": cmd_run, "resize": cmd_resize, "block": cmd_block, "unblock": lambda a: cmd_block(a, on=False)}
    if cmd in ("help", "-h", "--help"):
        print(command_help(args[0]) if args and command_help(args[0]) else __doc__)
        return
    if cmd not in (*commands, "start", "stop", "reboot", "ssh", "upgrade"):
        sys.exit(__doc__ if not cmd else f"pvelab: unknown command '{cmd}'\n\n{__doc__}")
    if "-h" in args or "--help" in args:
        print(command_help(cmd))
        return
    global CMD
    CMD = cmd
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


if __name__ == "__main__":
    try:
        main()
    except LabError as e:
        sys.exit(f"pvelab: {e}")
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"pvelab: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
