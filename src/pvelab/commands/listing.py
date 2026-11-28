"""pvelab list"""

import hashlib
import json
import re
import ssl

from .. import config, nodes, VERSION
from ..adopting import stray_line, strays
from ..blocks import block_label, blocks_load
from ..config import GUEST_USER, is_external, is_plain, MAX_VMID, STORAGE, STORAGE_FULL_PCT
from ..credentials import cred_rows
from ..downs import reasons
from ..expose import exposure, has_ssh_port, public_host, ssh_command, ssh_port
from ..forwards import canonical, recorded
from ..helptext import bad_usage
from ..ifaces import bridge_is_ours
from ..inventory import (login_of, vm_value, inventory, managed, net_of, shared_numbers, sorted_nets,
                         vm_names, vm_statuses)
from ..isolation import is_isolated
from ..args import net_arg, opt
from .. import nets as netinfo
from ..networks import vm_ip
from ..nodes import multi, node_names, this_node
from ..owners import owner_of, SHARED_GROUP
from ..resources import cpu_busy, free_mem, host_mem_mb, live, size_label, storage
from ..templates import node_templates, template_label
from ..ui import paint
from ..util import concurrently, LabError, number, parallel, warn


def cmd_list(args):
    ssh_only, only_net, passwords, only_exposed, as_json = False, None, False, False, False
    it = iter(args)
    for a in it:
        if a in ("--json", "-j"):
            as_json = True
        elif a == "--ssh":
            ssh_only = True
        elif a == "--passwords":
            passwords = True
        elif a == "--exposed":
            only_exposed = True
        elif (v := opt(a, it, "--net")) is not None:
            only_net = v
        else:
            bad_usage(f"unknown option '{a}'")
    if as_json and (ssh_only or passwords):
        bad_usage(f"--json can't go with {'--ssh' if ssh_only else '--passwords'}")
    if only_net is not None:
        only_net = "dhcp" if only_net == "dhcp" else net_arg(only_net)
    rows = cred_rows()
    inv = inventory()                        # every config, read once
    man = managed(inv)
    nets = {i: net_of(i, rows, c) for i, c in man.items()}               # each VM's net, once
    vms = {i: c for i, c in man.items() if only_net is None or nets[i] == only_net}
    had = {nets[i] for i in vms}
    if as_json:
        return list_json(vms, rows, nets, only_net, only_exposed)
    exposed, fwd = exposure(None if only_exposed else {n for n in had if not is_plain(n)})   # --exposed: every
                                                                                           # net; one read
    if only_exposed:                         # what users outside can reach (to hand out)
        vms = {i: c for i, c in vms.items() if nets[i] in exposed}
        if not vms:
            return print(not_exposed(only_net, had, exposed, {nets[i] for i in man}))
    if ssh_only:
        return list_ssh(vms, rows, nets, exposed, fwd)
    # pvesh, pvesm and (cached for the summary) the address users ssh to, at the same time
    (stats, mem), disk, _ = concurrently(live, storage, public_host if exposed else lambda: None)
    blocks, on_node = blocks_load(), node_templates(inv)
    ips = parallel(lambda i: vm_ip(i, rows, stats.get(i, {}).get("status") == "running"), vms)
    hosts = multi()                          # the HOST column only with more than one node
    cols = ([("NAME", 18), ("VMID", 6), ("TEMPLATE", 18), ("NET", 11), ("STATUS", 9)] + ([("HOST", 10)] if hosts else [])
            + [("CPU", 6), ("SIZE", 12), ("IP", 15), ("SSH", 6), ("BLOCKED", 13), ("USER", 9)]
            + ([("PASSWORD", 0)] if passwords else []))
    print(" ".join(f"{h:<{w}}" for h, w in cols).rstrip())
    names = {n: netinfo.net_name(n) for n in {nets[i] for i in vms}}     # each net's name read once
    for vmid, c in vms.items():
        r, st, net = rows.get(vmid, {}), stats.get(vmid, {}), nets[vmid]
        tpl = vm_value(c, "template")
        status = st.get("status", "?")
        cpu = f"{round(st.get('cpu', 0) * 100)}%" if status == "running" else "-"
        blocked = block_label(vmid, blocks)
        values = [c.get("name", ""), vmid, template_label(t, on_node) if (t := number(tpl, 1, MAX_VMID)) is not None
                  else "-" if tpl == "" else "?",
                  names[net], status] + ([c.get("_node", "?")] if hosts else []) + [cpu, size_label(c), ips[vmid],
                  ssh_port(vmid) if vmid in fwd else "-", blocked, r.get("user", "?")]
        if passwords:
            values.append(r.get("password") or "-")
        colors = {"STATUS": "green" if status == "running" else "dim" if status == "stopped" else "red", "CPU": "red" if cpu_busy(st) else None,
                  "BLOCKED": "red" if blocks.get(vmid) else None}
        print(" ".join(paint(v, colors.get(h), w) for v, (h, w) in zip(values, cols)).rstrip())
    down_lines(vms, stats)
    summary(vms, stats, mem, disk, {nets[i] for i in vms}, exposed, passwords, hosts)
    if not only_exposed:                     # --exposed: what to hand out
        stray_lines(inv, man, nets, only_net)


