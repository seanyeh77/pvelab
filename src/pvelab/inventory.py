"""Which VMs pvelab manages, and turning VM arguments (names, --net N, ranges, patterns, all) into VMIDs."""

import fnmatch
import os
import re
import urllib.parse

from .config import CONF_DIR, GUEST_USER, LEGACY_TAGS, MARKER, NODES_DIR, TAG
from .credentials import cred_rows
from .helptext import bad_usage
from .args import net_arg, no_bare_net, opt
from .nodes import conf_files, guest_conf_files, node_names, node_of, on_node, qm_argv, this_node
from .nets import all_nets, every_name, net_name
from . import registry, zones
from .util import concurrently, did_you_mean, die, run


def conf_path(vmid):
    """VM vmid's config file: on this node, else on whichever node has it; None if there is none."""
    local = f"{CONF_DIR}/{vmid}.conf"
    if os.path.exists(local):
        return local
    found = conf_files(vmid)
    return found[0] if found else None


def read_conf(vmid, path=None):
    """Main section of a VM config: dict of keys + 'description' (decoded) + '_node' (the node it is
    on; no PVE key starts with _) + '_vmid' + '_rec' (its registry record, when it has one), or None. path: its file, if the caller knows it."""
    d, desc = {}, []
    path = path or conf_path(vmid)
    try:
        lines = open(path).read().splitlines() if path else None
    except FileNotFoundError:
        lines = None
    if lines is None:
        return None
    for line in lines:
        if line.startswith("["):              # snapshots/pending sections
            break
        if line.startswith("#"):
            desc.append(urllib.parse.unquote(line[1:]))
            continue
        k, _, v = line.partition(": ")
        d[k] = v
    d["description"] = "\n".join(desc)
    d["_node"] = path_node(path)
    d["_vmid"] = vmid
    if (rec := registry.lookup(vmid, d)) is not None:       # pvelab's record of it (a VM pvelab made)
        d["_rec"] = rec
    return d


def path_node(path):
    """The node of a config file: <node> in NODES_DIR/<node>/qemu-server/, else (CONF_DIR) this one."""
    if path.startswith(NODES_DIR + "/"):
        return path.split("/")[-3]
    return this_node()


def inventory():
    """{vmid: conf} for every VM on every node; conf["_node"] is its node."""
    out = {}
    for f in conf_files():
        vmid = int(os.path.basename(f)[:-5])
        out[vmid] = read_conf(vmid, f)
    return dict(sorted(out.items()))


def on_net(net, inv):
    """{vmid: config} of pvelab's VMs on net, from inventory() inv."""
    return {i: c for i, c in inv.items() if is_managed(c) and vm_value(c, "net") == net}


def tag_list(tags):
    """PVE's tags (; or , or space separated) as a list."""
    return [t for t in re.split(r"[;, ]+", tags or "") if t]


def is_managed(conf):
    """Whether pvelab made this VM: it has a record (registry), or (made before the registry, not migrated
    yet) pvelab's tag and its managed-by line."""
    if not conf or conf.get("template") == "1":
        return False
    if "_rec" in conf:
        return True
    tags = tag_list(conf.get("tags", ""))
    return (TAG in tags or any(t in tags for t in LEGACY_TAGS)) and MARKER in conf.get("description", "")


def managed(inv=None):
    """The VMs pvelab made. inv: an inventory() the caller already read."""
    return {i: c for i, c in (inv or inventory()).items() if is_managed(c)}


def vm_exists(vmid):
    """VMID in use anywhere in the cluster (VM or container)."""
    return bool(guest_conf_files(vmid))


def vm_status(vmid):
    """running / stopped / ... right now. (resources.vm_stats() is cheaper but can lag ~10 s behind a
    start or stop - fine to show, not to decide on.)"""
    return run(*qm_argv(vmid, "status", vmid)).split()[-1]


def vm_statuses(ids):
    """{vmid: running / stopped / ...} right now for many VMs: one `qm list` per node that has some of
    them (same live status as qm status), all at the same time. One node: one call, as before."""
    if not ids:
        return {}
    by_node = {}
    for i in ids:
        by_node.setdefault(node_of(i), []).append(i)
    st = {}
    for out in concurrently(*[lambda n=n: run(*on_node(n, "qm", "list")) for n in by_node]):
        st.update({int(f[0]): f[2] for f in (l.split() for l in out.splitlines()[1:]) if len(f) > 2})
    return {i: st.get(i, "unknown") for i in ids}


