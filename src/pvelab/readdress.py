"""pvelab net set NAME [--cidr CIDR] [--gateway ADDR] and pvelab net rename OLD NEW: a class network
changed while it has VMs, keeping the VMs, their disks, passwords and SSH host keys.

net set: the gateway's bridge gets the new address (and the SDN subnet the new range); every rule that
names the VMs' addresses (isolation, ssh forwards, blocks) is made again from the new ones. Each VM keeps
its place in the range (.11 stays .11) when it fits; its cloud-init ipconfig0 gets the new address and
gateway, in its config and its snapshots' (a rollback lands on the new range too).
net rename: the net's name line, its SDN vnet's alias and its VMs' names (old-student01 -> new-student01;
their hostname is their name), in their configs, snapshots and the credentials.

Either way PVE gives each VM a new cloud-init instance id (its network or hostname changed), so at its next
boot cloud-init sets it up again: the new address or hostname. The vendor-data keeps its SSH host keys
(ssh_deletekeys false); its user's password would go back to the handout one, so for the running VMs it is
saved before their reboot and put back after."""

import contextlib
import ipaddress
import os
import re
import shutil
import time

from . import nets, sdn, ui
from .args import net_arg
from .config import bridge, CRED, GUEST_USER, IFACES, IP_FIRST, is_sdn, LAB_DIR, READY_TIMEOUT
from .credentials import cred_rows, cred_save
from .guest import agent_out
from .ifaces import add_net_block, blocks, bridge_is_ours, has_section, save_ifaces
from .inventory import conf_path, inventory, vm_statuses
from .lifecycle import boot, booting, first_boot_state, ip_config, set_conf
from .networks import bridge_stanza, net_rules, require_gateway, SETTERS, take_down
from .nodes import node_of, require_quorum
from .templates import ensure_snippet
from .ui import each, paint, Plan, Spinner
from .util import die, LabError, parallel, run, warn


def local_net(v, verb):
    """Net v's number, if its gateway is on this host (where its block is); else stop."""
    n = net_arg(v)
    if not bridge_is_ours(n):
        die(f"{nets.net_name(n)}'s gateway is not on this host: run pvelab net {verb} on the host that has "
            f"{bridge(n)}")
    return n


def new_ips(ips, old, new, gw):
    """{vmid: new address} for {vmid: old address} moved from range old to range new (gateway gw): each
    keeps its offset in the range when that fits (a usable address, not the gateway), the others get the
    free addresses from .11 up. None when they do not all fit."""
    out, taken, rest = {}, {gw}, []
    for vmid, ip in sorted(ips.items()):
        try:
            a = new.network_address + (int(ipaddress.ip_address(ip)) - int(old.network_address))
        except (ValueError, OverflowError):
            a = None
        if a is not None and nets.usable(a, new) and a not in taken:
            out[vmid] = a
            taken.add(a)
        else:
            rest.append(vmid)
    free = (a for a in nets.addresses(new, gw) if a not in taken)
    for vmid in rest:
        a = next(free, None)
        if a is None:
            return None
        out[vmid] = a
        taken.add(a)
    return {v: str(a) for v, a in out.items()}


