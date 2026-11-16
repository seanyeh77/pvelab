"""`pvelab` with no arguments on a terminal: a menu that asks step by step, shows the command it
built, and runs it. Every step can be typed directly next time; the wizard teaches the command line."""

import functools
import importlib
import shlex
import subprocess
from dataclasses import dataclass
from typing import NamedTuple

from . import accounts, nets
from .args import valid_net
from .commands.create import held_problem, per_owner_on, per_vm
from .credentials import cred_rows
from .catalogue import TEMPLATES
from .config import CORES, DEFAULT_TEMPLATE, EST_DISK_GB, MEM, RUN_TIMEOUT_MAX, RUN_TIMEOUT_MIN, SNAP
from .networks import is_new_net
from .inventory import inventory, lab_data, resolve, targets
from .joining import bad_addr
from .library import library_lines
from .hosts import node_list
from .nodes import members, multi, offline_nodes, online_nodes, this_node
from .placement import bridge_host, fits, node_room
from .renaming import bad_name
from .templates import catalogue_find, check_template_name, copy_needed, is_saved, node_templates, template_status
from .resources import full_disk, parse_cores, parse_disk, parse_mem, vm_stats
from .scripts import expose_warning, parse_chain
from .form import Field, Form, fits_screen
from .ui import interactive, paint, select, select_many
from .util import concurrently, LabError, net_number, number, pvelab_argv


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
    i = select(prompt + ":", [label for _, label in options], values.index(default) if default is not None and default in values else 0)
    if i is None:
        raise Back
    return values[i]


def per_answer_check(count, net=None, have=0, students_fixed=False):
    """The check of a VMs-per-owner answer: 1-99, what net (an existing one) already has, and 99 VMs in all.
    students_fixed: the owner count can't be changed any more (the questions), so the over-99 message says how
    many VMs per owner fit, not how many owners."""
    def check(a):
        if (per := number(a, 1, 99)) is None:
            return range_text(1, 99, "VMs")
        if have and per != have:
            return f"must be {vm_count(have)} per owner, as on {net}"
        return over_99(count, per, students_fixed)
    return check


def over_99(count, per, students_fixed=False):
    """Why count owners with per VMs each are too many (create makes 99 VMs at most), or None, with what
    to lower: the owners, or (students_fixed) the VMs per owner."""
    if count * per <= 99:
        return None
    most = (f"at most {vm_count(99 // count)} per owner" if students_fixed
            else f"at most {plural(99 // per, 'owner')}")
    return f"{plural(count, 'owner')} x {vm_count(per)} is over 99 VMs: {most}"


def fit_text(n):
    """'1 owner fits', '5 owners fit'."""
    return f"{plural(n, 'owner')} {'fits' if n == 1 else 'fit'}"


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def vm_count(n):
    return plural(n, "VM")


def range_text(lo, hi, unit=""):
    """What a number answer must be: a number 1-99 VMs."""
    return f"a number {lo}-{hi}" + (f" {unit}" if unit else "")


def is_int(lo, hi, unit=""):
    """The check of a number answer lo-hi; unit: what it counts (for the message)."""
    return lambda a: None if number(a, lo, hi) is not None else range_text(lo, hi, unit)


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


def each_vm(what, parse, per, k):
    """The check of a size or scripts answer, as create reads it (per_vm): one VM's value (k: exactly one,
    no comma), or the common one (one value, or one per VM with commas)."""
    return valid(lambda a: per_vm(what, a, 1 if k else per, parse))


def show_library():
    """The script library, dimmed, above a question that asks for scripts."""
    for line in library_lines() + ["  or a file path; join several with +"]:
        print(paint(line, "dim"))


OS_OPTIONS = [(t.short, t.name) for t in TEMPLATES]


def saved_templates(inv=None):
    """Names of the templates made with save-template (each once, though a host may hold a copy).
    inv: an inventory() read already."""
    return sorted({c.get("name", "") for c in (inventory() if inv is None else inv).values() if is_saved(c)} - {""})


def os_options(inv=None):
    """The menu's OS choices: the catalogue, then the templates saved from a VM. inv: an inventory() read already."""
    return OS_OPTIONS + [(n, f"{n}  (saved template)") for n in saved_templates(inv)]


def header(vms, net_names, down):
    """The line above the main menu: how many VMs, in which nets, and how many are not running."""
    return (plural(len(vms), "VM") + (f" in {', '.join(net_names)}" if net_names else "")
            + (f"; {vm_count(down)} not running (List VMs shows why)" if down else ""))


def ask_size(of="", defaults=(CORES, MEM, "")):
    """CPU cores, memory and disk (of one VM, e.g. " of VM -2") as typed: (cores, mem, disk)."""
    return (ask(f"CPU cores{of}", default=defaults[0], check=valid(parse_cores)),
            ask(f"Memory{of} (MB, or like 4G)", default=defaults[1], check=valid(parse_mem)),
            ask(f"Disk GB{of} (empty: the template's)", default=defaults[2], allow_empty=True,
                check=valid(full_disk)))


