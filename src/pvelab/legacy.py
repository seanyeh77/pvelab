"""The places pvelab kept a VM's facts before the registry, read only: the description's managed-by line
(net, template, prefix, owner / role), credentials.tsv's row (name, user, password, ip) and blocks.json.
A VM not migrated yet is read from them; its first record (pvelab migrate, or the first write) is
legacy_record's."""

from .inventory import vm_value
from .owners import SHARED_GROUP, owner_of


def legacy_record(vmid, conf, row, blocks):
    """The record VM vmid's old places describe. conf: its config (a record it has is not read: the
    description is), row: its cred_rows() row or None, blocks: blocks_load()'s {vmid: set}."""
    old = {k: v for k, v in (conf or {}).items() if k != "_rec"}
    rec = {k: value for k in ("net", "template", "prefix") if (value := vm_value(old, k))}
    owner = owner_of(old, (row or {}).get("name"))
    if owner == SHARED_GROUP:
        rec["role"] = "shared"
    elif owner:
        rec["owner"] = owner
    for k in ("name", "user", "password", "ip"):
        if (value := (row or {}).get(k)) and value != "-":
            rec[k] = value
    if blocks.get(vmid):
        rec["blocks"] = sorted(blocks[vmid])
    return rec
