"""pvelab destroy"""

import sys

from ..config import bridge, YES
from ..credentials import cred_rows
from ..ifaces import bridge_is_ours
from ..inventory import inventory, net_of, sorted_nets, targets, vm_names
from ..lifecycle import remove_vm
from ..networks import refresh_net, remove_bridge
from ..ui import dry_run
from ..util import confirm_yes


def emptied_nets(nets, rows):
    """Of nets, those with no VMs in rows whose bridge pvelab made (so it may go too)."""
    return [n for n in sorted_nets(nets) if bridge_is_ours(n) and all(r["net"] != n for r in rows.values())]


def cmd_destroy(args, dry=False):
    ids = targets(args)
    if not ids:
        print("no student VMs")
        return
    rows, names = cred_rows(), vm_names(ids, inventory())
    nets = {net_of(i, rows) for i in ids}
    left = {v: r for v, r in rows.items() if v not in ids}      # the credentials after destroying
    if dry:
        dry_run("DESTROY (VM, disks, snapshots, credentials)", [f"{names[i]} ({i})" for i in ids]
                + [f"net{n} would have no VMs left: its bridge {bridge(n)} can be removed too"
                   for n in emptied_nets(nets, left)])
        return
    confirm_yes("About to DESTROY: " + " ".join(f"{names[i]}({i})" for i in ids))
    for i in ids:
        remove_vm(i)
        print(f"{names[i]} destroyed")
    for n in sorted_nets(nets):
        refresh_net(n)                           # drop the destroyed VMs' ports, block rules, isolation
    for n in emptied_nets(nets, left):
        if YES:
            remove_bridge(n)
        elif sys.stdin.isatty():
            if input(f"net{n} has no VMs left. Remove bridge {bridge(n)} too? [y/N] ").strip().lower() == "y":
                remove_bridge(n)
            else:
                print(f"keeping {bridge(n)}")
        else:
            print(f"net{n} has no VMs left; bridge {bridge(n)} kept "
                  "(re-run interactively or with LAB_YES=1 to remove it)")
