"""pvelab list"""

from ..blocks import block_label, blocks_load
from ..config import DHCP_BRIDGE, GUEST_USER, STORAGE
from ..credentials import cred_rows
from ..expose import is_exposed, public_host, ssh_port
from ..helptext import bad_usage
from ..inventory import desc_value, managed
from ..isolation import is_isolated
from ..args import opt, valid_net
from ..networks import net_label, vm_ip
from ..resources import cpu_busy, host_mem_mb, size_label, storage, vm_stats
from ..templates import template_label
from ..ui import paint


def cmd_list(args):
    ssh_only, only_net, passwords = False, None, False
    it = iter(args)
    for a in it:
        if a == "--ssh":
            ssh_only = True
        elif a == "--passwords":
            passwords = True
        elif (v := opt(a, it, "--net")) is not None:
            only_net = v
        else:
            bad_usage(f"unknown option '{a}'")
    if only_net is not None:
        only_net = "dhcp" if only_net == "dhcp" else valid_net(only_net)
    rows = cred_rows()
    vms = {i: c for i, c in managed().items()
           if only_net is None or rows.get(i, {}).get("net", desc_value(c, "net") or "dhcp") == only_net}
    exposed = {n for n in {r["net"] for r in rows.values()} if n != "dhcp" and is_exposed(n)}
    if ssh_only:
        return list_ssh(vms, rows, exposed, only_net)
    stats, blocks = vm_stats(), blocks_load()
    cols = [("NAME", 18), ("VMID", 6), ("TEMPLATE", 18), ("NET", 11), ("STATUS", 9), ("CPU", 6), ("SIZE", 12),
            ("IP", 15), ("SSH", 6), ("BLOCKED", 13), ("USER", 9)] + ([("PASSWORD", 0)] if passwords else [])
    print(" ".join(f"{h:<{w}}" for h, w in cols).rstrip())
    for vmid, c in vms.items():
        r, st = rows.get(vmid, {}), stats.get(vmid, {})
        tpl = desc_value(c, "template")
        status = st.get("status", "?")
        cpu = f"{round(st.get('cpu', 0) * 100)}%" if status == "running" else "-"
        blocked = block_label(vmid, blocks)
        cells = [(c.get("name", ""), None), (vmid, None), (template_label(int(tpl)) if tpl.isdigit() else "?", None),
                 (net_label(r.get("net", "dhcp")), None),
                 (status, "green" if status == "running" else "dim"),
                 (cpu, "red" if cpu_busy(st) else None), (size_label(c), None), (vm_ip(vmid, rows), None),
                 (ssh_port(vmid) if r.get("net") in exposed else "-", None),
                 (blocked, "red" if blocked != "-" else None), (r.get("user", "?"), None)]
        if passwords:
            cells.append((r.get("password", ""), None))
        print(" ".join(paint(v, color, w) if color else f"{v!s:<{w}}" for (v, color), (_, w) in zip(cells, cols)).rstrip())
    summary(vms, stats, rows, exposed, passwords)


def summary(vms, stats, rows, exposed, passwords):
    """The lines under the table: counts, busy VMs, memory, storage, where the passwords are."""
    if not vms:
        print("(no student VMs)")
        return
    running = [i for i in vms if stats.get(i, {}).get("status") == "running"]
    print(f"\n{len(vms)} VM(s): {len(running)} running, {len(vms) - len(running)} not running")
    busy = [i for i in vms if cpu_busy(stats.get(i, {}))]
    if busy:
        print(paint(f"busy CPU: {' '.join(vms[i].get('name', str(i)) for i in busy)}", "red")
              + "  - expected? if not (mining?), look closer: pvelab top")
    total, avail = host_mem_mb()
    conf_gb = sum(int(stats.get(i, {}).get("maxmem", 0)) for i in running) / 2**30
    print(f"memory: running VMs may use {conf_gb:.0f} GB of the host's {total / 1024:.0f} GB "
          f"({avail / 1024:.0f} GB available now)")
    pct, free = storage()
    line = f"{STORAGE}: {pct if pct is not None else '?'}% used, {free} GB free"
    print(paint(line, "red") + "  - full storage breaks every VM" if pct is not None and pct >= 80 else line)
    for n in sorted({rows.get(i, {}).get("net") for i in vms} - {None, "dhcp"}, key=int):
        print(f"net{n}: students isolated from each other" if is_isolated(n) else
              paint(f"net{n}: students can reach each other's VMs", "yellow") + f" (pvelab isolate --net {n})")
    if exposed & {rows.get(i, {}).get("net") for i in vms}:
        print(f"students: ssh -p <SSH> <USER>@{public_host()}")
    if not passwords:
        print(paint("passwords hidden: pvelab list --passwords  (to hand out: pvelab list --ssh, pvelab handout)", "dim"))


def list_ssh(vms, rows, exposed, only_net):
    """One ready-to-send line per student: name, ssh command, password."""
    host, shown, closed = public_host(), 0, set()
    fmt = "{:<18} {:<40} {}"
    print(fmt.format("NAME", "SSH", "PASSWORD"))
    for vmid, c in vms.items():
        r = rows.get(vmid, {})
        if r.get("net") not in exposed:
            closed.add(r.get("net", "dhcp"))
            continue
        print(fmt.format(c.get("name", ""), f"ssh -p {ssh_port(vmid)} {r.get('user', GUEST_USER)}@{host}",
                         r.get("password", "")))
        shown += 1
    if not shown:
        print("(none)")
    for n in sorted(closed - {"dhcp"}, key=int):
        print(f"\nnet{n} is not exposed - open it with: pvelab expose --net {n}")
    if "dhcp" in closed:
        print(f"\nVMs on {DHCP_BRIDGE}(dhcp) can't be exposed; create them with --net N instead")
