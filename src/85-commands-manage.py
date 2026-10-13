# ================================================================ commands: run, resize, rollback, snapshot, destroy, ssh
def cmd_run(args):
    timeout, rest = RUN_TIMEOUT_MIN, []
    it = iter(args)
    for a in it:
        if a == "--timeout":
            v = next(it, None)
            if not v or not v.isdigit() or int(v) < 1:
                bad_usage("--timeout needs minutes (a number)")
            timeout = int(v)
        else:
            rest.append(a)
    if len(rest) < 2:
        bad_usage("which VM(s), and which script?")
    *vms, path = rest
    load_script(path)
    script_snapshot_name(path)
    codes = run_and_snapshot(targets(vms), path, timeout, pre_snap=True)
    if any(c != 0 for c in codes.values()):
        sys.exit(1)


def snapshot_names(vmid):
    try:
        return re.findall(r"^\[([^\]]+)\]$", open(f"{CONF_DIR}/{vmid}.conf").read(), re.M)
    except FileNotFoundError:
        return []


def cmd_resize(args):
    cores = mem = disk = None
    reboot, rest = False, []
    it = iter(args)
    for a in it:
        if a == "--cores":
            cores = parse_cores(next(it, None))
        elif a == "--memory":
            mem = parse_mem(next(it, None))
        elif a == "--disk":
            disk = parse_disk(next(it, None))
        elif a == "--reboot":
            reboot = True
        else:
            rest.append(a)
    if cores is None and mem is None and disk is None:
        bad_usage("what to change: --cores, --memory and/or --disk")
    ids = targets(rest)
    confs = {i: read_conf(i) for i in ids}
    if disk and not disk[0]:                 # absolute size: refuse to shrink anything
        small = [f"{confs[i]['name']}({conf_disk_gb(confs[i]):g}G)" for i in ids if disk[1] < conf_disk_gb(confs[i])]
        if small:
            die(f"--disk {disk[1]}: disks can only grow, these are bigger already: {' '.join(small)}")
    if mem:
        more = sum(max(0, mem - int(confs[i].get("memory", 0))) for i in ids)
        if more and more + 2048 > mem_available_mb():
            warn(f"memory: {more} MB more needed, {mem_available_mb()} MB available")
            if not FORCE:
                die("not enough memory on this host (LAB_FORCE=1 to do it anyway)")
    pending = []
    for i in ids:
        c, name, done = confs[i], confs[i]["name"], []
        sets = []
        if cores is not None and int(c.get("cores", 1)) != cores:
            sets += ["--cores", cores]
            done.append(f"cores {c.get('cores', 1)}->{cores}")
        if mem is not None and int(c.get("memory", 0)) != mem:
            sets += ["--memory", mem]
            done.append(f"memory {c.get('memory')}->{mem} MB")
        if sets:
            run("qm", "set", i, *sets)
        if disk:
            cur = conf_disk_gb(c)
            target = cur + disk[1] if disk[0] else disk[1]
            if target > cur:
                run("qm", "disk", "resize", i, "scsi0", f"{target:g}G")
                done.append(f"disk {cur:g}->{target:g} GB")
        print(f"{name}: " + (", ".join(done) if done else "nothing to change"))
        if done and vm_status(i) == "running":
            pending.append(i)
    if not pending:
        return
    names = " ".join(confs[i]["name"] for i in pending)
    if reboot:
        for i in pending:
            run("qm", "reboot", i, "--timeout", "120")
            print(f"{confs[i]['name']}: rebooted")
    else:
        print(f"\ntakes effect after a reboot (the disk's filesystem grows then too): {names}\n"
              f"  pvelab reboot {' '.join(confs[i]['name'] for i in pending)}   (or add --reboot)")


def cmd_rollback(args):
    snap, rest = SNAP, []
    it = iter(args)
    for a in it:
        if a == "--initial":
            snap = SNAP_INITIAL
        elif a == "--to":
            snap = next(it, None) or bad_usage("--to needs a snapshot name")
        elif a.startswith("--to="):
            snap = a.split("=", 1)[1]
        else:
            rest.append(a)
    ids = targets(rest)
    names = {i: read_conf(i)["name"] for i in ids}
    missing = [names[i] for i in ids if not has_snapshot(i, snap)]
    if missing:
        have = sorted({n for i in ids for n in snapshot_names(i)} - {"pending"})
        die(f"no '{snap}' snapshot on: {' '.join(missing)}"
            + (f" (snapshots there: {', '.join(have)})" if have else " (no snapshots there; take one with 'pvelab snapshot')"))
    confirm_yes(f"Roll back to '{snap}' (everything done since is lost): "
                + " ".join(f"{names[i]}({i})" for i in ids))
    for i in ids:
        run("qm", "rollback", i, snap, "--start", "1")
        print(f"{names[i]}: rolled back to '{snap}' and started")


def cmd_snapshot(args):
    ids = targets(args)
    for i in ids:
        name = read_conf(i)["name"]
        take_snapshot(i, SNAP, "pvelab: taken by 'pvelab snapshot'")
        print(f"{name}: '{SNAP}' snapshot = current state")


def cmd_destroy(args):
    ids = targets(args)
    if not ids:
        print("no student VMs")
        return
    inv, rows = inventory(), cred_rows()
    confirm_yes("About to DESTROY: " + " ".join(f"{inv[i]['name']}({i})" for i in ids))
    nets = {rows.get(i, {}).get("net", "dhcp") for i in ids}
    blocks = blocks_load()
    for i in ids:
        remove_vm(i)
        rows.pop(i, None)
        cred_save(rows)
        if blocks.pop(i, None) is not None:
            blocks_save(blocks)
        print(f"{inv[i]['name']} destroyed")
    for n in sorted(nets - {"dhcp"}):
        if bridge_is_ours(n) and is_exposed(n):
            set_expose(n, True)                  # drop the destroyed VMs' ports
        if bridge_is_ours(n) and block_re(n).search(open(IFACES).read()):
            apply_blocks(n)                      # and their block rules
        if any(r["net"] == n for r in rows.values()) or not bridge_is_ours(n):
            continue
        if YES:
            remove_bridge(n)
        elif sys.stdin.isatty():
            if input(f"net{n} has no VMs left. Remove bridge vmbr{n} too? [y/N] ").strip().lower() == "y":
                remove_bridge(n)
            else:
                print(f"keeping vmbr{n}")
        else:
            print(f"net{n} has no VMs left; bridge vmbr{n} kept "
                  "(re-run interactively or with LAB_YES=1 to remove it)")


def cmd_ssh(args, lock):
    if not args:
        bad_usage("which VM?")
    vmid = resolve(args[0])
    ip = vm_ip(vmid, cred_rows())
    if ip == "-":
        die(f"no IP yet for VMID {vmid} (still booting?)")
    lock.close()                     # don't hold the lab lock for the whole session
    # Private NAT networks whose VMs get rolled back or re-created with reused IPs: don't pin host keys.
    os.execvp("ssh", ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                      "-o", "LogLevel=ERROR", f"{GUEST_USER}@{ip}", *args[1:]])


