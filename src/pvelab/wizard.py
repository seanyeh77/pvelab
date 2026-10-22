"""`pvelab` with no arguments on a terminal: a menu that asks step by step, shows the command it
built, and runs it. Every step can be typed directly next time; the wizard teaches the command line."""

import os
import re
import shlex
import subprocess
import sys

from .catalogue import TEMPLATES
from .config import CORES, DEFAULT_TEMPLATE, MEM, RUN_TIMEOUT_MIN, SNAP
from .ui import paint
from .util import package_root


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
        if a.lower() in ("b", "back"):
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
    """options: [(value, label)]; the answer may be the number or the value itself."""
    for n, (v, label) in enumerate(options, 1):
        print(f"  {n}) {label}")
    values = [v for v, _ in options]

    def check(a):
        if a.isdigit() and 1 <= int(a) <= len(options) or a in values:
            return None
        return f"pick 1-{len(options)}"
    a = ask(prompt, default=default, check=check)
    return options[int(a) - 1][0] if a.isdigit() and 1 <= int(a) <= len(options) else a


def is_int(lo, hi):
    return lambda a: None if a.isdigit() and lo <= int(a) <= hi else f"a number {lo}-{hi}"


def is_file(a):
    return None if os.path.isfile(os.path.expanduser(a)) else f"no such file: {a}"


def is_mem(a):
    return None if re.fullmatch(r"\d+[MmGg]?", a) else "MB (4096) or a size like 4G"


def vm_words(answer):
    """What the user typed for 'which VMs' -> pvelab VM arguments: '30' -> --net 30."""
    out = []
    for w in answer.replace(",", " ").split():
        out += ["--net", w.removeprefix("net")] if w.removeprefix("net").isdigit() else [w]
    return out


class Wizard:
    def __init__(self, data):
        self.data = data                      # {"vms": [...], "nets": [...], "snaps": [...], "count": {net: n}}

    def which_vms(self, what="Which VMs", default=None):
        nets = ", ".join(f"net{n} ({self.data['count'].get(n, 0)} VMs)" for n in self.data["nets"]) or "none yet"
        print(paint(f"  classes: {nets}", "dim"))
        print(paint("  answer with: a net number (30), VM names, a range (net30-student01..05), or all", "dim"))

        def check(a):
            for w in vm_words(a):
                if w == "--net" or w.isdigit() and w in self.data["nets"] or w == "all" or "*" in w or ".." in w:
                    continue
                if w.isdigit():
                    return f"there is no net{w} (classes: {' '.join(self.data['nets']) or 'none'})"
                if w not in self.data["vms"]:
                    return f"no VM named {w}"
            return None
        return vm_words(ask(what, default=default, check=check))

    # --- one method per menu entry: each returns the pvelab arguments to run (or a list of them)

    def create(self):
        print("Create VMs for a class. Each student gets a VM with its own random password.")
        count = ask("How many VMs", default=20, check=is_int(1, 99))
        tpls = [(t.aliases[0] if t.aliases else t.name, t.name) for t in TEMPLATES]
        default = next((v for (v, _), t in zip(tpls, TEMPLATES)
                        if DEFAULT_TEMPLATE in (str(t.vmid), t.name, *t.aliases)), tpls[0][0])
        tpl = choose("Operating system", tpls, default=default)
        print(paint("  every class gets its own network (net N, 10.10.N.0/24); 'auto' picks a free one", "dim"))
        net = ask("Network number", default="auto",
                  check=lambda a: None if a == "auto" or a.isdigit() and 2 <= int(a) <= 254 and a != "10"
                  else "a number 2-254 (not 10), or auto")
        argv = ["create", count, tpl, "--net", net]
        if yes("Should students ssh in from outside (opens port 22000+VMID)", default=True):
            argv.append("--expose")
        if yes(f"Change the size (default {CORES} cores, {MEM} MB memory, the template's disk)"):
            argv += ["--cores", ask("CPU cores", default=CORES, check=is_int(1, 64))]
            argv += ["--memory", ask("Memory (MB, or like 4G)", default=MEM, check=is_mem)]
            disk = ask("Disk GB (empty: keep the template's)", allow_empty=True,
                       check=lambda a: None if a.isdigit() else "a number of GB")
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
        snaps = self.data["snaps"] or [SNAP]
        print(paint(f"  snapshots on student VMs: {' '.join(snaps)}", "dim"))
        snap = ask("Back to which snapshot", default=SNAP)
        return [["rollback", *vms, "--to", snap, "--dry-run"], ["rollback", *vms, "--to", snap]]

    def expose(self):
        if not self.data["nets"]:
            print("No classes yet - create VMs first.")
            raise Back
        net = choose("Which net", [(n, f"net{n}") for n in self.data["nets"]])
        on = choose("Students' ssh from outside", [("on", "open"), ("off", "close")], default="on")
        return ["expose", "--net", net] + (["--off"] if on == "off" else [])

    def block(self):
        verb = choose("What", [("block", "block (cut off)"), ("unblock", "unblock (allow again)")])
        kind = choose("Which access", [("internet", "internet (the VMs can't reach out)"),
                                       ("ssh", "ssh from outside (students can't log in)"), ("all", "both")])
        return [verb, kind, *self.which_vms()]

    def resize(self):
        vms = self.which_vms()
        argv = ["resize", *vms]
        cores = ask("CPU cores (empty: keep)", allow_empty=True, check=is_int(1, 64))
        mem = ask("Memory, MB or like 4G (empty: keep)", allow_empty=True, check=is_mem)
        disk = ask("Disk: new size in GB or +GB to add (empty: keep)", allow_empty=True,
                   check=lambda a: None if re.fullmatch(r"\+?\d+[Gg]?", a) else "GB (30) or +GB (+10)")
        for opt_, v in (("--cores", cores), ("--memory", mem), ("--disk", disk)):
            if v:
                argv += [opt_, v]
        if len(argv) == 1 + len(vms):
            print("Nothing to change.")
            raise Back
        if yes("Reboot running VMs now so it takes effect (students get disconnected)"):
            argv.append("--reboot")
        return [argv + ["--dry-run"], argv]

    def top(self):
        vms = self.which_vms("Which VMs (or all)", default="all")
        return ["top", *([] if vms == ["all"] else vms)] + (["--watch"] if yes("Keep refreshing") else [])

    def handout(self):
        vms = self.which_vms()
        out = ask("Save as (HTML file)", default="pvelab-handout.html")
        return ["handout", *vms, "--output", out]

    def ssh(self):
        name = ask("VM name", check=lambda a: None if a in self.data["vms"] else "no VM with that name")
        return ["ssh", name]

    def destroy(self):
        print(paint("Destroy deletes VMs with their disks and snapshots. This can't be undone.", "red"))
        vms = self.which_vms()
        return [["destroy", *vms, "--dry-run"], ["destroy", *vms]]