def resolve(name):
    inv = inventory()
    for i, c in inv.items():
        if c.get("name") == name and is_managed(c):
            return i
    if any(c.get("name") == name for c in inv.values()):
        die(f"'{name}' exists but was not created by lab - refusing to touch it")
    die(f"no pvelab VM named '{name}'"
        + did_you_mean(name, [c.get("name", "") for c in inv.values() if is_managed(c)]))


def expand_range(a):
    """'net30-user01..05' (or ..net30-user05) -> the names in between, or None if a is no range."""
    m = re.fullmatch(r"(.*?)(\d+)\.\.(?:\1)?(\d+)", a)
    if not m:
        return None
    pre, lo, hi = m.group(1), m.group(2), m.group(3)
    if int(hi) < int(lo):
        bad_usage(f"range '{a}' goes backwards")
    return [f"{pre}{k:0{len(lo)}d}" for k in range(int(lo), int(hi) + 1)]


def targets(args):
    """<name|--net N|name01..05|pattern|all>... -> VMIDs of managed VMs, in VMID order."""
    if not args:
        bad_usage("which VM(s)?")
    man, rows, out = managed(), cred_rows(), set()
    byname = {}
    for i, c in man.items():
        byname.setdefault(c.get("name"), set()).add(i)
    it = iter(args)
    for a in it:
        no_bare_net(a)
        n = opt(a, it, "--net")
        if n is not None:
            n = net_arg(n)
            ids = {i for i, c in man.items() if net_of(i, rows, c) == n}
            if not ids:
                die(f"no pvelab VMs on --net {n}")
            out |= ids
        elif (names := expand_range(a)) is not None:
            missing = [x for x in names if x not in byname]
            if missing:
                die(f"range {a}: no pvelab VM named " + (", ".join(missing) if len(missing) <= 3 else
                    f"{missing[0]} .. {missing[-1]} ({len(missing)} of the {len(names)} names)"))
            out |= set().union(*(byname[x] for x in names))
        elif a == "all":
            out |= set(man)
        elif a in byname:
            out |= byname[a]
        elif any(ch in a for ch in "*?["):
            ids = {i for i, c in man.items() if fnmatch.fnmatchcase(c.get("name", ""), a)}
            if not ids:
                die(f"no pvelab VM matches '{a}'")
            out |= ids
        else:
            out.add(resolve(a))              # explains unknown / not-ours names
    return sorted(out)


def vm_names(ids, confs=None):
    """{vmid: VM name} (the VMID if a config is gone or has no name). confs: configs already read."""
    confs = confs or {}
    return {i: ((confs.get(i) or read_conf(i)) or {}).get("name") or str(i) for i in ids}


def sorted_nets(nets):
    """Net numbers in numeric order, then external bridges by name; without "dhcp" (vmbr1) and empty ones."""
    return sorted(set(nets) - {"", "dhcp"}, key=lambda n: (0, int(n), "") if n.isdigit() else (1, 0, n))


def has_cloudinit(conf):
    """Whether a VM config has a cloud-init drive (an ide/sata/scsi disk that is one)."""
    return any(re.fullmatch(r"(ide|sata|scsi)\d+", k) and "cloudinit" in str(v) for k, v in (conf or {}).items())


def login_of(vmid, row=None, conf=None):
    """VM vmid's login: PVE's cloud-init user (ciuser) of its config when a cloud-init drive applies it (an
    adopted VM without one: what adopt recorded), else its credentials row's, else the default. conf: its
    config, if the caller has it."""
    conf = conf if conf is not None else read_conf(vmid) or {}
    return (conf.get("ciuser") if has_cloudinit(conf) else None) or (row or {}).get("user") or GUEST_USER


def marker_line(description):
    """The index of the description's managed-by line (its tokens: net=, owner=, ...), else 0."""
    lines = (description or "").split("\n")
    return next((k for k, l in enumerate(lines) if MARKER in l), 0)


