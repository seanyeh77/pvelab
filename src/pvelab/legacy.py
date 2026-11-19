"""The places pvelab kept a VM's facts before the registry, read only: the description's managed-by line
(net, template, prefix, owner / role), credentials.tsv's row (name, user, password, ip) and blocks.json.
A VM not migrated yet is read from them; its first record (pvelab migrate, or the first write) is
legacy_record's. pvelab writes none of them any more: only destroy takes a VM's rows out."""

import json
import os

from . import registry
from .config import LEGACY_TAGS, MARKER, TAG
from .inventory import is_managed, read_conf, tag_list, vm_value
from .owners import SHARED_GROUP, owner_of, tail_token
from .util import replace_file, write_file


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


def write(fields):
    """Records changed: {vmid: {field: value}} (None drops a field), each VM's config read now (its ctime).
    A VM not migrated yet gets its legacy_record first, so a write keeps the owner, login and blocks it had."""
    from .blocks import blocks_load               # both read records through inventory: imported here
    from .credentials import cred_rows
    confs = {v: read_conf(v) for v in fields}
    changes, rows, blocks = {}, None, None
    for v, f in fields.items():
        conf = confs[v]
        if conf is not None and "_rec" not in conf and is_managed(conf):    # made before the registry
            if rows is None:
                rows, blocks = cred_rows(), blocks_load()
            changes[v] = {**legacy_record(v, conf, rows.get(v), blocks), **f}
        else:
            changes[v] = dict(f)
    if changes:
        registry.update(changes, confs)


def drop(vmids):
    """VMs destroyed: their credentials.tsv rows and blocks.json entries taken out (a VMID used again must
    not get them); the files are only ever made smaller."""
    from . import blocks, credentials
    gone = {str(v) for v in vmids}
    if os.path.exists(credentials.CRED):
        lines = open(credentials.CRED).readlines()
        keep = [l for l in lines if l.split("\t", 1)[0] not in gone]
        if keep != lines:
            replace_file(credentials.CRED, "".join(keep), 0o600)
    try:
        old = json.load(open(blocks.BLOCKS_FILE))
    except (OSError, ValueError):
        return
    if isinstance(old, dict) and gone & set(old):
        write_file(blocks.BLOCKS_FILE, json.dumps({k: v for k, v in old.items() if k not in gone}, indent=1), 0o600)


def strip_legacy(description, tags):
    """(description, tags) without what pvelab wrote there before the registry: the managed-by line and an
    older rename's student= at the description's end; pvelab's tags and net-* tags. An admin's notes and
    own tags stay as they were."""
    lines = [l for l in (description or "").split("\n") if MARKER not in l]
    desc = "\n".join(lines)
    if m := tail_token("student").search(desc):
        desc = desc[:m.start()]
    desc = desc.rstrip().lstrip("\n")
    ours = {TAG, *LEGACY_TAGS}
    return desc, ";".join(t for t in tag_list(tags) if t not in ours and not t.startswith("net-"))
