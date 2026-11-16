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
from .nets import every_name, net_name
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
    on; no PVE key starts with _), or None. path: its file, if the caller knows it."""
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
    return {i: c for i, c in inv.items() if is_managed(c) and desc_value(c, "net") == net}


def tag_list(tags):
    """PVE's tags (; or , or space separated) as a list."""
    return [t for t in re.split(r"[;, ]+", tags or "") if t]


def is_managed(conf):
    if not conf or conf.get("template") == "1":
        return False
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
    """Net numbers in numeric order, without "dhcp" (vmbr1) and empty ones."""
    return sorted(set(nets) - {"", "dhcp"}, key=int)


def login_of(vmid, row=None, conf=None):
    """VM vmid's login: PVE's cloud-init user (ciuser) of its config, else its credentials row's, else
    the default. conf: its config, if the caller has it."""
    conf = conf if conf is not None else read_conf(vmid) or {}
    return conf.get("ciuser") or (row or {}).get("user") or GUEST_USER


def marker_line(description):
    """The index of the description's managed-by line (its tokens: net=, owner=, ...), else 0."""
    lines = (description or "").split("\n")
    return next((k for k, l in enumerate(lines) if MARKER in l), 0)


def desc_value(conf, key, anywhere=False):
    """A token key=VALUE of the description's managed-by line (an admin's notes on other lines aside), or
    (anywhere, or no managed-by line: a template's) of the whole description."""
    desc = (conf or {}).get("description", "") or ""
    line = desc.split("\n")[marker_line(desc)] if MARKER in desc and not anywhere else desc
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
    return (desc_value(conf, "net") if conf else "") or "dhcp"


def shared_nets(man, nums):
    """Names of the nets that have their shared VM (role=shared, whatever it is called)."""
    from .owners import is_shared_vm         # owners imports this module
    return sorted({net_name(n) for c, n in zip(man.values(), nums) if n and n != "dhcp" and is_shared_vm(c, n)})


def lab_data(snaps=False):
    """What the menu and tab completion offer: VM names (and VMIDs), net names (nets: those with VMs; all_nets:
    also those without, net create's), VMs per net, the PVE hosts and
    (snaps=True, which reads every config again) snapshot names. Empty when /etc/pve can't be read (not root)."""
    try:
        man = managed()
    except OSError:
        return {"vms": [], "ids": [], "nets": [], "all_nets": [], "count": {}, "shared": [], "snaps": [], "nodes": []}
    rows = cred_rows()
    nums = [net_of(i, rows, c) for i, c in man.items()]
    return {"vms": sorted(c.get("name", "") for c in man.values()), "ids": sorted(man),
            "nets": [net_name(n) for n in sorted_nets(nums)],
            "all_nets": every_name(),                       # also without VMs, and PVE vnets not taken over
            "count": {net_name(n): nums.count(n) for n in set(nums) - {""}},
            "shared": shared_nets(man, nums),
            "snaps": sorted({s for i in man for s in snapshot_names(i)}) if snaps else [],
            "nodes": node_names()}


def vm_mac(vmid):
    m = re.search(r"=([0-9A-Fa-f:]{17})", (read_conf(vmid) or {}).get("net0", ""))
    return m.group(1).lower() if m else None
