"""pvelab create"""

from .access import cmd_expose
from ..config import CORES, CRED, DEFAULT_TEMPLATE, ID_START, MEM, PREFIX, SNAP
from ..credentials import cred_rows
from ..expose import is_exposed, set_expose
from ..helptext import bad_usage
from ..inventory import inventory, read_conf, vm_exists
from ..lifecycle import clone_vm, take_snapshot, wait_first_boot
from ..args import opt, valid_net
from ..networks import ensure_bridge, ensure_dhcp_bridge, foreign_use, free_net, next_ip
from ..resources import check_resources, conf_disk_gb, parse_cores, parse_disk, parse_mem
from ..scripts import load_script, run_and_snapshot, script_snapshot_name
from ..templates import ensure_snippet, resolve_template
from ..util import die, warn


def cmd_create(args):
    count, tpl, net, expose, host, script = None, DEFAULT_TEMPLATE, "dhcp", False, None, None
    cores, mem, disk = CORES, MEM, None
    it = iter(args)
    for a in it:
        if a == "--expose":
            expose = True
        elif (v := opt(a, it, "--cores")) is not None:
            cores = parse_cores(v)
        elif (v := opt(a, it, "--memory")) is not None:
            mem = parse_mem(v)
        elif (v := opt(a, it, "--disk")) is not None:
            rel, disk = parse_disk(v)
            if rel:
                bad_usage("create takes the full disk size (--disk 30), not +N")
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
    if not count or not count.isdigit() or int(count) < 1:
        bad_usage("the first argument must be how many VMs to create" if count else "how many VMs?")
    if (expose or host) and net == "dhcp":
        bad_usage("--expose/--host only work together with --net N")
    if host and not expose:
        bad_usage("--host only makes sense with --expose")
    if script:                               # check the file before anything is created
        load_script(script)
        script_snapshot_name(script)
    count = int(count)
    if count > 99:
        bad_usage("at most 99 VMs per create")
    if net == "auto":
        net = free_net()
        print(f">> using net {net} (vmbr{net}, 10.10.{net}.0/24)")
    elif net != "dhcp":
        other = foreign_use(net)
        if other:
            die(f"can't use net{net}: {other}.\n"
                f"  pick another number, or --net auto for the next free one")
    tpl = resolve_template(tpl)
    ensure_snippet()                         # clones read it on first boot: keep it current
    tpl_gb = conf_disk_gb(read_conf(tpl))
    if disk and disk < tpl_gb:
        die(f"--disk {disk}: the template's disk is already {tpl_gb:g} GB - disks can only grow")
    if net == "dhcp":
        pfx = PREFIX
        ensure_dhcp_bridge()
    else:
        pfx = f"net{net}-{PREFIX}"

    existing = {c.get("name") for c in inventory().values()}
    todo = []
    for i in range(1, count + 1):
        name = f"{pfx}{i:02d}"
        if name in existing:
            print(f"== {name} already exists, skipping")
        else:
            todo.append(name)
    exp_args = ["--net", net] + (["--host", host] if host else [])
    if not todo:
        print("nothing to create")
        if expose:
            cmd_expose(exp_args)
        return
    check_resources(len(todo), mem)
    if net != "dhcp":
        ensure_bridge(net)
    rows, nxt, made = cred_rows(), ID_START, []
    for name in todo:
        while vm_exists(nxt):
            nxt += 1
        clone_vm(nxt, name, tpl, net, next_ip(net, rows) if net != "dhcp" else "", rows, cores, mem, disk)
        made.append(nxt)
        nxt += 1
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
    print(f">> '{SNAP}' snapshot taken on {len([i for i in made if i in done])}/{len(made)} VM(s)")
    if script and any(i in done for i in made):
        run_and_snapshot([i for i in made if i in done], script)
    print(f"created {len(todo)} VM(s). Credentials: {CRED}")
    if expose:
        print()
        cmd_expose(exp_args)                 # opens the ports and prints the ssh commands
    elif net != "dhcp" and is_exposed(net):
        set_expose(net, True)
