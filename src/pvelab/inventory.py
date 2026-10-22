"""Which VMs pvelab manages, and turning VM arguments (names, --net N, ranges, patterns, all) into VMIDs."""

import difflib
import fnmatch
import glob
import os
import re
import urllib.parse

from .config import CONF_DIR, MARKER, TAG
from .credentials import cred_rows
from .helptext import bad_usage
from .args import no_bare_net, opt, valid_net
from .util import die, run


def read_conf(vmid):
    """Main section of a VM config: dict of keys + 'description' (decoded), or None."""
    d = {"description": ""}
    desc = []
    try:
        lines = open(f"{CONF_DIR}/{vmid}.conf").read().splitlines()
    except FileNotFoundError:
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
    return d


def inventory():
    """{vmid: conf} for every VM on this node."""
    out = {}
    for f in glob.glob(f"{CONF_DIR}/*.conf"):
        vmid = int(os.path.basename(f)[:-5])
        out[vmid] = read_conf(vmid)
    return dict(sorted(out.items()))


def is_managed(conf):
    if not conf or conf.get("template") == "1":
        return False
    tags = re.split(r"[;, ]+", conf.get("tags", ""))
    return TAG in tags and MARKER in conf["description"]


def managed():
    return {i: c for i, c in inventory().items() if is_managed(c)}


def vm_exists(vmid):
    """VMID in use anywhere in the cluster (VM or container)."""
    return bool(glob.glob(f"/etc/pve/nodes/*/qemu-server/{vmid}.conf")
                or glob.glob(f"/etc/pve/nodes/*/lxc/{vmid}.conf"))


def vm_status(vmid):
    return run("qm", "status", vmid).split()[-1]


def resolve(name):
    inv = inventory()
    for i, c in inv.items():
        if c.get("name") == name and is_managed(c):
            return i
    if any(c.get("name") == name for c in inv.values()):
        die(f"'{name}' exists but was not created by lab - refusing to touch it")
    die(f"no student VM named '{name}'"
        + did_you_mean(name, [c.get("name", "") for c in inv.values() if is_managed(c)]))


def did_you_mean(word, choices):
    close = difflib.get_close_matches(word, choices, n=3, cutoff=0.75)
    return f" - did you mean {' or '.join(close)}?" if close else ""


def expand_range(a):
    """'net30-student01..05' (or ..net30-student05) -> the names in between, or None if a is no range."""
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
    it = iter(args)
    for a in it:
        no_bare_net(a)
        n = opt(a, it, "--net")
        if n is not None:
            n = valid_net(n)
            ids = {i for i, c in man.items() if rows.get(i, {}).get("net", desc_value(c, "net")) == n}
            if not ids:
                die(f"no student VMs on --net {n}")
            out |= ids
        elif (names := expand_range(a)) is not None:
            byname = {c.get("name"): i for i, c in man.items()}
            missing = [x for x in names if x not in byname]
            if missing:
                die(f"range {a}: no student VM named {', '.join(missing)}")
            out |= {byname[x] for x in names}
        elif a == "all":
            out |= set(man)
        elif any(c.get("name") == a for c in man.values()):
            out |= {i for i, c in man.items() if c.get("name") == a}
        elif any(ch in a for ch in "*?["):
            ids = {i for i, c in man.items() if fnmatch.fnmatchcase(c.get("name", ""), a)}
            if not ids:
                die(f"no student VM matches '{a}'")
            out |= ids
        else:
            out.add(resolve(a))              # explains unknown / not-ours names
    return sorted(out)


def desc_value(conf, key):
    m = re.search(rf"{key}=(\S+)", conf["description"])
    return m.group(1) if m else ""
