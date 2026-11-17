"""Nets: a private range (10.10.N.0/24 unless --cidr) with fixed IPs behind NAT - an SDN vnet labN (one network across the cluster's
nodes) where PVE has SDN, else a bridge vmbrN on this node - and the DHCP bridge vmbr1."""

import os
import re
import shlex
import shutil
import subprocess
import time

from .blocks import apply_blocks
from . import ifaces, nets, sdn
from . import config
from .config import bridge, DHCP_BRIDGE, DHCP_NET, is_sdn, LEASES, net_bridge, UPLINK
from .credentials import cred_rows
from .expose import set_expose
from .guest import guest_json
from .ifaces import (add_net_block, block_begin, block_end, bridge_is_ours, has_section, iptables, own_rules, post_lines,
                     remove_net_block, restore_lines, rule_cmd, save_ifaces, split_table, WAIT,
                     wait_in_blocks)
from .inventory import is_managed, read_conf, vm_mac
from .isolation import gw_mac, set_isolation
from .nodes import guest_conf_files, multi, this_node
from .ui import log
from .util import die, LabError, ok, run, warn, write_file


def net_in_use(n, own_range=True):
    """Why net n (its bridge, its CIDR) is taken, or None if it is free. own_range false: the net will get a
    custom CIDR, so the 10.10.N route and subnet do not matter."""
    br, net = bridge(n), nets.cidr(n)
    if vnet := config.held_numbers().get(str(n)):
        return f"its number is held by PVE vnet {vnet}, which pvelab took over"
    if link_exists(br):
        return f"{br} already exists"
    if re.search(rf"^(auto|iface) {br}( |$)", open(config.IFACES).read(), re.M):
        return f"{br} is configured in {config.IFACES}"
    if own_range and (why := nets.cidr_problem(n, net)):     # the other nets, SDN subnets, every host's routes
        return why
    if why := sdn.conflict(n, own_range):
        return why
    for vmid, _ in guests_on(br):
        return f"guest {vmid} has a network card on {br}"
    if any(r["net"] == n for r in cred_rows().values()):
        return f"pvelab has VMs recorded on {nets.net_name(n)}"
    return None


def foreign_use(n):
    """Why net n's bridge / CIDR is used by something that is NOT pvelab's, or None.
    A net pvelab made itself (with or without VMs) is fine to reuse."""
    br, net = bridge(n), nets.cidr(n)
    ours = bridge_is_ours(n)
    if not ours and config.taken_over(n):    # a PVE vnet pvelab takes over: only its range must be clear
        return nets.cidr_problem(n, net) or guest_problem(br)
    if not ours:
        if link_exists(br):
            return f"{br} exists and was not made by pvelab"
        if re.search(rf"^(auto|iface) {br}( |$)", open(config.IFACES).read(), re.M):
            return f"{br} is configured in {config.IFACES} (not by pvelab)"
        if why := sdn.conflict(n):
            return why
        if why := nets.cidr_problem(n, net):
            return why
    return guest_problem(br)


def guest_problem(br):
    """Why a guest that is not pvelab's stops pvelab from using bridge br, or None."""
    for vmid, f in guests_on(br):
        if not is_managed(read_conf(vmid) if "/qemu-server/" in f else None):
            return f"guest {vmid} (not a pvelab VM) has a network card on {br}"
    return None


def guests_on(br):
    """[(vmid, config file)] of the guests (VMs and containers, any node) with a network card on bridge br,
    now or in a snapshot."""
    return [(int(os.path.basename(f)[:-5]), f) for f in guest_conf_files()
            if re.search(rf"bridge={br}(,|$)", open(f).read(), re.M)]


def free_net(custom=False):
    """Lowest free N. custom: the net gets its own CIDR, so the 10.10.N route / subnet is ignored."""
    for n in range(2, 255):
        if n != 10 and not net_in_use(str(n), not custom):
            return str(n)
    die("no free net number left (2-254)")



def link_exists(dev):
    return ok("ip", "link", "show", dev)


def link_up(dev):
    """Whether dev exists and is set up (its UP flag; a bridge without ports has no carrier yet)."""
    m = re.search(r"<([^>]*)>", run("ip", "link", "show", dev, check=False, quiet=True))
    return bool(m) and "UP" in m[1].split(",")


