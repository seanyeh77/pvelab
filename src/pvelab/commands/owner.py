"""pvelab owner VM... KEY: whose VMs these are (owner KEY in their records; isolation follows)."""

from .. import accounts, legacy, nets
from ..credentials import cred_rows
from ..helptext import bad_usage
from ..inventory import vm_value, inventory, on_net, read_conf, targets, vm_names
from ..ifaces import gateway_elsewhere
from ..isolation import is_isolated, set_isolation
from ..owners import by_owner, group_login, is_shared_vm, key_problem, net_name_of, owner_from_name, owner_of, SLOT, without_net
from ..util import die, LabError, warn


def refresh_isolation(on_nets, inv, changed):
    """Isolation tables follow owners: rebuilt for the nets that have it on (inv: inventory()). An SDN net
    whose gateway is on another host has its isolation there: said (for the nets in changed, whose owners
    changed), not done from here."""
    for n in sorted(on_nets):
        name = nets.net_name(n)
        if is_isolated(n):
            try:
                set_isolation(n, confs=inv, tell_offline=True)     # an offline host keeps the old owners
            except (LabError, OSError) as e:     # the other nets still; this one: once the problem is fixed
                warn(f"{name}: isolation not rebuilt everywhere ({e}) - pvelab isolate --net {name}")
        elif n in changed and (where := gateway_elsewhere(n)):
            warn(f"{name}'s gateway is on {where}: if {name} is isolated, run pvelab isolate --net {name} there, "
                 "so its isolation follows the new owners")


def cmd_owner(args):
    if "--check" in args:                    # what an older pvelab recorded by itself: pvelab migrate now
        bad_usage("owner --check is gone: pvelab migrate --dry-run lists the VMs made before and their owners")
    if len(args) < 2:
        bad_usage("pvelab owner VM... KEY")
    *vms, arg = args
    if why := accounts.vm_name_problem(arg):
        bad_usage(f"KEY (an owner's name): {why}")
    confs = {i: read_conf(i) or die(f"VM {i}: no config") for i in targets(vms)}
    net = {i: vm_value(c, "net") for i, c in confs.items()}
    found = nets.all_nets()                  # read once: which nets this host knows, and their names
    known, called = nets.known_nets(found), vm_names(confs, confs)
    if far := [called[i] for i in confs if not nets.known_here(net[i], known)]:
        die(f"{', '.join(far)}: its net is not known on this host - run pvelab owner on the host with that net")
    keys = {i: without_net(arg, nets.net_name(net[i]) if net[i] not in ("", "dhcp") else None)
            for i in confs}                  # net2-alice01 on net2: alice01
    names = nets.pvelab_names(found)
    for k in set(keys.values()):
        if why := key_problem(k, names):
            bad_usage(f"KEY {arg}: {why}")
    for i, c in confs.items():
        if is_shared_vm(c):
            die(f"{called[i]} is {nets.net_name(net[i])}'s shared VM: it is no one's (its fixed MAC and its rules "
                "stay with it)")

    todo = {i: c for i, c in confs.items() if vm_value(c, "owner") != keys[i]}
    for i, c in confs.items():
        if i not in todo:
            print(f"{called[i]}: owner {keys[i]} already")

    if todo:                                 # one write, every VM's record
        legacy.write({i: {"owner": keys[i], "role": None} for i in todo})
    done = {}
    for i in sorted(todo):
        was = owner_of(todo[i])
        done[i] = True, f"{called[i]}: owner {keys[i]}" + (f" (was {was})" if was != keys[i] else "")
    for _, line in done.values():
        print(line)
    on_nets = {n for n in net.values() if n not in ("", "dhcp")}
    by_net = {}                              # net -> {vmid: (its config before, its new owner)}
    for i, (ok, _) in done.items():
        if ok and net[i] in on_nets:
            by_net.setdefault(net[i], {})[i] = (todo[i], keys[i])
    if on_nets:                              # the named VMs' nets, changed or not (a run again rebuilds too)
        inv = inventory()
        if by_net:
            warn_layout(by_net, inv)
        refresh_isolation(on_nets, inv, set(by_net))


def warn_layout(by_net, inv):
    """After owners changed ({net: {vmid: (config before, new owner)}}): what pvelab expects of an owner's
    VMs of a net - on one host, one login and password (handout, passwd), as many VMs as the others
    (create's NxM) - and names create would take for another owner's slot."""
    rows = cred_rows()
    for n, moved in sorted(by_net.items()):
        mine = on_net(n, inv)
        groups = by_owner(mine, mine)
        for key in sorted({k for _, k in moved.values()}):
            ids = groups.get(key, [])
            if len(hosts := {mine[i].get("_node") for i in ids}) > 1:
                warn(f"{key}'s VMs on {nets.net_name(n)} are on {len(hosts)} hosts ({', '.join(sorted(hosts))}): "
                     f"pvelab move them together (pvelab move VM... --to HOST)")
            known = [i for i in ids if i in rows]       # a password not recorded is unknown, not another one
            user, pw = group_login(rows, known) if known else (None, None)
            if any((rows[i]["user"], rows[i]["password"] or pw) != (user, pw) for i in known):
                warn(f"{key}'s VMs on {nets.net_name(n)} have different logins or passwords (one login card shows "
                     f"one): pvelab passwd {mine[ids[0]].get('name')} gives them one password")
        for c, key in moved.values():        # create would make the slot's owner a VM by this name
            if (was := owner_from_name(c.get("name", ""), net_name_of(c))) != key and SLOT.fullmatch(was):
                warn(f"{c.get('name')} is named as {was}'s VM: pvelab create on {nets.net_name(n)} stops at it - "
                     f"pvelab rename {c.get('name')} NEW")
        counts = [len(ids) for ids in groups.values()]
        if len(set(counts)) > 1:
            warn(f"{nets.net_name(n)}: owners have {min(counts)} to {max(counts)} VMs - pvelab create on it "
                 "counts VMs per owner (NxM): even them out first")
