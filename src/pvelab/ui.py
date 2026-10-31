"""Terminal output: colors and a progress line for long operations."""

import os
import re
import select as select_
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty

from .nodes import board_hosts
from .config import FORCE, YES
from .util import die, LabError, parallel

CODES = {"green": "32", "red": "31", "yellow": "33", "dim": "2", "bold": "1", "pick": "1;36"}


SCREEN = None         # the real stdout while a Spinner or Board sends other output to the log


def color_on():
    return (SCREEN or sys.stdout).isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


class ToLog:
    """sys.stdout while a spinner or board is on screen: what the work prints goes to the log, so it
    can't break the lines being redrawn (the newest of it shows, dim, at the end of a spinner line)."""

    def __init__(self):
        self.part = ""

    def write(self, text):
        self.part += text
        *lines, self.part = self.part.split("\n")
        for line in lines:
            if line.strip():
                log(line)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


def to_log():
    """Send sys.stdout to the log; return the real one (to draw on). release() undoes it."""
    global SCREEN
    if SCREEN is None:
        SCREEN = sys.stdout
        sys.stdout = ToLog()
        return SCREEN, True
    return SCREEN, False                     # already sent (a spinner inside a board)


def release(mine):
    global SCREEN
    if mine and SCREEN is not None:
        part = sys.stdout.part if isinstance(sys.stdout, ToLog) else ""
        sys.stdout, SCREEN = SCREEN, None
        if part.strip():
            log(part)


def paint(text, color, width=0):
    """text padded to width, then colored (padding first, so columns line up)."""
    text = f"{text!s:<{width}}" if width else str(text)
    return f"\033[{CODES[color]}m{text}\033[0m" if color and color_on() else text


KEYS = {b"\x1b[A": -1, b"\x1bOA": -1, b"k": -1, b"\x1b[B": 1, b"\x1bOB": 1, b"j": 1}


def key(fd):
    """One key press: a byte, or a whole escape sequence (arrow keys); a lone Esc is b"\\x1b"."""
    k = os.read(fd, 1)
    while k[:1] == b"\x1b" and select_.select([fd], [], [], 0.05)[0]:
        k += os.read(fd, 1)
        if len(k) > 2 and (k[-1:].isalpha() or k[-1:] == b"~"):
            break
    return k


def select(prompt, labels, default=0, esc="back"):
    """Pick one of labels with Up/Down (or k/j) and Enter: its index, or None for back (Esc, b, q,
    Ctrl-C, Ctrl-D; esc names it). The list is replaced by 'prompt: choice' when done."""
    fd, i, n = sys.stdin.fileno(), default, len(labels)
    old = termios.tcgetattr(fd)

    def draw():
        for k, label in enumerate(labels):
            sys.stdout.write("\r\033[K" + (paint(f"> {label}", "pick") if k == i else f"  {label}") + "\n")

    print(f"{prompt} " + paint(f"(Up/Down, Enter; Esc: {esc})", "dim"))
    sys.stdout.write("\033[?25l")                 # no blinking cursor under the list
    try:
        tty.setcbreak(fd)
        draw()
        while True:
            k = key(fd)
            if k in (b"\r", b"\n"):
                break
            if k in (b"\x1b", b"b", b"q", b"\x04"):
                i = None
                break
            i = (i + KEYS.get(k, 0)) % n
            sys.stdout.write(f"\033[{n}A")
            draw()
    except KeyboardInterrupt:
        i = None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(f"\033[{n + 1}A\r\033[J\033[?25h")
    print(f"{prompt} " + (paint(labels[i], "bold") if i is not None else paint(f"({esc})", "dim")))
    return i


def some(names, n=3, sep=", "):
    """The first n names, then '+K' for the rest: 'a, b, c +4'."""
    names = list(names)
    return sep.join(names[:n]) + (f" +{len(names) - n}" if len(names) > n else "")


