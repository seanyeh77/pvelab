"""`pvelab` with no arguments on a terminal: a menu that asks step by step, shows the command it
built, and runs it. Every step can be typed directly next time; the wizard teaches the command line."""

import importlib
import shlex
import subprocess

from .args import valid_net
from .commands.create import per_vm
from .catalogue import TEMPLATES
from .config import CORES, DEFAULT_TEMPLATE, EST_DISK_GB, MEM, RUN_TIMEOUT_MIN, SNAP
from .networks import is_new_net
from .inventory import inventory, lab_data, resolve, targets
from .joining import bad_addr
from .library import library_lines
from .nodes import multi
from .placement import bridge_host, fits, node_room
from .templates import catalogue_find, check_template_name, copy_needed, is_saved, node_templates, template_status
from .resources import full_disk, parse_cores, parse_disk, parse_mem
from .scripts import expose_warning, parse_chain
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
    """options: [(value, label)]: an arrow-key list; returns the value picked."""
    values = [v for v, _ in options]
    i = select(prompt + ":", [label for _, label in options], values.index(default) if default in values else 0)
    if i is None:
        raise Back
    return values[i]


def is_int(lo, hi):
    return lambda a: None if a.isdigit() and lo <= int(a) <= hi else f"a number {lo}-{hi}"


def valid(parse):
    """An answer check from one of pvelab's own option parsers: its error text, or None if it parses."""
    def check(a):
        try:
            parse(a)
        except LabError as e:
            return str(e).splitlines()[0]
        return None
    return check


check_chain = valid(parse_chain)        # 'harden+./setup.sh': library names or files, joined with +


def show_library():
    """The script library, dimmed, above a question that asks for scripts."""
    for line in library_lines() + ["  or a file path; join several with +"]:
        print(paint(line, "dim"))


OS_OPTIONS = [(t.short, t.name) for t in TEMPLATES]


def ask_size(of="", defaults=(CORES, MEM, "")):
    """CPU cores, memory and disk (of one VM, e.g. " of VM -2") as typed: (cores, mem, disk)."""
    return (ask(f"CPU cores{of}", default=defaults[0], check=valid(parse_cores)),
            ask(f"Memory{of} (MB, or like 4G)", default=defaults[1], check=valid(parse_mem)),
            ask(f"Disk GB{of} (empty: the template's)", default=defaults[2], allow_empty=True,
                check=valid(full_disk)))


def ask_where(net, tpls, mem):
    """create's "Where" question (2 or more hosts only): --node auto or --node NAME, as argv words, or []
    when there is nothing to ask. tpls: the template of each of a student's VMs (aliases); mem: their MB."""
    if not multi():                          # one host, or no cluster: nothing is asked, as before
        return []
    pin = bridge_host(net)
    if pin:                                  # a plain bridge exists on one host: its VMs stay there
        print(paint(f"  {'the new net' if net == 'auto' else 'net' + net} is on {pin} only", "dim"))
        return []
    need, gb = sum(mem), len(mem) * EST_DISK_GB    # one student: all their VMs, as create counts them
    room = node_room()
    print(paint(f"  {'HOST':<14} {'MEM FREE':>9} {'DISK FREE':>10}  STUDENTS THAT FIT", "dim"))
    for n, r in sorted(room.items()):
        print(paint(f"  {n:<14} {r['mem'] / 1024:>6.1f} GB {r['disk']:>7} GB  {fits(r, need, gb)}", "dim"))
    node = choose("Where", [("auto", "spread by free space (recommended)"),
                            *((n, f"all on {n}") for n in sorted(room))])
    for line in copy_notes(tpls, sorted(room) if node == "auto" else [node]):
        print(paint(f"  {line}", "yellow"))
    return ["--node", node]


def copy_notes(tpls, hosts):
    """'debian-13-cloud is copied to pve2 first (~2.1 GB)' for each of hosts that lacks a current copy of
    one of the templates tpls (the menu's OS choices). A template not built yet: nothing (create says so)."""
    inv = inventory()
    originals = {name: i for i, name in node_templates(inv).items()}
    out = []
    for t in dict.fromkeys(tpls):
        c = catalogue_find(t)
        name = c.name if c else t
        if name not in originals:
            continue
        size = None                          # the template's GB, read once (lvs on its node)
        for h in hosts:
            gb = copy_needed(originals[name], h, inv, size)
            if gb is not None:
                size = gb
                out.append(f"{name} is copied to {h} first (~{gb:.1f} GB)")
    return out


