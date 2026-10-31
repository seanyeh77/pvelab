"""pvelab list"""

from ..blocks import block_label, blocks_load
from ..config import STORAGE, STORAGE_FULL_PCT
from ..credentials import cred_rows
from ..expose import forwarded_on, is_exposed, public_host, ssh_command, ssh_port
from ..helptext import bad_usage
from ..inventory import desc_value, inventory, managed, net_of, sorted_nets, vm_names
from ..isolation import is_isolated
from ..args import opt, valid_net
from ..networks import net_label, vm_ip
from ..nodes import multi, node_names, this_node
from ..resources import cpu_busy, free_mem, host_mem_mb, node_mem, size_label, storage, vm_stats
from ..templates import node_templates, template_label
from ..ui import paint
from ..util import concurrently, parallel


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
    inv = inventory()                        # every config, read once
    man = managed(inv)
    nets = {i: net_of(i, rows, c) for i, c in man.items()}               # each VM's net, once
    vms = {i: c for i, c in man.items() if only_net is None or nets[i] == only_net}
    exposed = {n for n in {nets[i] for i in vms} - {"dhcp"} if is_exposed(n)}
    fwd = forwarded_on(exposed)                         # VMs whose ssh port is really forwarded
    if ssh_only:
        return list_ssh(vms, rows, nets, exposed, fwd)
    # pvesh, pvesm and (cached for the summary) the address students ssh to, at the same time
    stats, disk, _ = concurrently(vm_stats, storage, public_host if exposed else lambda: None)
    blocks, on_node = blocks_load(), node_templates(inv)
    ips = parallel(lambda i: vm_ip(i, rows, stats.get(i, {}).get("status") == "running"), vms)
    hosts = multi()                          # the HOST column only with more than one node
    cols = ([("NAME", 18), ("VMID", 6), ("TEMPLATE", 18), ("NET", 11), ("STATUS", 9)] + ([("HOST", 10)] if hosts else [])
            + [("CPU", 6), ("SIZE", 12), ("IP", 15), ("SSH", 6), ("BLOCKED", 13), ("USER", 9)]
            + ([("PASSWORD", 0)] if passwords else []))
    print(" ".join(f"{h:<{w}}" for h, w in cols).rstrip())
    for vmid, c in vms.items():
        r, st, net = rows.get(vmid, {}), stats.get(vmid, {}), nets[vmid]
        tpl = desc_value(c, "template")
        status = st.get("status", "?")
        cpu = f"{round(st.get('cpu', 0) * 100)}%" if status == "running" else "-"
        blocked = block_label(vmid, blocks)
        values = [c.get("name", ""), vmid, template_label(int(tpl), on_node) if tpl.isdigit() else "?",
                  net_label(net), status] + ([c.get("_node", "?")] if hosts else []) + [cpu, size_label(c), ips[vmid],
                  ssh_port(vmid) if vmid in fwd else "-", blocked, r.get("user", "?")]
        if passwords:
            values.append(r.get("password", ""))
        colors = {"STATUS": "green" if status == "running" else "dim", "CPU": "red" if cpu_busy(st) else None,
                  "BLOCKED": "red" if blocks.get(vmid) else None}
        print(" ".join(paint(v, colors.get(h), w) for v, (h, w) in zip(values, cols)).rstrip())
    summary(vms, stats, disk, {nets[i] for i in vms}, exposed, passwords, hosts)


def summary(vms, stats, disk, nets, exposed, passwords, hosts=False):
    """The lines under the table: counts, busy VMs, memory, storage, where the passwords are."""
    if not vms:
        print("(no student VMs)")
        return
    running = [i for i in vms if stats.get(i, {}).get("status") == "running"]
    print(f"\n{len(vms)} VM(s): {len(running)} running, {len(vms) - len(running)} not running")
    busy = [i for i in vms if cpu_busy(stats.get(i, {}))]
    if busy:
        print(paint(f"busy CPU: {' '.join(vm_names(busy, vms).values())}", "red")
              + "  - expected? if not (mining?), look closer: pvelab top")
    if hosts:
        memory_per_host(running, stats)
    else:
        total, avail = host_mem_mb()
        conf_gb = sum(int(stats.get(i, {}).get("maxmem", 0)) for i in running) / 2**30
        print(f"memory: running VMs may use {conf_gb:.0f} GB of the host's {total / 1024:.0f} GB "
              f"({avail / 1024:.0f} GB available now)")
    pct, free = disk                         # storage()
    line = (f"{STORAGE}" + (f" on {this_node()}" if hosts else "")      # several hosts: storage() is this one's
            + f": {pct if pct is not None else '?'}% used, {free} GB free")
    print(paint(line, "red") + "  - full storage breaks every VM" if pct is not None and pct >= STORAGE_FULL_PCT else line)
    for n in sorted_nets(nets):
        print(f"net{n}: students isolated from each other" if is_isolated(n) else
              paint(f"net{n}: students can reach each other's VMs", "yellow") + f" (pvelab isolate --net {n})")
    if exposed:
        print(f"students: ssh -p <SSH> <USER>@{public_host()}")
    if not passwords:
        print(paint("passwords hidden: pvelab list --passwords  (to hand out: pvelab list --ssh, pvelab handout)", "dim"))


def memory_per_host(running, stats):
    """The summary's memory line, one per node (more than one node)."""
    mem = node_mem()
    for node in node_names():
        conf_gb = sum(int(stats[i].get("maxmem", 0)) for i in running if stats[i].get("node") == node) / 2**30
        if not (m := free_mem(node, mem)):
            print(paint(f"memory on {node}: host offline", "red"))
            continue
        total, avail = m
        print(f"memory on {node}: running VMs may use {conf_gb:.0f} GB of the host's {total / 1024:.0f} GB "
              f"({avail / 1024:.0f} GB available now)")


def list_ssh(vms, rows, nets, exposed, fwd):
    """One ready-to-send line per student: name, ssh command, password. fwd: VMs whose port is forwarded."""
    host, shown, closed = public_host() if exposed else None, 0, set()
    fmt = "{:<18} {:<40} {}"
    print(fmt.format("NAME", "SSH", "PASSWORD"))
    for vmid, c in vms.items():
        r = rows.get(vmid, {})
        if nets[vmid] not in exposed:
            closed.add(nets[vmid])
            continue
        print(fmt.format(c.get("name", ""), ssh_command(vmid, r, host) if vmid in fwd
                         else "(no port forward: see pvelab expose)", r.get("password", "")))
        shown += 1
    if not shown:
        print("(none)")
    for n in sorted_nets(closed):
        print(f"\nnet{n} is not exposed - open it with: pvelab expose --net {n}")
    if "dhcp" in closed:
        print(f"\nVMs on {net_label('dhcp')} can't be exposed; create them with --net N instead")
