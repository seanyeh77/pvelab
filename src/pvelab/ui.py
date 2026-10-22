"""Terminal output: colors and a progress line for long operations."""

import os
import sys
import time

CODES = {"green": "32", "red": "31", "yellow": "33", "dim": "2", "bold": "1"}


def color_on(stream=None):
    stream = stream or sys.stdout
    return stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def paint(text, color, width=0):
    """text padded to width, then colored (padding first, so columns line up)."""
    text = f"{text:<{width}}" if width else str(text)
    return f"\033[{CODES[color]}m{text}\033[0m" if color and color_on() else text


def elapsed(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m}:{s:02d}"


class Progress:
    """One line that updates in place on a terminal: '>> label [#####-----] 12/35 ...'.
    When the output is not a terminal (logs, pipes), a line is printed only when the counts change."""

    def __init__(self, label, total):
        self.label, self.total, self.t0 = label, total, time.time()
        self.tty, self.last = sys.stdout.isatty(), None

    def update(self, done, failed=0, running=(), note=""):
        bar_w = 20
        fill = bar_w * (done + failed) // self.total if self.total else bar_w
        bar = "#" * fill + "-" * (bar_w - fill)
        counts = f"{done}/{self.total} done" + (f", {paint(f'{failed} failed', 'red')}" if failed else "")
        if running:
            shown = ", ".join(running[:3]) + (f" +{len(running) - 3}" if len(running) > 3 else "")
            counts += f", {len(running)} running ({shown})"
        line = f">> {self.label} [{bar}] {counts} {paint(elapsed(time.time() - self.t0), 'dim')}"
        if note:
            line += f"  {note}"
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