def vm_value(conf, key):
    """What pvelab knows of a VM: field key of its record ("" when the record has none); a VM without one
    (made before the registry): the token key=VALUE of its description's managed-by line (an admin's notes
    on other lines aside), or (no managed-by line: a template's) of the whole description."""
    if "_rec" in (conf or {}):
        value = conf["_rec"].get(key)
        return "" if value is None else str(value)
    desc = (conf or {}).get("description", "") or ""
    line = desc.split("\n")[marker_line(desc)] if MARKER in desc else desc
    m = re.search(rf"(?:^|\s){re.escape(key)}=(\S+)", line)
    return m.group(1) if m else ""


def snapshot_names(vmid):
    path = conf_path(vmid)
    try:
        found = re.findall(r"^\[([^\]]+)\]$", open(path).read(), re.M) if path else []
    except FileNotFoundError:
        return []
    # not snapshots: [PENDING] (PVE's queued changes) and [special:cloudinit] (what cloud-init last got)
    return [n for n in found if n.lower() != "pending" and not n.startswith("special:")]


def net_of(vmid, rows, conf=None):
    """The net a VM is on: its credentials row, else its config's description, else dhcp.
    conf: its config, if the caller has read it already (read here only for a VM without a row)."""
    if rows.get(vmid, {}).get("net"):
        return rows[vmid]["net"]
    conf = conf or read_conf(vmid)
    return (vm_value(conf, "net") if conf else "") or "dhcp"


def shared_numbers(man, nums):
    """The net numbers that have their shared VM (role=shared, whatever it is called); nums: man's nets."""
    from .owners import is_shared_vm         # owners imports this module
    return {n for c, n in zip(man.values(), nums) if n and n != "dhcp" and is_shared_vm(c, n)}


def shared_nets(man, nums):
    """Names of the nets that have their shared VM."""
    return sorted(net_name(n) for n in shared_numbers(man, nums))


def lab_data(snaps=False, strays=False):
    """What the menu and tab completion offer: VM names (and VMIDs), net names (nets: those with VMs; all_nets:
    also those without, net create's), VMs per net, the PVE hosts, PVE's SDN zones, (strays=True) the VMs made
    by hand that pvelab adopt takes, and (snaps=True, which reads every config again) snapshot names. Empty when
    /etc/pve can't be read (not root)."""
    try:
        inv = inventory()
        man = managed(inv)
    except OSError:
        return {"vms": [], "ids": [], "nets": [], "all_nets": [], "count": {}, "shared": [], "snaps": [], "nodes": [],
                "zones": [], "strays": []}
    rows = cred_rows()
    nums = [net_of(i, rows, c) for i, c in man.items()]
    found = all_nets()
    if strays:
        from .adopting import adoptable      # adopting imports this module
    return {"vms": sorted(c.get("name", "") for c in man.values()), "ids": sorted(man),
            "nets": [net_name(n) for n in sorted_nets(nums)],
            "all_nets": every_name(found),                  # also without VMs, and PVE vnets not taken over
            "count": {net_name(n): nums.count(n) for n in set(nums) - {""}},
            "shared": shared_nets(man, nums),
            "snaps": sorted({s for i in man for s in snapshot_names(i)}) if snaps else [],
            "nodes": node_names(),
            "zones": sorted(zones.zones()),
            "strays": list(adoptable(inv)) if strays else []}


MAC = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}")


def pve_opts(value):
    """{key: value} of a PVE option string (k=v,k=v,..., such as a network card's or ipconfig0's), in its order; a
    part without = has the value ""."""
    return dict(p.partition("=")[::2] for p in str(value or "").split(",") if p)


def pve_value(opts):
    """The PVE option string of opts ({key: value}, as pve_opts gives): pve_opts' inverse."""
    return ",".join(f"{k}={v}" if v else k for k, v in opts.items())


def nic(value):
    """(bridge, MAC) of a network card's config value (MODEL=MAC,bridge=BR,...; the MAC lower case), None for
    what it lacks."""
    opts = pve_opts(value)
    mac = next((v for k, v in opts.items() if k != "bridge" and MAC.fullmatch(v)), None)
    return opts.get("bridge") or None, mac.lower() if mac else None


def net0(conf):
    """(bridge, MAC) of a VM config's net0 (see nic)."""
    return nic((conf or {}).get("net0"))


def vm_mac(vmid):
    return net0(read_conf(vmid))[1]