def ask_where(net, tpls, mem):
    """create's "Where" question (2 or more hosts only): --node auto or --node NAME, as argv words, or []
    when there is nothing to ask. tpls: the template of each of an owner's VMs (aliases); mem: their MB."""
    if not multi():                          # one host, or no cluster: nothing is asked, as before
        return []
    pin = bridge_host(net)
    if pin:                                  # a plain bridge exists on one host: its VMs stay there
        print(paint(f"  {nets.net_name(net) if nets.resolve(net) else 'the new net'} is on {pin} only", "dim"))
        return []
    need, gb = sum(mem), len(mem) * EST_DISK_GB    # one owner: all their VMs, as create counts them
    room = node_room()
    print(paint(f"  {'HOST':<14} {'MEM FREE':>9} {'DISK FREE':>10}  OWNERS THAT FIT", "dim"))
    for n, r in sorted(room.items()):
        print(paint(f"  {n:<14} {r['mem'] / 1024:>6.1f} GB {r['disk']:>7} GB  {fits(r, need, gb)}", "dim"))
    node = choose("Where", [("auto", "spread by free space (recommended)"),
                            *((n, f"all on {n}") for n in sorted(room))])
    for line in copy_notes(tpls, sorted(room) if node == "auto" else [node]):
        print(paint(f"  {line}", "yellow"))
    return ["--node", node]


def copy_notes(tpls, hosts, inv=None):
    """'debian-13-cloud is copied to pve2 first (~2.1 GB)' for each of hosts that lacks a current copy of
    one of the templates tpls (the menu's OS choices). A template not built yet: nothing (create says so)."""
    return [line for _, line in copy_needs(tpls, hosts, inv)]


def copy_needs(tpls, hosts, inv=None):
    """copy_notes, each with its host: [(host, line)]."""
    inv = inventory() if inv is None else inv
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
                out.append((h, f"{name} is copied to {h} first (~{gb:.1f} GB)"))
    return out


def joined(vals):
    """One VM's value each -> one value if they are all the same, else a comma list (create's)."""
    return vals[0] if len(set(vals)) == 1 else ",".join(vals)


def check_net(a):
    """The answer to 'Network': auto, a number or netN, an existing net's name, or a new valid name."""
    if a == "auto":
        return None
    if net_number(a) is not None:
        return valid(valid_net)(a) or (held_problem(valid_net(a)) if is_new_net(valid_net(a)) else None)
    return None if nets.resolve(a) or nets.free_vnet(a) else nets.name_problem(a, "0")


def net_notes(have):
    """What the Network question says: the nets now (name, range; PVE vnets not taken over too) and how an
    answer is read."""
    listed = ", ".join([f"{name} {net}" for name, net in have]
                       + [f"{v['alias'] or v['vnet']} {v['cidr']} (PVE)" for v in nets.free_vnets()]) or "none yet"
    return [f"nets now: {listed}",
            "a new name makes a new net (its VMs are named after it); a number or name of an existing net adds to it;",
            "auto: a new net with a free number"]


def range_check(n):
    """The check of a new net's range: private, free here and on every host (n: its number, or None)."""
    return valid(lambda a: nets.cidr_free(n or None, nets.parse_cidr(a)))   # no number: no bridge to skip


def cidr_arg(a, n):
    """--cidr's value for the range answer a, or None when create would pick it anyway (empty, the default)."""
    return None if not a or (n and nets.parse_cidr(a) == nets.default_cidr(n)) else a


def ask_cidr(net, n):
    """The new net's range as typed (None: the default 10.10.N.0/24, or a name's free N, so create picks it)."""
    text = f"Address range (CIDR), empty: {nets.default_cidr(n) if n else '10.10.N.0/24, N = the lowest free number'}"
    with nets.cached_inputs():               # one read of the hosts' routes for every answer tried
        return cidr_arg(ask(text, allow_empty=True, check=range_check(n)), n)


def vm_words(answer, net_names=()):
    """What the user typed for 'which VMs' -> pvelab VM arguments: '30' -> --net 30; a net's name too."""
    out = []
    for w in answer.replace(",", " ").split():
        out += ["--net", str(n)] if (n := net_number(w)) is not None else \
            ["--net", w] if w in net_names else [w]
    return out


@dataclass
class NetInfo:
    """What a Network answer means: its number (None: auto, or a name not made yet), whether create makes
    it, the hint beside it, whether it is a net at all, and an existing net's name and VMs per owner."""
    n: str | None = None
    new: bool = True
    hint: str = ""
    valid: bool = True
    name: str | None = None
    per: int = 0


class Shape(NamedTuple):
    """What the answers make: owners, VMs per owner, whether each VM has its own answers (Per VM ticked
    and more than 1 VM per owner), the Network answer's NetInfo."""
    count: int
    per: int
    diff: bool
    info: NetInfo


