"""pvelab net set NAME [--cidr CIDR] [--gateway ADDR]: a class network's range or gateway changed while it
has VMs, keeping the VMs, their disks, passwords and SSH host keys.

How: the gateway's bridge gets the new address (and the SDN subnet the new range); every rule that names
the VMs' addresses (isolation, ssh forwards, blocks) is made again from the new ones. Each VM keeps its
place in the range (.11 stays .11) when it fits; its cloud-init ipconfig0 gets the new address and
gateway, in the current config and in its snapshots' (a rollback lands on the new range too). PVE then
gives the VM a new cloud-init instance id, so at the next boot cloud-init sets up the network again -
and would also make new SSH host keys and set the handout password again. So before the reboot, each
running VM is told to keep its keys (/etc/cloud/cloud.cfg.d/99-pvelab-keep-host-keys.cfg) and its
user's password hash is saved; after the reboot the hash is put back."""

import ipaddress
import os
import re
import shutil
import time

from . import nets, sdn, ui
from .config import bridge, IP_FIRST, is_sdn, LAB_DIR, NODES_DIR, CRED, IFACES
from .credentials import cred_rows, cred_save
from .guest import agent_out
from .ifaces import add_net_block, bridge_is_ours, has_section, remove_net_block
from .lifecycle import boot, first_boot_state, booting, ip_config
from .networks import bridge_stanza, link_exists, net_rules, SETTERS
from .nodes import node_of, on_node, require_quorum
from .ui import each, paint, Plan, Spinner
from .util import die, LabError, parallel, run

KEEP_KEYS = "/etc/cloud/cloud.cfg.d/99-pvelab-keep-host-keys.cfg"
CLOUD_INIT_WAIT = 600


def new_ips(ips, old, new, gw):
    """{vmid: new address} for {vmid: old address} moved from range old to range new (gateway gw): each
    keeps its offset in the range when that fits (not the gateway, not the net's own or broadcast
    address), the others get the free addresses from .11 up. None when they do not all fit."""
    out, taken = {}, {gw}
    rest = []
    for vmid, ip in sorted(ips.items()):
        try:
            a = new.network_address + (int(ipaddress.ip_address(ip)) - int(old.network_address))
        except (ValueError, OverflowError):
            a = None
        if a is not None and a in new and a not in (new.network_address, new.broadcast_address) and a not in taken:
            out[vmid] = a
            taken.add(a)
        else:
            rest.append(vmid)
    free = (a for a in new.hosts() if int(a) - int(new.network_address) >= IP_FIRST and a not in taken)
    for vmid in rest:
        a = next(free, None)
        if a is None:
            return None
        out[vmid] = a
        taken.add(a)
    return {v: str(a) for v, a in out.items()}


def with_snapshots_ipconfig(text, value):
    """A VM config's text with ipconfig0 set to value in every snapshot section ([name]); the current
    config (qm set changes it) and PVE's [special:...] sections are left as they are."""
    parts = re.split(r"(?m)^(?=\[)", text)
    out = [parts[0]]
    for p in parts[1:]:
        if not p.startswith("[special:"):
            p = re.sub(r"(?m)^ipconfig0: .*$", f"ipconfig0: {value}", p)
        out.append(p)
    return "".join(out)


def survey(name):
    n = nets.resolve(name)
    if n is None:
        die(f"no network '{name}' (see: pvelab net list)")
    if not bridge_is_ours(n):
        die(f"{nets.net_name(n)}'s gateway is not on this host: run pvelab net set on the host that has {bridge(n)}")
    rows = {i: r for i, r in cred_rows().items() if r["net"] == n}
    return n, rows