class Change:
    """What net set and net rename share: the net's VMs (running or not), the warnings, the backup, and the
    reboot that brings the running ones onto the change with their passwords kept."""

    def __init__(self, n, what):
        self.n, self.what = n, what              # what: "address" / "name", in the texts
        require_quorum("the network can't be changed")
        if is_sdn(n):
            sdn.refuse_foreign()
        rows = {i: r for i, r in cred_rows().items() if r["net"] == n}
        self.node = {i: h for i in rows if (h := node_of(i))}
        if gone := [r["name"] for i, r in rows.items() if i not in self.node]:
            warn(f"recorded on {nets.net_name(n)} but gone (deleted outside pvelab): {ui.some(gone, 5)} - left as "
                 "they are")
        self.rows = {i: r for i, r in rows.items() if i in self.node}
        status = vm_statuses(sorted(self.rows))
        self.running = [i for i in sorted(self.rows) if status[i] == "running"]
        self.stopped = [i for i in sorted(self.rows) if status[i] != "running"]
        self.vms, self.kept = set(), {}
        self.plan = Plan()
        self.plan.do(f"back up /etc/network/interfaces, {os.path.basename(CRED)} and the VMs' configs")

    def warnings(self, vms):
        """The steps and warnings about the VMs that change (vms), and the SDN apply."""
        self.vms = vms
        running, stopped = [i for i in self.running if i in vms], [i for i in self.stopped if i in vms]
        if running:
            self.plan.do(f"reboot the {len(running)} running VM(s) onto the new {self.what}; their passwords "
                         "and SSH host keys are kept")
            self.plan.warn("the running VMs reboot: students lose their sessions - do it outside class time")
        if stopped:
            self.plan.warn(f"{len(stopped)} VM(s) are not running "
                           f"({ui.some([self.rows[i]['name'] for i in stopped], 5)}): they take the new "
                           f"{self.what} at their next start, with the handout password again")
        if is_sdn(self.n):
            self.plan.warn(sdn.RELOAD_WARNING)

    def start(self, dry):
        """Ask (or show, on a dry run): True to go on. Then the backup and the running VMs' passwords."""
        if not self.plan.go(dry):
            return False
        self.backup = f"{LAB_DIR}/net-change-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        os.makedirs(self.backup, mode=0o700, exist_ok=True)
        shutil.copy2(IFACES, f"{self.backup}/interfaces")
        if os.path.exists(CRED):
            shutil.copy2(CRED, f"{self.backup}/{os.path.basename(CRED)}")
        for i in self.rows:
            shutil.copy(conf_path(i), f"{self.backup}/{i}.conf")
        self.kept = self.save_passwords([i for i in self.running if i in self.vms])
        return True

    def save_passwords(self, ids):
        """{vmid: its user's password hash} of the running VMs ids whose guest agent answered."""
        if not ids:
            return {}

        def one(i):
            out = agent_out(i, "getent", "shadow", self.rows[i]["user"] or GUEST_USER, node=self.node[i])
            return out.split(":")[1] if out and out.count(":") > 1 else None
        with Spinner(f"{len(ids)} VM(s): saving their passwords") as line:
            got = parallel(one, ids, workers=8)
            missed = [self.rows[i]["name"] for i in ids if not got.get(i)]
            if missed:
                line.fail(f"no guest agent in {ui.some(missed, 5)}: they get the handout password back")
            else:
                line.ok(f"{len(ids)} VM(s): passwords saved")
        return {i: h for i, h in got.items() if h}

    def set_vms(self, key, values):
        """key (ipconfig0, name) of each VM in values, in its config and snapshots: all at once."""
        if not values:
            return
        def one(i):
            try:
                set_conf(i, key, values[i], node=self.node[i])
            except (LabError, OSError) as e:
                return str(e)
        with Spinner(f"the VMs' new {self.what} in their configs and snapshots") as line:
            failed = {i: e for i, e in parallel(one, sorted(values), workers=8).items() if e}
            if failed:
                line.fail(f"not changed in {ui.some([self.rows[i]['name'] for i in failed], 5)}: "
                          f"{next(iter(failed.values()))} - see the log; the backup has their configs")
            else:
                line.ok(f"{len(values)} VM config(s) changed")
        self.vms = {i for i in self.vms if i not in failed}   # not rebooted onto a change they do not have

    def finish(self, done, rows):
        """Reboot the running VMs that changed (done: {vmid: the text its line ends with}; rows: their rows
        as they are now) and put their passwords back; then where the backup is."""
        ids = [i for i in self.running if i in self.vms]
        for host in sorted({self.node[i] for i in self.vms}):     # stopped ones too: they boot with it later
            try:
                ensure_snippet(host)             # the vendor-data that keeps SSH host keys, there too
            except LabError as e:
                warn(f"vendor-data on {host} not updated ({e}): its VMs may get new SSH host keys")
        if ids:
            reboot_all(ids, rows, self.kept, done, self.node, self.what)
        print(f"  backup: {self.backup}" + (f"\n  log: {ui.LOG}" if ui.LOG else ""))
        stopped = [self.rows[i]["name"] for i in self.stopped if i in self.vms]
        if stopped:
            print(f"  not running: {ui.some(stopped, 8)} - they take it when started (pvelab start ...)")


