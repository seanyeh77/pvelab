"""Terminal output: colors and a progress line for long operations."""

import os
import select as select_
import sys
import termios
import time
import tty

CODES = {"green": "32", "red": "31", "yellow": "33", "dim": "2", "bold": "1", "pick": "1;36"}


def color_on():
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


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


def elapsed(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m}:{s:02d}"


class Progress:
    """One line that updates in place on a terminal: '>> label [#####-----] 12/35 ...'.
    When the output is not a terminal (logs, pipes), a line is printed only when the counts change."""

    def __init__(self, label, total):
        self.label, self.total, self.t0 = label, total, time.time()
        self.tty, self.last = sys.stdout.isatty(), None

    def update(self, done, failed=0, running=()):
        bar_w = 20
        fill = bar_w * (done + failed) // self.total if self.total else bar_w
        bar = "#" * fill + "-" * (bar_w - fill)
        counts = f"{done}/{self.total} done" + (f", {paint(f'{failed} failed', 'red')}" if failed else "")
        if running:
            shown = ", ".join(running[:3]) + (f" +{len(running) - 3}" if len(running) > 3 else "")
            counts += f", {len(running)} running ({shown})"
        line = f">> {self.label} [{bar}] {counts} {paint(elapsed(time.time() - self.t0), 'dim')}"
        if self.tty:
            sys.stdout.write("\r\033[K" + line)
            sys.stdout.flush()
        elif (done, failed) != self.last:
            print(line)
        self.last = (done, failed)

    def finish(self):
        if self.tty:
            sys.stdout.write("\n")
            sys.stdout.flush()

    @classmethod
    def each(cls, label, items, name, fn):
        """fn(item) for each item, one after the other, showing which one is in progress."""
        bar = cls(label, len(items))
        for n, i in enumerate(items):
            bar.update(n, running=[name(i)])
            fn(i)
        if items:
            bar.update(len(items))
            bar.finish()


def dry_run(what, lines):
    """--dry-run output: what would happen, one line per VM (or change)."""
    print(f"dry run - would {what}:")
    for line in lines:
        print(f"  {line}")
