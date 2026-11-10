"""An inline form for the menu: every question of a command on screen at once, under the cursor (not
full screen), redrawn in place. Up/Down move between fields, typing edits, Left/Right change a choice,
Space ticks a box; the command it builds and its warnings show live below. Enter on Run returns."""

import shutil
import sys
import textwrap
from dataclasses import dataclass, field

from . import ui
from .ui import edit_loop, edit_text, ENTER, fit, LEFT, paint, redraw, RIGHT

UP, DOWN = ui.UP | {b"\x1b[Z"}, ui.DOWN | {b"\t"}     # Shift-Tab and Tab too
CANCEL = {b"\x1b", b"\x03", b"\x04"}                   # Esc, Ctrl-C, Ctrl-D
BUTTONS = "__buttons__"
HELP = "Up/Down move   type to edit   Left/Right change   Space tick   Enter next   Esc back to the menu"


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"                   # text, choice ([(value, label)] options) or check (a box)
    options: list = field(default_factory=list)
    check: object = None                 # check(value) -> error text or None (text fields)
    hint: str = ""                       # dim, right of the value
    notes: list = field(default_factory=list)     # dim lines under the field while it has the focus
    text: str = ""                       # a check box's words: [x] text
    empty: str = None                    # text: may be left empty, which means this (shown dim in its place)
    depends: str = ""                    # what else check reads (another answer): part of its memo key