def reboot_all(ids, rows, kept, done, node, what):
    """Reboot VMs ids (cloud-init sets them up again), then put back the password hashes kept."""
    def one(i, board):
        boot(i, node[i], board, ("reboot", "--timeout", "60"), "rebooting", said="rebooted, but ")
        board.stage(i, "cloud-init", live=True)
        t0 = time.time()
        while True:
            st, line = first_boot_state(i, node[i])
            if line:
                board.tail(i, line)
            if not booting(st):
                break
            if time.time() - t0 > READY_TIMEOUT:
                raise LabError(f"cloud-init not finished after {READY_TIMEOUT // 60} min")
            time.sleep(4)
        if kept.get(i) and agent_out(i, "usermod", "-p", kept[i], rows[i]["user"] or GUEST_USER, node=node[i]) is None:
            raise LabError(f"{done[i]}, but its password was not put back (pvelab list --passwords has the "
                           "handout one)")
        return done[i]
    each(f"reboot: {len(ids)} VM(s) onto the new {what}", ids, {i: rows[i]["name"] for i in ids}, one, workers=8,
         failed="{failed} of {total} VM(s) did not come back cleanly")


def net_set(name, cidr_opt, gw_opt, dry=False):
    if not cidr_opt and not gw_opt:
        die("nothing to change: give --cidr CIDR and/or --gateway ADDR")
    with nets.cached_inputs():
        n = local_net(name, "set")
        label, old_net, old_gw = nets.net_name(n), nets.cidr(n), nets.gateway(n)
        new_net = nets.parse_cidr(cidr_opt) if cidr_opt else old_net
        if new_net != old_net:
            nets.cidr_free(n, new_net)
        new_gw = nets.parse_gateway(gw_opt, new_net) if gw_opt else \
            new_net.network_address + (int(old_gw) - int(old_net.network_address))    # its place in the range
        if not nets.usable(new_gw, new_net):
            new_gw = new_net.network_address + 1
        if (new_net, new_gw) == (old_net, old_gw):
            die(f"{label} has {old_net} with the gateway {old_gw} already")
    c = Change(n, "address")
    ips = new_ips({i: r["ip"] for i, r in c.rows.items()}, old_net, new_net, new_gw)
    if ips is None:
        die(f"{new_net} has no room for the {len(c.rows)} VM(s) of {label} (from .{IP_FIRST} up)")
    c.plan.do(f"{label}: {old_net} gateway {old_gw} -> {new_net} gateway {new_gw} ({bridge(n)}"
              + (", the SDN subnet on every host)" if is_sdn(n) else ")"))
    c.plan.do("isolation, ssh forwards and blocks made again for the new addresses")
    if c.rows:
        c.plan.do("the VMs' addresses (also in their snapshots): "
                  + ui.some([f"{c.rows[i]['name']} {c.rows[i]['ip']} -> {ips[i]}" for i in sorted(c.rows)], 6))
    c.warnings(set(c.rows))
    if not c.start(dry):
        return
    with Spinner(f"{label}: {new_net}, gateway {new_gw} on {bridge(n)}") as line:
        readdress_host(n, label, new_net, new_gw, ips)
        line.ok(f"{label}: {new_net}, gateway {new_gw}; rules made again for the new addresses")
    c.set_vms("ipconfig0", {i: ip_config(n, ip) for i, ip in ips.items()})
    print(paint(f"\n{label} is {new_net} now, gateway {new_gw}.", "green"))
    c.finish({i: f"running at {ips[i]}" for i in ips}, c.rows)


