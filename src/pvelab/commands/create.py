"""pvelab create"""

import re

from ..config import bridge, CORES, CRED, DEFAULT_TEMPLATE, ID_START, MEM, PREFIX, SNAP, subnet
from ..credentials import cred_rows, gen_password
from ..expose import expose_net
from ..isolation import is_isolated, set_isolation, student_of, vms_per_student
from ..helptext import bad_usage
from ..inventory import inventory, read_conf, vm_exists
from ..lifecycle import clone_vm, take_snapshot, wait_first_boot
from ..args import opt, valid_net
from ..networks import ensure_bridge, ensure_dhcp_bridge, foreign_use, free_net, is_new_net, next_ip, refresh_net
from ..resources import check_resources, conf_disk_gb, full_disk, parse_cores, parse_mem
from ..scripts import check_script, run_and_snapshot
from ..templates import ensure_snippet, resolve_template
from ..util import die, warn


def per_vm(what, v, per, parse):
    """'4' or '2,4,4' -> one parsed value for each of a student's per VMs (the k-th for VM -k)."""
    vals = str(v).split(",")
    if len(vals) not in (1, per):
        bad_usage(f"{what} {v}: one value, or one for each of a student's {per} VMs" if per > 1
                  else f"{what} {v}: several values only with STUDENTSxVMS (20x3)")
    return [parse(x) for x in vals] * (per // len(vals))


def cmd_create(args):
    count, tpl, net, expose, host, script = None, DEFAULT_TEMPLATE, "dhcp", False, None, None
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
    if script:                               # check the file before anything is created
        script = check_script(script)
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

    existing = {c.get("name") for c in inventory().values()}
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
    check_resources(len(todo), sum(mem[k] for _, k in todo))
    if net != "dhcp":
        new_net = is_new_net(net)
        ensure_bridge(net)
        if new_net and isolate:              # before the VMs start: nothing between students
            set_isolation(net, True)
        elif not new_net and not isolate and is_isolated(net):
            warn(f"net{net} already isolates its students; to let them reach each other: "
                 f"pvelab isolate --net {net} --off")
    rows, nxt, made = cred_rows(), ID_START, []
    passwords = {student_of(r["name"]): r["password"] for r in rows.values() if r["net"] == net}
    for name, k in todo:
        while vm_exists(nxt):
            nxt += 1
        pw = passwords.setdefault(student_of(name), gen_password())     # a student's VMs: one password
        clone_vm(nxt, name, tpl[k], net, next_ip(net, rows) if net != "dhcp" else "", rows, pw,
                 cores[k], mem[k], disk[k])
        made.append(nxt)
        nxt += 1
    if net != "dhcp":
        refresh_net(net)                     # ports, block rules, isolation for the new VMs
    done = wait_first_boot(made)
    for i in made:
        name = rows[i]["name"]
        if i not in done:
            warn(f"{name}: first boot not finished in time - no '{SNAP}' snapshot "
                 f"(take it later: pvelab snapshot {name})")
            continue
        if not done[i].startswith("done"):
            warn(f"{name}: cloud-init ended with '{done[i]}'")
        take_snapshot(i, SNAP, "pvelab: after first boot")
    booted = [i for i in made if i in done]
    print(f">> '{SNAP}' snapshot taken on {len(booted)}/{len(made)} VM(s)")
    if script and booted:
        run_and_snapshot(booted, script)
    shape = f" ({len(todo) // per} students x {per})" if per > 1 else ""
    print(f"created {len(todo)} VM(s){shape}. Credentials: {CRED}")
    if net != "dhcp" and is_isolated(net):
        print(f"net{net}: students can't reach each other's VMs")
    if expose:
        print()
        expose_net(net, host)                # opens the ports and prints the ssh commands
