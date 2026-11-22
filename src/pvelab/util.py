"""Small helpers: errors, running commands, downloads, prompts, sudo."""

import difflib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
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


def listening_ports(proto="tcp"):
    """The ports (proto tcp or udp) where a service of this host listens for what comes in on UPLINK (ss):
    on every address, or on UPLINK's own - not one bound to loopback or a lab bridge only."""
    mine, out = None, set()
    for line in run("ss", {"tcp": "-Hltn", "udp": "-Hlun"}[proto]).splitlines():
        if len(f := line.split()) < 4:
            continue
        addr, _, port = f[3].rpartition(":")
        addr, _, dev = addr.strip("[]").partition("%")          # 0.0.0.0%lo: bound to that device
        if dev and dev != UPLINK or not port.isdigit():
            continue
        if addr not in ("*", "0.0.0.0", "::"):                  # one address: UPLINK's own only
            mine = mine or uplink_ip() or "-"
            if addr != mine:
                continue
        out.add(int(port))
    return out


def ascii_int(v):
    """v as an int when it is ASCII digits (str.isdigit() also takes digits int() can't read, such as ²),
    else None. Its real value, not a capped one, so a range check says "too large"; leading zeros don't
    count. None past the digits int() reads here (sys.get_int_max_str_digits(), 4300 by default)."""
    v = str(v)
    if not re.fullmatch(r"[0-9]+", v):
        return None
    digits, limit = v.lstrip("0") or "0", sys.get_int_max_str_digits()    # 0: no limit
    return int(digits) if not limit or len(digits) <= limit else None


def number(v, lo, hi):
    """v as an int when it is a number lo-hi (ascii_int), else None."""
    n = ascii_int(v)
    return n if n is not None and lo <= n <= hi else None


def net_number(v):
    """'30' or 'net30' -> 30; anything else (a name, net², netfoo) -> None."""
    return ascii_int(str(v).removeprefix("net"))


def die(msg):
    raise LabError(msg)


def did_you_mean(word, choices):
    close = difflib.get_close_matches(word, choices, n=3, cutoff=0.75)
    return f" - did you mean {' or '.join(close)}?" if close else ""


def warn(msg):
    """A warning: printed above a live board when one is on screen (ui.ACTIVE), else on stderr."""
    from . import ui
    ui.log(f"WARNING: {msg}")
    if ui.ACTIVE:
        ui.ACTIVE.say(ui.paint(f"pvelab: WARNING: {msg}", "yellow"))
    else:
        print(f"pvelab: WARNING: {msg}", file=sys.stderr)


LOGFILE = None      # this command's log (ui.start_log sets it): what run and pipe's commands print goes there


ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")
STAMP = re.compile(r"^\d\d:\d\d:\d\d ")     # ui.log() puts the time first


def last_line(path):
    """The last line of a log file that says something ('' if none): without escape codes, the time
    ui.log() puts first, '#' header lines and '== ' step lines."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            lines = [ANSI.sub("", l).strip() for l in f.read().decode(errors="replace").replace("\r", "\n").split("\n")]
    except OSError:
        return ""
    return next((t for t in (STAMP.sub("", l) for l in reversed(lines) if l and not l.startswith("#"))
                 if not t.startswith("== ")), "")


def run(*cmd, check=True, quiet=False, capture=True, data=None, timeout=None, logfile=None):
    """Run a command; return stdout. close_fds (default) keeps the lock out of VM processes.
    data: text for its stdin. Without data, a command over ssh gets no stdin (ssh would otherwise
    swallow what the user types meanwhile). timeout: seconds; then it is killed (check: die, else "").
    logfile (default: LOGFILE, the command's log): its messages (stderr, and stdout unless captured) are
    appended there, not shown; a failure names its last message. Without a log they are shown."""
    logfile = logfile or LOGFILE
    stream = open(logfile, "a") if logfile and not capture else None
    err = subprocess.DEVNULL if quiet else subprocess.PIPE if logfile and capture else stream
    try:
        r = subprocess.run([str(c) for c in cmd], text=True, input=data, stdin=no_stdin(cmd, data),
                           stdout=subprocess.PIPE if capture else stream, stderr=err, timeout=timeout,
                           errors="replace")
    except subprocess.TimeoutExpired:
        if check:
            die(f"command timed out after {timeout}s: {' '.join(map(str, cmd))}")
        return ""
    finally:
        if stream:
            stream.close()
    if logfile and capture and not quiet and r.stderr:
        try:
            with open(logfile, "a") as f:
                f.write(r.stderr if r.stderr.endswith("\n") else r.stderr + "\n")
        except OSError:
            pass
    if check and r.returncode:
        why = "" if not logfile or quiet else (
            next((l.strip() for l in reversed(r.stderr.splitlines()) if l.strip()), "") if capture
            else last_line(logfile))
        die(f"command failed ({r.returncode}): {' '.join(map(str, cmd))}" + (f": {why}" if why else "")
            + (f" (see {logfile})" if logfile else ""))
    return r.stdout or ""


def pipe(first, second, logfile=None):
    """Run `first | second` (two argvs, either may be ssh to another node); die if either fails.
    Their messages go to logfile (default: LOGFILE), or show as they come. On Ctrl-C (or any error) both
    are stopped."""
    logfile = logfile or LOGFILE
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


def wait_for(what, test, limit):
    t0 = time.time()
    while not test():
        if time.time() - t0 > limit:
            die(f"{what}: still not ready after {limit // 60} min")
        time.sleep(5)


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


def replace_file(path, text, mode=None):
    """Write text to path whole: into path.new (chmod mode, if given), then moved over path, so a reader
    never sees half of it."""
    new = path + ".new"
    with open(new, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(new, mode)
    os.replace(new, path)


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
