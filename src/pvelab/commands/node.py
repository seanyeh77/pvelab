"""pvelab node list | add ADDR | upgrade [HOST...] [--major] [--dry-run] | rename OLD NEW [--dry-run] | rename
(also: pvelab nodes)"""

import json

from ..config import STORAGE
from ..helptext import bad_usage
from ..inventory import inventory, managed
from ..joining import node_add
from ..nodes import is_local, members
from ..renaming import node_rename, pending
from ..resources import free_mem, node_mem, pvesh_json, storage
from ..templates import templates_by_node, with_old
from ..upgrading import node_upgrade
from ..ui import paint
from ..util import parallel


def cmd_node(args, dry=False):
    if args[:1] == ["upgrade"]:
        hosts = [a for a in args[1:] if a not in ("--major", "--minor")]
        if any(h.startswith("-") for h in hosts):
            bad_usage(f"node upgrade: unknown option {next(h for h in hosts if h.startswith('-'))}")
        if "--major" in args and "--minor" in args:
            bad_usage("node upgrade: --major or --minor, not both")
        return node_upgrade(hosts, major_="--major" in args, dry=dry, minor="--minor" in args)
    if len(args) == 3 and args[0] == "rename":
        return node_rename(args[1], args[2], dry=dry)
    if args == ["rename"] and pending():        # alone: finishes the unfinished rename
        return node_rename(None, None, dry=dry)
    if dry:
        bad_usage("--dry-run works with node upgrade and node rename")
    flags = {a for a in args if a in ("-v", "--verbose", "-j", "--json")}
    if [a for a in args if a not in flags] in ([], ["list"]):
        return node_list(verbose=bool(flags & {"-v", "--verbose"}), as_json=bool(flags & {"-j", "--json"}))
    if len(args) == 2 and args[0] == "add":
        return node_add(args[1])
    bad_usage("node takes: list, add ADDR, upgrade [HOST...] [--major] [--dry-run], or rename OLD NEW [--dry-run]")


def host_facts(node, m, live, vms, tpls):
    """What node list knows about one host: the table's columns, and (for -v / -j) its hardware and system
    from PVE's API (/nodes/NODE/status and the storage's status), which answers for every host of the cluster."""
    f = {"host": node, "ip": m.get("ip"), "online": bool(m["online"]), "this_host": is_local(node),
         "pvelab_vms": sum(1 for c in vms.values() if c.get("_node") == node),
         "templates": [with_old(t, where[node]) for t, where in sorted(tpls.items()) if node in where],
         "mem_free_gb": None, "disk_free_gb": None}
    if not m["online"]:
        return f
    ram, disk = free_mem(node, live), storage(node)[1]
    f["mem_free_gb"] = None if ram is None else round(ram[1] / 1024, 1)
    f["disk_free_gb"] = disk
    return f


def host_details(f):
    """f plus the hardware and system facts of an online host (one API call each)."""
    if not f["online"]:
        return f
    st = pvesh_json(f"/nodes/{f['host']}/status")
    st = st if isinstance(st, dict) else {}
    cpu, mem, root = st.get("cpuinfo", {}), st.get("memory", {}), st.get("rootfs", {})
    flags = cpu.get("flags", "").split()
    sto = pvesh_json(f"/nodes/{f['host']}/storage/{STORAGE}/status")
    sto = sto if isinstance(sto, dict) else {}
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
        "pve": (st.get("pveversion") or "").split("/")[1] if "/" in (st.get("pveversion") or "") else None,
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
    inv = inventory()
    vms = managed(inv)
    tpls = templates_by_node(inv)
    live = node_mem()
    hosts = sorted(members().items(), key=lambda nm: (not is_local(nm[0]), nm[0]))   # this host first
    facts = [host_facts(node, m, live, vms, tpls) for node, m in hosts]
    if verbose or as_json:
        details = parallel(lambda k: host_details(facts[k]), range(len(facts)))    # one API call per host, at once
        facts = [details[k] for k in range(len(facts))]
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