def stray_lines(inv, man, nets, only_net):
    """Under the summary: each VM made by hand on one of pvelab's nets (only_net's, with --net), which pvelab
    leaves alone, and the command that hands it over. nets: each of man's VMs' net."""
    found = strays(inv, only_net=only_net)
    if not found:
        return
    shared = shared_numbers(man, [nets[i] for i in man])
    print()
    for s in found:
        print(stray_line(s, shared))


def down_lines(vms, stats):
    """Under the table: why each VM that is not running is down, and what brings it back."""
    down = {i: (stats.get(i, {}).get("status", "unknown"), c.get("_node")) for i, c in vms.items()
            if stats.get(i, {}).get("status") != "running"}
    if not down:
        return
    why = reasons(down)
    width = max(len(vms[i].get("name", "")) for i in down)
    print("\nnot running:")
    for i in sorted(down, key=lambda i: vms[i].get("name", "")):
        name, (text, fix) = vms[i].get("name", str(i)), why[i]
        print(f"  {name:<{width}}  " + paint(text, "dim" if down[i][0] == "stopped" else "red")
              + (paint(f"  -> {fix.format(name=name)}", "dim") if fix else ""))


def summary(vms, stats, mem, disk, nets, exposed, passwords, hosts=False):
    """The lines under the table: counts, busy VMs, memory (mem: node_mem()), storage, where the
    passwords are."""
    if not vms:
        print("(no pvelab VMs)")
        return
    running = [i for i in vms if stats.get(i, {}).get("status") == "running"]
    print(f"\n{len(vms)} VM(s): {len(running)} running, {len(vms) - len(running)} not running")
    busy = [i for i in vms if cpu_busy(stats.get(i, {}))]
    if busy:
        print(paint(f"busy CPU: {' '.join(vm_names(busy, vms).values())}", "red")
              + "  - expected? if not (mining?), look closer: pvelab top")
    if hosts:
        memory_per_host(running, stats, mem)
    else:
        memory_line("memory", sum(int(stats.get(i, {}).get("maxmem", 0)) for i in running), host_mem_mb())
    pct, free = disk                         # storage()
    line = (f"{STORAGE}" + (f" on {this_node()}" if hosts else "")      # several hosts: storage() is this one's
            + f": {pct if pct is not None else '?'}% used, {free} GB free")
    print(paint(line, "red") + "  - full storage breaks every VM" if pct is not None and pct >= STORAGE_FULL_PCT else line)
    with netinfo.cached_inputs():            # one read of the hosts' routes and SDN subnets for all nets
        for n in sorted_nets(nets):
            if is_external(n):               # an admin's bridge: nothing of pvelab's to show
                print(f"{n}: not pvelab's network - owners can reach each other's VMs, no port forwards")
                continue
            label = netinfo.net_name(n) if n == "dhcp" else f"{netinfo.net_name(n)} ({netinfo.cidr(n)})"
            print(f"{label}: owners isolated from each other" if is_isolated(n) else
                  paint(f"{label}: owners can reach each other's VMs", "yellow")
                  + f" (pvelab isolate --net {netinfo.net_name(n)})")
            if n != "dhcp":
                for line in netinfo.overlaps(n):
                    print(paint(line, "red"))
    found = recorded({i: c["_rec"] for i, c in vms.items() if "_rec" in c}, say=True)     # the records read with the configs
    if shown := [f"{vms[i].get('name', i)} {canonical(f)}" for i, fs in found.items() for f in fs]:
        print(f"port forwards: {', '.join(shown)}  (pvelab forward)")
    if exposed:
        print(f"users: ssh -p <SSH> <USER>@{public_host()}")
    if not passwords:
        print(paint("passwords hidden: pvelab list --passwords  (to hand out: pvelab list --ssh --exposed, pvelab handout)", "dim"))