MENU = [
    ("create", "Create VMs for a class"),
    ("list", "List VMs (status, IPs, ssh ports, passwords)"),
    ("power", "Start / stop / reboot VMs"),
    ("run", "Run a script in VMs"),
    ("snapshots", "Snapshot or roll back"),
    ("expose", "Let students ssh in from outside"),
    ("block", "Block / unblock internet or ssh"),
    ("resize", "Change CPU / memory / disk"),
    ("top", "Watch CPU / memory / network (spot busy VMs)"),
    ("handout", "Print login cards for students"),
    ("ssh", "Open a shell in a VM"),
    ("destroy", "Destroy VMs"),
]


def run_pvelab(argv):
    """Run pvelab argv as a child (its own lock, exit code and Ctrl-C) and return its exit code."""
    env = {**os.environ, "PYTHONPATH": package_root()}
    try:
        return subprocess.run([sys.executable, "-m", "pvelab", *argv], env=env).returncode
    except KeyboardInterrupt:
        return 130


def execute(plan):
    """plan: one argv, or [preview argv (--dry-run), real argv]."""
    steps = plan if plan and isinstance(plan[0], list) else [plan]
    *preview, real = steps
    for p in preview:
        print()
        run_pvelab(p)
    print("\n  " + paint("pvelab " + shlex.join(real), "bold") + paint("   <- you can type this directly next time", "dim"))
    if not yes("Run it", default=True):
        print("Not run.")
        return
    print()
    code = run_pvelab(real)
    if code not in (0, 130):
        print(paint(f"\n(exit code {code})", "yellow"))


def lab_overview():
    from .commands.complete import lab_data
    from .inventory import desc_value, managed
    data = lab_data()
    count = {}
    for c in managed().values():
        n = desc_value(c, "net")
        count[n] = count.get(n, 0) + 1
    data["count"] = count
    return data


def wizard():
    print(paint("pvelab", "bold") + " - student VMs on this Proxmox VE node. "
          "Answer the questions; 'b' or Ctrl-C goes back, 'q' quits.")
    while True:
        data = lab_overview()
        n_vm = len(data["vms"])
        print(f"\n{n_vm} student VM(s)" + (f" in {', '.join('net' + n for n in data['nets'])}" if data["nets"] else "")
              + ". What do you want to do?")
        for n, (_, label) in enumerate(MENU, 1):
            print(f"  {n:>2}) {label}")
        print("   h) Help on all commands     q) Quit")
        try:
            a = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if a in ("q", "quit", "exit"):
            return
        if a in ("h", "help", "?"):
            run_pvelab(["help"])
            continue
        if not (a.isdigit() and 1 <= int(a) <= len(MENU)):
            print(paint(f"  pick 1-{len(MENU)}, h or q", "yellow"))
            continue
        key = MENU[int(a) - 1][0]
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