TCPMSS = "-p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"     # MTU-safe TCP through NAT


def bridge_stanza(n, br, rules, sdn_made=False):
    """interfaces stanza of net n's NAT bridge br for its CIDR (host: the first address): forwarding and masquerading on,
    plus iptables rules ("[-t TABLE] CHAIN ...") added when it comes up, removed when it goes down.
    sdn_made: SDN makes the bridge itself (its own stanza); this one only adds the gateway to it - with
    several hosts, with the gateway's fixed MAC (gw_mac; one host: as before, ensure_gw_mac adds it later)."""
    net, pve = nets.cidr(n), nets.pve_gateway(n)      # pve: PVE gives the vnet its gateway and NAT itself
    up, down = iptables(([] if pve else [f"-t nat POSTROUTING -s {net} -o {UPLINK} -j MASQUERADE"]) + rules)
    stanza = f"iface {br} inet static\n" + ("" if pve else f"        address {nets.gateway(n)}/{net.prefixlen}\n")
    if sdn_made:
        if multi() and not pve:
            stanza += f"        hwaddress {gw_mac(n)}\n"
    else:
        stanza = f"auto {br}\n{stanza}        bridge-ports none\n        bridge-stp off\n        bridge-fd 0\n"
    if n != "dhcp" and nets.net_name(n) != f"net{n}":     # old nets have no name line
        stanza = nets.NAME_LINE.format(nets.net_name(n)) + stanza
    return stanza + post_lines(["sysctl -qw net.ipv4.ip_forward=1"] + up, down)


