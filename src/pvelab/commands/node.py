"""pvelab node list | add ADDR   (also: pvelab nodes)"""

from ..config import STORAGE
from ..helptext import bad_usage
from ..inventory import inventory, managed
from ..joining import node_add
from ..nodes import is_local, members
from ..resources import free_mem, node_mem, storage
from ..templates import templates_by_node, with_old
from ..ui import paint


def cmd_node(args):
    if args in ([], ["list"]):
        return node_list()
    if len(args) == 2 and args[0] == "add":
        return node_add(args[1])
    bad_usage("node takes: list, or add ADDR")


def node_list():
    """One row per host: online, free memory and disk, pvelab VMs, templates. No cluster: this host."""
    inv = inventory()
    vms = managed(inv)
    tpls = templates_by_node(inv)
    live = node_mem()
    cols = [("HOST", 14), ("IP", 15), ("STATUS", 8), ("MEM FREE", 9), (f"DISK FREE ({STORAGE})", 22), ("VMS", 5), ("TEMPLATES", 0)]
    print(" ".join(f"{h:<{w}}" for h, w in cols).rstrip())
    for node, m in sorted(members().items(), key=lambda nm: (not is_local(nm[0]), nm[0])):   # this host first
        mem_cell, disk_cell = "-", "-"
        if m["online"]:
            ram, disk = free_mem(node, live), storage(node)[1]
            mem_cell = "?" if ram is None else f"{ram[1] / 1024:.1f} GB"
            disk_cell = "?" if disk is None else f"{disk} GB"
        count = sum(1 for c in vms.values() if c.get("_node") == node)
        names = [with_old(t, where[node]) for t, where in sorted(tpls.items()) if node in where]
        values = [node, m.get("ip") or "-", "online" if m["online"] else "offline", mem_cell, disk_cell, count, ", ".join(names) or "-"]
        colors = {"STATUS": "green" if m["online"] else "red"}
        print(" ".join(paint(str(v), colors.get(h), w) for v, (h, w) in zip(values, cols)).rstrip())
