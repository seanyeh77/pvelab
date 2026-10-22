"""CPU / memory / disk sizes, storage, and host resource checks."""

import json
import os
import re
import sys

from .config import CPU_HIGH, EST_DISK_GB, FORCE, MEM, STORAGE
from .helptext import bad_usage
from .util import die, run, warn


def storage():
    """(percent used, GB free) of STORAGE, or (None, None)."""
    lines = run("pvesm", "status", "--storage", STORAGE, check=False, quiet=True).splitlines()
    if len(lines) < 2:
        return None, None
    f = lines[1].split()
    return int(float(f[6].rstrip("%"))), int(f[5]) // 1024 // 1024


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


def conf_disk_gb(conf):
    m = re.search(r"size=(\d+)([KMGT]?)", (conf or {}).get("scsi0", ""))
    if not m:
        return 0
    return int(m.group(1)) * {"K": 1 / 1048576, "M": 1 / 1024, "G": 1, "T": 1024, "": 1 / 1073741824}[m.group(2)]


def size_label(conf):
    mem = int(conf.get("memory", 512))
    return f"{conf.get('cores', 1)}c/{mem // 1024 if mem % 1024 == 0 else round(mem / 1024, 1)}G/{conf_disk_gb(conf):g}G"


def mem_available_mb():
    return next(int(l.split()[1]) // 1024 for l in open("/proc/meminfo") if l.startswith("MemAvailable:"))


def check_resources(n, mem=MEM):
    problems = []
    avail = mem_available_mb()
    if n * mem + 2048 > avail:
        problems.append(f"memory: {n} new VM(s) need {n * mem} MB, only {avail} MB available "
                        "(keeping 2 GB for the host)")
    pct, free = storage()
    if pct is not None and pct >= 80:
        problems.append(f"{STORAGE} is {pct}% full")
    if free is not None and n * EST_DISK_GB > free:
        problems.append(f"{STORAGE}: ~{n * EST_DISK_GB} GB may be needed, {free} GB free")
    if not problems:
        return
    for p in problems:
        warn(p)
    if FORCE:
        warn("continuing because LAB_FORCE=1")
    elif not sys.stdin.isatty():
        die("resources look insufficient; re-run with LAB_FORCE=1 to continue anyway")
    elif input("Continue anyway? [y/N] ").strip().lower() != "y":
        die("aborted")


def vm_stats():
    """{vmid: PVE's live numbers} for this node's VMs: status, cpu (0..1 of its cores), mem/maxmem,
    netin/netout (bytes since start), uptime. One API call; {} if it fails."""
    out = run("pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json",
              check=False, quiet=True)
    try:
        return {r["vmid"]: r for r in json.loads(out or "[]") if r.get("node") == os.uname().nodename}
    except ValueError:
        return {}


def cpu_busy(stat):
    return stat.get("status") == "running" and stat.get("cpu", 0) >= CPU_HIGH


def host_mem_mb():
    """(total, available) MB of this host."""
    m = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in open("/proc/meminfo")}
    return m["MemTotal"], m["MemAvailable"]
