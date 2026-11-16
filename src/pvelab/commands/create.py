"""pvelab create"""

import os
import re
from collections import Counter

from .. import config
from ..config import (bridge, CORES, CRED, DEFAULT_TEMPLATE, EST_DISK_GB, GUEST_USER, ID_START, is_sdn, MEM, PREFIX,
                      SNAP)
from .. import accounts, nets, sdn, ui
from ..credentials import cred_rows, gen_password
from ..expose import expose_net
from ..isolation import is_isolated, set_isolation, shared_mac
from ..owners import is_shared_vm, owner_from_name, owner_of, shared_name, vms_per_owner, without_net
from ..helptext import bad_usage
from ..inventory import desc_value, inventory, is_managed, read_conf
from ..lifecycle import clone_vm, wait_first_boot
from ..args import opt
from ..networks import (ensure_bridge, ensure_dhcp_bridge, ensure_gw_mac, foreign_use, free_net, is_new_net, link_exists, next_ip,
                        refresh_net, vnet_up)
from ..nodes import is_local, multi, online_host, require_quorum, this_node, used_vmids
from ..placement import bridge_host, node_room, place
from ..resources import (check_copy_space, check_resources, conf_disk_gb, full_disk, host_numbers, parse_cores,
                         parse_mem)
from ..scripts import expose_warning, parse_chain, run_and_snapshot, snapshot_all
from ..templates import copy_needed, ensure_snippet, resolve_template, template_on
from ..ui import Board, paint, Plan, some, Spinner, step
from ..util import die, LabError, warn


def net_defaults(net, inv):
    """(name prefix, login) for more VMs on net: those of its owners made by default naming (PREFIXnn,
    whatever the VMs are called now; their ciuser), the most common, so a net keeps its own; else (or
    LAB_PREFIX / LAB_USER set) the defaults. Owners from --accounts don't count: each has their own login."""
    made = [(m.group(1), c.get("ciuser")) for c in inv.values()
            if is_managed(c) and desc_value(c, "net") == net and not is_shared_vm(c)
            and (m := re.fullmatch(r"([a-z]+)\d{2}", owner_of(c)))]    # owners named PREFIXnn (not accounts)
    pfx, login = Counter(p for p, _ in made), Counter(u for _, u in made if u)
    return ("LAB_PREFIX" not in os.environ and pfx and pfx.most_common(1)[0][0] or PREFIX,
            "LAB_USER" not in os.environ and login and login.most_common(1)[0][0] or GUEST_USER)


def per_owner_on(net, rows=None):
    """How many VMs each owner has on net (from the credentials; rows: cred_rows() read already), 0 for
    none: more VMs keep it. The net's shared VM is no owner's."""
    return vms_per_owner({i: r["name"] for i, r in (cred_rows() if rows is None else rows).items() if r["net"] == net})


def login_name(v):
    """--user's value: a login name (root, user, s1101001)."""
    if why := accounts.user_problem(v):
        bad_usage(f"--user: {why}")
    return v