def net_set(name, cidr_opt, gw_opt, dry=False):
    if not cidr_opt and not gw_opt:
        die("nothing to change: give --cidr CIDR and/or --gateway ADDR")
    with nets.cached_inputs():
        n, rows = survey(name)
        label = nets.net_name(n)
        old_net, old_gw = nets.cidr(n), nets.gateway(n)
        new_net = nets.parse_cidr(cidr_opt) if cidr_opt else old_net
        if new_net != old_net:
            nets.cidr_free(n, new_net)
        new_gw = nets.parse_gateway(gw_opt, new_net) if gw_opt else (
            old_gw if new_net == old_net else new_net.network_address + (int(old_gw) - int(old_net.network_address)))
        if new_gw not in new_net or new_gw in (new_net.network_address, new_net.broadcast_address):
            new_gw = new_net.network_address + 1
        if (new_net, new_gw) == (old_net, old_gw):
            die(f"{label} has {old_net} with the gateway {old_gw} already")
        ips = new_ips({i: r["ip"] for i, r in rows.items()}, old_net, new_net, new_gw)
        if ips is None:
            die(f"{new_net} has no room for the {len(rows)} VM(s) of {label} (from .{IP_FIRST} up)")
    require_quorum("the network can't be changed")
    if is_sdn(n):
        sdn.refuse_foreign()
    status = dict(parallel(vm_state, sorted(rows)))
    running = [i for i in sorted(rows) if status[i] == "running"]
    stopped = [i for i in sorted(rows) if status[i] != "running"]
    plan = Plan()
    plan.do(f"back up /etc/network/interfaces, {os.path.basename(CRED)} and the VMs' configs")
    plan.do(f"{label}: {old_net} gateway {old_gw} -> {new_net} gateway {new_gw} ({bridge(n)}"
            + (", the SDN subnet on every host)" if is_sdn(n) else ")"))
    plan.do("isolation, ssh forwards and blocks made again for the new addresses")
    if rows:
        moved = [f"{rows[i]['name']} {rows[i]['ip']} -> {ips[i]}" for i in sorted(rows)]
        plan.do("the VMs' addresses (also in their snapshots): " + ui.some(moved, 6))
    if running:
        plan.do(f"reboot the {len(running)} running VM(s) so they take the new address; their passwords "
                "and SSH host keys are kept")
        plan.warn("the running VMs reboot: students lose their sessions - do it outside class time")
    if stopped:
        plan.warn(f"{len(stopped)} VM(s) are not running ({ui.some([rows[i]['name'] for i in stopped], 5)}): "
                  "they take the new address at their next start, with new SSH host keys and the handout "
                  "password again (no guest agent to tell them)")
    if rows:
        plan.warn("after a later rollback, a VM gets new SSH host keys once (the snapshot's disk does not "
                  "have the keep-keys setting): students see a host key warning")
    if is_sdn(n):
        plan.warn("applying SDN reloads the network of every host: students on other SDN nets may lose the "
                  "connection for a few seconds")
    if dry:
        plan.show()
        print("dry run - nothing was changed")
        return
    plan.ask()
    backup = back_up(rows)
    kept = prepare_guests(running, rows)
    with Spinner(f"{label}: {new_net}, gateway {new_gw} on {bridge(n)}") as line:
        readdress_host(n, label, new_net, new_gw, ips)
        line.ok(f"{label}: {new_net}, gateway {new_gw}; rules made again for the new addresses")
    with Spinner("the VMs' new addresses in their configs and snapshots") as line:
        for i in sorted(rows):
            set_vm_address(i, ips[i], n)
        line.ok(f"{len(rows)} VM config(s) changed")
    if running:
        reboot_all(running, rows, kept, ips)
    print(paint(f"\n{label} is {new_net} now, gateway {new_gw}.", "green"))
    print(f"  backup: {backup}" + (f"\n  log: {ui.LOG}" if ui.LOG else ""))
    if stopped:
        print(f"  not running: {ui.some([rows[i]['name'] for i in stopped], 8)} - they take the new address "
              "when started (pvelab start ...)")


def vm_state(vmid):
    out = run(*on_node(node_of(vmid), "qm", "status", vmid), check=False, quiet=True)
    return out.split(":", 1)[1].strip() if ":" in out else "unknown"


def conf_path(vmid):
    return f"{NODES_DIR}/{node_of(vmid)}/qemu-server/{vmid}.conf"


def back_up(rows):
    d = f"{LAB_DIR}/net-set-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    os.makedirs(d, mode=0o700, exist_ok=True)
    shutil.copy2(IFACES, f"{d}/interfaces")
    if os.path.exists(CRED):
        shutil.copy2(CRED, f"{d}/{os.path.basename(CRED)}")
    for i in rows:
        shutil.copy(conf_path(i), f"{d}/{i}.conf")
    return d


