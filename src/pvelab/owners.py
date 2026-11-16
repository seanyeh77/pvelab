"""Who a VM belongs to (its owner: a person, a team, a project) and whether it is a net's shared VM.

Both are kept in PVE, in the VM's description line (managed-by=lab.sh template=N net=N owner=KEY
[role=shared]): rollback keeps the description, and a VM's name can change without changing either.
KEY has no net name in it and is unique within a net; a group is (net, KEY). VMs made before these
tokens (not migrated yet) are read from their names: net2-student03-2 belongs to student03, and
NET-shared is the net's shared VM. The legacy token student=net2-student03 means owner student03."""

import re

from . import nets
from .inventory import desc_value, read_conf

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


def _net_name(conf):
    n = desc_value(conf, "net")
    return nets.net_name(n) if n and n != "dhcp" else None


def owner_of(conf, name=None):
    """A VM config's owner: owner=, else the legacy student= (its net prefix dropped), else its name."""
    conf = conf or {}
    if key := desc_value(conf, "owner"):
        return key
    if key := desc_value(conf, "student"):
        return without_net(key, _net_name(conf))
    return owner_from_name(conf.get("name") or name or "", _net_name(conf))


def is_shared_vm(conf, net=None):
    """Whether a VM config is a net's shared VM: role=shared, else (not migrated) its name is the net's
    shared name. net: the VM's net, if the config has no net= (else read from it)."""
    conf = conf or {}
    if role := desc_value(conf, "role"):
        return role == SHARED
    n = desc_value(conf, "net") or net
    return bool(n) and n != "dhcp" and conf.get("name") == shared_name(n)


def confs_of(vms, confs=None):
    """{vmid: config} of vms (each read now unless confs has it). vms {vmid: name}: a config without a name
    (gone) gets that one, so the legacy name rules still see it."""
    names = vms if isinstance(vms, dict) else {}
    out = {}
    for v in vms:
        conf = confs[v] if confs is not None and v in confs else read_conf(v) or {}
        out[v] = conf if conf.get("name") or v not in names else {**conf, "name": names[v]}
    return out


def by_owner(vms, confs=None):
    """{vmid: name} of one net's VMs -> {owner: [vmid, ...]} (VMIDs in order), its shared VM left out."""
    return {owner: ids for (_, owner), ids in groups_of(vms, confs).items() if owner != SHARED_GROUP}


def vms_per_owner(vms, confs=None):
    """How many VMs each owner has on a net ({vmid: name}), or 0 for none."""
    return max(map(len, by_owner(vms, confs).values()), default=0)


def with_tokens(description, **tokens):
    """description with key=value tokens set, replaced, or (value None) removed on its first line (the
    managed-by line); the other lines as they were."""
    first, _, rest = (description or "").partition("\n")
    for key, value in tokens.items():
        first = re.sub(rf"(?:^|\s+){re.escape(key)}=\S*", "", first).strip()
        if value is not None:
            first = f"{first} {key}={value}".strip()
    return first + ("\n" + rest if rest else "")


def group_of(conf, name=None):
    """(net, owner) of a VM config; a net's shared VM is a group of its own: (net, SHARED_GROUP)."""
    conf = conf or {}
    n = desc_value(conf, "net") or "dhcp"
    return (n, SHARED_GROUP) if is_shared_vm(conf) else (n, owner_of(conf, name))


def groups_of(vms, confs=None):
    """{vmid: name} of VMs on any nets -> {(net, owner): [vmid, ...]} (VMIDs in order)."""
    confs = confs_of(vms, confs)
    out = {}
    for v in sorted(vms):
        out.setdefault(group_of(confs[v]), []).append(v)
    return out
