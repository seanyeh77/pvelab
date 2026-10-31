"""pvelab create"""

import re
from collections import Counter

from ..config import (bridge, CORES, CRED, DEFAULT_TEMPLATE, EST_DISK_GB, ID_START, is_sdn, MEM, PREFIX, SNAP, subnet,
                      YES)
from ..credentials import cred_rows, gen_password
from ..expose import expose_net
from ..isolation import is_isolated, set_isolation, student_of, vms_per_student
from ..helptext import bad_usage
from ..inventory import did_you_mean, inventory, read_conf, vm_exists
from ..lifecycle import clone_vm, take_snapshot, wait_first_boot
from ..args import opt, valid_net
from ..networks import (ensure_bridge, ensure_dhcp_bridge, ensure_gw_mac, foreign_use, free_net, is_new_net, next_ip,
                        refresh_net)
from ..nodes import is_local, members, multi
from ..placement import bridge_host, node_room, place
from ..resources import check_resources, confirm, conf_disk_gb, full_disk, parse_cores, parse_mem
from ..scripts import expose_warning, parse_chain, print_chain, run_and_snapshot
from ..sdn import wait_vnet
from ..templates import ensure_snippet, resolve_template, template_on
from ..util import die, warn


def per_vm(what, v, per, parse):
    """'4' or '2,4,4' -> one parsed value for each of a student's per VMs (the k-th for VM -k)."""
    vals = str(v).split(",")
    if len(vals) not in (1, per):
        bad_usage(f"{what} {v}: one value, or one for each of a student's {per} VMs" if per > 1
                  else f"{what} {v}: several values only with STUDENTSxVMS (20x3)")
    return [parse(x) for x in vals] * (per // len(vals))


def valid_node(v):
    """--node's value: auto, or an online node of the cluster."""
    if v == "auto":
        return v
    m = members()
    if v not in m:
        die(f"--node {v}: no such host (hosts: {', '.join(sorted(m))})" + did_you_mean(v, list(m)))
    if not m[v]["online"]:
        die(f"--node {v}: that host is offline")
    return v


def pinned_host(net, node):
    """The one host net's VMs may go to (a plain bridge exists on one host only), or None (any host).
    Dies when --node names another host."""
    pin = bridge_host(net)
    if pin and node not in ("auto", pin):
        die(f"VMs without --net use the DHCP bridge {bridge(net)} on {pin} only: they stay on {pin}" if net == "dhcp"
            else f"net{net} is a bridge {bridge(net)} on {pin} only (not an SDN vnet): its VMs stay on {pin}")
    return pin


def placement(todo, net, mem, per, node, inv, pin):
    """{student: node} for the students of todo (several hosts only), after the resource check on each
    host. Students who already have a VM on this net keep its host. pin: pinned_host(net, node)."""
    students = list(dict.fromkeys(student_of(name) for name, _ in todo))
    fixed = {}
    for c in inv.values():                   # a student's VM made earlier: the rest of theirs go there too
        s = student_of(c.get("name") or "")
        if s in students and s not in fixed:
            fixed[s] = c["_node"]
    need = sum(mem[k] for k in range(per))   # one student: all their VMs
    where = place(students, need, per * EST_DISK_GB, node_room(), None if node == "auto" else node,
                  [pin] if pin else None, fixed)
    count = Counter(where.values())
    print(">> placement: " + ", ".join(f"{h} {n} student{'s' * (n != 1)}" for h, n in sorted(count.items())))
    for h in sorted(count):
        mine = [(name, k) for name, k in todo if where[student_of(name)] == h]
        check_resources(len(mine), sum(mem[k] for _, k in mine), h)
    return where


def check_net_on(net, used):
    """Before cloning on the hosts used: the net must be there. A new net whose SDN vnet just failed is a
    bridge here only; and a host's network reload after the SDN apply may still be running."""
    pin = bridge_host(net)
    if pin and any(h != pin for h in used):
        die(f"net{net} is now a bridge {bridge(net)} on {pin} only (its SDN vnet failed); no VM was made. "
            f"Run the create again: its students then go to {pin}")
    for h in used:
        if not is_local(h) and is_sdn(net) and not wait_vnet(h, net):
            die(f"{bridge(net)} is not up on {h} after 1 min; no VM was made. On {h}: ifreload -a, "
                "then run the create again")


def cmd_create(args):
    count, tpl, net, expose, host, script = None, DEFAULT_TEMPLATE, "dhcp", False, None, None
    node = "auto"
    cores, mem, disk, isolate = CORES, MEM, "", True
    it = iter(args)
    for a in it:
        if a == "--expose":
            expose = True
        elif a == "--no-isolate":
            isolate = False
        elif (v := opt(a, it, "--cores")) is not None:
            cores = v
        elif (v := opt(a, it, "--memory")) is not None:
            mem = v
        elif (v := opt(a, it, "--disk")) is not None:
            disk = v
        elif (v := opt(a, it, "--script")) is not None:
            script = v
        elif (v := opt(a, it, "--host")) is not None:
            host = v
        elif (v := opt(a, it, "--node")) is not None:
            node = valid_node(v)
        elif (v := opt(a, it, "--net")) is not None:
            net = v if v == "auto" else valid_net(v)
        elif (v := opt(a, it, "--template", "-t")) is not None:
            tpl = v
        elif count is None:
            count = a
        else:
            tpl = a
    m = re.fullmatch(r"(\d+)(?:x(\d+))?", count or "")
    if not m or int(m.group(1)) < 1 or m.group(2) and int(m.group(2)) < 1:
        bad_usage("the first argument must be how many students (20), or students x VMs each (20x3)"
                  if count else "how many VMs?")
    count, per = int(m.group(1)), int(m.group(2) or 1)
    tpl = per_vm("TEMPLATE", tpl, per, str)
    cores = per_vm("--cores", cores, per, parse_cores)
    mem = per_vm("--memory", mem, per, parse_mem)
    disk = per_vm("--disk", disk, per, full_disk)
    if per > 1 and net == "dhcp":
        bad_usage("several VMs per student (NxM) need --net N")
    if (expose or host) and net == "dhcp":
        bad_usage("--expose/--host only work together with --net N")
    if host and not expose:
        bad_usage("--host only makes sense with --expose")
    # each of a student's VMs: its chain of scripts (harden+docker,  -> VM -2 runs none), all
    # resolved and checked before anything is created
    chains = per_vm("--script", script or "", per, parse_chain)
    if count * per > 99:
        bad_usage("at most 99 VMs per create")
    same = len(set(chains)) == 1
    for k, chain in enumerate(chains[:1] if same else chains):
        if chain:
            print_chain(chain, "every VM" if same else f"VM -{k + 1}")
    if expose:                               # ssh from outside: weak passwords are at risk without harden
        confirm(expose_warning(chains, per), skip=YES, why="LAB_YES=1")
    if net == "auto":
        net = free_net()
        print(f">> using net {net} ({bridge(net)}, {subnet(net)}.0/24)")
    elif net != "dhcp":
        other = foreign_use(net)
        if other:
            die(f"can't use net{net}: {other}.\n"
                f"  pick another number, or --net auto for the next free one")
    hosts = multi()                          # one host (or no cluster): no placement, as before
    pin = pinned_host(net, node) if hosts else None    # refuses --node OTHER before anything is made
    vmids = {t: resolve_template(t) for t in dict.fromkeys(tpl)}
    tpl = [vmids[t] for t in tpl]
    ensure_snippet()                         # clones read it on first boot: keep it current
    for t, gb in zip(tpl, disk):
        tpl_gb = conf_disk_gb(read_conf(t))
        if gb and gb < tpl_gb:
            die(f"--disk {gb}: template {t}'s disk is already {tpl_gb:g} GB - disks can only grow")
    if net == "dhcp":
        pfx = PREFIX
        ensure_dhcp_bridge()
    else:
        pfx = f"net{net}-{PREFIX}"

    inv = inventory()
    existing = {c.get("name") for c in inv.values()}
    if net != "dhcp":
        have = vms_per_student([r["name"] for r in cred_rows().values() if r["net"] == net])
        if have and have != per:
            die(f"net{net} has {have} VM(s) per student; create more with {count}x{have}" if have > 1
                else f"net{net} has 1 VM per student; create more with {count} (no x{per})")
    todo = []
    for i in range(1, count + 1):
        for k in range(per):                 # k: which of the student's VMs (its specs)
            name = f"{pfx}{i:02d}" if per == 1 else f"{pfx}{i:02d}-{k + 1}"
            if name in existing:
                print(f"== {name} already exists, skipping")
            else:
                todo.append((name, k))
    if not todo:
        print("nothing to create")
        if expose:
            expose_net(net, host)
        return
    if hosts:
        where = placement(todo, net, mem, per, node, inv, pin)
        used = sorted(set(where.values()))
        # each host's own template (copied there first if it has none, after asking) and vendor-data snippet
        tpl_on = {h: {t: template_on(t, h) for t in {tpl[k] for name, k in todo if where[student_of(name)] == h}}
                  for h in used}
        for h in used:
            if not is_local(h):
                ensure_snippet(h)
    else:
        check_resources(len(todo), sum(mem[k] for _, k in todo))
    if net != "dhcp":
        new_net = is_new_net(net)
        ensure_bridge(net)
        if hosts and not all(map(is_local, used)):
            ensure_gw_mac(net)               # VMs on another host reach the gateway by its fixed MAC
        if new_net and isolate:              # before the VMs start: nothing between students
            set_isolation(net, True)
        elif not new_net and not isolate and is_isolated(net):
            warn(f"net{net} already isolates its students; to let them reach each other: "
                 f"pvelab isolate --net {net} --off")
        if hosts:
            check_net_on(net, used)
    rows, nxt, made = cred_rows(), ID_START, []
    passwords = {student_of(r["name"]): r["password"] for r in rows.values() if r["net"] == net}
    for name, k in todo:
        while vm_exists(nxt):
            nxt += 1
        pw = passwords.setdefault(student_of(name), gen_password())     # a student's VMs: one password
        ip = next_ip(net, rows) if net != "dhcp" else ""
        src = tpl_on[where[student_of(name)]][tpl[k]] if hosts else tpl[k]    # its host's copy of the template
        clone_vm(nxt, name, src, net, ip, rows, pw, cores[k], mem[k], disk[k], orig=tpl[k])
        made.append((nxt, k))
        nxt += 1
    if net != "dhcp":
        refresh_net(net)                     # ports, block rules, isolation for the new VMs
    done = wait_first_boot([i for i, _ in made])
    for i, _ in made:
        name = rows[i]["name"]
        if i not in done:
            warn(f"{name}: first boot not finished in time - no '{SNAP}' snapshot "
                 f"(take it later: pvelab snapshot {name})")
            continue
        if not done[i].startswith("done"):
            warn(f"{name}: cloud-init ended with '{done[i]}'")
        take_snapshot(i, SNAP, "pvelab: after first boot")
    booted = [i for i, _ in made if i in done]
    print(f">> '{SNAP}' snapshot taken on {len(booted)}/{len(made)} VM(s)")
    todo_chains = {i: chains[k] for i, k in made if i in done and chains[k]}
    if todo_chains:                          # 'clean' is the before-snapshot here
        run_and_snapshot(todo_chains, pre_snap=False)
    shape = f" ({len(todo) // per} students x {per})" if per > 1 else ""
    print(f"created {len(todo)} VM(s){shape}. Credentials: {CRED}")
    if net != "dhcp" and is_isolated(net):
        print(f"net{net}: students can't reach each other's VMs")
    if expose:
        print()
        expose_net(net, host)                # opens the ports and prints the ssh commands
