"""pvelab destroy"""

import sys

from ..blocks import apply_blocks, blocks_load, blocks_save
from ..config import IFACES, YES
from ..credentials import cred_rows, cred_save
from ..expose import is_exposed, set_expose
from ..ifaces import block_re, bridge_is_ours
from ..inventory import inventory, targets
from ..lifecycle import remove_vm
from ..networks import remove_bridge
from ..util import confirm_yes


def cmd_destroy(args):
    dry = "--dry-run" in args
    ids = targets([a for a in args if a != "--dry-run"])
    if not ids:
        print("no student VMs")
        return
    inv, rows = inventory(), cred_rows()
    if dry:
        print("dry run - would DESTROY (VM, disks, snapshots, credentials):")
        for i in ids:
            print(f"  {inv[i]['name']} ({i})")
        left = {r["net"] for v, r in rows.items() if v not in ids}
        for n in sorted({rows.get(i, {}).get("net", "dhcp") for i in ids} - {"dhcp"} - left, key=int):
            print(f"  net{n} would have no VMs left: its bridge vmbr{n} can be removed too")
        return
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
