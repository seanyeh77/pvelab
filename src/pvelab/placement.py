"""Where create puts new students when there are several PVE hosts.

A student's VMs always share one host. A class may span hosts: each new student goes, as a whole, to the host
with the most room left (free memory and free disk), so that a class spreads. Students who already have a VM
keep its host. Nets that are a plain bridge (vmbrN, and vmbr1 for VMs without --net) exist on one host only."""

from .config import HOST_RESERVE_MB, is_sdn, STORAGE
from .nodes import this_node
from .resources import node_mem, pvesh_json


def node_room():
    """{node: {"mem": MB free, "disk": GB free}} of the online nodes, from one API call. Memory leaves
    HOST_RESERVE_MB for the host itself; disk is what STORAGE has free there. {} if the call fails."""
    rows = pvesh_json("/cluster/resources")
    room = {n: {"mem": total - used - HOST_RESERVE_MB, "disk": 0} for n, (total, used) in node_mem(rows).items()}
    for r in rows:
        if r.get("type") == "storage" and r.get("storage") == STORAGE and r.get("node") in room:
            room[r["node"]]["disk"] = (r.get("maxdisk", 0) - r.get("disk", 0)) // 2**30
    return room


def fits(room, mem, gb):
    """How many students needing mem MB and gb GB each fit in one node's room (never below 0)."""
    return max(0, int(min(score(room, mem, gb), 99999)))


def score(room, mem, gb):
    """How many times one more student fits in room (a fraction): the host with the highest gets the next."""
    return min(room["mem"] / mem if mem else float("inf"), room["disk"] / gb if gb else float("inf"))


def place(students, mem, gb, room, node=None, allowed=None, fixed=None):
    """{student: node} for the students of this run, each needing mem MB and gb GB.
    fixed: {student: node} of students who already have a VM - they stay there.
    node: put every other student there. Else the allowed node (default: any in room) with the most room
    after the students placed so far; ties go to the first name. When nothing fits, the best host still
    gets them (create's resource check asks first)."""
    fixed, left, out = fixed or {}, {n: dict(r) for n, r in room.items()}, {}
    pool = sorted(n for n in (allowed or left) if n in left)
    for s in students:
        if s in fixed:
            pick = fixed[s]
        elif node:
            pick = node
        elif pool:
            pick = max(pool, key=lambda n: score(left[n], mem, gb))     # max keeps the first of equals
        else:                                # no numbers (the API call failed): the allowed host, or here
            pick = sorted(allowed)[0] if allowed else this_node()
        out[s] = pick
        if pick in left:                     # the next student sees what this one takes
            left[pick]["mem"] -= mem
            left[pick]["disk"] -= gb
    return out


def bridge_host(net):
    """The one host a net's VMs must be on: here for a plain bridge (vmbrN, or vmbr1 for "dhcp"); None
    for an SDN vnet (labN), which is on every host."""
    return this_node() if net == "dhcp" or not is_sdn(net) else None
