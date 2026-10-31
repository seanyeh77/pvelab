"""pvelab create"""

import re
from collections import Counter

from ..config import (bridge, CORES, CRED, DEFAULT_TEMPLATE, EST_DISK_GB, FORCE, ID_START, is_sdn, MEM, PREFIX, SNAP,
                      subnet, YES)
from .. import ui
from ..credentials import cred_rows, gen_password
from ..expose import expose_net
from ..isolation import is_isolated, set_isolation, student_of, vms_per_student
from ..helptext import bad_usage
from ..inventory import did_you_mean, inventory, read_conf
from ..lifecycle import clone_vm, wait_first_boot
from ..args import opt, valid_net
from ..networks import (ensure_bridge, ensure_dhcp_bridge, ensure_gw_mac, foreign_use, free_net, is_new_net, next_ip,
                        refresh_net)
from ..nodes import is_local, members, multi, this_node, used_vmids
from ..placement import bridge_host, node_room, place
from ..resources import (check_copy_space, check_resources, conf_disk_gb, full_disk, host_numbers, parse_cores,
                         parse_mem)
from ..scripts import expose_warning, parse_chain, run_and_snapshot, snapshot_all
from ..sdn import wait_vnet
from ..templates import copy_needed, ensure_snippet, resolve_template, template_on
from ..ui import Board, paint, Plan, some, Spinner, step
from ..util import die, LabError, warn


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


def placement(students, mem, per, node, inv, pin, hosts):
    """{student: host} for students. One host (hosts false): all here, no placement. Else students who
    already have a VM keep its host, the others go where there is room. pin: pinned_host(net, node)."""
    if not hosts:
        return dict.fromkeys(students, this_node())
    fixed = {}
    for c in inv.values():                   # a student's VM made earlier: the rest of theirs go there too
        s = student_of(c.get("name") or "")
        if s in students and s not in fixed:
            fixed[s] = c["_node"]
    need = sum(mem[k] for k in range(per))   # one student: all their VMs
    return place(students, need, per * EST_DISK_GB, node_room(), None if node == "auto" else node,
                 [pin] if pin else None, fixed)


def forced(plan, problems):
    """Resource and space problems join the plan's warnings (LAB_FORCE=1 skips them)."""
    for p in problems:
        plan.warn(p, FORCE, "LAB_FORCE=1")


def build_plan(todo, students, tpl, mem, per, chains, net, node, pin, isolate, expose, inv, hosts):
    """Step 1: what create will do and every warning about it, asked once. Returns (where, used, need,
    copies): {student: host}, the hosts used, {host: the templates its VMs are cloned from}, and
    {(template, host): GB} of the template copies the plan makes."""
    plan = Plan()
    if expose:                               # ssh from outside: weak passwords are at risk without harden
        for line in expose_warning(chains, per):
            plan.warn(line, YES, "LAB_YES=1")
    step(1, 3, "plan")
    names = sorted({read_conf(t).get("name", str(t)) for t in tpl})
    plan.do(f"create {len(todo)} VM(s)" + (f" ({len(students)} students x {per})" if per > 1 else "")
            + f" from {', '.join(names)}")
    if net == "dhcp":
        plan.do(f"on the DHCP bridge {bridge(net)}")
    else:
        plan.do(f"on net{net} ({subnet(net)}.0/24, {bridge(net)})" + (" - a new network" if is_new_net(net) else "")
                + (", students kept apart" if isolate and is_new_net(net) else ""))
    where = placement(students, mem, per, node, inv, pin, hosts)
    on = {name: where[student_of(name)] for name, _ in todo}
    used = sorted(set(where.values()))
    numbers = host_numbers(used)             # {host: (MB available, storage())}, read once for the checks
    for h in used:
        mine = [k for name, k in todo if on[name] == h]
        forced(plan, check_resources(len(mine), sum(mem[k] for k in mine), h, *numbers[h]))
    if hosts:
        count_on = Counter(where.values())
        plan.do("place them: " + ", ".join(f"{h} {n} student{'s' * (n != 1)}" for h, n in sorted(count_on.items()))
                + " (each student's VMs on one host)")
    need = {h: sorted({tpl[k] for name, k in todo if on[name] == h}) for h in used}
    copies, sizes = {}, {}                   # sizes: each template's GB, read once (lvs on its node)
    for h in used:                           # the templates each host's VMs are cloned from
        for t in need[h]:
            gb = copy_needed(t, h, inv, sizes.get(t))
            if gb is not None:
                copies[(t, h)] = sizes[t] = gb
                forced(plan, check_copy_space(gb, h, numbers[h][1]))
                tname = read_conf(t).get("name")
                plan.do(f"copy template {tname} to {h} first (~{gb:.1f} GB)")
                plan.warn(f"template {tname} is copied to {h} (~{gb:.1f} GB, a few minutes)", YES, "LAB_YES=1")
    same = len(set(chains)) == 1
    for k, chain in enumerate(chains[:1] if same else chains):
        if chain:
            plan.do(f"then run scripts on {'every VM' if same else f'VM -{k + 1}'}: "
                    + " + ".join(s.label for s in chain))
    if expose:
        plan.do("let students ssh in from outside (--expose)")
    plan.ask()
    return where, used, need, copies


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