def prepare_guests(running, rows):
    """Tell each running VM to keep its SSH host keys at the next cloud-init run, and save its user's
    password hash: {vmid: hash} of those that answered (empty hash: none to put back)."""
    script = (f"mkdir -p {os.path.dirname(KEEP_KEYS)} && echo 'ssh_deletekeys: false' > {KEEP_KEYS} && "
              "getent shadow \"$1\" | cut -d: -f2")

    def one(i):
        out = agent_out(i, "sh", "-c", script, "sh", rows[i]["user"] or "student", wait=20, timeout=30)
        return None if out is None else out.strip()
    with Spinner(f"{len(running)} VM(s): keep their SSH host keys, save their passwords") as line:
        got = parallel(one, running, workers=8)
        missed = [rows[i]["name"] for i in running if got.get(i) is None]
        if missed:
            line.fail(f"no guest agent in {ui.some(missed, 5)}: they get new SSH host keys and the handout "
                      "password")
        else:
            line.ok(f"{len(running)} VM(s) keep their SSH host keys and passwords")
    return {i: h for i, h in got.items() if h is not None}


def readdress_host(n, label, new_net, new_gw, ips):
    """The gateway side: the bridge's address, the SDN subnet, and every rule made again from the new
    addresses (the old rules go down with the bridge)."""
    br, made_by_sdn = bridge(n), is_sdn(n)          # read before the block goes (bridge() reads it)
    on = [k for k in SETTERS if k == "block" or has_section(k, n)]
    if link_exists(br):
        run("ifdown", br, check=False)       # runs the old post-down lines: the old NAT and rules go
    for kind, setter in SETTERS.items():
        setter(n, False)
    remove_net_block(n)
    nets.register(n, label, new_net, new_gw)
    add_net_block(n, bridge_stanza(n, br, net_rules(br), sdn_made=made_by_sdn))
    if made_by_sdn:
        with sdn.session():
            sdn.replace_subnet(n)
            sdn.apply()                      # every host reloads; the gateway's bridge comes up with the new address
    else:
        run("ifup", br)
    if f"inet {new_gw}/" not in run("ip", "-4", "addr", "show", br, check=False, quiet=True):
        die(f"{br} came up without its new gateway {new_gw} (see {ui.LOG or 'ip addr show ' + br})")
    all_rows = cred_rows()
    for i, ip in ips.items():
        all_rows[i]["ip"] = ip
    cred_save(all_rows)
    for kind in on:
        SETTERS[kind](n, True)


def set_vm_address(vmid, ip, n):
    value = ip_config(n, ip)
    run(*on_node(node_of(vmid), "qm", "set", vmid, "--ipconfig0", value))
    path = conf_path(vmid)
    text = open(path).read()
    new = with_snapshots_ipconfig(text, value)
    if new != text:
        with open(path, "w") as f:            # /etc/pve: written in place (pmxcfs)
            f.write(new)


def reboot_all(running, rows, kept, ips):
    names = {i: rows[i]["name"] for i in running}

    def one(i, board):
        node = node_of(i)
        boot(i, node, board, ("reboot", "--timeout", "60"), "rebooting", said="rebooted, but ")
        board.stage(i, "cloud-init (new address)")
        t0 = time.time()
        while booting(first_boot_state(i, node)[0]):
            if time.time() - t0 > CLOUD_INIT_WAIT:
                raise LabError(f"cloud-init not finished after {CLOUD_INIT_WAIT // 60} min")
            time.sleep(4)
        if kept.get(i):
            user = rows[i]["user"] or "student"
            if agent_out(i, "usermod", "-p", kept[i], user, node=node) is None:
                raise LabError(f"running at {ips[i]}, but its password was not put back "
                               f"(pvelab list --passwords has the handout one)")
        return f"running at {ips[i]}"
    each(f"reboot: {len(running)} VM(s) onto the new addresses", running, names, one, workers=8,
         failed="{failed} of {total} VM(s) did not come back cleanly")