def readdress_host(n, label, new_net, new_gw, ips):
    """The gateway side: the bridge's address, the SDN subnet, and every rule made again from the new
    addresses (take_down: the old ones go first). An SDN net holds PVE's SDN lock from the start: nothing changes if it can't."""
    br, made_by_sdn = bridge(n), is_sdn(n)          # read before the block goes (bridge() reads it)
    on = [k for k in SETTERS if k == "block" or has_section(k, n)]
    with sdn.session() if made_by_sdn else contextlib.nullcontext():
        take_down(n, br)
        nets.register(n, label, new_net, new_gw)
        add_net_block(n, bridge_stanza(n, br, net_rules(br), sdn_made=made_by_sdn))
        if made_by_sdn:
            sdn.replace_subnet(n)
            sdn.apply()                      # every host reloads; the gateway's bridge comes up with the new address
        else:
            run("ifup", br)
    require_gateway(br, new_gw)
    rows = cred_rows()
    for i, ip in ips.items():
        rows[i]["ip"] = ip
    cred_save(rows)
    for kind in on:
        SETTERS[kind](n, True)


def renamed(name, old, new):
    """A VM's name with the net's old name as its prefix (old-student01) changed to new; others as they are."""
    return new + name[len(old):] if name.startswith(f"{old}-") else name


def with_name_line(block_text, name):
    """A net's interfaces block with its name line for name (added when an old net has none)."""
    line = nets.NAME_LINE.format(name)
    new, k = re.subn(r"(?m)^\s*# pvelab name \S+\n", line, block_text, count=1)
    return new if k else re.sub(r"(?m)^(?=(auto|iface) )", line, block_text, count=1)


def net_rename(old, new, dry=False):
    n = local_net(old, "rename")
    old = nets.net_name(n)
    if new == old:
        die(f"{old} has that name already")
    if why := nets.name_problem(new, n):
        die(f"{new}: {why}")
    c = Change(n, "name")
    names = {i: renamed(r["name"], old, new) for i, r in c.rows.items()}
    change = {i for i in c.rows if names[i] != c.rows[i]["name"]}
    taken = {conf.get("name") for i, conf in inventory().items() if i not in c.rows}
    if clash := sorted({names[i] for i in change} & taken):
        die(f"VM names in use already: {', '.join(clash)}")
    c.plan.do(f"{old} is called {new}" + (f" (the alias of SDN vnet {bridge(n)})" if is_sdn(n) else ""))
    if change:
        c.plan.do("the VMs' names (also in their snapshots and the credentials): "
                  + ui.some([f"{c.rows[i]['name']} -> {names[i]}" for i in sorted(change)], 4))
        c.plan.warn("handouts printed before show the old VM names (the ssh commands and passwords stay the "
                    f"same): pvelab handout --net {new}")
    c.warnings(change)
    if not c.start(dry):
        return
    with Spinner(f"{old} -> {new}") as line:
        text = open(IFACES).read()
        block = blocks(text).get(str(n)) or die(f"{old}'s block is not in {IFACES}")
        save_ifaces(text.replace(block, with_name_line(block, new)))
        nets.register(n, new, nets.cidr(n), nets.gateway(n))
        if is_sdn(n):
            with sdn.session():
                sdn.set_alias(n)
                sdn.apply()
        rows = cred_rows()
        for i in change:
            rows[i]["name"] = names[i]
        cred_save(rows)
        line.ok(f"{old} is {new}" + (f"; {len(change)} VM(s) to rename" if change else ""))
    c.set_vms("name", {i: names[i] for i in change})
    print(paint(f"\n{old} is called {new} now.", "green"))
    c.finish({i: f"running as {names[i]}" for i in change}, {i: {**r, "name": names[i]} for i, r in c.rows.items()})
    if change:
        print(f"  pvelab handout --net {new}       login cards with the new names")