class CreateForm:
    """The fields of create's form, the command they make and its warnings, from the answers (values)."""

    def __init__(self, data):
        self.data = data
        tpl = (catalogue_find(DEFAULT_TEMPLATE) or TEMPLATES[0]).short
        self.inv = inventory()                  # every VM config, read once for the form
        self.oses = os_options(self.inv)
        self.values = {"count": "20", "per": "1", "os": tpl, "diff": False, "net": "auto", "cidr": "",
                       "group": False, "expose": True, "cores": str(CORES), "mem": str(MEM), "disk": "",
                       "scripts": "", "where": "auto"}
        self.nets_now = sorted(nets.all_nets().values())
        self.multi = multi()                    # read once: the hosts don't change while the form is open
        self._nets, self._pins, self._copies, self._warnings = {}, {}, {}, {}

    @functools.cached_property
    def room(self):
        return node_room()

    @functools.cached_property
    def creds(self):
        """The credentials, read once and only when an existing net is chosen (its VMs per owner)."""
        return cred_rows()

    def pinned(self, v):
        """The one host the Network answer's VMs must be on (bridge_host), read once per answer; None for
        an answer that is not a net yet (being typed)."""
        info = self.net_info(v["net"])
        if not info.valid:                      # not a net (yet): nothing to look up
            return None
        n = info.n or v["net"]
        if n not in self._pins:
            self._pins[n] = bridge_host(n)
        return self._pins[n]

    def asks_where(self, v):
        return self.multi and not self.pinned(v)

    def shape(self, v):
        """The Shape of these answers (owners and VMs per owner: 1 while not a number 1-99)."""
        count, per = number(v["count"], 1, 99) or 1, number(v["per"], 1, 99) or 1
        diff = per > 1 and v["diff"] and count * per <= 99      # no rows for VMs over the limit (marked)
        return Shape(count, per, diff, self.net_info(v["net"]))

    @staticmethod
    def asks_range(v, info):
        """A new net named or numbered here gets a range (auto: create picks one)."""
        return info.new and v["net"] != "auto"

    def net_info(self, net):
        """NetInfo of a Network answer, worked out once per answer (it reads the host's files)."""
        if net not in self._nets:
            if net == "auto":
                info = NetInfo(hint="a new net, 10.10.N.0/24, N = the lowest free number")
            elif check_net(net):
                info = NetInfo(new=False, valid=False)    # no range or group work asked until it is a net
            else:
                n = nets.resolve(net)
                if n is not None and not is_new_net(n):
                    name = nets.net_name(n)       # lab_data counts VMs by the net's name
                    info = NetInfo(n, False, f"adds to {name} ({vm_count(self.data['count'].get(name, 0))})",
                                   name=name, per=per_owner_on(n, self.creds))
                elif n is None and (vn := nets.free_vnet(net)):     # PVE's range and gateway: none asked
                    info = NetInfo(None, False, f"PVE vnet {vn['vnet']}, {vn['cidr']}, gateway {vn['gateway']}",
                                   name=net)
                else:
                    info = NetInfo(n, hint=f"a new net: net{n}" if n else "a new net")
            self._nets[net] = info
        return self._nets[net]

    @staticmethod
    def seed(v, per):
        """Each VM's answers (Per VM) the first time they are shown, then its own: unticking Per VM shows
        the common answers again, ticking it again shows the VMs' answers as they were left."""
        for name in ("os", "cores", "mem", "disk", "scripts"):
            first = f"{name}1" not in v        # ticked for the first time, or VMs added later
            for k in range(1, per + 1):
                if f"{name}{k}" not in v:
                    v[f"{name}{k}"] = CreateForm.start(v, name, k, per, first)

    @staticmethod
    def start(v, name, k, per, first):
        """Where VM -k's answer starts: on the first tick, its part of a common answer that has one value
        per VM, else the whole common answer (still marked if it was wrong); a VM added later: VM -(k-1)'s."""
        if not first:
            return v[f"{name}{k - 1}"]
        parts = v[name].split(",")
        return parts[k - 1] if len(parts) == per else v[name]

    def fields(self, v):
        shape = self.shape(v)
        count, per, diff, info = shape.count, shape.per, shape.diff, shape.info   # count: 1 while Owners is wrong
        n, new = info.n, info.new
        out = [Field("count", "Owners", check=is_int(1, 99, "owners")),
               Field("per", "VMs per owner", check=per_answer_check(count, info.name, info.per),
                     depends=f"{count} {v['net']}",
                     hint="2 or more: each owner gets VMs -1, -2, ..." if per == 1 else "")]
        if per > 1:
            out.append(Field("diff", "Per VM", "check", text="a different OS, size or scripts for each"))
        if not diff:
            out.append(Field("os", "Operating system", "choice", self.oses))
        out.append(Field("net", "Network", check=check_net, hint=info.hint, notes=net_notes(self.nets_now)))
        if self.asks_range(v, info):
            out.append(Field("cidr", "Address range", check=range_check(n), depends=str(n),
                             empty=str(nets.default_cidr(n)) if n else "10.10.N.0/24, N = the lowest free"))
        if new:
            out.append(Field("group", "Group work", "check", text="owners reach each other's VMs"))
        out.append(Field("expose", "ssh from outside", "check", text="opens port 22000+VMID"))
        lib = library_lines() + ["  or a file path; join several with +"]
        if diff:
            self.seed(v, per)
        for k in (range(1, per + 1) if diff else [None]):
            sfx, of = (str(k), f" -{k}") if k else ("", "")
            dep = "" if k else str(per)         # one VM's value does not depend on how many there are
            if k:
                out.append(Field(f"os{k}", f"VM{of} OS", "choice", self.oses))
            out += [Field(f"cores{sfx}", f"CPU cores{of}", check=each_vm("--cores", parse_cores, per, k), depends=dep),
                    Field(f"mem{sfx}", f"Memory{of}", check=each_vm("--memory", parse_mem, per, k), depends=dep,
                          hint="MB, or like 4G"),
                    Field(f"disk{sfx}", f"Disk GB{of}", check=each_vm("--disk", full_disk, per, k), depends=dep,
                          empty="the template's")]
            each = ["  each VM its own: harden+docker,harden (a comma between VMs)"] if per > 1 and not k else []
            out.append(Field(f"scripts{sfx}", f"Scripts{of}", check=each_vm("--script", parse_chain, per, k),
                             depends=dep, notes=lib + each, empty="none"))
        if self.asks_where(v):
            room = self.room
            try:
                mems = per_vm("--memory", self.col(v, "mem", shape), per, parse_mem)
            except LabError:                    # not a size yet (UsageError too): what fits by the default
                mems = [MEM] * per
            out.append(Field("where", "Where", "choice",
                             [("auto", "spread by free space")] + [(h, f"all on {h}") for h in sorted(room)],
                             notes=[f"{h}: {r['mem'] / 1024:.1f} GB memory, {r['disk']} GB disk free;"
                                    f" {fit_text(fits(r, sum(mems), len(mems) * EST_DISK_GB))}"
                                    for h, r in sorted(room.items())]))
        return out

    @staticmethod
    def col(v, key, shape):
        """One option's value: the common answer, or (Per VM) each VM's answers joined as create takes them."""
        if shape.diff:
            return joined([v[f"{key}{k}"] for k in range(1, shape.per + 1)])
        return v[key]

    def argv(self, v):
        """The create command of the answers (ones the form shows no error for: Run is refused before)."""
        shape = self.shape(v)
        count, per, diff, info = shape.count, shape.per, shape.diff, shape.info   # read numbers: no padding
        tpl = self.col(v, "os", shape)
        argv = ["create", str(count) if per == 1 else f"{count}x{per}", tpl, "--net", v["net"]]
        if self.asks_range(v, info) and (cidr := cidr_arg(v["cidr"], info.n)):
            argv += ["--cidr", cidr]
        if info.new and v["group"]:
            argv.append("--no-isolate")
        if v["expose"]:
            argv.append("--expose")
        cores, mem, disk = (self.col(v, k, shape) for k in ("cores", "mem", "disk"))
        if diff or cores != str(CORES):
            argv += ["--cores", cores]
        if diff or mem != str(MEM):
            argv += ["--memory", mem]
        if disk.strip(","):
            argv += ["--disk", disk]
        scripts = self.col(v, "scripts", shape)
        if scripts.strip(","):
            argv += ["--script", scripts]
        if self.asks_where(v):
            argv += ["--node", v["where"]]
        return argv

    def chains(self, v, shape):
        """Each VM's script chain, as create reads the answer."""
        return per_vm("--script", self.col(v, "scripts", shape), shape.per, parse_chain)

    def preview(self, v, errors):
        if errors:
            return [("pvelab create ...  (fix the answers in yellow)", "dim")]
        shape, argv = self.shape(v), self.argv(v)
        per = shape.per
        out = [("pvelab " + shlex.join(argv), "bold")]
        if v["expose"]:
            key = (self.col(v, "scripts", shape), per)
            if key not in self._warnings:       # parse_chain looks at files: once per scripts answer
                self._warnings[key] = expose_warning(self.chains(v, shape), per)
            out += [(line, "yellow") for line in self._warnings[key]]
        if "--node" in argv:
            tpls = tuple(self.col(v, "os", shape).split(","))
            if tpls not in self._copies:      # for every host at once: lvs over ssh, not on each Where change
                self._copies[tpls] = copy_needs(tpls, sorted(self.room), self.inv)
            out += [(line, "yellow") for h, line in self._copies[tpls] if v["where"] in ("auto", h)]
        return out


