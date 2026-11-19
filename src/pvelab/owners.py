"""Who a VM belongs to (its owner: a person, a team, a project) and whether it is a net's shared VM.

Both are kept in PVE, in the VM's description line (managed-by=lab.sh template=N net=N owner=KEY
[role=shared]): rollback keeps the description, and a VM's name can change without changing either.
KEY has no net name in it and is unique within a net; a group is (net, KEY). VMs made before these
tokens (not migrated yet) are read from their names: net2-student03-2 belongs to student03, and
NET-shared is the net's shared VM. The legacy token student=net2-student03 means owner student03."""

import re

from . import nets
from .config import MARKER
from .inventory import vm_value, marker_line, read_conf

SHARED = "shared"
SHARED_GROUP = "+shared"                     # group_of's owner for a shared VM: no owner key has a +


def shared_name(n):
    """The default name of net n's shared VM (pvelab create --shared): NET-shared."""
    return f"{nets.net_name(n)}-{SHARED}"


def owner_from_name(name, net_name=None):
    """The owner a name tells: net2-student03-2 -> student03 (net_name net2: its prefix dropped); a name
    without -N after a number is its own owner."""
    m = re.fullmatch(r"(.+-\D*\d+)-\d+", name or "")
    return without_net(m.group(1) if m else (name or ""), net_name)


def without_net(key, net_name):
    """key without a leading NET- (an account's or a legacy student= name); its numbers as they are."""
    return key[len(net_name) + 1:] if net_name and key.startswith(f"{net_name}-") else key


def net_name_of(conf):
    """The name of a VM config's net, or None (DHCP, or not known)."""
    n = vm_value(conf or {}, "net")
    return nets.net_name(n) if n and n != "dhcp" else None


SLOT = re.compile(r"([a-z]+)\d{2}")          # an owner create names: PREFIXnn (f"{prefix}{i:02d}")


def key_problem(key, names):
    """Why key can't be an owner's (names: pvelab's own nets' names), or None."""
    if key == SHARED:
        return "'shared' marks a net's shared VM"
    if other := next((m for m in names if key.startswith(f"{m}-")), None):
        return f"{other} is another network - an owner's name without it"
    return None


def tail_token(key):
    """key=VALUE at the very end of a text (VALUE in group 1): after a space, or alone on its line."""
    return re.compile(rf"(?:\A|\n|[ \t]+){re.escape(key)}=(\S+)\s*\Z")


def legacy_student(conf):
    """student=KEY on the managed-by line, or at the very end of the description (an older pvelab rename
    appended it there, after an admin's notes)."""
    if key := vm_value(conf, "student"):
        return key
    m = tail_token("student").search((conf or {}).get("description", "") or "")
    return m.group(1) if m else ""


def owner_of(conf, name=None):
    """A VM config's owner: SHARED_GROUP for a net's shared VM (no owner's), owner=, else the legacy student=
    (its net prefix dropped), else its name."""
    conf = conf or {}
    if is_shared_vm(conf):
        return SHARED_GROUP
    if key := vm_value(conf, "owner"):
        return key
    if key := legacy_student(conf):
        return without_net(key, net_name_of(conf))
    return owner_from_name(conf.get("name") or name or "", net_name_of(conf))


def is_shared_vm(conf, net=None):
    """Whether a VM config is a net's shared VM: role=shared, else (not migrated) its name is the net's
    shared name. net: the VM's net, if the config has no net= (else read from it)."""
    conf = conf or {}
    if role := vm_value(conf, "role"):
        return role == SHARED
    if vm_value(conf, "owner"):            # recorded as someone's: its name says nothing
        return False
    n = vm_value(conf, "net") or net
    return bool(n) and n != "dhcp" and conf.get("name") == shared_name(n)


def confs_of(vms, confs=None):
    """{vmid: config} of vms (each read now unless confs has it). vms {vmid: credentials row} (or {vmid:
    name}): a VM whose config is gone gets one from its row - its name and net - so the legacy name rules
    still put it with its owner."""
    out = {}
    for v in vms:
        conf = confs.get(v) if confs is not None else None
        conf = conf if conf is not None else read_conf(v)
        if conf is None and isinstance(vms, dict):       # gone: what its row says
            row = vms[v] if isinstance(vms[v], dict) else {"name": vms[v]}
            conf = {"name": row.get("name"), "description": f"{MARKER} net={row['net']}" if row.get("net") else ""}
        out[v] = conf or {}
    return out


def by_owner(vms, confs=None):
    """{vmid: name or row} of one net's VMs -> {owner: [vmid, ...]} (VMIDs in order), its shared VM left out."""
    confs, out = confs_of(vms, confs), {}
    for v in sorted(vms):
        if (owner := owner_of(confs[v]) or f"#{v}") != SHARED_GROUP:      # no name, no owner=: its own
            out.setdefault(owner, []).append(v)
    return out


def vms_per_owner(vms, confs=None):
    """How many VMs each owner has on a net ({vmid: name or row}), or 0 for none."""
    return max(map(len, by_owner(vms, confs).values()), default=0)


def is_recorded(conf):
    """Whether a VM config has its owner (owner=) or role (role=shared) in its description."""
    return bool(vm_value(conf, "owner") or vm_value(conf, "role"))


def with_tokens(description, **tokens):
    """description with key=value tokens set or replaced on its managed-by line (marker_line), or (value
    None) removed from it and from the description's end; the other lines (an admin's notes) as they were."""
    lines = (description or "").split("\n")
    k = marker_line(description)
    for key, value in tokens.items():
        drop = re.compile(rf"(?:^|\s+){re.escape(key)}=\S*")
        if value is None:                    # off the managed-by line, and the end of the description (where
            lines[k] = drop.sub("", lines[k]).strip()      # an older rename appended student=); notes stay
            after = "\n".join(lines[k + 1:])
            if after and (m := tail_token(key).search(after)):
                after = after[:m.start()]    # blank lines before it stay
                lines = lines[:k + 1] + (after.split("\n") if after else [])
        else:
            lines[k] = f"{drop.sub('', lines[k]).strip()} {key}={value}".strip()
    return "\n".join(lines)


def group_of(conf, name=None):
    """(net, owner) of a VM config; a net's shared VM is a group of its own: (net, SHARED_GROUP)."""
    conf = conf or {}
    n = vm_value(conf, "net") or "dhcp"
    return n, owner_of(conf, name)


def groups_of(vms, confs=None):
    """{vmid: name or row} of VMs on any nets -> {(net, owner): [vmid, ...]} (VMIDs in order); the net is the
    row's when vms has rows (the credentials: a config left behind by a failed change may disagree)."""
    confs = confs_of(vms, confs)
    out = {}
    for v in sorted(vms):
        conf, row = confs[v], vms[v] if isinstance(vms, dict) and isinstance(vms[v], dict) else {}
        if row.get("net") and row["net"] != vm_value(conf, "net"):       # the row's net: its prefix too
            conf = {**conf, "description": with_tokens(conf.get("description", ""), net=row["net"])}
        n, owner = group_of(conf)
        out.setdefault((n, owner or f"#{v}"), []).append(v)     # no name, no owner=: a group of its own
    return out
