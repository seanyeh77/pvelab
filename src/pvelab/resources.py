"""CPU / memory / disk sizes, storage, and host resource checks."""

import json
import re
import sys

from .config import CPU_HIGH, EST_DISK_GB, FORCE, HOST_RESERVE_MB, STORAGE, STORAGE_FULL_PCT
from .helptext import bad_usage
from .nodes import is_local, multi, this_node
from .util import concurrently, die, parallel, run, warn


def storage(node=None):
    """(percent used, GB free) of STORAGE on this host (node: another one), or (None, None). PVE's API
    answers for this host too."""
    return storage_numbers(storage_status(node or this_node()))


def storage_status(node):
    """PVE's status of STORAGE on node (total / used / avail bytes, ...), through the API; {} if that fails."""
    st = pvesh_json(f"/nodes/{node}/storage/{STORAGE}/status")
    return st if isinstance(st, dict) else {}


def storage_numbers(st):
    """storage_status() -> (percent used, GB free), or (None, None)."""
    if not st.get("total"):
        return None, None
    return int(st.get("used", 0) * 100 // st["total"]), int(st.get("avail", 0)) // 2**30


def parse_mem(v):
    """'4096' (MB) or '4G' / '512M' -> MB"""
    m = re.fullmatch(r"(\d+)([MmGg]?)", v or "")
    if not m:
        bad_usage(f"--memory takes MB or a size like 4G, not '{v}'")
    mb = int(m.group(1)) * (1024 if m.group(2).lower() == "g" else 1)
    if mb < 512:
        bad_usage("--memory must be at least 512 MB")
    return mb


def parse_cores(v):
    if not (v or "").isdigit() or not 1 <= int(v) <= 64:
        bad_usage(f"--cores takes a number 1-64, not '{v}'")
    return int(v)


def parse_disk(v):
    """'30' / '30G' -> (False, 30); '+10' -> (True, 10)   (GB)"""
    m = re.fullmatch(r"(\+?)(\d+)[Gg]?", v or "")
    if not m or int(m.group(2)) < 1:
        bad_usage(f"--disk takes GB (30, or +10 to add), not '{v}'")
    return bool(m.group(1)), int(m.group(2))


def full_disk(v):
    """create's --disk: the full size in GB; empty = the template's (None)."""
    if not v:
        return None
    rel, gb = parse_disk(v)
    if rel:
        bad_usage("--disk takes the full size here (30), not +N")
    return gb


def conf_disk_gb(conf):
    m = re.search(r"size=(\d+)([KMGT]?)", (conf or {}).get("scsi0", ""))
    if not m:
        return 0
    return int(m.group(1)) * {"K": 1 / 1048576, "M": 1 / 1024, "G": 1, "T": 1024, "": 1 / 1073741824}[m.group(2)]


def size_label(conf):
    mem = int(conf.get("memory", 512))
    return f"{conf.get('cores', 1)}c/{round(mem / 1024, 1):g}G/{conf_disk_gb(conf):g}G"


def where(node):
    """' on <host>' for messages when there are several hosts, else ''."""
    return f" on {node or this_node()}" if multi() else ""


def mem_shortfall(more, what, node, avail):
    """A warning if `what` needing `more` MB of memory would leave the host (node: None for this one) less
    than its reserve. avail: its MB available (host_mem_mb())."""
    if not more:
        return None
    if more + HOST_RESERVE_MB > avail:
        return (f"memory{where(node)}: {what} need {more} MB, only {avail} MB available "
                f"(keeping {HOST_RESERVE_MB} MB for the host)")
    return None


def host_numbers(hosts):
    """{host: (MB of memory available, storage())} of hosts, read once for the checks, all at the same time."""
    return parallel(lambda h: tuple(concurrently(lambda: host_mem_mb(h)[1], lambda: storage(h))), hosts)


def check_resources(n, mem, node, avail, sto):
    """The problems (warnings) if n new VMs needing mem MB in all on node look short of memory or VM
    storage. avail, sto: the host's numbers (host_numbers())."""
    short = mem_shortfall(mem, f"{n} new VM(s)", node, avail)
    problems = [short] if short else []
    pct, free = sto
    if pct is not None and pct >= STORAGE_FULL_PCT:
        problems.append(f"{STORAGE}{where(node)} is {pct}% full")
    if free is not None and n * EST_DISK_GB > free:
        problems.append(f"{STORAGE}{where(node)}: ~{n * EST_DISK_GB} GB may be needed, {free} GB free")
    return problems


def confirm(problems, skip=None, why="LAB_FORCE=1"):
    """Warn about problems (if any); go on only with the user's yes, or without asking when skip
    (default: LAB_FORCE=1; why names it)."""
    if not problems:
        return
    for p in problems:
        warn(p)
    skip = FORCE if skip is None else skip
    if skip:
        warn(f"continuing because {why}")
    elif not sys.stdin.isatty():
        die(f"not continuing without a yes; re-run interactively or with {why} to continue anyway")
    elif input("Continue anyway? [y/N] ").strip().lower() != "y":
        die("aborted")


def check_copy_space(gb, node=None, sto=None):
    """A full copy writing gb GB to STORAGE (node: on another node): refuse if it can't fit (a full
    LVM-thin pool stops every VM on it); the problem (a warning) if it leaves the storage STORAGE_FULL_PCT
    or more full. sto: its storage(), if the caller has it."""
    pct, free = storage(node) if sto is None else sto
    if free is None:
        return []
    if gb >= free:
        die(f"{STORAGE}{where(node)}: the copy needs ~{gb:.1f} GB, only {free} GB free")
    after = 100 - (free - gb) * (100 - pct) / free if free else 100
    return ([f"{STORAGE}{where(node)} will be ~{after:.0f}% full after the copy (~{gb:.1f} GB)"]
            if after >= STORAGE_FULL_PCT else [])


def pvesh_json(path, *args):
    """`pvesh get path args` as parsed JSON (a list), or [] if it fails."""
    out = run("pvesh", "get", path, *args, "--output-format", "json", check=False, quiet=True)
    try:
        return json.loads(out or "[]")
    except ValueError:
        return []


def vm_stats(rows=None):
    """{vmid: PVE's live numbers} for the VMs of every node (VMIDs are cluster-wide): node, status,
    cpu (0..1 of its cores), mem/maxmem, netin/netout (bytes since start), uptime. One API call; {} if
    it fails. rows: /cluster/resources read already (live())."""
    rows = pvesh_json("/cluster/resources", "--type", "vm") if rows is None else rows
    return {r["vmid"]: r for r in rows if r.get("type") in ("qemu", "lxc")}


def node_mem(rows=None):
    """{node: (total, used) MB} of every online node, from PVE's live numbers; {} if that fails.
    rows: /cluster/resources read already (live())."""
    rows = pvesh_json("/cluster/resources", "--type", "node") if rows is None else rows
    return {r["node"]: (r["maxmem"] // 2**20, r.get("mem", 0) // 2**20)
            for r in rows if r.get("type") == "node" and r.get("status") == "online" and r.get("maxmem")}


def live():
    """(vm_stats(), node_mem()) from one API call, for list and top."""
    rows = pvesh_json("/cluster/resources")
    return vm_stats(rows), node_mem(rows)


def cpu_busy(stat):
    return stat.get("status") == "running" and stat.get("cpu", 0) >= CPU_HIGH


def host_mem_mb(node=None):
    """(total, available) MB of this host (node: of another one, through the API; (0, 0) if that fails)."""
    if not is_local(node):
        m = pvesh_json(f"/nodes/{node}/status")
        m = m.get("memory", {}) if isinstance(m, dict) else {}
        return m.get("total", 0) // 2**20, m.get("available", m.get("free", 0)) // 2**20
    m = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in open("/proc/meminfo")}
    return m["MemTotal"], m["MemAvailable"]


def free_mem(node, live):
    """(total, available) MB of node, as list, top and node list show it: this host's own numbers, another
    one's from live (node_mem(), read once by the caller); None when it is offline."""
    if is_local(node):
        return host_mem_mb()
    if node not in live:
        return None
    total, used = live[node]
    return total, total - used
