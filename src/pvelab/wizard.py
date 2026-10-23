"""`pvelab` with no arguments on a terminal: a menu that asks step by step, shows the command it
built, and runs it. Every step can be typed directly next time; the wizard teaches the command line."""

import os
import shlex
import subprocess
import sys

from .args import valid_net
from .catalogue import TEMPLATES
from .commands.templates import template_status
from .config import CORES, DEFAULT_TEMPLATE, MEM, RUN_TIMEOUT_MIN, SNAP
from .inventory import inventory, lab_data, resolve, targets
from .templates import catalogue_find, node_templates
from .resources import parse_cores, parse_disk, parse_mem
from .ui import paint, select
from .util import LabError, pvelab_argv


class Back(Exception):
    """The user left the current question (empty answer to 'b', Ctrl-C or Ctrl-D): back to the menu."""


def ask(prompt, default=None, check=None, allow_empty=False, show_default=True):
    """One answer. default is used for an empty answer; check(answer) returns an error text or None.
    'b' (or Ctrl-C / Ctrl-D) goes back to the menu."""
    hint = f" [{default}]" if default not in (None, "") and show_default else ""
    while True:
        try:
            a = input(f"{prompt}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise Back
        if a.lower() in ("b", "back") or "\x1b" in a:      # Esc, then Enter
            raise Back
        if not a:
            if default is not None:
                a = str(default)
            elif not allow_empty:
                print(paint("  please answer (or 'b' to go back)", "yellow"))
                continue
        problem = check(a) if check and a else None
        if problem:
            print(paint(f"  {problem}", "yellow"))
            continue
        return a


def yes(prompt, default=False):
    a = ask(prompt + (" (Y/n)" if default else " (y/N)"), default="y" if default else "n", show_default=False,
            check=lambda a: None if a.lower() in ("y", "yes", "n", "no") else "y or n")
    return a.lower() in ("y", "yes")


def choose(prompt, options, default=None):
    """options: [(value, label)]. On a terminal: an arrow-key list; otherwise numbered, and the
    answer may be the number or the value itself."""
    values = [v for v, _ in options]
    if sys.stdin.isatty() and sys.stdout.isatty():
        i = select(prompt + ":", [label for _, label in options], values.index(default) if default in values else 0)
        if i is None:
            raise Back
        return values[i]
    for n, (v, label) in enumerate(options, 1):
        print(f"  {n}) {label}")
    a = ask(prompt, default=default,
            check=lambda a: None if pick(a, len(values)) is not None or a in values else f"pick 1-{len(values)}")
    i = pick(a, len(values))
    return values[i] if i is not None else a


def pick(a, n):
    """'3' -> index 2 when there are n choices, else None."""
    return int(a) - 1 if a.isdigit() and 1 <= int(a) <= n else None


def is_int(lo, hi):
    return lambda a: None if a.isdigit() and lo <= int(a) <= hi else f"a number {lo}-{hi}"


def is_file(a):
    return None if os.path.isfile(os.path.expanduser(a)) else f"no such file: {a}"


def valid(parse):
    """An answer check from one of pvelab's own option parsers: its error text, or None if it parses."""
    def check(a):
        try:
            parse(a)
        except LabError as e:
            return str(e).splitlines()[0]
        return None
    return check


def vm_words(answer):
    """What the user typed for 'which VMs' -> pvelab VM arguments: '30' -> --net 30."""
    out = []
    for w in answer.replace(",", " ").split():
        out += ["--net", w.removeprefix("net")] if w.removeprefix("net").isdigit() else [w]
    return out


class Wizard:
    def __init__(self, data):
        self.data = data                      # inventory.lab_data(): vms, nets, count {net: VMs}

    def which_vms(self, what="Which VMs", default=None):
        nets = ", ".join(f"net{n} ({self.data['count'].get(n, 0)} VMs)" for n in self.data["nets"]) or "none yet"
        print(paint(f"  classes: {nets}", "dim"))
        print(paint("  answer with: a net number (30), VM names, a range (net30-student01..05), or all", "dim"))
        return vm_words(ask(what, default=default, check=valid(lambda a: targets(vm_words(a)))))

    # --- one method per menu entry: each returns the pvelab arguments to run (or a list of them)

    def create(self):
        print("Create VMs for a class. Each student gets one or more VMs with a random password.")
        count = ask("How many students", default=20, check=is_int(1, 99))
        per = ask("VMs per student", default=1, check=is_int(1, 99 // int(count) or 1))
        tpl = choose("Operating system", [(t.short, t.name) for t in TEMPLATES],
                     default=(catalogue_find(DEFAULT_TEMPLATE) or TEMPLATES[0]).short)
        print(paint("  every class gets its own network (net N, 10.10.N.0/24); 'auto' picks a free one", "dim"))
        net = ask("Network number", default="auto", check=lambda a: None if a == "auto" else valid(valid_net)(a))
        argv = ["create", count if per == "1" else f"{count}x{per}", tpl, "--net", net]
        if net == "auto" or net not in self.data["nets"]:
            print(paint("  students can't reach each other's VMs (their own VMs can), unless you allow it", "dim"))
            if yes("Let students reach each other's VMs (group work)"):
                argv.append("--no-isolate")
        if yes("Should students ssh in from outside (opens port 22000+VMID)", default=True):
            argv.append("--expose")
        if yes(f"Change the size (default {CORES} cores, {MEM} MB memory, the template's disk)"):
            argv += ["--cores", ask("CPU cores", default=CORES, check=valid(parse_cores))]
            argv += ["--memory", ask("Memory (MB, or like 4G)", default=MEM, check=valid(parse_mem))]
            disk = ask("Disk GB (empty: keep the template's)", allow_empty=True,
                       check=lambda a: "the full size in GB, not +N" if a.startswith("+") else valid(parse_disk)(a))
            if disk:
                argv += ["--disk", disk]
        script = ask("Setup script to run in every VM after creating (empty: none)", allow_empty=True,
                     check=is_file)
        if script:
            argv += ["--script", script]
        return argv

    def list(self):
        net = ask("Show which net (number, or empty for all)", allow_empty=True,
                  check=lambda a: None if a.removeprefix("net") in self.data["nets"] else "no such net")
        argv = ["list"] + (["--net", net.removeprefix("net")] if net else [])
        return argv + (["--passwords"] if yes("Show the passwords too") else [])

    def power(self):
        action = choose("What", [("start", "start"), ("stop", "stop (clean shutdown)"), ("reboot", "reboot")])
        return [action, *self.which_vms()]

    def run(self):
        print("Run a script as root in each VM. Snapshots are taken before and after, so it can be undone.")
        vms = self.which_vms()
        script = ask("Script file (must start with #!, e.g. #!/bin/bash)", check=is_file)
        timeout = ask("Give up after how many minutes", default=RUN_TIMEOUT_MIN, check=is_int(1, 1440))
        return ["run", *vms, script] + (["--timeout", timeout] if timeout != str(RUN_TIMEOUT_MIN) else [])

    def snapshots(self):
        what = choose("What", [("snapshot", f"save the current state as '{SNAP}' (replaces the old one)"),
                               ("rollback", "go back to a snapshot (changes since are lost)")])
        vms = self.which_vms()
        if what == "snapshot":
            return ["snapshot", *vms]
        snaps = lab_data(snaps=True)["snaps"] or [SNAP]
        print(paint(f"  snapshots on student VMs: {' '.join(snaps)}", "dim"))
        snap = ask("Back to which snapshot", default=SNAP)
        return ["rollback", *vms, "--to", snap]

    def net_switch(self, cmd, prompt, on_label, off_label):
        """expose / isolate: pick a net, then on or off."""
        if not self.data["nets"]:
            print("No classes yet - create VMs first.")
            raise Back
        net = choose("Which net", [(n, f"net{n}") for n in self.data["nets"]])
        on = choose(prompt, [("on", on_label), ("off", off_label)], default="on")
        return [cmd, "--net", net] + (["--off"] if on == "off" else [])

    def expose(self):
        return self.net_switch("expose", "Students' ssh from outside", "open", "close")

    def isolate(self):
        return self.net_switch("isolate", "Between students", "isolated: each student reaches only their own VMs",
                               "open: all VMs of the net reach each other")

    def block(self):
        verb = choose("What", [("block", "block (cut off)"), ("unblock", "unblock (allow again)")])
        kind = choose("Which access", [("internet", "internet (the VMs can't reach out)"),
                                       ("ssh", "ssh from outside (students can't log in)"), ("all", "both")])
        return [verb, kind, *self.which_vms()]

    def resize(self):
        vms = self.which_vms()
        argv = ["resize", *vms]
        cores = ask("CPU cores (empty: keep)", allow_empty=True, check=valid(parse_cores))
        mem = ask("Memory, MB or like 4G (empty: keep)", allow_empty=True, check=valid(parse_mem))
        disk = ask("Disk: new size in GB or +GB to add (empty: keep)", allow_empty=True, check=valid(parse_disk))
        for opt_, v in (("--cores", cores), ("--memory", mem), ("--disk", disk)):
            if v:
                argv += [opt_, v]
        if not (cores or mem or disk):
            print("Nothing to change.")
            raise Back
        if yes("Reboot running VMs now so it takes effect (students get disconnected)"):
            argv.append("--reboot")
        return argv

    def top(self):
        vms = self.which_vms("Which VMs (or all)", default="all")
        return ["top", *([] if vms == ["all"] else vms)] + (["--watch"] if yes("Keep refreshing") else [])

    def handout(self):
        vms = self.which_vms()
        out = ask("Save as (HTML file)", default="pvelab-handout.html")
        return ["handout", *vms, "--output", out]

    def ssh(self):
        name = ask("VM name", check=valid(resolve))
        return ["ssh", name]

    def templates(self):
        print("New VMs are made from these OS templates. Updating one gets the distribution's newest image;")
        print("VMs already made are not changed.")
        inv = inventory()
        on_node = node_templates(inv)
        which = choose("Which template", [("all", "all of them")] + [
            (t.short, f"{t.short:<9} {t.name:<19} {template_status(t, inv, on_node)}") for t in TEMPLATES])
        return ["pull", which]

    def destroy(self):
        print(paint("Destroy deletes VMs with their disks and snapshots. This can't be undone.", "red"))
        vms = self.which_vms()
        return ["destroy", *vms]


MENU = [
    ("create", "Create VMs for a class"),
    ("list", "List VMs (status, IPs, ssh ports, passwords)"),
    ("power", "Start / stop / reboot VMs"),
    ("run", "Run a script in VMs"),
    ("snapshots", "Snapshot or roll back"),
    ("expose", "Let students ssh in from outside"),
    ("block", "Block / unblock internet or ssh"),
    ("isolate", "Isolate students from each other (or allow group work)"),
    ("resize", "Change CPU / memory / disk"),
    ("top", "Watch CPU / memory / network (spot busy VMs)"),
    ("handout", "Print login cards for students"),
    ("ssh", "Open a shell in a VM"),
    ("templates", "Download or update OS templates"),
    ("destroy", "Destroy VMs"),
]


def run_pvelab(argv):
    """Run pvelab argv as a child (its own lock, exit code and Ctrl-C) and return its exit code."""
    try:      # PVELAB_WIZARD_STEP: no repeated start-up warnings
        return subprocess.run(pvelab_argv(*argv, env=["PVELAB_WIZARD_STEP=1"])).returncode
    except KeyboardInterrupt:
        return 130


def execute(argv):
    """Show what argv would do (--dry-run, for commands that have it), the command line, then run it."""
    from .cli import DRY_RUN
    if argv[0] in DRY_RUN:
        print()
        run_pvelab(argv + ["--dry-run"])
    print("\n  " + paint("pvelab " + shlex.join(argv), "bold") + paint("   <- you can type this directly next time", "dim"))
    if not yes("Run it", default=True):
        print("Not run.")
        return
    print()
    code = run_pvelab(argv)
    if code not in (0, 130):
        print(paint(f"\n(exit code {code})", "yellow"))


def wizard():
    print(paint("pvelab", "bold") + " - student VMs on this Proxmox VE node. "
          "Esc, 'b' or Ctrl-C goes back to the menu.")
    entries = MENU + [("help", "Help on all commands"), ("quit", "Quit")]
    last = 0
    while True:
        data = lab_data()
        n_vm = len(data["vms"])
        print(f"\n{n_vm} student VM(s)" + (f" in {', '.join('net' + n for n in data['nets'])}" if data["nets"] else ""))
        last = select("What do you want to do?", [label for _, label in entries], last, esc="quit")
        key = entries[last][0] if last is not None else "quit"
        if key == "quit":
            return
        if key == "help":
            run_pvelab(["help"])
            continue
        print()
        try:
            execute(getattr(Wizard(data), key)())
        except Back:
            print("(back to the menu)")
            continue
        try:
            input(paint("\nPress Enter for the menu ", "dim"))
        except (EOFError, KeyboardInterrupt):
            print()
            return