def ensure_dhcp_bridge():
    """vmbr1: NAT bridge with dnsmasq DHCP for VMs made without --net (create makes it when needed).
    (On OpenStack, port security drops guest MACs on vmbr0, so guests need NAT.)"""
    if not nets.dhcp_bridge_ours():          # an admin's: its own addresses, no dnsmasq of pvelab's on it
        die(f"{DHCP_BRIDGE} is not pvelab's (made by hand): its VMs go there with --net {DHCP_BRIDGE}; for "
            f"pvelab's DHCP bridge pick a free name: LAB_DHCP_BRIDGE=vmbrN")
    if not link_exists(DHCP_BRIDGE):
        if not re.search(rf"^(auto|iface) {DHCP_BRIDGE}( |$)", open(config.IFACES).read(), re.M):
            print(f">> creating NAT bridge {DHCP_BRIDGE} ({DHCP_NET}.1/24)")
            stanza = bridge_stanza("dhcp", DHCP_BRIDGE, [f"-t mangle FORWARD {TCPMSS}"])
            save_ifaces(open(config.IFACES).read() + "\n" + stanza)
        run("ifup", DHCP_BRIDGE)       # only this bridge; also when it is configured but not up
    conf = f"/etc/dnsmasq.d/{DHCP_BRIDGE}.conf"
    want = (f"interface={DHCP_BRIDGE}\nbind-interfaces\nexcept-interface=lo\n"
            f"dhcp-range={DHCP_NET}.100,{DHCP_NET}.200,255.255.255.0,12h\n"
            f"dhcp-option=option:router,{DHCP_NET}.1\n"
            f"dhcp-option=option:dns-server,{DHCP_NET}.1\n")
    if not shutil.which("dnsmasq"):
        print(f">> installing dnsmasq for DHCP/DNS on {DHCP_BRIDGE}")
        run("env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "-q", "dnsmasq")
    if write_file(conf, want) or not ok("systemctl", "is-active", "--quiet", "dnsmasq"):   # also when an earlier start failed
        run("systemctl", "restart", "dnsmasq")


def is_new_net(n):
    """create makes net n (and sets its isolation) only if pvelab has no bridge for it yet."""
    return not bridge_is_ours(n)


def net_rules(br):
    return [f"-t mangle FORWARD -i {br} {TCPMSS}",
            f"-t mangle FORWARD -o {br} {TCPMSS}",
            f"FORWARD -i {br} -o {br} -j ACCEPT",                     # same subnet
            f"FORWARD -i {br} -o {UPLINK} -j ACCEPT",                 # internet
            f"FORWARD -o {br} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            f"FORWARD -i {br} -j DROP",                               # other subnets
            f"FORWARD -o {br} -j DROP"]


IP_FORWARD = "/proc/sys/net/ipv4/ip_forward"


def forwarding():
    """Whether this host forwards IPv4 (its nets' post-up line turns it on)."""
    try:
        return open(IP_FORWARD).read().strip() == "1"
    except OSError:
        return True


class Unknown(LabError):
    """iptables could not tell whether a rule is there, or where: not a missing rule."""


def has_rule(rule):
    """Whether this host has rule; Unknown when iptables cannot tell (the lock still held after its
    wait, a rule it does not understand) - such a rule is not missing."""
    r = subprocess.run(shlex.split(rule_cmd(rule, "-C")), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode and not (r.returncode == 1 and "Bad rule" in r.stderr):   # 1 also: no such chain/target
        raise Unknown(f"iptables could not check {rule} ({r.stderr.strip() or f'exit {r.returncode}'})")
    return r.returncode == 0


def missing_rules(n, rules=None):
    """Net n's own iptables rules ("[-t TABLE ]CHAIN ...") this host lacks now: one whose line failed
    when the bridge came up. [] without a block here. rules: own_rules(n), if read already."""
    rules = own_rules(n) if rules is None else rules
    return [r for r, _ in rules if not has_rule(r)]


def chain_of(rule):
    table, rest = split_table(rule)
    return table, rest.split(" ", 1)[0]


def canon(words):
    """A rule's words as iptables -S prints them, to compare: no "-m tcp/udp" (it adds one after -p),
    comma lists sorted (it prints RELATED,ESTABLISHED for ESTABLISHED,RELATED)."""
    words = [",".join(sorted(w.split(","))) if "," in w else w for w in words]
    out = []
    for w in words:
        if out and out[-1] == "-m" and w in ("tcp", "udp"):
            out.pop()
            continue
        out.append(w)
    return tuple(out)


def listed(table, chain):
    """chain's rules in table, in order, as canon() of each (iptables -S); Unknown when it cannot tell."""
    r = subprocess.run(["iptables", *WAIT.split(), "-t", table, "-S", chain], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode:
        raise Unknown(f"iptables could not list {table} {chain} ({r.stderr.strip() or f'exit {r.returncode}'})")
    return [canon(line.split()[1:]) for line in r.stdout.splitlines() if line.startswith(f"-A {chain} ")]


UNCHECKED = "unchecked"


def is_drop(rule):
    return rule.endswith(" -j DROP")


def out_of_order(mine, gone):
    """Where one chain's own rules mine [(rule, op)] are out of order: a rule that lets traffic through
    (or marks it) found below a DROP rule the block lists after it - one a reload added again after the
    net's DROP rules (the order among the DROPs, or among the others, does not matter). (the index in
    mine to move all from, that rule), None when in order, UNCHECKED when a rule is not found as
    iptables -S prints it (its place is unknown)."""
    there = [(i, r) for i, (r, op) in enumerate(mine) if op == "-A" and r not in gone]
    if not any(is_drop(r) for _, r in there) or all(is_drop(r) for _, r in there):
        return None
    rows = listed(*chain_of(there[0][1]))
    where = {}
    for i, r in there:
        if (c := canon(split_table(r)[1].split())) not in rows:
            return UNCHECKED
        where[i] = rows.index(c)
    for i, r in there:                       # in block order: the first one too far down
        if not is_drop(r) and any(is_drop(d) and j > i and where[j] < where[i] for j, d in there):
            return i, r
    return None


def problems(n, rules):
    """(missing rules, {chain: (index to move all from, misplaced rule)} where out of order, whether a
    chain's order could not be checked) of net n."""
    gone, wrong, unchecked = missing_rules(n, rules), {}, False
    for chain in dict.fromkeys(chain_of(r) for r, _ in rules):
        try:
            found = out_of_order([(r, op) for r, op in rules if chain_of(r) == chain], gone)
        except Unknown:                      # its order unknown; the missing ones are still known
            found = UNCHECKED
        if found == UNCHECKED:
            unchecked = True
        elif found:
            wrong[chain] = found
    return gone, wrong, unchecked


def missing(n, rules=None):
    """(what is wrong with net n's own rules, each said in words: a missing rule, a rule out of order;
    whether the order of a chain could not be checked)."""
    rules = own_rules(n) if rules is None else rules
    gone, wrong, unchecked = problems(n, rules)
    return said_of(gone, wrong), unchecked


def said_of(gone, wrong):
    """What is wrong with the rules, in words: missing rules, a rule out of order."""
    return [f"missing {r}" for r in gone] + [f"out of order: {bad}" for _, bad in wrong.values()]


def repair_rules(n, strict=False):
    """Net n's own rules back in their order when one is missing or out of order; returns what was
    wrong (missing()'s words). In each chain from its first missing or misplaced rule on, each rule is
    added at the end and the older copies of those that were there removed - each table in one
    iptables-restore, so it changes whole or not at all, and the net's DROP rules are there throughout
    (an -I rule: inserted first, as ifup does). pvelab's older lines get -w first (a host without SDN
    has no reload that would give it to them). When iptables cannot tell (Unknown), that is said and
    nothing done (strict: it stops)."""
    wait_in_blocks()
    rules, off = own_rules(n), not forwarding()
    if off:                                  # no iptables needed: whatever the rules
        run("sysctl", "-qw", "net.ipv4.ip_forward=1")
    try:
        gone, wrong, _ = problems(n, rules)
    except Unknown as e:
        msg = f"{nets.net_name(n)}: {e} - nothing put back; again later (another program may hold the xtables lock)"
        if strict:
            die(msg)
        warn(msg + f": pvelab net check {nets.net_name(n)}")
        return ["net.ipv4.ip_forward was off"] if off else []
    if not (said := (["net.ipv4.ip_forward was off"] if off else []) + said_of(gone, wrong)):
        return []
    ops = []                                 # (rule, op) in the order they run
    for chain in dict.fromkeys(chain_of(r) for r, _ in rules):
        mine = [(r, op) for r, op in rules if chain_of(r) == chain]
        ops += [(r, op) for r, op in mine if op == "-I" and r in gone]     # first in the chain anyway
        starts = [i for i, (r, op) in enumerate(mine) if op == "-A" and r in gone][:1] + \
            ([wrong[chain][0]] if chain in wrong else [])
        if not starts:
            continue
        later = [(r, op) for r, op in mine[min(starts):] if op == "-A"]
        ops += later + [(r, "-D") for r, _ in later if r not in gone]     # -D: the first copy, the older one
    if ops:
        try:
            run("iptables-restore", *WAIT.split(), "--noflush", data=restore_lines(ops))
        except LabError as e:
            die(f"{nets.net_name(n)}: {'; '.join(said)}; putting the rules back failed ({e}) - each table is whole: "
                f"as it was, or put back. What is still wrong: pvelab net check {nets.net_name(n)}")
    warn(f"{nets.net_name(n)}: {'; '.join(said)} - " + ("put back in order" if ops else "forwarding on again"))
    return said


def check_rules(n):
    """repair_rules where stopping would leave things half done (a new net before its isolation: create
    checks it again after; net set before its VMs' addresses): said, not stopped at."""
    try:
        repair_rules(n)
    except LabError as e:
        warn(str(e))


def settled(n, secs=20):
    """Wait (up to secs) until net n's own rules are all there: SDN's apply starts this host's network
    reload and does not wait for it, and the reload's post-up lines add them."""
    t0, rules = time.time(), own_rules(n)
    while rules and time.time() - t0 < secs:
        try:
            if all(has_rule(r) for r, _ in rules):
                return
        except Unknown:                      # the reload may hold the lock: not done yet
            pass
        time.sleep(2)


def bring_up(n, br):
    """ifup net n's bridge br. ifup fails also when a post-up line did (a rule: the rules check after
    puts it back), so a failure stops here only when br is not up or lacks its gateway; else it is
    said, with ifup's own last line (all of it in the log)."""
    r = subprocess.run(["ifup", br], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.stderr.strip():                     # its messages in the log, as run() keeps them
        log(f"ifup {br}: exit {r.returncode}\n{r.stderr}")
    if not r.returncode:
        return
    last = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
    if not link_up(br):
        die(f"ifup {br} failed: {last}")
    if not nets.pve_gateway(n):              # PVE gives a taken-over vnet its gateway
        try:
            require_gateway(br, nets.gateway(n))
        except LabError as e:
            die(f"{e} (ifup: {last})")
    warn(f"ifup {br} reported a failure ({last}) - its rules are checked next")


def check_free(n, br, net):
    if link_exists(br) or re.search(rf"^(auto|iface) {br}( |$)", open(config.IFACES).read(), re.M):
        die(f"{br} already exists and was not created by lab - pick another --net")
    nets.cidr_free(n, net)


def ensure_bridge(n):
    br, net = bridge(n), nets.cidr(n)
    if bridge_is_ours(n):
        if not link_exists(br):
            bring_up(n, br)
        repair_rules(n)
        return
    if config.taken_over(n):                 # a PVE vnet (PVE gives its gateway and SNAT): pvelab's rules on it
        print(f">> {nets.net_name(n)}: pvelab's rules on PVE vnet {br}")
        add_net_block(n, bridge_stanza(n, br, net_rules(br), sdn_made=True))
        config.record_taken(br, n, this_node())    # every host knows it is pvelab's now
        bring_up(n, br)
        sdn.tidy_snat()                      # ifup ran PVE's own SNAT line again
        check_rules(n)
        return
    check_free(n, br, net)
    if is_sdn(n):
        try:
            make_sdn_net(n, br, net)
        except LabError as e:
            br = net_bridge(n, False)
            warn(f"SDN vnet for {nets.net_name(n)} failed ({e}) - using a bridge {br} on this node instead")
            check_free(n, br, net)
        else:                                # made: a rule it lacks is no reason for a bridge instead
            settled(n)
            check_rules(n)
            return
    print(f">> creating isolated NAT bridge {br} ({nets.gateway(n)}/{net.prefixlen})")
    add_net_block(n, bridge_stanza(n, br, net_rules(br)))
    bring_up(n, br)                  # only this bridge; vmbr0/vmbr1 untouched
    check_rules(n)


def make_sdn_net(n, br, net):
    """Net n as SDN vnet br (on every node) with its gateway here; on failure, undo all of it."""
    print(f">> creating SDN vnet {br} ({net}, VXLAN zone across the nodes) with NAT gateway {nets.gateway(n)} here")
    with sdn.session():                        # one hold on PVE's SDN lock, the undo included
        applied = False
        try:                                   # the block only once the session is open: the undo removes it
            add_net_block(n, bridge_stanza(n, br, net_rules(br), sdn_made=True))   # before apply brings br up
            sdn.add_vnet(n)
            applied = True                     # from here on the nodes may have it
            sdn.apply()
            require_gateway(br, nets.gateway(n), wait=60)
        except BaseException:
            remove_bridge(n, quiet=True)
            if applied:
                try:
                    sdn.apply()
                except LabError as e:
                    warn(f"undoing the SDN vnet {br}: {e}")
            raise


def ensure_gw_mac(n):
    """Give SDN net n's gateway its fixed MAC (gw_mac), now and in its stanza, if an older net lacks it.
    Run when the net first gets VMs on another host; a vmbrN net is on one host and is never touched."""
    if not is_sdn(n) or not bridge_is_ours(n):
        return
    s, br, mac = open(config.IFACES).read(), bridge(n), gw_mac(n)
    line = f"        address {nets.gateway(n)}/{nets.cidr(n).prefixlen}\n"
    block = re.search(rf"^{re.escape(block_begin(n))}.*?^{re.escape(block_end(n))}", s, re.M | re.S)
    if not block or "hwaddress " in block.group(0) or line not in block.group(0):
        return
    print(f">> {br}: the gateway {nets.gateway(n)} gets the fixed MAC {mac} (VMs on other hosts reach only it)")
    save_ifaces(s.replace(block.group(0), block.group(0).replace(line, line + f"        hwaddress {mac}\n", 1)))
    run("ip", "link", "set", br, "address", mac)


def vnet_up(host, n, nothing, then=""):
    """Wait (up to 1 min) until SDN net n's vnet is up on host; else LabError. nothing: what was not done
    ("no VM was made"); then: what to do after the ifreload on host."""
    if not sdn.wait_vnet(host, n):
        die(f"{bridge(n)} is not up on {host} after 1 min; {nothing}. On {host}: ifreload -a{then}")


SETTERS = {"expose": set_expose, "block": apply_blocks, "isolate": set_isolation}   # (net, on): each kind


def require_gateway(br, gw, wait=0, prefix=None):
    """Stop unless bridge br is up with the gateway address gw (prefix: with that prefix length too - a
    net set that keeps its gateway). wait: seconds to wait for it (SDN's apply starts this host's
    reload and does not wait for it)."""
    want, t0 = f"inet {gw}/" + (f"{prefix} " if prefix else ""), time.time()
    while wait and want not in run("ip", "-4", "addr", "show", br, check=False, quiet=True) \
            and time.time() - t0 < wait:
        time.sleep(2)
    if want not in run("ip", "-4", "addr", "show", br, check=False, quiet=True):
        die(f"{br} came up without its gateway {gw}" + (f"/{prefix}" if prefix else ""))


def take_down(n, br, quiet=False):
    """Net n's rules and its interfaces block gone; its bridge br down - but a PVE vnet stays up (ifdown
    would delete it, and every VM's port on it): only the post-down lines of pvelab's block are run."""
    if config.taken_over(n):
        block = ifaces.blocks(open(config.IFACES).read()).get(str(n), "")
        for cmd in re.findall(r"^\s*post-down\s+(.+)$", block, re.M):
            run("sh", "-c", cmd, check=False, quiet=True)
    else:
        run("ifdown", br, check=False, quiet=quiet)
    # ifdown replays the config saved at ifup time, which may predate later sections: undo those here
    for setter in SETTERS.values():
        setter(n, False)
    remove_net_block(n)


def remove_bridge(n, quiet=False):
    """Remove net n's bridge and rules. An SDN vnet is gone from the nodes after sdn.apply(), which the
    caller runs once for all the nets it removes; returns whether there is one to apply.
    quiet: undoing an unfinished net (no messages)."""
    br, made_by_sdn = bridge(n), is_sdn(n)
    if not bridge_is_ours(n):
        warn(f"{br} was not created by lab, leaving it")
        return False
    taken = config.taken_over(n)
    take_down(n, br, quiet)
    if taken:                                # a PVE vnet: only pvelab's rules go; the vnet stays in PVE
        config.record_taken(br)
        if not quiet:
            print(f">> pvelab's rules on {br} removed (the vnet stays in PVE)")
        return False
    if made_by_sdn:
        sdn.remove_vnet(n)
    elif link_exists(br):
        run("ip", "link", "delete", br, check=False)
    if not quiet:
        print(f">> {'SDN vnet' if made_by_sdn else 'bridge'} {br} removed")
    return made_by_sdn


def refresh_net(n):
    """Rebuild every section that follows net n's VM list (ssh forwards, block rules, isolation)
    after VMs were added or removed - those that are on. Block rules are always on (mining ports)."""
    if bridge_is_ours(n):
        for kind, setter in SETTERS.items():
            if kind == "block" or has_section(kind, n):       # block: mining-port rules on every net
                setter(n, True)


def next_ip(n, rows):
    used = {r["ip"] for r in rows.values() if r["net"] == n}
    for a in nets.vm_addresses(n):
        if str(a) not in used:
            return str(a)
    die(f"{nets.net_name(n)} ({nets.cidr(n)}) is full")


def host_dns():
    for line in open("/etc/resolv.conf"):
        if line.startswith("nameserver"):
            return line.split()[1]
    return "1.1.1.1"


def vm_ip(vmid, rows, running=True):
    """The VM's IPv4: recorded (--net), else from its guest agent (running VMs only), else its DHCP lease."""
    r = rows.get(vmid)
    if r and r["ip"] != "-":
        return r["ip"]
    if running:                                  # a stopped VM has no agent to ask
        for i in guest_json("cmd", vmid, "network-get-interfaces", timeout=5) or []:
            if i.get("name") == "lo":
                continue
            for a in i.get("ip-addresses", []):
                if a.get("ip-address-type") == "ipv4":
                    return a["ip-address"]
    mac = vm_mac(vmid)
    if mac and os.path.exists(LEASES):           # agent not up yet: DHCP lease
        hits = [l.split()[2] for l in open(LEASES) if len(l.split()) > 2 and l.split()[1] == mac]
        if hits:
            return hits[-1]
    return "-"