def per_vm(what, v, per, parse):
    """'4' or '2,4,4' -> one parsed value for each of an owner's per VMs (the k-th for VM -k)."""
    vals = str(v).split(",")
    if len(vals) not in (1, per):
        bad_usage(f"{what} {v}: one value, or one for each of an owner's {per} VMs" if per > 1
                  else f"{what} {v}: several values only with OWNERSxVMS (20x3)")
    return [parse(x) for x in vals] * (per // len(vals))


def valid_node(v):
    """--node's value: auto, or an online node of the cluster."""
    return v if v == "auto" else online_host("--node", v)


def pinned_host(net, node):
    """The one host net's VMs may go to (a plain bridge exists on one host only), or None (any host).
    Dies when --node names another host."""
    pin = bridge_host(net)
    if pin and node not in ("auto", pin):
        die(f"VMs without --net use the DHCP bridge {bridge(net)} on {pin} only: they stay on {pin}" if net == "dhcp"
            else f"{nets.net_name(net)} is a bridge {bridge(net)} on {pin} only (not an SDN vnet): its VMs stay on {pin}")
    return pin


def placement(keys, mem, per, node, inv, pin, hosts, net=None):
    """{owner: host} for owners. One host (hosts false): all here, no placement. Else owners who
    already have a VM keep its host, the others go where there is room. pin: pinned_host(net, node)."""
    if not hosts:
        return dict.fromkeys(keys, this_node())
    fixed = {}
    for c in inv.values():                   # an owner's VM made earlier: the rest of theirs go there too
        if net is not None and desc_value(c, "net") != net:
            continue                         # an owner is one of its net's
        s = owner_of(c)
        if s in keys and s not in fixed:
            fixed[s] = c["_node"]
    need = sum(mem[k] for k in range(per))   # one owner: all their VMs
    return place(keys, need, per * EST_DISK_GB, node_room(), None if node == "auto" else node,
                 [pin] if pin else None, fixed)


def forced(plan, problems):
    """Resource and space problems join the plan's warnings (LAB_FORCE=1 skips them)."""
    for p in problems:
        plan.warn(p, force=True)


def build_plan(todo, keys, tpl, mem, per, chains, net, node, pin, isolate, expose, inv, hosts, shared, key,
               login):
    """Step 1: what create will do and every warning about it, asked once. Returns (where, used, need,
    copies): {owner: host}, the hosts used, {host: the templates its VMs are cloned from}, and
    {(template, host): GB} of the template copies the plan makes. key: {name: owner}; login: {name: (user,
    password or None)}."""
    plan = Plan()
    if expose:                               # ssh from outside: weak passwords are at risk without harden
        for line in expose_warning(chains, per):
            plan.warn(line)
    step(1, 3, "plan")
    names = sorted({read_conf(t).get("name", str(t)) for t in tpl})
    if shared:
        plan.do(f"create the shared VM {todo[0][0]} from {', '.join(names)}: every owner of the net reaches it")
    else:
        plan.do(f"create {len(todo)} VM(s)" + (f" ({len(keys)} owners x {per})" if per > 1 else "")
                + f" from {', '.join(names)}")
    if net == "dhcp":
        plan.do(f"on the DHCP bridge {bridge(net)}")
    else:
        plan.do(f"on {nets.net_name(net)} ({nets.cidr(net)}, gateway {nets.gateway(net)}, {bridge(net)})"
                + (" - a new network" if is_new_net(net) else "")
                + (", owners kept apart" if isolate and is_new_net(net) else ""))
    users = sorted({u for u, _ in login.values()})
    if users and users != [GUEST_USER]:
        given = sum(1 for _, p in login.values() if p)
        plan.do(f"log in as {some(users, 6)}; passwords: "
                + (f"{given} given, the others random" if 0 < given < len(login) else "given" if given else "random"))
    elif any(p for _, p in login.values()):
        plan.do("passwords: given")
    for line in accounts.short_warnings({n: p for n, (_, p) in login.items()}):
        plan.warn(line)
    if "root" in users:
        plan.warn("root logs in over ssh with its password" + (" from outside (--expose): use --script harden"
                                                               if expose else ""))
    where = placement(keys, mem, per, node, inv, pin, hosts, net)
    on = {name: where[key[name]] for name, _ in todo}
    used = sorted(set(where.values()))
    numbers = host_numbers(used)             # {host: (MB available, storage())}, read once for the checks
    for h in used:
        mine = [k for name, k in todo if on[name] == h]
        forced(plan, check_resources(len(mine), sum(mem[k] for k in mine), h, *numbers[h]))
    if hosts:
        count_on = Counter(where.values())
        plan.do(f"place it on {used[0]}" if shared else
                "place them: " + ", ".join(f"{h} {n} owner{'s' * (n != 1)}" for h, n in sorted(count_on.items()))
                + " (each owner's VMs on one host)")
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
                plan.warn(f"template {tname} is copied to {h} (~{gb:.1f} GB, a few minutes)")
    same = len(set(chains)) == 1
    for k, chain in enumerate(chains[:1] if same else chains):
        if chain:
            plan.do(f"then run scripts on {'every VM' if same else f'VM -{k + 1}'}: "
                    + " + ".join(s.label for s in chain))
    if expose:
        plan.do("let users ssh in from outside (--expose)")
    plan.ask()
    return where, used, need, copies


def check_net_on(net, used):
    """Before cloning on the hosts used: the net must be there. A new net whose SDN vnet just failed is a
    bridge here only; and a host's network reload after the SDN apply may still be running."""
    pin = bridge_host(net)
    if pin and any(h != pin for h in used):
        die(f"{nets.net_name(net)} is now a bridge {bridge(net)} on {pin} only (its SDN vnet failed); no VM was made. "
            f"Run the create again: its owners then go to {pin}")
    for h in used:
        if not is_local(h) and is_sdn(net):
            vnet_up(h, net, "no VM was made", ", then run the create again")


def prepare_net(net, isolate, used):
    """Step 2's network: the DHCP bridge, or net's bridge (made if new, its owners kept apart unless
    --no-isolate), there on every host used before anything is cloned."""
    if net == "dhcp":
        with Spinner(f"DHCP bridge {bridge(net)}"):
            ensure_dhcp_bridge()
        return
    new_net = is_new_net(net)
    with Spinner(f"{nets.net_name(net)}: {bridge(net)}, {nets.cidr(net)}") as line:
        ensure_bridge(net)
        if not all(map(is_local, used)):
            ensure_gw_mac(net)               # VMs on another host reach the gateway by its fixed MAC
        if new_net and isolate:              # before the VMs start: nothing between owners
            set_isolation(net, True)
        check_net_on(net, used)
        line.ok(f"{nets.net_name(net)}: {bridge(net)}, gateway {nets.gateway(net)}" + (" (new)" if new_net else ""))
    if not new_net and not isolate and is_isolated(net):
        warn(f"{nets.net_name(net)} already isolates its owners; to let them reach each other: "
             f"pvelab isolate --net {nets.net_name(net)} --off")


def summary(board, ids, todo, per, host_of, net, expose, host):
    """The lines after the board: how many VMs are ready (and where), which are not, and what next."""
    ok = [i for i in ids if board.rows[i]["state"] == "ok"]
    bad = [i for i in ids if board.rows[i]["state"] != "ok"]
    print()
    shape = f" ({len(todo) // per} owners x {per})" if per > 1 else ""
    spread = ""
    if host_of:                              # several hosts: how many on each
        on = Counter(host_of[i] for i in ok)
        spread = " - " + ", ".join(f"{h} {n}" for h, n in sorted(on.items()))
    print(paint(f"created {len(ok)}/{len(todo)} VM(s){shape}{spread}", "green" if not bad else "yellow"))
    if bad:
        print(paint("not ready: " + " ".join(board.rows[i]["name"] for i in bad)
                    + f"  (their lines above say why; no '{SNAP}' snapshot where first boot did not finish)", "red"))
    if net != "dhcp" and is_isolated(net):
        print(f"{nets.net_name(net)}: owners can't reach each other's VMs"
              + (f", but every owner reaches {shared_name(net)}" if any(n == shared_name(net) for n, _ in todo)
                 else ""))
    if expose:
        print()
        expose_net(net, host)                # opens the ports and prints the ssh commands
    which = f"--net {nets.net_name(net)}" if net != "dhcp" else ""
    print(paint("\nNext:", "bold"))
    print(f"  pvelab list {which} --passwords".replace("  --", " --") + "     VMs, IPs and passwords")
    print(f"  pvelab handout {which}".rstrip() + "               login cards to print")
    if bad:
        print(f"  pvelab destroy {' '.join(board.rows[i]['name'] for i in bad[:3])}{' ...' if len(bad) > 3 else ''}"
              "   then create again, to replace the VMs that failed")
    print(paint(f"credentials: {CRED}" + (f"\nlog: {ui.LOG}" if ui.LOG else ""), "dim"))


def pick_net(v, cidr_opt, gw_opt=None):
    """--net V, --cidr and --gateway -> the net's number ("dhcp" without --net). A new net (auto, a free number
    or a new name) is checked (name, CIDR free, gateway in it) and registered; everything is refused here,
    before a plan is shown."""
    if v == "dhcp":
        return v
    if v != "auto" and (vn := nets.free_vnet(v)):
        return take_over(vn, cidr_opt, gw_opt)
    n = None if v == "auto" else nets.resolve(v)
    if n is not None and is_new_net(n) and config.taken_over(n):     # recorded as taken over, no block here
        host, vnet = config.taken_host(n), bridge(n)
        if host == this_node():              # its block is gone (the file restored?): take it over again, as N
            warn(f"{vnet} was recorded as taken over here, but its block is gone: taking it over again")
            return take_over(nets.pve_vnet(vnet), cidr_opt, gw_opt, n)     # in vnets.cfg: taken_vnets says so
        die(f"{nets.net_name(n)} is PVE vnet {vnet}, taken over on {host or 'another host'}: its rules are "
            f"there - run pvelab create on {host or 'that host'}")
    if n is not None and is_new_net(n) and (why := held_problem(n)):
        die(why)
    if n is not None and not is_new_net(n):
        if cidr_opt and nets.parse_cidr(cidr_opt) != nets.cidr(n):
            die(f"{nets.net_name(n)} exists with {nets.cidr(n)}; --cidr only applies to a new net")
        if gw_opt and nets.parse_gateway(gw_opt, nets.cidr(n)) != nets.gateway(n):
            die(f"{nets.net_name(n)} exists with the gateway {nets.gateway(n)}; --gateway only applies to a new net")
        return n
    custom = bool(cidr_opt)
    if n is None:
        n = free_net(custom)
        name = f"net{n}" if v == "auto" else v
        if v != "auto" and (why := nets.name_problem(name, n)):
            die(f"--net {v}: {why}")
    else:
        name = nets.net_name(n)
    net = nets.parse_cidr(cidr_opt) if cidr_opt else nets.default_cidr(n)
    if cidr_opt:                             # default ranges are checked by foreign_use, with its usual text
        nets.cidr_free(n, net)
    nets.register(n, name, net, nets.parse_gateway(gw_opt, net) if gw_opt else None)
    if v == "auto":
        print(f">> using net {n} ({bridge(n)}, {net})")
    return n


def held_problem(n):
    """Why net number n can't be a new net: a PVE vnet pvelab took over holds it (deleted in PVE, not
    applied yet: a rollback brings it back with that number), or None."""
    vnet = config.held_numbers().get(str(n))
    if vnet and vnet not in config.taken_vnets():      # one still in PVE's config is a net, not just a number
        return (f"net {n} is held by PVE vnet {vnet}, which pvelab took over and which is deleted in PVE but not "
                f"applied: apply or roll back that SDN change first, or pick another net")
    return None


def take_over(vn, cidr_opt=None, gw_opt=None, n=None):
    """A PVE vnet (nets.pve_vnets row) as a pvelab net: a free number, its alias (or id) as the name, its
    subnet's range and gateway. Only a vnet whose gateway and SNAT PVE gives itself (a simple or EVPN zone)
    and that is on this host; pvelab writes its block (and the record) when the net is prepared. n: its
    number (a vnet recorded as taken over here, whose block is gone), else a free one."""
    vnet = vn["vnet"]
    if cidr_opt or gw_opt:
        die(f"{vnet} is a vnet of PVE: its subnet ({vn['cidr'] or 'none'}) decides the range and gateway; "
            f"change them with pvelab net set {vn['alias'] or vnet}")
    if vn["type"] not in config.PVE_GATEWAY_ZONES:
        die(f"PVE vnet {vnet} is in a {vn['type'] or 'unknown'} zone ({vn['zone']}): PVE gives no gateway there, "
            f"and its subnet's gateway may be a router of that network; pvelab takes over vnets of simple or "
            f"EVPN zones only")
    if vn["cidr"] is None or vn["gateway"] is None:
        die(f"PVE vnet {vnet} has no subnet with a gateway: add one first (Datacenter -> SDN -> VNets -> "
            f"{vnet} -> Subnets)")
    if not link_exists(vnet):
        die(f"PVE vnet {vnet} is not on this host (is zone {vn['zone']} limited to other hosts?)")
    n = n or free_net(custom=True)
    config.TAKING[n] = vnet
    name = vn["alias"] if vn["alias"] and not nets.name_problem(vn["alias"], n) else vnet
    if why := nets.name_problem(name, n):
        die(f"PVE vnet {vnet}: {why}; give it an alias that is a net name (lowercase letters, digits, -)")
    nets.register(n, name, vn["cidr"], vn["gateway"])
    print(f">> {name}: PVE vnet {vnet} (zone {vn['zone']}), {vn['cidr']}, gateway {vn['gateway']}")
    return n


def cmd_create(args):
    with nets.cached_inputs():               # the hosts' routes and SDN subnets: read once for every range check
        return create(args)


def create(args):
    count, tpl, net, expose, host, script = None, DEFAULT_TEMPLATE, "dhcp", False, None, None
    node, cidr_opt, gw_opt, shared, pos = "auto", None, None, False, []
    accounts_opt, user_opt, pw_opt = None, None, None
    cores, mem, disk, isolate = CORES, MEM, "", True
    it = iter(args)
    for a in it:
        if a == "--expose":
            expose = True
        elif a == "--no-isolate":
            isolate = False
        elif a == "--shared":
            shared = True
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
            net = v
        elif (v := opt(a, it, "--cidr")) is not None:
            cidr_opt = v
        elif (v := opt(a, it, "--gateway")) is not None:
            gw_opt = v
        elif (v := opt(a, it, "--accounts")) is not None:
            accounts_opt = v
        elif (v := opt(a, it, "--user")) is not None:
            user_opt = v
        elif (v := opt(a, it, "--password")) is not None:
            pw_opt = v
        elif (v := opt(a, it, "--template", "-t")) is not None:
            tpl = v
        else:
            pos.append(a)
    if shared:                               # one VM, no count: pvelab create --shared [TEMPLATE] --net N
        if len(pos) > 1:
            bad_usage("--shared makes one VM: pvelab create --shared [TEMPLATE] --net N")
        count, tpl = "1", (pos or [tpl])[0]
    elif len(pos) > 2:
        bad_usage(f"too many arguments: {' '.join(pos[2:])}")
    else:
        count, tpl = (pos[0] if pos else None), (pos[1] if len(pos) > 1 else tpl)
    n_acc = None
    if accounts_opt is not None:             # the owners come from the list (its length: the count)
        if shared or user_opt or pw_opt:
            bad_usage("--accounts gives each owner's user and password: not with --shared, --user or --password")
        if net == "dhcp":
            bad_usage("--accounts needs --net NAME: the VMs are named after it")
        if count and not re.fullmatch(r"\d+(x\d+)?", count):    # the count left out: the template first
            count, tpl = None, count
        n_acc = len(accounts.rows_of(accounts_opt))
        count = count or str(n_acc)
    m = re.fullmatch(r"(\d+)(?:x(\d+))?", count or "")
    if not m or int(m.group(1)) < 1 or m.group(2) and int(m.group(2)) < 1:
        bad_usage("the first argument must be how many owners (20), or owners x VMs each (20x3)"
                  if count else "how many VMs?")
    count, per = int(m.group(1)), int(m.group(2) or 1)
    if n_acc is not None and count != n_acc:
        bad_usage(f"--accounts has {n_acc} account(s), not {count}")
    if pw_opt is not None and not accounts.password_ok(pw_opt):
        bad_usage("--password: one password (an owner's VMs share it), without tabs or line breaks")
    tpl = per_vm("TEMPLATE", tpl, per, str)
    cores = per_vm("--cores", cores, per, parse_cores)
    mem = per_vm("--memory", mem, per, parse_mem)
    disk = per_vm("--disk", disk, per, full_disk)
    if (cidr_opt or gw_opt) and net == "dhcp":
        die(f"{'--cidr' if cidr_opt else '--gateway'} needs --net NAME or --net auto")
    if shared and net == "dhcp":
        bad_usage("--shared needs --net N|NAME: the shared VM is one every owner of that net reaches")
    if per > 1 and net == "dhcp":
        bad_usage("several VMs per owner (NxM) need --net N")
    if (expose or host) and net == "dhcp":
        bad_usage("--expose/--host only work together with --net N")
    if host and not expose:
        bad_usage("--host only makes sense with --expose")
    # each of an owner's VMs: its chain of scripts (harden+docker,  -> VM -2 runs none), all
    # resolved and checked before anything is created
    chains = per_vm("--script", script or "", per, parse_chain)
    if count * per > 99:
        bad_usage("at most 99 VMs per create")
    require_quorum("no VM can be made")     # before a net or anything else is made
    net = pick_net(net, cidr_opt, gw_opt)            # after the usage checks: it reads the hosts' routes
    if net != "dhcp":
        other = foreign_use(net)
        if other:
            die(f"can't use {nets.net_name(net)}: {other}.\n"
                f"  pick another number, or --net auto for the next free one")
    hosts = multi()                          # one host (or no cluster): no placement, as before
    pin = pinned_host(net, node) if hosts else None    # refuses --node OTHER before anything is made
    vmids = {t: resolve_template(t) for t in dict.fromkeys(tpl)}
    tpl = [vmids[t] for t in tpl]
    for t, gb in zip(tpl, disk):
        tpl_gb = conf_disk_gb(read_conf(t))
        if gb and gb < tpl_gb:
            die(f"--disk {gb}: template {t}'s disk is already {tpl_gb:g} GB - disks can only grow")
    inv = inventory()
    if shared and (other := next((c.get("name") for c in inv.values() if is_managed(c)
                                  and desc_value(c, "net") == net and is_shared_vm(c)
                                  and c.get("name") != shared_name(net)), None)):
        die(f"{other} is {nets.net_name(net)}'s shared VM already (one per net: they share a fixed MAC)")
    prefix, login_default = net_defaults(net, inv)
    users = per_vm("--user", user_opt or login_default, per, login_name)
    pfx = prefix if net == "dhcp" else f"{nets.net_name(net)}-{prefix}"
    existing = {c.get("name"): desc_value(c, "net") if is_managed(c) else "" for c in inv.values()}   # name -> its net
    owner = {c.get("name"): owner_of(c) for c in inv.values() if is_managed(c) and desc_value(c, "net") == net}
    have_of = Counter(owner.values())          # per owner: their VMs on the net (any name)
    if net != "dhcp" and not shared:
        have = per_owner_on(net)
        if have and have != per:
            die(f"{nets.net_name(net)} has {have} VM(s) per owner; create more with {count}x{have}" if have > 1
                else f"{nets.net_name(net)} has 1 VM per owner; create more with {count} (no x{per})")
    todo, skipped, key, login = [], [], {}, {}     # key: name -> its owner; login: name -> (user, password)
    made = Counter()                               # per owner: the VMs this run adds
    accts = accounts.parse(accounts_opt, nets.net_name(net), per) if accounts_opt is not None else None
    for i in range(1, count + 1):
        for k in range(per):                 # k: which of the owner's VMs (its specs)
            if accts:
                a = accts[i - 1]
                name = a.name if per == 1 else f"{a.name}-{k + 1}"
                key[name], login[name] = without_net(a.name, nets.net_name(net)), (a.user, a.password)
            else:
                name = shared_name(net) if shared else f"{pfx}{i:02d}" if per == 1 else f"{pfx}{i:02d}-{k + 1}"
                key[name], login[name] = owner_from_name(name, nets.net_name(net)), (users[k], pw_opt)
            if existing.get(name) not in (None, net):          # a pvelab VM of another net, or not pvelab's
                die(f"VM name {name} is taken by " + (f"a VM of {nets.net_name(existing[name])}" if existing[name]
                                                      else "a VM pvelab did not make") + ": give another one")
            if name in owner and owner[name] != key[name]:      # a VM renamed to this name is another's
                die(f"VM name {name} is owner {owner[name]}'s VM (renamed): rename it first, or give "
                    f"{key[name]} another name")
            done = name in existing or have_of[key[name]] + made[key[name]] >= per   # all theirs (one renamed?)
            (skipped if done else todo).append((name, k))
            made[key[name]] += not done
    if skipped:
        print(f"== already there, skipped: {some([n for n, _ in skipped], 6, ' ')}")
        if accts:                             # the list may have changed since: say where it differs
            had = {r["name"]: r for r in cred_rows().values()}
            differ = [n for n, _ in skipped if n in had and (had[n]["user"] != login[n][0]
                                                             or login[n][1] and had[n]["password"] != login[n][1])]
            if differ:
                warn(f"the list differs from these VMs, which stay as they are: {some(differ, 6)} - a new password: "
                     "pvelab passwd VM --password PW; another user: destroy the VM and create it again")
    if not todo:
        print("nothing to create")
        if expose:
            expose_net(net, host)
        return

    if net != "dhcp" and is_sdn(net) and is_new_net(net):
        sdn.refuse_foreign()                 # before anything is cloned
    # --- 1. the plan: what will happen, every warning, one question ---
    keys = list(dict.fromkeys(key[n] for n, _ in todo))
    where, used, need, copies = build_plan(todo, keys, tpl, mem, per, chains, net, node, pin, isolate, expose,
                                           inv, hosts, shared, key, login)

    # --- 2. templates and the network ---
    step(2, 3, "templates and network")
    # each host's own template (copied there first if it has none) and vendor-data snippet
    tpl_on = {h: {t: template_on(t, h, asked=True, inv=inv, gb=copies.get((t, h))) for t in need[h]} for h in used}
    for h in used:
        if not is_local(h):
            ensure_snippet(h)
    prepare_net(net, isolate, used)

    # --- 3. the VMs, each on its own line of the board ---
    rows, nxt, ids, taken = cred_rows(), ID_START, [], used_vmids()    # read late: after the prompt and copies
    for _ in todo:                           # their VMIDs, all picked now (the board shows them all)
        while nxt in taken or nxt in ids:
            nxt += 1
        ids.append(nxt)
    passwords = {owner_of(inv.get(i), r["name"]): r["password"] for i, r in rows.items() if r["net"] == net}
    host_of = {i: where[key[name]] for i, (name, _) in zip(ids, todo)}
    title = f"[3/3] VMs: clone, first boot{', scripts' if any(chains) else ''}"     # the board's first line
    with Board(title, {i: name for i, (name, _) in zip(ids, todo)}, host_of if hosts else None) as board:
        for vmid, (name, k) in zip(ids, todo):
            user, pw = login[name]
            pw = pw or passwords.setdefault(key[name], gen_password())       # an owner's VMs: one password
            ip = next_ip(net, rows) if net != "dhcp" else ""
            src = tpl_on[host_of[vmid]][tpl[k]]                              # its host's copy of the template
            try:
                clone_vm(vmid, name, src, net, ip, rows, pw, cores[k], mem[k], disk[k], orig=tpl[k], board=board,
                         mac=shared_mac(net) if shared else None, user=user,
                         owner=None if shared else key[name], shared=shared)
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
