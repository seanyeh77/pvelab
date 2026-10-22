"""pvelab destroy"""

import sys

from ..blocks import blocks_load, blocks_save
from ..config import YES
from ..credentials import cred_rows, cred_save
from ..ifaces import bridge_is_ours
from ..inventory import inventory, net_of, targets
from ..lifecycle import remove_vm
from ..networks import refresh_net, remove_bridge
from ..ui import dry_run
from ..util import confirm_yes


def emptied_nets(nets, rows):
    """Of nets, those with no VMs in rows whose bridge pvelab made (so it may go too)."""
    return sorted((n for n in nets - {"dhcp"} if bridge_is_ours(n) and all(r["net"] != n for r in rows.values())),
                  key=int)


def cmd_destroy(args, dry=False):
    ids = targets(args)
    if not ids:
        print("no student VMs")
        return
    inv, rows = inventory(), cred_rows()
    nets = {net_of(i, rows) for i in ids}
    if dry:
        left = {v: r for v, r in rows.items() if v not in ids}
        dry_run("DESTROY (VM, disks, snapshots, credentials)", [f"{inv[i]['name']} ({i})" for i in ids]
                + [f"net{n} would have no VMs left: its bridge vmbr{n} can be removed too"
                   for n in emptied_nets(nets, left)])
        return
    confirm_yes("About to DESTROY: " + " ".join(f"{inv[i]['name']}({i})" for i in ids))
    blocks = blocks_load()
    for i in ids:
        remove_vm(i)
        rows.pop(i, None)
        cred_save(rows)
        if blocks.pop(i, None) is not None:
            blocks_save(blocks)
        print(f"{inv[i]['name']} destroyed")
    for n in sorted(nets - {"dhcp"}):
        refresh_net(n)                           # drop the destroyed VMs' ports, block rules, isolation
    for n in emptied_nets(nets, rows):
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
