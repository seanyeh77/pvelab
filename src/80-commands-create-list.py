# ================================================================ commands
def cmd_create(args):
    count, tpl, net, expose, host, script = None, DEFAULT_TEMPLATE, "dhcp", False, None, None
    cores, mem, disk = CORES, MEM, None
    it = iter(args)
    for a in it:
        if a == "--expose":
            expose = True
        elif a in ("--cores", "--memory", "--disk"):
            v = next(it, None)
            if a == "--cores":
                cores = parse_cores(v)
            elif a == "--memory":
                mem = parse_mem(v)
            else:
                rel, disk = parse_disk(v)
                if rel:
                    bad_usage("create takes the full disk size (--disk 30), not +N")
        elif a == "--script":
            script = next(it, None) or bad_usage("--script needs a file")
        elif a.startswith("--script="):
            script = a.split("=", 1)[1]
        elif a == "--host":
            host = next(it, None) or bad_usage("--host needs an address")
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
        elif a in ("--net", "--template", "-t"):
            v = next(it, None)
            if v is None:
                bad_usage(f"{a} needs a value")
            if a == "--net":
                net = v if v == "auto" else valid_net(v)
            else:
                tpl = v
        elif a.startswith("--net="):
            v = a.split("=", 1)[1]
            net = v if v == "auto" else valid_net(v)
        elif a.startswith("--template="):
            tpl = a.split("=", 1)[1]
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
    exp_args = [net] + (["--host", host] if host else [])
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


def cmd_list(args):
    ssh_only, only_net = False, None
    it = iter(args)
    for a in it:
        if a == "--ssh":
            ssh_only = True
        elif a == "--net":
            only_net = next(it, None) or bad_usage("--net needs a number (or dhcp)")
        elif a.startswith("--net="):
            only_net = a.split("=", 1)[1]
        else:
            bad_usage(f"unknown option '{a}'")
    if only_net is not None:
        only_net = "dhcp" if only_net == "dhcp" else valid_net(only_net.removeprefix("net"))
    rows = cred_rows()
    vms = {i: c for i, c in managed().items()
           if only_net is None or rows.get(i, {}).get("net", desc_value(c, "net") or "dhcp") == only_net}
    exposed = {n for n in {r["net"] for r in rows.values()} if n != "dhcp" and is_exposed(n)}
    if ssh_only:
        return list_ssh(vms, rows, exposed, only_net)
    status = {int(l.split()[0]): l.split()[2] for l in run("qm", "list").splitlines()[1:] if l.split()}
    blocks = blocks_load()
    fmt = "{:<18} {:<6} {:<18} {:<11} {:<9} {:<12} {:<15} {:<6} {:<13} {:<9} {}"
    print(fmt.format("NAME", "VMID", "TEMPLATE", "NET", "STATUS", "SIZE", "IP", "SSH", "BLOCKED", "USER",
                     "PASSWORD"))
    for vmid, c in vms.items():
        r = rows.get(vmid, {})
        tpl = desc_value(c, "template")
        print(fmt.format(c.get("name", ""), vmid, template_label(int(tpl)) if tpl.isdigit() else "?",
                         net_label(r.get("net", "dhcp")), status.get(vmid, "?"), size_label(c),
                         vm_ip(vmid, rows),
                         ssh_port(vmid) if r.get("net") in exposed else "-", block_label(vmid, blocks),
                         r.get("user", "?"),
                         r.get("password", "")))
    if exposed & {rows.get(i, {}).get("net") for i in vms}:
        print(f"\nstudents: ssh -p <SSH> <USER>@{public_host()}   (pvelab list --ssh for the full commands)")
    pct, free = storage()
    print(f"\n{STORAGE}: {pct if pct is not None else '?'}% used, {free} GB free")
    if pct is not None and pct >= 80:
        warn(f"{STORAGE} is over 80% full")


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
        print(f"\nnet{n} is not exposed - open it with: pvelab expose {n}")
    if "dhcp" in closed:
        print(f"\nVMs on {DHCP_BRIDGE}(dhcp) can't be exposed; create them with --net N instead")


def cmd_templates(_):
    on_node = node_templates()
    fmt = "  {:<6} {:<20} {:<20} {}"
    print("Catalogue:")
    print(fmt.format("VMID", "NAME", "ALIASES", "STATUS"))
    for t in TEMPLATES:
        if t.vmid in on_node:
            st = "on this node"
        elif vm_exists(t.vmid):
            st = "VMID in use by something else!"
        else:
            st = "not built (pvelab pull " + (t.aliases[0] if t.aliases else t.name) + ")"
        if str(t.vmid) == DEFAULT_TEMPLATE or DEFAULT_TEMPLATE in (t.name, *t.aliases):
            st += "  (default)"
        print(fmt.format(t.vmid, t.name, ",".join(t.aliases), st))
    other = {i: n for i, n in on_node.items() if not catalogue_find(i)}
    if other:
        print("\nOther templates on this node (usable by VMID or name):")
        for i, n in other.items():
            print(fmt.format(i, n, "", ""))


def cmd_pull(args):
    if not args:
        bad_usage("which template(s)?")
    todo = TEMPLATES if args == ["all"] else [catalogue_find(a) or die(f"unknown template '{a}' "
                                                                     "(see: pvelab templates)") for a in args]
    for t in todo:
        build_template(t)


def cmd_expose(args):
    n, off, host = None, False, None
    it = iter(args)
    for a in it:
        if a == "--off":
            off = True
        elif a == "--host":
            host = next(it, None) or bad_usage("--host needs an address")
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
        elif n is None:
            n = valid_net(a)
        else:
            bad_usage(f"unexpected '{a}'")
    if n is None:
        bad_usage("which net? (the number, e.g. 40)")
    if not bridge_is_ours(n):
        die(f"net{n} has no lab bridge (create VMs with --net {n} first)")
    if host:
        with open(PUBLIC_HOST_FILE, "w") as f:
            f.write(host + "\n")
    if off:
        if not is_exposed(n):
            print(f"net{n} is not exposed")
            return
        set_expose(n, False)
        print(f"net{n}: ssh port forwards removed")
        return
    set_expose(n, True)
    rows = cred_rows()
    ids = sorted(v for v, r in rows.items() if r["net"] == n)
    h = public_host()
    for v in ids:
        print(f"{rows[v]['name']:<18} ssh -p {ssh_port(v)} {GUEST_USER}@{h}")
    if ids:
        print(f"\nnet{n} exposed. Allow TCP {ssh_port(ids[0])}-{ssh_port(ids[-1])} in the cloud "
              "security group / firewall in front of this host.")
    else:
        print(f"net{n} exposed (no VMs yet; ports follow create/destroy)")


def cmd_power(action, args):
    for vmid in targets(args):
        name, st = read_conf(vmid).get("name"), vm_status(vmid)
        if action == "start":
            if st == "running":
                print(f"{name}: already running")
                continue
            run("qm", "start", vmid)
            print(f"{name}: started")
        elif action == "stop":
            if st == "stopped":
                print(f"{name}: already stopped")
                continue
            run("qm", "shutdown", vmid, "--timeout", "60", "--forceStop", "1")
            print(f"{name}: stopped")
        else:
            if st != "running":
                print(f"{name}: not running, starting")
                run("qm", "start", vmid)
                continue
            run("qm", "reboot", vmid, "--timeout", "60")
            print(f"{name}: rebooted")


