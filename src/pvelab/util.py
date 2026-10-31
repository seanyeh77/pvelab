"""Small helpers: errors, running commands, downloads, prompts, sudo."""

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import UPLINK, YES


class LabError(Exception):
    pass


def uplink_ip():
    """This host's IPv4 address on UPLINK, or None."""
    for f in run("ip", "-4", "-o", "addr", "show", "dev", UPLINK, check=False, quiet=True).split():
        if "/" in f and f[0].isdigit():
            return f.split("/")[0]
    return None


def die(msg):
    raise LabError(msg)


def warn(msg):
    """A warning: printed above a live board when one is on screen (ui.ACTIVE), else on stderr."""
    from . import ui
    ui.log(f"WARNING: {msg}")
    if ui.ACTIVE:
        ui.ACTIVE.say(ui.paint(f"pvelab: WARNING: {msg}", "yellow"))
    else:
        print(f"pvelab: WARNING: {msg}", file=sys.stderr)


def run(*cmd, check=True, quiet=False, capture=True, data=None):
    """Run a command; return stdout. close_fds (default) keeps the lock out of VM processes.
    data: text for its stdin. Without data, a command over ssh gets no stdin (ssh would otherwise
    swallow what the user types meanwhile)."""
    r = subprocess.run([str(c) for c in cmd], text=True, input=data, stdin=no_stdin(cmd, data),
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.DEVNULL if quiet else None)
    if check and r.returncode:
        die(f"command failed ({r.returncode}): {' '.join(map(str, cmd))}")
    return r.stdout or ""


def pipe(first, second, logfile=None):
    """Run `first | second` (two argvs, either may be ssh to another node); die if either fails.
    Their messages show as they come, or go to logfile. On Ctrl-C (or any error) both are stopped."""
    out = open(logfile, "a") if logfile else None
    a = subprocess.Popen([str(c) for c in first], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=out)
    b = None
    try:
        b = subprocess.Popen([str(c) for c in second], stdin=a.stdout, stdout=out, stderr=out)
        a.stdout.close()                     # only second reads it now: first stops if second does
        rb, ra = b.wait(), a.wait()
    except BaseException:
        for p in (a, b):
            if p and p.poll() is None:
                p.kill()
                p.wait()
        raise
    finally:
        if out:
            out.close()
    if ra or rb:
        die(f"command failed ({ra or rb}): {' '.join(map(str, first))} | {' '.join(map(str, second))}")


def no_stdin(cmd, data):
    """subprocess's stdin for cmd: /dev/null for ssh without data, else as usual (None)."""
    return subprocess.DEVNULL if data is None and cmd and cmd[0] == "ssh" else None


def ok(*cmd):
    return subprocess.run([str(c) for c in cmd], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def confirm_yes(msg, typed=False):
    """Print msg, then go on only after a typed yes. LAB_YES=1 answers it, unless typed: then it must
    be typed on a real terminal, even with LAB_YES=1 (changes to the cluster itself)."""
    print(msg)
    if YES and not typed:
        print("(LAB_YES=1, not asking)")
        return
    if not sys.stdin.isatty():
        die("this needs a typed yes on a terminal (LAB_YES=1 does not count here); run it interactively" if typed
            else "confirmation needed; run interactively or set LAB_YES=1")
    if input("Type 'yes' to continue: ").strip() != "yes":
        die("aborted")


def write_file(path, text, mode=0o644):
    """Write text to path unless it already holds exactly that; True if it was (re)written."""
    try:
        if open(path).read() == text:
            return False
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return True


def http_open(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pvelab"}), timeout=timeout)


def file_hash(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
    os.execvp("sudo", ["sudo", *pvelab_argv(*sys.argv[1:], env=env)])


def pvelab_argv(*args, env=()):
    """The command line that runs this same pvelab again (single file or checkout) with args;
    env: extra NAME=value settings (sudo drops the caller's environment)."""
    return ["env", *env, f"PYTHONPATH={package_root()}", sys.executable, "-m", "pvelab", *args]


def package_root():
    """Where the pvelab package is imported from: the single-file pvelab (a zip) or a directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def concurrently(*fns):
    """[fn() for fn in fns], all run at the same time (independent slow read-only calls)."""
    with ThreadPoolExecutor(len(fns)) as pool:
        return [f.result() for f in [pool.submit(fn) for fn in fns]]


def parallel(fn, items, workers=8):
    """{item: fn(item)}, several at a time - for read-only per-VM queries (each one is a slow qm/pvesh)."""
    items = list(items)
    with ThreadPoolExecutor(workers) as pool:
        return dict(zip(items, pool.map(fn, items)))