check_accounts = valid(lambda a: accounts.parse(a, "net", 1))     # names checked again with the net's name


def size_and_script(argv, script_prompt):
    """argv with the size asked (if it is to change) and the scripts (the library shown first)."""
    if yes(f"Change the size (default {CORES} cores, {MEM} MB memory, the template's disk)"):
        cores, mem, disk = ask_size()
        argv = argv + ["--cores", cores, "--memory", mem] + (["--disk", disk] if disk else [])
    show_library()
    script = ask(script_prompt, allow_empty=True, check=check_chain)
    return argv + (["--script", script] if script else [])


def ask_range(cidr_prompt, gw_prompt):
    """A CIDR and a gateway typed (either may be left empty): their options for net create / set."""
    cidr = ask(cidr_prompt, allow_empty=True, check=valid(nets.parse_cidr))
    gw = ask(gw_prompt, allow_empty=True, check=valid(lambda a: nets.parse_gateway(a, nets.parse_cidr(cidr)))
             if cidr else None)              # no CIDR: the net's own range, checked by the command
    return (["--cidr", cidr] if cidr else []) + (["--gateway", gw] if gw else [])


class Wizard:
    def __init__(self, data):
        self.data = data                      # inventory.lab_data(): vms, nets, count {net: VMs}
        self.confirmed = False                # the form's Run: execute does not ask again

    def which_vms(self, what="Which VMs", default=None):
        """VM arguments: a whole net, all, or VMs ticked in a list (typed without a terminal, or when asked)."""
        if not interactive():
            return self.typed_vms(what, default)
        vms = self.vm_names()
        known = self.data["nets"]
        options = ([(vm_words(n, known), f"{n}: all {vm_count(self.data['count'].get(n, 0))}") for n in known]
                   + [(["all"], f"all {vm_count(len(vms))}"), ("some", "some VMs: tick them in a list"),
                      ("type", "type names or a range (net30-user01..05)")])
        got = choose(what, options, default=["all"] if default == "all" else None)
        if got == "type":
            return self.typed_vms(what, default)
        if got == "some":
            picked = select_many(what, vms)
            if picked is None:
                raise Back
            return [vms[i] for i in picked]
        return got

    def typed_vms(self, what, default):
        listed = ", ".join(f"{n} ({vm_count(self.data['count'].get(n, 0))})" for n in self.data["nets"]) or "none yet"
        print(paint(f"  networks: {listed}", "dim"))
        print(paint("  answer with: a net number (30) or name, VM names, a range (net30-user01..05), or all", "dim"))
        known = self.data["nets"]
        return vm_words(ask(what, default=default, check=valid(lambda a: targets(vm_words(a, known)))), known)

    def vm_names(self):
        """The pvelab VMs, sorted; none: back to the menu."""
        vms = sorted(self.data["vms"])
        if not vms:
            print("No VMs yet - create VMs first.")
            raise Back
        return vms

    def one_vm(self, prompt):
        """One VM, picked from a list of the pvelab VMs (typing filters it), or any other VM typed."""
        vms = sorted(self.data["vms"])
        got = choose(prompt, [(v, v) for v in vms] + [(None, "another VM: type its name")]) if vms else None
        return got or ask("VM name", check=valid(resolve))

    # --- one method per menu entry: each returns the pvelab arguments to run (or a list of them)

    def create(self):
        """The inline form on a terminal tall enough for it, else the questions one by one."""
        if fits_screen(22):
            return self.create_form()
        return self.create_questions()

    def create_form(self):
        """Create as one form: every answer on screen, any of them changed in any order, the command
        and its warnings below. Returns the arguments; Esc raises Back."""
        form = CreateForm(self.data)
        with nets.cached_inputs():              # the hosts' routes read once for every range tried
            ok = Form("Create VMs (each owner gets VMs and a random password)",
                      form.fields, form.preview, form.values, run_label="Create VMs").run()
        if not ok:
            raise Back
        self.confirmed = True
        return form.argv(form.values)

    def create_questions(self):
        print("Create VMs. Each owner gets one or more VMs with a random password.")
        owners = number(ask("How many owners", default=20, check=is_int(1, 99, "owners")), 1, 99)
        per = number(ask("VMs per owner", default=1, check=per_answer_check(owners, students_fixed=True)), 1, 99)
        tpl = (catalogue_find(DEFAULT_TEMPLATE) or TEMPLATES[0]).short
        oses = os_options()
        size = None                                     # (cores, memory, disk), if asked for
        if per > 1 and yes("Different OS or size for each of an owner's VMs"):
            specs, prev = [], (tpl, CORES, MEM, "")     # each VM's defaults: the previous VM's answers
            for k in range(1, per + 1):
                prev = (choose(f"Operating system of VM -{k}", oses, default=prev[0]),
                        *ask_size(f" of VM -{k}", prev[1:]))
                specs.append(prev)
            tpl, *size = (joined(col) for col in zip(*specs))
        else:
            tpl = choose("Operating system", oses, default=tpl)
        for line in net_notes(sorted(nets.all_nets().values())):
            print(paint(f"  {line}", "dim"))
        net = ask("Network", default="auto", check=check_net)
        n = nets.resolve(net) if net != "auto" else None
        pve = n is None and net != "auto" and nets.free_vnet(net) is not None      # a PVE vnet: its own range
        new = net == "auto" or n is None or is_new_net(n)
        cidr = ask_cidr(net, n) if new and net != "auto" and not pve else None
        argv = ["create", str(owners) if per == 1 else f"{owners}x{per}", tpl, "--net", net] + (["--cidr", cidr] if cidr else [])
        if new:                                        # create sets isolation only on a new net
            print(paint("  owners can't reach each other's VMs (their own VMs can), unless allowed (--no-isolate)", "dim"))
            if yes("Let owners reach each other's VMs (group work)"):
                argv.append("--no-isolate")
        if yes("Should users ssh in from outside (opens port 22000+VMID)", default=True):
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
        return argv + ask_where(n or net, tpl.split(","), mems)     # last: every size is known

    @staticmethod
    def net_key(a):
        """The net a (number, netN, name, dhcp or its label vmbr1(dhcp)) means: 'dhcp', a number or None."""
        from . import nets
        return "dhcp" if a in ("dhcp", nets.net_name("dhcp")) else nets.resolve(a)

    def has_net(self, a):
        """True if a is one of the nets that have VMs."""
        n = self.net_key(a)
        return n is not None and any(self.net_key(x) == n for x in self.data["nets"])

    def list(self):
        net = ask("Show which net (number or name, empty for all)", allow_empty=True,
                  check=lambda a: None if self.has_net(a) else "no such net")
        net = "dhcp" if net and self.net_key(net) == "dhcp" else net
        argv = ["list"] + (["--net", net] if net else [])
        return argv + (["--passwords"] if yes("Show the passwords too") else [])

    def power(self):
        action = choose("What", [("start", "start"), ("stop", "stop (clean shutdown)"), ("reboot", "reboot")])
        return [action, *self.which_vms()]

    def run(self):
        print("Run scripts as root in each VM. Snapshots are taken before and after, so it can be undone.")
        vms = self.which_vms()
        show_library()
        script = ask("Scripts (library names or files, joined with +)", check=check_chain)
        timeout = ask("Give up after how many minutes", default=RUN_TIMEOUT_MIN, check=is_int(1, RUN_TIMEOUT_MAX, "minutes"))
        return ["run", *vms, script] + (["--timeout", timeout] if timeout != str(RUN_TIMEOUT_MIN) else [])

    def snapshots(self):
        what = choose("What", [("snapshot", f"save the current state as '{SNAP}' (replaces the old one)"),
                               ("rollback", "go back to a snapshot (changes since are lost)")])
        vms = self.which_vms()
        if what == "snapshot":
            return ["snapshot", *vms]
        snaps = lab_data(snaps=True)["snaps"] or [SNAP]
        print(paint(f"  snapshots on the VMs: {' '.join(snaps)}", "dim"))
        snap = ask("Back to which snapshot", default=SNAP)
        return ["rollback", *vms, "--to", snap]

    def net_switch(self, cmd, prompt, on_label, off_label):
        """expose / isolate: pick a net, then on or off."""
        if not self.data["nets"]:
            print("No networks yet - create VMs first.")
            raise Back
        net = choose("Which net", [(n, n) for n in self.data["nets"]])
        on = choose(prompt, [("on", on_label), ("off", off_label)], default="on")
        return [cmd, "--net", net] + (["--off"] if on == "off" else [])

    def expose(self):
        return self.net_switch("expose", "ssh from outside", "open", "close")

    def isolate(self):
        return self.net_switch("isolate", "Between owners", "isolated: each owner reaches only their own VMs",
                               "open: all VMs of the net reach each other")

    def block(self):
        verb = choose("What", [("block", "block (cut off)"), ("unblock", "unblock (allow again)")])
        kind = choose("Which access", [("internet", "internet (the VMs can't reach out)"),
                                       ("ssh", "ssh from outside (users can't log in)"), ("all", "both")])
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
        if yes("Reboot running VMs now so it takes effect (logged-in users get disconnected)"):
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
        return ["ssh", self.one_vm("Which VM")]

    def templates(self):
        print("New VMs are made from templates. VMs already made are not changed by any of these.")
        inv = inventory()
        saved = saved_templates(inv)
        what = choose("What", [("pull", "download or update an OS template (newest image)"),
                               ("save", "save a VM as a new template (with what is installed in it)")]
                      + ([("delete", "delete a saved template")] if saved else []))
        if what == "save":
            print(paint("  set up one VM the way every new VM needs it (software, Docker images), then save it", "dim"))
            vm = self.one_vm("Which VM")
            name = ask("Name for the template (like docker-lab)", check=valid(lambda a: check_template_name(a, inv)))
            return ["save-template", vm, name]
        if what == "delete":
            return ["delete-template", choose("Which template", [(n, n) for n in saved])]
        which = choose("Which template", [("all", "all of them")] + [
            (t.short, f"{t.short:<9} {t.name:<19} {template_status(t, inv)}") for t in TEMPLATES])
        return ["pull", which]

    def nodes(self):
        node_list()
        print()
        gone = [n for n in offline_nodes() if n != this_node()]     # only an offline host can be removed
        what = choose("What", [("add", "add a PVE host to the cluster (checks it first)"),
                               ("rename", "rename a host (it reboots once; its running VMs start again)")]
                      + ([("remove", "remove a host that is gone for good (offline: " + ", ".join(gone) + ")")]
                         if gone else []) + [("back", "back")])
        if what == "back":
            raise Back
        if what == "rename":
            hosts = sorted(members())
            old = hosts[0] if len(hosts) == 1 else choose("Which host", [(n, n) for n in hosts], default=this_node())
            print(paint(f"  renaming {old}: node rename checks first and shows the plan before it changes anything",
                        "dim"))
            new = ask("New name", check=lambda a: bad_name(a) or (
                f"'{a}' is already a host of this cluster" if a in members() else None))
            return ["node", "rename", old, new]
        if what == "remove":
            print(paint("  node remove checks the host first and asks before it changes anything", "dim"))
            return ["node", "remove", choose("Which host", [(n, n) for n in gone])]
        print(paint("  node add checks the host first and asks before it changes anything", "dim"))
        return ["node", "add", ask("Address of the new host", check=bad_addr)]

    def networks(self):
        """net create / remove: a network on its own (its CIDR and gateway chosen)."""
        from .commands.netcmd import net_list
        net_list()
        print()
        names = self.data.get("all_nets", [])   # with or without VMs (net create makes ones without)
        what = choose("What", [("create", "make a network before its VMs (name, CIDR, gateway)")]
                      + ([("set", "change a network's CIDR or gateway (its VMs are kept; running ones reboot)"),
                          ("rename", "rename a network (its VMs' names stay, or follow it)"),
                          ("remove", "remove a network that has no VMs")] if names else []) + [("back", "back")])
        if what == "back":
            raise Back
        if what == "set":
            net = choose("Which network", [(n, n) for n in names])
            opts = ask_range("New CIDR (empty: keep it)", "New gateway (an address in it, or .254; empty: keep its place)")
            if not opts:
                print("Nothing to change.")
                raise Back
            return ["net", "set", net] + opts
        if what == "remove":
            return ["net", "remove", choose("Which network", [(n, n) for n in names])]
        if what == "rename":
            net = choose("Which network", [(n, n) for n in names])
            new = ask("New name", check=lambda a: nets.name_problem(a, None))
            vms = choose("Its VMs' names", [("keep", "keep them (no reboot)"),
                                            ("rename", f"{net}-... -> {new}-... (running VMs reboot)")])
            return ["net", "rename", net, new] + (["--rename-vms"] if vms == "rename" else [])
        name = ask("Name (like prod)", check=lambda a: nets.name_problem(a, None))
        return ["net", "create", name] + ask_range("CIDR (like 10.5.0.0/24; empty: 10.10.N.0/24)",
                                                   "Gateway (an address in it, or .254; empty: .1)")

    def roster(self):
        """create --accounts: VMs from a list of accounts (each owner's VM name, user and password)."""
        print("A list of accounts: a CSV file, one owner per line, with the columns name,user,password")
        print("(name and password may be empty: the VM is NET-USER, the password random), or typed inline as")
        print("user:password,user,... One line, one owner.")
        src = ask("File (or the list itself)", check=check_accounts)
        for line in net_notes(sorted(nets.all_nets().values())):
            print(paint(f"  {line}", "dim"))
        net = ask("Network", check=lambda a: "auto has no name for the VMs: give the net's name or number"
                  if a == "auto" else check_net(a))
        n = nets.resolve(net)
        try:                                  # every name and user checked now, with the net's name
            n_acc = len(accounts.parse(src, nets.net_name(n) if n else net, 1))
        except LabError as e:
            print(paint(str(e), "yellow"))
            raise Back
        per = int(ask("VMs per owner", default="1", check=per_answer_check(n_acc, students_fixed=True)))
        tpl = choose("Operating system", os_options(), default=(catalogue_find(DEFAULT_TEMPLATE) or TEMPLATES[0]).short)
        argv = ["create", f"{n_acc}x{per}" if per > 1 else str(n_acc), tpl, "--net", net, "--accounts", src]
        if yes("Should users ssh in from outside (opens port 22000+VMID)", default=True):
            argv.append("--expose")
        return size_and_script(argv, "Scripts for every VM (empty: none)")

    def rename(self):
        """rename: one VM's new name."""
        return ["rename", self.one_vm("Which VM"), ask("New name", check=accounts.vm_name_problem)]

    def passwords(self):
        """passwd: check whether users changed their passwords, or give new ones."""
        lab_nets = [n for n in self.data["nets"] if self.net_key(n) != "dhcp"]
        if not lab_nets:
            print("No networks yet - create VMs first.")
            raise Back
        what = choose("What", [("check", "check: did users change their passwords in the VMs?"),
                               ("set", "new passwords (an owner's VMs share one)"), ("back", "back")])
        if what == "back":
            raise Back
        net = choose("Which net", [(c, c) for c in lab_nets])
        if what == "check":
            return ["passwd", "--check", "--net", net]
        who = ask("Which VMs (empty: all of the net)", allow_empty=True)
        pw = ask("The password (empty: a random one each)", allow_empty=True)
        argv = ["passwd", *(who.split() if who else ["--net", net])] + (["--password", pw] if pw else [])
        return argv + (["--keep-changed"] if yes("Leave alone owners who changed theirs") else [])

    def shared(self):
        """create --shared: one VM on a net that every owner of it reaches."""
        lab_nets = [n for n in self.data["nets"] if self.net_key(n) != "dhcp"]
        if not lab_nets:
            print("No networks yet - create VMs first.")
            raise Back
        free = [n for n in lab_nets if n not in self.data.get("shared", [])]
        if not free:
            print("Every network has its shared VM already (" + ", ".join(lab_nets) + ").")
            raise Back
        print("A shared VM (NET-shared) is one more VM on a net that every owner reaches, even when")
        print("owners can't reach each other: a Git or CI server. Its password is not on the handout.")
        net = choose("Which net", [(n, f"{n}  -> {n}-shared") for n in free])
        tpl = choose("Operating system", os_options(), default=(catalogue_find(DEFAULT_TEMPLATE) or TEMPLATES[0]).short)
        return size_and_script(["create", "--shared", tpl, "--net", net], "Scripts for it (empty: none)")

    def move(self):
        print("Move VMs to another host (to balance the hosts, or to empty one). Only SDN nets (labN) can move;")
        print("an owner's VMs move together. Their snapshots are deleted; the plan shows everything first.")
        vms = self.which_vms()
        to = choose("To which host", [(n, n) for n in online_nodes()])
        return ["move", *vms, "--to", to]

    def destroy(self):
        print(paint("Destroy deletes VMs with their disks and snapshots. This can't be undone.", "red"))
        vms = self.which_vms()
        return ["destroy", *vms]