class Form:
    """build(values) -> the Fields that apply to these answers (a field may come and go with others);
    preview(values, errors) -> [(line, color)] under the fields: the command, warnings (errors: the
    marked answers, {key: text}). values: the answers (text and choice: str, check: bool), edited in place."""

    def __init__(self, title, build, preview, values):
        self.title, self.build, self.preview, self.values = title, build, preview, values
        self.button, self.fresh = 0, True    # fresh: the first key typed replaces the value
        self.touched, self.tried, self.done, self.focus_at = set(), False, False, 0
        self.memo = {}
        self.refresh()
        self.focus = self.fields[0].key if self.fields else BUTTONS

    # --- state

    def error(self, f):
        if f.kind != "text":
            return None
        if not self.values[f.key]:
            return None if f.empty is not None else "please answer"
        if not f.check:
            return None
        k = (f.key, self.values[f.key], f.depends)
        if k not in self.memo:              # a check may read the hosts (routes): once per answer
            self.memo[k] = f.check(self.values[f.key])
        return self.memo[k]

    def errors(self):
        return {f.key: e for f in self.fields if (e := self.error(f))}

    def keys(self):
        return [f.key for f in self.fields] + [BUTTONS]

    def move(self, step):
        keys = self.keys()
        if self.focus in self.by_key:
            self.touched.add(self.focus)
        self.focus = keys[max(0, min(len(keys) - 1, keys.index(self.focus) + step))]
        self.fresh = True

    def refresh(self):
        """Fields again from the answers; the focus stays on its field, or the one now in its place."""
        old = getattr(self, "fields", None)
        at = self.keys().index(self.focus) if old and self.focus in self.keys() else 0
        self.fields = self.build(self.values)
        self.by_key = {f.key: f for f in self.fields}
        if old is not None and self.focus not in self.keys():
            self.focus = self.keys()[min(at, len(self.keys()) - 1)]

    def handle(self, k):
        """One key press: None to go on, "run" or "cancel" when done."""
        if k in CANCEL:
            return "cancel"
        if k in UP:
            self.move(-1)
            return None
        if k in DOWN:
            self.move(1)
            return None
        if self.focus == BUTTONS:
            if k in LEFT or k in RIGHT:
                self.button = 1 - self.button
            elif k in ENTER:
                if self.button == 1:
                    return "cancel"
                errs = self.errors()
                if not errs:
                    return "run"
                self.tried, self.focus, self.fresh = True, next(iter(errs)), True
            return None
        f = self.by_key[self.focus]
        if k in ENTER:
            if self.error(f):
                self.touched.add(f.key)
            else:
                self.move(1)
            return None
        v = self.values[f.key]
        before = dict(self.values)
        if f.kind == "choice":
            vals = [o for o, _ in f.options]
            i = vals.index(v) if v in vals else 0
            if k in LEFT or k in RIGHT:
                self.values[f.key] = vals[(i + (1 if k in RIGHT else -1)) % len(vals)]
            else:                           # a letter: the next option that starts with it
                ch = k.decode(errors="ignore").lower()
                nxt = [o for o in vals[i + 1:] + vals[:i + 1] if ch and str(o).lower().startswith(ch)]
                if nxt:
                    self.values[f.key] = nxt[0]
        elif f.kind == "check":
            if k in LEFT or k in RIGHT or k == b" ":
                self.values[f.key] = not v
            elif k in (b"y", b"Y", b"n", b"N"):
                self.values[f.key] = k in (b"y", b"Y")
        else:
            new = edit_text(v, k)
            if new is not None:
                self.values[f.key] = new[-1] if self.fresh and len(new) > len(v) else new
            self.fresh = False
        if self.values != before:           # fields again only when an answer changed
            self.refresh()
        return None

    # --- drawing

    def lines(self, width_cols=80):
        """What is drawn (colored when the terminal shows colors); the preview wrapped to width_cols."""
        width = max((len(f.label) for f in self.fields), default=0) + 2
        errs = self.errors()
        out = [paint(self.title, "bold")]
        for f in self.fields:
            on = f.key == self.focus and not self.done
            v = self.values[f.key]
            if f.kind == "choice":
                label = dict(f.options).get(v, str(v))
                shown = f"< {label} >" if on else label
            elif f.kind == "check":
                shown = ("[x] " if v else "[ ] ") + f.text
            else:
                shown = v + ("_" if on else "")
            row = ("> " if on else "  ") + f"{f.label:<{width}}" + (paint(shown, "pick") if on else shown)
            if f.kind == "text" and not v and f.empty is not None:
                row += paint(f"{' ' if on else ''}empty: {f.empty}", "dim")
            if on:
                self.focus_at = len(out)
            out.append(row + (paint("   " + f.hint, "dim") if f.hint else ""))
            e = errs.get(f.key)
            if e and (on or f.key in self.touched or self.tried):
                out.append(paint(f"    {e}", "yellow"))
            if on:
                out += [paint(f"    {n}", "dim") for n in f.notes]
        out.append(paint("  " + "-" * 60, "dim"))
        for t, c in self.preview(self.values, errs):     # wrapped, not cut: the command is read whole
            for part in textwrap.wrap(t, max(20, width_cols - 4), subsequent_indent="    ",
                                      break_on_hyphens=False) or [""]:
                out.append("  " + (paint(part, c) if c else part))
        if self.done:
            return out
        run, cancel = "[ Run ]", "[ Cancel ]"
        if self.focus == BUTTONS:
            run, cancel = (paint(run, "pick"), cancel) if self.button == 0 else (run, paint(cancel, "pick"))
        if self.focus == BUTTONS:
            self.focus_at = len(out)
        out.append("  " + run + "  " + cancel + ("   " + paint("fix the answers in yellow first", "yellow")
                                                 if self.tried and errs else ""))
        out.append(paint("  " + HELP, "dim"))
        return out

    def run(self):
        """Draw under the cursor and edit until Run (-> True) or Esc / Cancel (-> False)."""
        def render():
            size = shutil.get_terminal_size((80, 24))
            return window([fit(line, size.columns - 1) for line in self.lines(size.columns)],
                          self.focus_at, size.lines - 1)
        try:
            result, drawn = edit_loop(render, self.handle)
        finally:
            self.done = True
        redraw(sys.stdout, render(), drawn)     # the answers stay on screen, without the focus and the help
        return result == "run"


def window(lines, at, rows):
    """At most rows of lines, with line at in view (a third of the way down when the form is taller)."""
    if len(lines) <= rows:
        return lines
    start = max(0, min(at - rows // 3, len(lines) - rows))
    return lines[start:start + rows]


def fits_screen(rows):
    """True if the form can be drawn here: a terminal on both ends, tall enough for rows lines."""
    return ui.interactive() and shutil.get_terminal_size((80, 24)).lines > rows + 2
