"""pvelab top: CPU / memory / network of the student VMs, busy ones flagged."""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from ..args import opt
from ..config import CPU_HIGH
from ..helptext import bad_usage
from ..inventory import managed, targets
from ..resources import cpu_busy, host_mem_mb, vm_stats
from ..ui import elapsed, paint
from ..util import run


def rate(n):
    """bytes/s -> '1.2M' style."""
    for unit in ("", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "" else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def snapshot_rates(ids, wait=2.0):
    """Two readings `wait` s apart: (latest stats, {vmid: (in B/s, out B/s)})."""
    a = vm_stats()
    time.sleep(wait)
    b = vm_stats()
    rates = {i: ((b[i].get("netin", 0) - a[i].get("netin", 0)) / wait,
                 (b[i].get("netout", 0) - a[i].get("netout", 0)) / wait)
             for i in ids if i in a and i in b}
    return b, rates


def show(ids, names):
    stats, rates = snapshot_rates(ids)
    total, avail = host_mem_mb()
    load = os.getloadavg()[0]
    print(f"host: load {load:.1f} on {os.cpu_count()} cores, memory {(total - avail) / 1024:.0f}/{total / 1024:.0f} GB used"
          f"   {time.strftime('%H:%M:%S')}")
    fmt = "{:<18} {:<6} {:<9} {:>6} {:>13} {:>9} {:>9} {:>9}"
    print(fmt.format("NAME", "VMID", "STATUS", "CPU", "MEMORY", "NET IN/s", "NET OUT/s", "UP"))
    order = sorted(ids, key=lambda i: -stats.get(i, {}).get("cpu", 0))
    for i in order:
        st = stats.get(i, {})
        up = st.get("status") == "running"
        cpu = f"{st.get('cpu', 0) * 100:.0f}%" if up else "-"
        mem = f"{st.get('mem', 0) / 2**30:.1f}/{st.get('maxmem', 0) / 2**30:.0f}G" if up else "-"
        nin, nout = rates.get(i, (0, 0))
        line = fmt.format(names[i], i, st.get("status", "?"), cpu, mem, rate(nin) if up else "-",
                          rate(nout) if up else "-", elapsed(st.get("uptime", 0)) if up else "-")
        print(paint(line + "  ! busy", "red") if cpu_busy(st) else line)
    busy = [names[i] for i in ids if cpu_busy(stats.get(i, {}))]
    if busy:
        print(paint(f"\n{len(busy)} VM(s) at {CPU_HIGH * 100:.0f}%+ CPU right now: {' '.join(busy)}", "red"))
        print("  a student compiling is normal; hours of it with nobody logged in can be a cryptominer:\n"
              "  pvelab top --check   (average of the last hour)")


def hour_cpu(vmid):
    """Average CPU (0..1) of the last hour from PVE's RRD data, or None."""
    out = run("pvesh", "get", f"/nodes/{os.uname().nodename}/qemu/{vmid}/rrddata", "--timeframe", "hour",
              "--cf", "AVERAGE", "--output-format", "json", check=False, quiet=True)
    try:
        pts = [p["cpu"] for p in json.loads(out or "[]") if p.get("cpu") is not None]
    except ValueError:
        return None
    return sum(pts) / len(pts) if pts else None


def check(ids, names):
    """For cron: VMs whose CPU averaged CPU_HIGH+ over the last hour; exit 1 if there are any."""
    with ThreadPoolExecutor(8) as pool:
        avg = dict(zip(ids, pool.map(hour_cpu, ids)))
    hot = [i for i in ids if avg[i] is not None and avg[i] >= CPU_HIGH]
    for i in hot:
        print(f"{names[i]} ({i}): {avg[i] * 100:.0f}% CPU on average over the last hour")
    if hot:
        sys.exit(1)
    print(f"ok: no VM above {CPU_HIGH * 100:.0f}% CPU on average over the last hour")


def cmd_top(args):
    watch, do_check, rest = None, False, []
    it = iter(args)
    for a in it:
        if a == "--check":
            do_check = True
        elif a == "--watch":                 # --watch alone: every 5 s; --watch=N: every N s
            watch = 5
        elif (v := opt(a, it, "--watch")) is not None:
            if not v.isdigit() or int(v) < 1:
                bad_usage("--watch=SECONDS takes a number")
            watch = int(v)
        else:
            rest.append(a)
    man = managed()
    ids = targets(rest) if rest else sorted(man)
    names = {i: man.get(i, {}).get("name", str(i)) for i in ids}
    if do_check:
        return check(ids, names)
    if watch is None:
        return show(ids, names)
    try:
        while True:
            sys.stdout.write("\033[H\033[2J" if sys.stdout.isatty() else "\n")
            show(ids, names)
            print(paint(f"\nevery {watch}s - Ctrl-C to stop", "dim"))
            time.sleep(max(0, watch - 2))
    except KeyboardInterrupt:
        print()