MENU = [
    ("create", "Create VMs"),
    ("roster", "Create VMs from a list of accounts (each owner's name, user and password)"),
    ("shared", "Add a shared VM to a network (a Git or CI server every owner reaches)"),
    ("list", "List VMs (status, IPs, ssh ports, passwords)"),
    ("power", "Start / stop / reboot VMs"),
    ("run", "Run scripts in VMs"),
    ("snapshots", "Snapshot or roll back"),
    ("passwords", "Passwords: check whether users changed theirs, or give new ones"),
    ("rename", "Rename a VM (its hostname too)"),
    ("expose", "Let users ssh in from outside"),
    ("block", "Block / unblock internet or ssh"),
    ("isolate", "Isolate owners from each other (or allow group work)"),
    ("resize", "Change CPU / memory / disk"),
    ("top", "Watch CPU / memory / network (spot busy VMs)"),
    ("handout", "Print login cards"),
    ("ssh", "Open a shell in a VM"),
    ("templates", "Templates: download, update, or save a VM as one"),
    ("networks", "Networks: make a network with its own CIDR and gateway, change, rename or remove one"),
    ("nodes", "Nodes: add, rename or remove a PVE host, or see each host's free space"),
    ("move", "Move VMs to another host"),
    ("destroy", "Destroy VMs"),
]