def elapsed(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m}:{s:02d}"


ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")
UTF8 = "UTF-8" in (os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG") or "").upper() \
    or (sys.stdout.encoding or "").lower().replace("-", "") == "utf8"
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if UTF8 else "|/-\\"
OK_MARK, BAD_MARK = ("✔", "✘") if UTF8 else ("ok", "XX")


def last_line(path):
    """The last non-empty line of a log file, without escape codes ('' if none)."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            lines = [ANSI.sub("", l).strip() for l in f.read().decode(errors="replace").replace("\r", "\n").split("\n")]
    except OSError:
        return ""
    return next((t for t in (STAMP.sub("", l) for l in reversed(lines) if l and not l.startswith("#"))
                 if not t.startswith("== ")), "")


STAMP = re.compile(r"^\d\d:\d\d:\d\d ")     # log() puts the time first


def fit(text, width):
    """text cut to width visible characters (escape codes don't count; a cut text loses its colors)."""
    plain = ANSI.sub("", text)
    return text if len(plain) <= width else plain[:max(0, width - 1)] + "…"


class Live:
    """What Spinner and Board share: while one is on screen, sys.stdout goes to the log (to_log), the
    cursor is hidden and a thread redraws (self._loop) until it is stopped."""

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self._stop, self._thread, self.out, self.mine = threading.Event(), None, sys.stdout, False

    def _on(self):
        self.out, self.mine = to_log()
        self.out.write("\033[?25l")
        self._first()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _off(self):
        if self._thread:
            self._stop.set()
            self._thread.join()
            self._thread = None
            self._last()
            self.out.write("\033[?25h")
            self.out.flush()
        release(self.mine)
        self.mine = False

    def _first(self):
        """Drawn before the thread starts (nothing by default)."""

    def _last(self):
        """Drawn once the thread has stopped."""


class Spinner(Live):
    """`with Spinner(label) as s:` - one line, redrawn every 0.2 s on a terminal: a turning frame, the
    label, the seconds, and (dim) the last line of this command's log (LOG), so you see what is going on without the
    full output. At the end it becomes '  ✔ label' (s.ok(text) sets the text), or '  ✘ label: why' when
    s.fail(why) was called or the block raised. Without a terminal (pipes, logs): plain lines only.
    The width is measured again while drawing, so a narrowed window does not fill with copies."""

    def __init__(self, label):
        super().__init__()
        self.label, self.t0 = label, time.time()
        self.result, self.problem = None, None

    def ok(self, text=None):
        self.result = text

    def fail(self, why):
        self.problem = why

    def _loop(self):
        i, cols = 0, 80
        while not self._stop.wait(0.2 if i else 0):
            if i % 5 == 0:
                cols = shutil.get_terminal_size((80, 24)).columns
            head = f"  {FRAMES[i % len(FRAMES)]} {self.label} {int(time.time() - self.t0)}s"
            tail = last_line(LOG) if LOG else ""
            room = cols - len(head) - 3
            line = head + (paint("  " + fit(tail, room), "dim") if tail and room > 8 else "")
            self.out.write("\r\033[K" + fit(line, cols - 1))
            self.out.flush()
            i += 1

    def _last(self):
        self.out.write("\r\033[K")         # the line is cleared

    def __enter__(self):
        if self.tty:
            self._on()
        return self

    def __exit__(self, kind, exc, tb):
        self._off()
        why = self.problem or (("interrupted" if kind is KeyboardInterrupt else str(exc) or kind.__name__) if kind else None)
        secs = paint(f"({int(time.time() - self.t0)}s)", "dim") if time.time() - self.t0 >= 2 else ""
        out = SCREEN or sys.stdout           # inside a board: still above it
        if why:
            log(f"FAILED {self.label}: {why}")
            text = f"  {paint(BAD_MARK, 'red')} {self.label}: {why} {secs}".rstrip()
        else:
            text = f"  {paint(OK_MARK, 'green')} {self.result or self.label} {secs}".rstrip()
        if ACTIVE:
            ACTIVE.say(text)
        else:
            out.write(text + "\n")
            out.flush()
        return False


class Ok(str):
    """What a passed check returns when it has something to show: '✔ <this text>' instead of its label."""


def check_line(label, fn, *a):
    """Run check fn(*a) on a spinner line that ends as ✔ (with the Ok text it returned, if any) or ✘ (the
    problem it returned, or what it raised); the problem, or None when it passed."""
    with Spinner(label) as line:
        try:
            result = fn(*a)
        except (LabError, OSError) as e:
            result = f"the check itself failed: {e}"
        if result is None or isinstance(result, Ok):
            line.ok(result)
        else:
            line.fail(result)
    return line.problem


LOG = None            # this command's log file (start_log sets it for commands that change things)
ACTIVE = None         # the Board on screen now: warn() prints above it


def start_log(directory, name):
    """Log this command to <directory>/<name>-<time>.log; the path, or None if it can't be made."""
    global LOG
    try:
        os.makedirs(directory, exist_ok=True)
        LOG = f"{directory}/{name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        with open(LOG, "a") as f:
            f.write(f"# pvelab {' '.join(sys.argv[1:])}\n")
    except OSError:
        LOG = None
    return LOG


def log(text):
    """Append a line to this command's log (nothing when there is none, e.g. in tests)."""
    if LOG:
        try:
            with open(LOG, "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {text.rstrip()}\n")
        except OSError:
            pass


def logged(*cmd):
    """Run cmd here with its output going to the log only (the spinner shows its last line)."""
    with open(LOG or os.devnull, "a") as f:
        r = subprocess.run([str(c) for c in cmd], stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
    if r.returncode:
        die(f"command failed ({r.returncode}): {' '.join(map(str, cmd))}" + (f" - see {LOG}" if LOG else ""))


def mmss(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


STUCK = 120           # a live stage with no new output for this long is marked (yellow)


class Board(Live):
    """`with Board(title, {key: name}, hosts) as b:` - one line per VM, redrawn in place every 0.2 s:
        [2/4] first boot                                         3/20 done, 1 failed  1:12
          ⠹ net40-student01  pve9  first boot   0:48  Setting up qemu-guest-agent ...
          ✔ net40-student02  pve9  ready
          ✘ net40-student03  pve9  harden: exit 1   see /root/lab/run-logs/...
    b.stage(key, text, live=True) moves a VM on (live: it prints output, and b.tail(key, line) shows
    the newest line, dim; no new line for STUCK s turns it yellow). b.ok(key) / b.fail(key, why) end it.
    More VMs than rows: failed ones first, finished ones as one line, then running ones, then '… N more'.
    Width and height are measured on every frame, so a resized window is not filled with copies.
    Without a terminal: one plain line per change. hosts: {key: host} or None (one host: no column)."""

    def __init__(self, title, names, hosts=None):
        super().__init__()
        now = time.time()
        self.title, self.t0, self.hosts = title, now, hosts
        self.rows = {k: {"name": n, "stage": "queued", "t": now, "tail": "", "tail_t": now, "live": False,
                         "state": "run", "why": ""} for k, n in names.items()}
        self.lock, self.drawn, self.frame = threading.RLock(), 0, 0
        self.after = []                      # (text, color): lines printed under the board when it is done

    # --- what the work reports ---
    def stage(self, key, text, live=False):
        with self.lock:
            r = self.rows[key]
            r.update(stage=text, t=time.time(), tail="", tail_t=time.time(), live=live)
        log(f"{r['name']}: {text}")
        if not self.tty:
            self.out.write(f"   {r['name']}: {text}\n")
            self.out.flush()

    def tail(self, key, line):
        line = ANSI.sub("", line or "").strip()
        with self.lock:
            r = self.rows[key]
            if line and line != r["tail"]:
                r.update(tail=line, tail_t=time.time())
                log(f"{r['name']}  | {line}")

    def ok(self, key, text="done"):
        self._end(key, "ok", text)

    def fail(self, key, why):
        self._end(key, "fail", why)

    def _end(self, key, state, text):
        with self.lock:
            r = self.rows[key]
            r.update(state=state, why=text, t=time.time())
        log(f"{r['name']}: {'FAILED: ' if state == 'fail' else ''}{text}")
        if not self.tty:
            self.out.write(f"  {OK_MARK if state == 'ok' else BAD_MARK} {r['name']}: {text}\n")
            self.out.flush()

    def counts(self):
        states = [r["state"] for r in self.rows.values()]
        return states.count("ok"), states.count("fail"), len(states)

    def say(self, text):
        """Print a line above the board (warnings) without breaking it."""
        with self.lock:
            if self.tty and self.drawn:
                self.out.write(f"\033[{self.drawn}A\r\033[J")
                self.drawn = 0
            self.out.write(text.rstrip("\n") + "\n")
            if self._thread:
                self._draw()
            self.out.flush()

    # --- drawing ---
    def _row(self, r, cols, wname, whost):
        now = time.time()
        host = f"{self.hosts.get(r['key'], ''):<{whost}} " if self.hosts else ""
        if r["state"] == "ok":
            return f"  {paint(OK_MARK, 'green')} {r['name']:<{wname}} {host}{r['why']}"
        if r["state"] == "fail":
            return fit(f"  {paint(BAD_MARK, 'red')} {r['name']:<{wname}} {host}" + paint(r["why"], "red"), cols)
        mark = FRAMES[self.frame % len(FRAMES)] if r["stage"] != "queued" else "·"
        head = f"  {mark} {r['name']:<{wname}} {host}{r['stage']:<20} {mmss(now - r['t']):>5}"
        quiet = now - r["tail_t"]
        if r["live"] and quiet >= STUCK:
            tail = paint(fit(f"  no output for {mmss(quiet)}" + (f": {r['tail']}" if r["tail"] else ""),
                             max(0, cols - len(head))), "yellow")
        elif r["live"]:
            tail = paint(fit("  " + (r["tail"] or "(no output yet)"), max(0, cols - len(head))), "dim")
        else:
            tail = ""
        return fit(head + tail, cols) if len(ANSI.sub("", head)) >= cols else head + tail

    def _lines(self):
        size = shutil.get_terminal_size((100, 30))
        cols, room = size.columns - 1, max(4, size.lines - 3)
        rows = [dict(r, key=k) for k, r in self.rows.items()]
        wname = min(max((len(r["name"]) for r in rows), default=4), 28)
        whost = max((len(h) for h in (self.hosts or {}).values()), default=0)
        done, failed, total = self.counts()
        tally = f"{done}/{total} done" + (f", {failed} failed" if failed else "") + f"  {mmss(time.time() - self.t0)}"
        lines = [fit(paint(self.title, "bold") + "  " + paint(tally, "red" if failed else "dim"), cols)]
        if len(rows) + 1 <= room:
            return lines + [self._row(r, cols, wname, whost) for r in rows]
        fails = [r for r in rows if r["state"] == "fail"]
        oks = [r for r in rows if r["state"] == "ok"]
        busy = sorted((r for r in rows if r["state"] == "run"), key=lambda r: r["stage"] == "queued")
        out = [self._row(r, cols, wname, whost) for r in fails[:max(1, room - 3)]]
        if oks:
            out.append(f"  {paint(OK_MARK, 'green')} {len(oks)} done")
        left = room - len(out) - 1
        shown = busy[:max(0, left)]
        out += [self._row(r, cols, wname, whost) for r in shown]
        if len(busy) > len(shown):
            out.append(paint(f"  … {len(busy) - len(shown)} more", "dim"))
        return lines + out

    def _draw(self):
        lines = self._lines()
        up = f"\033[{self.drawn}A" if self.drawn else ""
        self.out.write(up + "".join("\r\033[K" + line + "\n" for line in lines)
                       + ("\033[J" if len(lines) < self.drawn else ""))
        self.out.flush()
        self.drawn = len(lines)

    def _loop(self):
        while not self._stop.wait(0.2):
            with self.lock:
                self.frame += 1
                self._draw()

    def _first(self):
        with self.lock:
            self._draw()

    _last = _first                           # the last frame stays on screen

    def __enter__(self):
        global ACTIVE
        ACTIVE = self
        log(f"== {self.title}")
        if self.tty:
            self._on()
        else:
            print(self.title, flush=True)
        return self

    def __exit__(self, kind, exc, tb):
        global ACTIVE
        ACTIVE = None
        self._off()
        done, failed, total = self.counts()
        log(f"== {self.title}: {done}/{total} done, {failed} failed")
        for line, color in self.after:
            log(line)
            print(paint(f"  {line}", color))
        return False


def each(title, ids, names, work, workers=4, failed="{failed} VM(s) failed"):
    """work(vmid, board) on every VM, a few (workers; 1: one after the other) at a time, each on its own line of a live board:
    it returns the text its line ends with; a LabError marks the line failed. When some failed, a line
    under the board says so (failed: its start, with {failed} and {total}). Returns the board."""
    with Board(title, names, board_hosts(ids)) as board:
        def one(i):
            try:
                board.ok(i, work(i, board))
            except LabError as e:
                board.fail(i, str(e))
        if workers == 1:                     # here, so Ctrl-C stops before the next VM
            for i in ids:
                one(i)
        else:
            parallel(one, ids, workers=workers)
    _, bad, total = board.counts()
    if bad:
        print(paint(failed.format(failed=bad, total=total) + ": see their lines above", "red"))
    return board


class Plan:
    """What a command will do and every warning about it, shown together before anything changes, with
    one question (or none when there is nothing to warn about):
        This will:
          1. ...
        Warnings:
          ! local-lvm on pve9 is 86% full
        Continue? [y/N]
    Each warning has its own skip: LAB_YES=1, or LAB_FORCE=1 for force=True. Without a terminal the
    command goes on only if every warning shown may be skipped; else it stops and names the settings."""

    def __init__(self):
        self.steps, self.warnings = [], []

    def do(self, text):
        self.steps.append(text)

    def warn(self, text, force=False):
        self.warnings.append((text, *((FORCE, "LAB_FORCE=1") if force else (YES, "LAB_YES=1"))))

    def show(self):
        """Print the steps and the warnings (ask() does, then asks)."""
        if self.steps:
            print("This will:")
            for k, text in enumerate(self.steps, 1):
                print(f"  {k}. {text}")
        if self.warnings:
            print(paint("Warnings:", "yellow"))
        for text, _, _ in self.warnings:
            log(f"WARNING: {text}")
            print(paint(f"  ! {text}", "yellow"))

    def ask(self):
        """Show the plan; return when it may go on, else stop (util.die) with the reason."""
        self.show()
        if not self.warnings:
            return
        need = sorted({why for _, skip, why in self.warnings if not skip})
        if not need:
            print(paint(f"  continuing because {', '.join(sorted({w for _, _, w in self.warnings}))}", "dim"))
            return
        if not sys.stdin.isatty():
            die(f"not continuing without a yes; re-run interactively or with {' '.join(need)} to continue anyway")
        if input("Continue? [y/N] ").strip().lower() != "y":
            die("aborted")


def step(k, n, title):
    """'[k/n] title' - the numbered steps of a command."""
    text = f"[{k}/{n}] {title}"
    log(f"== {text}")
    print(paint(text, "bold"), flush=True)


def dry_run(what, lines):
    """--dry-run output: what would happen, one line per VM (or change)."""
    print(f"dry run - would {what}:")
    for line in lines:
        print(f"  {line}")
