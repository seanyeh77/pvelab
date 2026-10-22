"""pvelab expose / block / unblock / isolate"""

from ..blocks import apply_blocks, blocks_load, blocks_save
from ..config import BLOCK_KINDS, GUEST_USER, PUBLIC_HOST_FILE
from ..credentials import cred_rows
from ..expose import is_exposed, public_host, set_expose, ssh_port
from ..helptext import bad_usage
from ..ifaces import bridge_is_ours
from ..isolation import is_isolated, set_isolation
from ..inventory import targets
from ..args import no_bare_net, opt, valid_net
from ..util import die


def cmd_block(args, on=True):
    word = "block" if on else "unblock"
    if not args or args[0] not in (*BLOCK_KINDS, "all"):
        bad_usage(f"what to {word}: internet, ssh or all?")
    kinds = set(BLOCK_KINDS) if args[0] == "all" else {args[0]}
    ids, rows = targets(args[1:]), cred_rows()
    dhcp = [rows.get(i, {}).get("name", str(i)) for i in ids if rows.get(i, {}).get("net", "dhcp") == "dhcp"]
    if dhcp:
        die(f"only VMs made with --net N can be blocked; on vmbr1 (DHCP): {' '.join(dhcp)}")
    b = blocks_load()
    for i in ids:
        b.setdefault(i, set())
        b[i] = (b[i] | kinds) if on else (b[i] - kinds)
    blocks_save(b)
    for n in sorted({rows[i]["net"] for i in ids}, key=int):
        apply_blocks(n)
    what = " and ".join(sorted(kinds))
    print(f"{what} {'blocked' if on else 'unblocked'} for: " + " ".join(rows[i]["name"] for i in ids))


def cmd_expose(args):
    n, off, host = None, False, None
    it = iter(args)
    for a in it:
        if a == "--off":
            off = True
        elif (v := opt(a, it, "--host")) is not None:
            host = v
        elif (v := opt(a, it, "--net")) is not None:
            n = valid_net(v)
        else:
            no_bare_net(a)
            bad_usage(f"unexpected '{a}'")
    if n is None:
        bad_usage("which net? --net N (e.g. --net 40)")
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


def cmd_isolate(args):
    n, off = None, False
    it = iter(args)
    for a in it:
        if a == "--off":
            off = True
        elif (v := opt(a, it, "--net")) is not None:
            n = valid_net(v)
        else:
            no_bare_net(a)
            bad_usage(f"unexpected '{a}'")
    if n is None:
        bad_usage("which net? --net N (e.g. --net 40)")
    if not bridge_is_ours(n):
        die(f"net{n} has no lab bridge (create VMs with --net {n} first)")
    if off == (not is_isolated(n)):
        print(f"net{n}: students can already reach each other" if off else f"net{n} is already isolated")
        return
    set_isolation(n, not off)
    print(f"net{n}: students can't reach each other's VMs any more (a student's own VMs still can)" if not off
          else f"net{n}: all VMs can reach each other again")
