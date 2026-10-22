"""Small helpers: errors, running commands, downloads, prompts, sudo."""

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import YES


class LabError(Exception):
    pass


def die(msg):
    raise LabError(msg)


def warn(msg):
    print(f"pvelab: WARNING: {msg}", file=sys.stderr)


def run(*cmd, check=True, quiet=False, capture=True):
    """Run a command; return stdout. close_fds (default) keeps the lock out of VM processes."""
    r = subprocess.run([str(c) for c in cmd], text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.DEVNULL if quiet else None)
    if check and r.returncode:
        die(f"command failed ({r.returncode}): {' '.join(map(str, cmd))}")
    return r.stdout or ""


def ok(*cmd):
    return subprocess.run([str(c) for c in cmd], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def confirm_yes(msg):
    print(msg)
    if YES:
        print("(LAB_YES=1, not asking)")
        return
    if not sys.stdin.isatty():
        die("confirmation needed; run interactively or set LAB_YES=1")
    if input("Type 'yes' to continue: ").strip() != "yes":
        die("aborted")


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


def parallel(fn, items, workers=8):
    """{item: fn(item)}, several at a time - for read-only per-VM queries (each one is a slow qm/pvesh)."""
    items = list(items)
    with ThreadPoolExecutor(workers) as pool:
        return dict(zip(items, pool.map(fn, items)))