def memory_per_host(running, stats, mem):
    """The summary's memory line, one per node (more than one node); mem: node_mem()."""
    for node in node_names():
        if not (m := free_mem(node, mem)):
            print(paint(f"memory on {node}: host offline", "red"))
            continue
        memory_line(f"memory on {node}", sum(int(stats[i].get("maxmem", 0)) for i in running
                                             if stats[i].get("node") == node), m)


def memory_line(label, conf, m):
    """'<label>: running VMs may use ...': conf bytes configured, m (total, available) MB of the host."""
    total, avail = m
    print(f"{label}: running VMs may use {conf / 2**30:.0f} GB of the host's {total / 1024:.0f} GB "
          f"({avail / 1024:.0f} GB available now)")


def not_exposed(net, had, exposed, used):
    """Why list --exposed shows nothing, and what to do: net (None: every net), the nets that had VMs
    before the filter, the exposed nets, and every net with VMs."""
    empty = [netinfo.net_name(n) for n in sorted_nets(exposed - used - {net})]    # net: said on its own
    also = f" (exposed, no VMs yet: {', '.join(empty)})" if empty else ""
    if net == "dhcp" or net is None and had == {"dhcp"}:
        return f"VMs on {netinfo.net_name('dhcp')} can't be exposed; create them with --net N instead" + also
    if net is not None:
        name = netinfo.net_name(net)
        if net in exposed:
            return f"{name} is exposed but has no VMs yet - add them with: pvelab create OWNERS --net {name}" + also
        return (f"{name} is not exposed - open it with: pvelab expose --net {name}" if had
                else f"{name} has no VMs - create them exposed with: pvelab create OWNERS --net {name} --expose") + also
    if had:
        return "no net with VMs is exposed - open one with: pvelab expose --net N" + also
    if empty:
        return (f"no pvelab VMs yet - exposed: {', '.join(empty)}; create them with: "
                f"pvelab create OWNERS --net {empty[0]}")
    return "no pvelab VMs yet - create them exposed with: pvelab create OWNERS --net N --expose"


def list_ssh(vms, rows, nets, exposed, fwd):
    """One ready-to-send line per VM: name, ssh command, password. fwd: VMs whose port is forwarded.
    A VM of a net that is not exposed: ssh to its own address, which works from the PVE hosts only."""
    host, closed = public_host() if exposed else None, set()
    inside = [i for i in vms if nets[i] not in exposed]
    ips = parallel(lambda i: vm_ip(i, rows), inside) if inside else {}    # dhcp VMs: ask their agents at once
    fmt = "{:<18} {:<40} {}"
    print(fmt.format("NAME", "SSH", "PASSWORD"))
    for vmid, c in vms.items():
        r = rows.get(vmid, {})
        if nets[vmid] in exposed:
            cmd = ssh_command(vmid, r, host) if vmid in fwd else "(no port forward: see pvelab expose)"
        else:
            closed.add(nets[vmid])
            ip = ips.get(vmid) or "-"
            cmd = f"ssh {r.get('user', GUEST_USER)}@{ip}" if ip != "-" else "(no address yet)"
        print(fmt.format(c.get("name", ""), cmd, r.get("password") or "-"))
    if not vms:
        print("(none)")
    names = [netinfo.net_name(n) for n in sorted_nets(closed) if not is_plain(n)]
    if names:
        print(f"\n{', '.join(names)}: not exposed - the ssh commands above work from the PVE hosts only."
              f" For users outside: pvelab expose --net {names[0]}" + (" (each net)" if len(names) > 1 else ""))
    if outside := [n for n in sorted_nets(closed) if is_external(n)]:
        print(f"\nVMs on {', '.join(outside)}: ssh to their own addresses, from where that network reaches"
              " (pvelab has no port forwards there)")
    if "dhcp" in closed:
        print(f"\nVMs on {netinfo.net_name('dhcp')}: from the PVE hosts only, and they can't be exposed;"
              " create them with --net N instead")


