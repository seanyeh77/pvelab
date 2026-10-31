"""pvelab expose / block / unblock / isolate"""

from ..blocks import apply_blocks, blocks_load, blocks_save
from ..config import BLOCK_KINDS
from ..credentials import cred_rows
from ..expose import expose_net, is_exposed, set_expose
from ..helptext import bad_usage
from ..ifaces import bridge_is_ours
from ..isolation import is_isolated, set_isolation
from ..inventory import net_of, sorted_nets, targets, vm_names
from ..args import no_bare_net, opt, valid_net
from ..ui import some
from ..util import die


def cmd_block(args, on=True):
    word = "block" if on else "unblock"
    if not args or args[0] not in (*BLOCK_KINDS, "all"):
        bad_usage(f"what to {word}: internet, ssh or all?")
    kinds = set(BLOCK_KINDS) if args[0] == "all" else {args[0]}
    ids, rows = targets(args[1:]), cred_rows()
    names = vm_names(ids)
    dhcp = [names[i] for i in ids if net_of(i, rows) == "dhcp"]
    if dhcp:
        die(f"only VMs made with --net N can be blocked; on vmbr1 (DHCP): {some(dhcp, 10, ' ')}")
    b = blocks_load()
    for i in ids:
        b[i] = b.get(i, set()) | kinds if on else b.get(i, set()) - kinds
    blocks_save(b)
    for n in sorted_nets({net_of(i, rows) for i in ids}):
        apply_blocks(n)
    what = " and ".join(sorted(kinds))
    print(f"{what} {'blocked' if on else 'unblocked'} for: " + " ".join(names.values()))


def net_switch_args(args, host_ok=False):
    """--net N [--off] (expose also [--host ADDR]) -> (net, off, host)."""
    n, off, host = None, False, None
    it = iter(args)
    for a in it:
        if a == "--off":
            off = True
        elif (v := opt(a, it, "--net")) is not None:
            n = valid_net(v)
        elif host_ok and (v := opt(a, it, "--host")) is not None:
            host = v
        else:
            no_bare_net(a)
            bad_usage(f"unexpected '{a}'")
    if n is None:
        bad_usage("which net? --net N (e.g. --net 40)")
    if not bridge_is_ours(n):
        die(f"net{n} has no lab bridge (create VMs with --net {n} first)")
    return n, off, host


def cmd_expose(args):
    # No "--expose without harden" warning here, only in create: on an existing net there is no
    # reliable way to tell which VMs ran harden. Snapshot names get cut or hashed and can be rolled
    # back past, harden may have been run from a file, and a marker file in the VM would need the
    # guest agent of every running VM.
    n, off, host = net_switch_args(args, host_ok=True)
    if not off:
        return expose_net(n, host)
    if not is_exposed(n):
        print(f"net{n} is not exposed")
        return
    set_expose(n, False)
    print(f"net{n}: ssh port forwards removed")


def cmd_isolate(args):
    n, off, _ = net_switch_args(args)
    if is_isolated(n) != off:
        print(f"net{n}: students can already reach each other" if off else f"net{n} is already isolated")
        return
    set_isolation(n, not off)
    print(f"net{n}: all VMs can reach each other again" if off
          else f"net{n}: students can't reach each other's VMs any more (a student's own VMs still can)")