def prepare_net(net, isolate, used):
    """Step 2's network: the DHCP bridge, or net's bridge (made if new, its students kept apart unless
    --no-isolate), there on every host used before anything is cloned."""
    if net == "dhcp":
        with Spinner(f"DHCP bridge {bridge(net)}"):
            ensure_dhcp_bridge()
        return
    new_net = is_new_net(net)
    with Spinner(f"net{net}: {bridge(net)}, {subnet(net)}.0/24") as line:
        ensure_bridge(net)
        if not all(map(is_local, used)):
            ensure_gw_mac(net)               # VMs on another host reach the gateway by its fixed MAC
        if new_net and isolate:              # before the VMs start: nothing between students
            set_isolation(net, True)
        check_net_on(net, used)
        line.ok(f"net{net}: {bridge(net)}, gateway {subnet(net)}.1" + (" (new)" if new_net else ""))
    if not new_net and not isolate and is_isolated(net):
        warn(f"net{net} already isolates its students; to let them reach each other: "
             f"pvelab isolate --net {net} --off")


def summary(board, ids, todo, per, host_of, net, expose, host):
    """The lines after the board: how many VMs are ready (and where), which are not, and what next."""
    ok = [i for i in ids if board.rows[i]["state"] == "ok"]
    bad = [i for i in ids if board.rows[i]["state"] != "ok"]
    print()
    shape = f" ({len(todo) // per} students x {per})" if per > 1 else ""
    spread = ""
    if host_of:                              # several hosts: how many on each
        on = Counter(host_of[i] for i in ok)
        spread = " - " + ", ".join(f"{h} {n}" for h, n in sorted(on.items()))
    print(paint(f"created {len(ok)}/{len(todo)} VM(s){shape}{spread}", "green" if not bad else "yellow"))
    if bad:
        print(paint("not ready: " + " ".join(board.rows[i]["name"] for i in bad)
                    + f"  (their lines above say why; no '{SNAP}' snapshot where first boot did not finish)", "red"))
    if net != "dhcp" and is_isolated(net):
        print(f"net{net}: students can't reach each other's VMs")
    if expose:
        print()
        expose_net(net, host)                # opens the ports and prints the ssh commands
    which = f"--net {net}" if net != "dhcp" else ""
    print(paint("\nNext:", "bold"))
    print(f"  pvelab list {which} --passwords".replace("  --", " --") + "     VMs, IPs and passwords")
    print(f"  pvelab handout {which}".rstrip() + "               login cards to print")
    if bad:
        print(f"  pvelab destroy {' '.join(board.rows[i]['name'] for i in bad[:3])}{' ...' if len(bad) > 3 else ''}"
              "   then create again, to replace the VMs that failed")
    print(paint(f"credentials: {CRED}" + (f"\nlog: {ui.LOG}" if ui.LOG else ""), "dim"))


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
    for t, gb in zip(tpl, disk):
        tpl_gb = conf_disk_gb(read_conf(t))
        if gb and gb < tpl_gb:
            die(f"--disk {gb}: template {t}'s disk is already {tpl_gb:g} GB - disks can only grow")
    pfx = PREFIX if net == "dhcp" else f"net{net}-{PREFIX}"

    inv = inventory()
    existing = {c.get("name") for c in inv.values()}
    if net != "dhcp":
        have = vms_per_student([r["name"] for r in cred_rows().values() if r["net"] == net])
        if have and have != per:
            die(f"net{net} has {have} VM(s) per student; create more with {count}x{have}" if have > 1
                else f"net{net} has 1 VM per student; create more with {count} (no x{per})")
    todo, skipped = [], []
    for i in range(1, count + 1):
        for k in range(per):                 # k: which of the student's VMs (its specs)
            name = f"{pfx}{i:02d}" if per == 1 else f"{pfx}{i:02d}-{k + 1}"
            (skipped if name in existing else todo).append((name, k))
    if skipped:
        print(f"== already there, skipped: {some([n for n, _ in skipped], 6, ' ')}")
    if not todo:
        print("nothing to create")
        if expose:
            expose_net(net, host)
        return

    # --- 1. the plan: what will happen, every warning, one question ---
    students = list(dict.fromkeys(student_of(n) for n, _ in todo))
    where, used, need, copies = build_plan(todo, students, tpl, mem, per, chains, net, node, pin, isolate, expose,
                                           inv, hosts)

    # --- 2. templates and the network ---
    step(2, 3, "templates and network")
    # each host's own template (copied there first if it has none) and vendor-data snippet
    tpl_on = {h: {t: template_on(t, h, asked=True, inv=inv, gb=copies.get((t, h))) for t in need[h]} for h in used}
    for h in used:
        if not is_local(h):
            ensure_snippet(h)
    prepare_net(net, isolate, used)

    # --- 3. the VMs, each on its own line of the board ---
    rows, nxt, ids, taken = cred_rows(), ID_START, [], used_vmids()
    for _ in todo:                           # their VMIDs, all picked now (the board shows them all)
        while nxt in taken or nxt in ids:
            nxt += 1
        ids.append(nxt)
    passwords = {student_of(r["name"]): r["password"] for r in rows.values() if r["net"] == net}
    host_of = {i: where[student_of(name)] for i, (name, _) in zip(ids, todo)}
    title = f"[3/3] VMs: clone, first boot{', scripts' if any(chains) else ''}"     # the board's first line
    with Board(title, {i: name for i, (name, _) in zip(ids, todo)}, host_of if hosts else None) as board:
        for vmid, (name, k) in zip(ids, todo):
            pw = passwords.setdefault(student_of(name), gen_password())     # a student's VMs: one password
            ip = next_ip(net, rows) if net != "dhcp" else ""
            src = tpl_on[host_of[vmid]][tpl[k]]                              # its host's copy of the template
            try:
                clone_vm(vmid, name, src, net, ip, rows, pw, cores[k], mem[k], disk[k], orig=tpl[k], board=board)
            except LabError as e:
                board.fail(vmid, f"clone: {e}")
        cloned = [i for i in ids if board.rows[i]["state"] == "run"]
        if net != "dhcp":
            refresh_net(net)                 # ports, block rules, isolation for the new VMs
        done = wait_first_boot(cloned, board)
        for i in cloned:
            if i in done and not done[i].startswith("done"):
                warn(f"{rows[i]['name']}: cloud-init ended with '{done[i]}'")
        booted = [i for i in cloned if i in done]
        snapshot_all(booted, board, SNAP, lambda i: "pvelab: after first boot")
        todo_chains = {i: chains[k] for i, (_, k) in zip(ids, todo) if i in done and chains[k]}
        for i in booted:
            if i not in todo_chains:
                board.ok(i, "ready")
        if todo_chains:                      # 'clean' is the before-snapshot here
            run_and_snapshot(todo_chains, pre_snap=False, board=board)

    # --- the summary ---
    summary(board, ids, todo, per, host_of if hosts else None, net, expose, host)