def menu():
    """MENU as this cluster shows it: "Move VMs to another host" only with 2 or more hosts."""
    return [e for e in MENU if e[0] != "move" or multi()]


def run_pvelab(argv):
    """Run pvelab argv as a child (its own lock, exit code and Ctrl-C) and return its exit code."""
    try:      # PVELAB_WIZARD_STEP: no repeated start-up warnings
        return subprocess.run(pvelab_argv(*argv, env=["PVELAB_WIZARD_STEP=1"])).returncode
    except KeyboardInterrupt:
        return 130


def execute(argv, confirmed=False):
    """Show what argv would do (--dry-run, for commands that have it), the command line, then run it.
    confirmed: the form's Run was the answer, so Run it is not asked."""
    from .cli import DRY_RUN
    if argv[0] in DRY_RUN:
        print()
        run_pvelab(argv + ["--dry-run"])
    print("\n  " + paint("pvelab " + shlex.join(argv), "bold") + paint("   <- you can type this directly next time", "dim"))
    if not confirmed and not yes("Run it", default=True):
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
    print(paint("pvelab", "bold") + " - VMs on this Proxmox VE node or cluster. "
          "Typing filters lists; Esc (in typed answers: 'b') goes back.")
    entries = menu() + [("help", "Help on all commands"), ("quit", "Quit")]
    last, stale = 0, True
    while True:
        if stale:                           # again only after a command ran, not after help or back
            data, stats = concurrently(lab_data, vm_stats)    # the configs, and pvesh, at once
            down = sum(1 for i in data["ids"] if stats.get(i, {}).get("status", "running") != "running")
            stale = False
        print("\n" + header(data["vms"], data["nets"], down))
        last = select("What do you want to do?", [label for _, label in entries], last, esc="quit")
        key = entries[last][0] if last is not None else "quit"
        if key == "quit":
            return
        if key == "help":
            run_pvelab(["help"])
            continue
        print()
        try:
            w = Wizard(data)
            argv = getattr(w, key)()
            stale = True
            execute(argv, w.confirmed)
        except Back:
            print("(back to the menu)")
            continue
        try:
            print(paint("\nPress Enter for the menu ", "dim"), end="", flush=True)   # editline drops ESC
            input()                                                        # from an input() prompt
        except (EOFError, KeyboardInterrupt):
            print()
            return