def list_json(vms, rows, nets, only_net, only_exposed):
    """list --json: the nets, their owners and VMs as one JSON object (for scripts), built field by field -
    never a password, key or config. nets: each VM's net; only_net: the net asked for (shown even without
    VMs), else every net. Nothing is asked of the guests or the outside."""
    shown = {only_net} if only_net is not None else {nets[i] for i in vms} | set(netinfo.all_nets())
    shown.discard("dhcp")                    # vmbr1: no net of pvelab's to describe
    exposed, fwd = exposure(None if only_exposed else {n for n in shown if not is_plain(n)})
    if only_exposed:
        shown &= exposed
    vms = {i: c for i, c in vms.items() if nets[i] in shown}
    try:
        status = vm_statuses(list(vms))
    except LabError as e:                    # a host that does not answer: the rest is still shown
        warn(f"VM status unknown: {e}")
        status = {}
    pools, here = vm_pools(config.read_text(config.USER_CFG)), this_node()
    ca_sha256, ca_pem = ca_facts(config.read_text(config.PVE_CA))
    out, warnings = [], []
    for n in sorted_nets(shown):
        plain, gw, name = is_plain(n), gateway_node(n), netinfo.net_name(n)
        known = gw == here                   # its isolation and forwards are in this host's interfaces file
        on = False if plain else (n in exposed) if known else None

        def vm(i, index=False):
            c, r = vms[i], rows.get(i, {})
            d = {"vmid": i, "name": c.get("name", "")} | ({"index": index} if index is not False else {})
            return d | {"node": c.get("_node"), "pool": pools.get(i), "user": login_of(i, r, c),
                        "ip": r["ip"] if r.get("ip") not in (None, "", "-") else None,
                        "status": status.get(i, "unknown"),
                        "ssh_port": ssh_port(i) if on and i in fwd and has_ssh_port(i) else None}
        groups = {}
        for i in sorted(i for i in vms if nets[i] == n):
            groups.setdefault(owner_of(vms[i]), []).append(i)
        shared = groups.pop(SHARED_GROUP, [])
        owners = []
        for key in sorted(groups):
            idx = {i: vm_index(vms[i].get("name", ""), key, len(groups[key])) for i in groups[key]}
            hosts = sorted({vms[i].get("_node") or "?" for i in groups[key]})
            owners.append({"owner": key, "node": hosts[0] if len(hosts) == 1 else None,
                           "vms": [vm(i, idx[i]) for i in sorted(idx, key=lambda i: (idx[i] is None, idx[i] or 0, i))]})
            if len(hosts) > 1:
                warnings.append({"kind": "owner_split", "net": name, "owner": key, "nodes": hosts})
        if gw is None and not plain:
            warnings.append({"kind": "gateway_unknown", "net": name})
        out.append({"name": name, "number": None if plain else n, "cidr": None if plain else str(netinfo.cidr(n)),
                    "gateway": None if plain else str(netinfo.gateway(n)), "bridge": config.bridge(n),
                    "gateway_node": gw, "internal_host": internal_host(gw),
                    "isolated": False if plain else is_isolated(n) if known else None,
                    "exposed": on, "shared": vm(shared[0]) if shared else None, "owners": owners})
    print(json.dumps({"schema": 1, "pvelab": VERSION, "node": here, "public_host": saved_public_host(),
                      "cluster": {"ca_sha256": ca_sha256, "ca_pem": ca_pem}, "nets": out, "warnings": warnings},
                     indent=2))


def vm_index(name, owner, count):
    """Which of its owner's VMs a VM is: N of a name ending -N (NET-OWNER-N), 1 for an owner's only VM, else
    None. A name ending -OWNER (an owner key of digits) has no -N."""
    m = re.fullmatch(r".+-(\d+)", name)
    if m and not name.endswith(f"-{owner}"):
        return int(m.group(1))
    return 1 if count == 1 else None


def gateway_node(n):
    """The host with net n's gateway, isolation and ssh forwards: this one when its block is here, else the
    host a take-over record names, else None (pvelab's own SDN nets: only that host knows; an external
    bridge)."""
    if is_plain(n):
        return None
    if bridge_is_ours(n):
        return this_node()
    entry = config.taken_entry(n)
    return entry[1].get("host") if entry else None


def internal_host(node):
    """node's address on the hosts' network: its address in .members, else (this host) vmbr0's, else None."""
    if node is None:
        return None
    return nodes.members().get(node, {}).get("ip") or (nodes.uplink_ip() if node == this_node() else None)


def vm_pools(text):
    """{vmid: pool} from user.cfg's text (pool:NAME:COMMENT:VMID,VMID:STORAGE,...:)."""
    out = {}
    for line in text.splitlines():
        f = line.strip().split(":")
        if f[0] == "pool" and len(f) > 3:
            out.update({int(v): f[1] for v in f[3].split(",") if v.strip().isdigit()})
    return out


def ca_facts(text):
    """(SHA-256 fingerprint, as openssl x509 -fingerprint prints it; the PEM text) of the cluster CA in text,
    or (None, None) when it is no certificate."""
    try:
        der = ssl.PEM_cert_to_DER_cert(text.strip())
    except ValueError:
        return None, None
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()), text


def saved_public_host():
    """The address saved with expose --host, else None: list --json never asks the outside."""
    return config.read_text(config.PUBLIC_HOST_FILE).strip() or None
