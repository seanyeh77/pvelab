"""The host table of pvelab node list: one row per host (online, free memory and disk, pvelab VMs, templates),
with -v each host's hardware and system, with -j all of it as JSON."""

import json

from .config import STORAGE
from .inventory import inventory, managed
from .nodes import here_first, is_local, members, parse_pveversion
from .resources import free_mem, node_mem, pvesh_json, storage_numbers, storage_status
from .templates import templates_by_node, with_old
from .ui import paint
from .util import concurrently, parallel


def host_facts(node, m, live, vms, tpls, details=False):
    """What node list knows about one host: the table's columns, and (details: for -v / -j) its hardware
    and system from PVE's API (/nodes/NODE/status and the storage's status), which answers for every host
    of the cluster."""
    f = {"host": node, "ip": m.get("ip"), "online": bool(m["online"]), "this_host": is_local(node),
         "pvelab_vms": sum(1 for c in vms.values() if c.get("_node") == node),
         "templates": [with_old(t, where[node]) for t, where in sorted(tpls.items()) if node in where],
         "mem_free_gb": None, "disk_free_gb": None}
    if not m["online"]:
        return f
    ram = free_mem(node, live)
    f["mem_free_gb"] = None if ram is None else round(ram[1] / 1024, 1)
    if not details:
        f["disk_free_gb"] = storage_numbers(storage_status(node))[1]
        return f
    # read once, both at the same time: the table's disk free and the details' storage, and the host's status
    sto, st = concurrently(lambda: storage_status(node), lambda: pvesh_json(f"/nodes/{node}/status"))
    f["disk_free_gb"] = storage_numbers(sto)[1]
    return host_details(f, sto, st)


def host_details(f, sto, st):
    """f plus the hardware and system facts of an online host; sto: its storage_status(), st: its
    /nodes/NODE/status."""
    st = st if isinstance(st, dict) else {}
    cpu, mem, root = st.get("cpuinfo", {}), st.get("memory", {}), st.get("rootfs", {})
    flags = cpu.get("flags", "").split()
    f.update({
        "cpu": {"model": cpu.get("model"), "sockets": cpu.get("sockets"), "cores": cpu.get("cores"),
                "threads": cpu.get("cpus"), "mhz": float(cpu["mhz"]) if cpu.get("mhz") else None,
                "virtualization": "VT-x" if "vmx" in flags else "AMD-V" if "svm" in flags else None},
        "memory_gb": {"total": gb(mem.get("total")), "available": gb(mem.get("available")),
                      "swap": gb(st.get("swap", {}).get("total"))},
        "root_fs_gb": {"total": gb(root.get("total")), "available": gb(root.get("avail"))},
        "storage": {"name": STORAGE, "total_gb": gb(sto.get("total")), "used_gb": gb(sto.get("used")),
                    "available_gb": gb(sto.get("avail")),
                    "used_pct": round(sto["used"] * 100 / sto["total"]) if sto.get("total") else None},
        "pve": parse_pveversion(st.get("pveversion")),
        "kernel": st.get("current-kernel", {}).get("release"),
        "boot": st.get("boot-info", {}).get("mode"),
        "uptime_s": st.get("uptime"),
        "load": [float(x) for x in st.get("loadavg", [])],
    })
    return f


def gb(n):
    return round(n / 2**30, 1) if n else None


def ago(seconds):
    d, rest = divmod(int(seconds or 0), 86400)
    h, rest = divmod(rest, 3600)
    return f"{d}d {h}h" if d else f"{h}h {rest // 60}m"


def detail_lines(f):
    """-v: a few lines about one host under the table."""
    head = paint(f"{f['host']} ({f['ip'] or '-'})", "bold") + ("  (this host)" if f["this_host"] else "")
    if not f["online"]:
        return [head, paint("  offline", "red")]
    c, m, r, s = f["cpu"], f["memory_gb"], f["root_fs_gb"], f["storage"]
    cpu = (f"{c['model'] or '?'}, {c['sockets'] or '?'} socket(s), {c['threads'] or '?'} CPUs"
           + (f", {c['mhz']:.0f} MHz" if c["mhz"] else "")
           + (f", {c['virtualization']}" if c["virtualization"] else paint(", no hardware virtualization", "yellow")))
    disk = (f"{s['name']} {s['total_gb']} GB, {s['used_pct']}% used, {s['available_gb']} GB free" if s["total_gb"]
            else f"{s['name']}: ?")
    return [head,
            f"  CPU      {cpu}",
            f"  memory   {m['total']} GB, {m['available']} GB available; swap {m['swap'] or 0} GB",
            f"  disk     {disk}; / {r['total']} GB, {r['available']} GB free",
            f"  system   PVE {f['pve'] or '?'}, kernel {f['kernel'] or '?'}, {f['boot'] or '?'} boot, "
            f"up {ago(f['uptime_s'])}, load {' '.join(f'{x:.2f}' for x in f['load']) or '?'}",
            f"  pvelab   {f['pvelab_vms']} VM(s); templates: {', '.join(f['templates']) or '-'}"]


def node_list(verbose=False, as_json=False):
    """One row per host: online, free memory and disk, pvelab VMs, templates. No cluster: this host.
    verbose: a block per host with its hardware and system; as_json: everything as JSON (for scripts)."""
    inv, live = concurrently(inventory, node_mem)
    vms = managed(inv)
    tpls = templates_by_node(inv)
    mem = members()
    order = here_first(mem)
    facts = parallel(lambda node: host_facts(node, mem[node], live, vms, tpls, verbose or as_json), order)
    facts = [facts[node] for node in order]  # every host at once, shown in this order
    if as_json:
        print(json.dumps(facts, indent=2))
        return
    cols = [("HOST", 14), ("IP", 15), ("STATUS", 8), ("MEM FREE", 9), (f"DISK FREE ({STORAGE})", 22), ("VMS", 5), ("TEMPLATES", 0)]
    print(" ".join(f"{h:<{w}}" for h, w in cols).rstrip())
    for f in facts:
        online = f["online"]
        values = [f["host"], f["ip"] or "-", "online" if online else "offline",
                  "-" if not online else "?" if f["mem_free_gb"] is None else f"{f['mem_free_gb']:.1f} GB",
                  "-" if not online else "?" if f["disk_free_gb"] is None else f"{f['disk_free_gb']} GB",
                  f["pvelab_vms"], ", ".join(f["templates"]) or "-"]
        colors = {"STATUS": "green" if online else "red"}
        print(" ".join(paint(str(v), colors.get(h), w) for v, (h, w) in zip(values, cols)).rstrip())
    if verbose:
        for f in facts:
            print()
            print("\n".join(detail_lines(f)))