def joined(vals):
    """One VM's value each -> one value if they are all the same, else a comma list (create's)."""
    return vals[0] if len(set(vals)) == 1 else ",".join(vals)


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
        per = int(ask("VMs per student", default=1, check=is_int(1, 99 // int(count) or 1)))
        tpl = (catalogue_find(DEFAULT_TEMPLATE) or TEMPLATES[0]).short
        size = None                                     # (cores, memory, disk), if asked for
        if per > 1 and yes("Different OS or size for each of a student's VMs"):
            specs, prev = [], (tpl, CORES, MEM, "")     # each VM's defaults: the previous VM's answers
            for k in range(1, per + 1):
                prev = (choose(f"Operating system of VM -{k}", OS_OPTIONS, default=prev[0]),
                        *ask_size(f" of VM -{k}", prev[1:]))
                specs.append(prev)
            tpl, *size = (joined(col) for col in zip(*specs))
        else:
            tpl = choose("Operating system", OS_OPTIONS, default=tpl)
        print(paint("  every class gets its own network (net N, 10.10.N.0/24); 'auto' picks a free one", "dim"))
        net = ask("Network number", default="auto", check=lambda a: None if a == "auto" else valid(valid_net)(a))
        argv = ["create", count if per == 1 else f"{count}x{per}", tpl, "--net", net]
        if net == "auto" or is_new_net(net):           # create sets isolation only on a new net
            print(paint("  students can't reach each other's VMs (their own VMs can), unless you allow it", "dim"))
            if yes("Let students reach each other's VMs (group work)"):
                argv.append("--no-isolate")
        if yes("Should students ssh in from outside (opens port 22000+VMID)", default=True):
            argv.append("--expose")
        if size is None and yes(f"Change the size (default {CORES} cores, {MEM} MB memory, the template's disk)"):
            size = ask_size()
        if size:
            cores, mem, disk = size
            argv += ["--cores", cores, "--memory", mem] + (["--disk", disk] if disk else [])
        show_library()
        scripts = [ask("Scripts for every VM (empty: none)" if per == 1 else f"Scripts for VM -{k} (empty: none)",
                       allow_empty=True, check=check_chain) for k in range(1, per + 1)]
        if any(scripts):
            argv += ["--script", joined(scripts)]
        if "--expose" in argv:                         # create asks again before it starts
            for line in expose_warning([parse_chain(a) for a in scripts], per):
                print(paint(f"  {line}", "yellow"))
        mems = per_vm("--memory", size[1] if size else MEM, per, parse_mem)
        return argv + ask_where(net, tpl.split(","), mems)     # last: every size is known

    def list(self):
        net = ask("Show which net (number, or empty for all)", allow_empty=True,
                  check=lambda a: None if a.removeprefix("net") in self.data["nets"] else "no such net")
        argv = ["list"] + (["--net", net.removeprefix("net")] if net else [])
        return argv + (["--passwords"] if yes("Show the passwords too") else [])

    def power(self):
        action = choose("What", [("start", "start"), ("stop", "stop (clean shutdown)"), ("reboot", "reboot")])
        return [action, *self.which_vms()]

    def run(self):
        print("Run scripts as root in each VM. Snapshots are taken before and after, so it can be undone.")
        vms = self.which_vms()
        show_library()
        script = ask("Scripts (library names or files, joined with +)", check=check_chain)
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
        print("New VMs are made from templates. VMs already made are not changed by any of these.")
        inv = inventory()
        saved = [c["name"] for c in inv.values() if is_saved(c)]
        what = choose("What", [("pull", "download or update an OS template (newest image)"),
                               ("save", "save a VM as a new template (with what is installed in it)")]
                      + ([("delete", "delete a saved template")] if saved else []))
        if what == "save":
            print(paint("  set up one VM the way the class needs it (software, Docker images), then save it", "dim"))
            vm = ask("Which VM", check=valid(resolve))
            name = ask("Name for the template (like docker-lab)", check=valid(lambda a: check_template_name(a, inv)))
            return ["save-template", vm, name]
        if what == "delete":
            return ["delete-template", choose("Which template", [(n, n) for n in saved])]
        which = choose("Which template", [("all", "all of them")] + [
            (t.short, f"{t.short:<9} {t.name:<19} {template_status(t, inv)}") for t in TEMPLATES])
        return ["pull", which]

    def nodes(self):
        from .commands.node import node_list
        node_list()
        print()
        if choose("What", [("add", "add a PVE host to the cluster (checks it first)"), ("back", "back")]) != "add":
            raise Back
        print(paint("  node add checks the host first and asks before it changes anything", "dim"))
        return ["node", "add", ask("Address of the new host", check=bad_addr)]

    def destroy(self):
        print(paint("Destroy deletes VMs with their disks and snapshots. This can't be undone.", "red"))
        vms = self.which_vms()
        return ["destroy", *vms]


MENU = [
    ("create", "Create VMs for a class"),
    ("list", "List VMs (status, IPs, ssh ports, passwords)"),
    ("power", "Start / stop / reboot VMs"),
    ("run", "Run scripts in VMs"),
    ("snapshots", "Snapshot or roll back"),
    ("expose", "Let students ssh in from outside"),
    ("block", "Block / unblock internet or ssh"),
    ("isolate", "Isolate students from each other (or allow group work)"),
    ("resize", "Change CPU / memory / disk"),
    ("top", "Watch CPU / memory / network (spot busy VMs)"),
    ("handout", "Print login cards for students"),
    ("ssh", "Open a shell in a VM"),
    ("templates", "Templates: download, update, or save a VM as one"),
    ("nodes", "Nodes: add a PVE host, or see each host's free space"),
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
    try:
        importlib.import_module("readline")     # line editing in typed answers (arrow keys move the cursor)
    except ImportError:
        pass
    print(paint("pvelab", "bold") + " - student VMs on this Proxmox VE node. "
          "Esc (in lists), 'b' (in typed answers) or Ctrl-C goes back.")
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
