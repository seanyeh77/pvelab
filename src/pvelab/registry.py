"""pvelab's records of its VMs and templates: one cluster-wide file, REGISTRY (in /etc/pve/priv: root only,
it holds the passwords), so pvelab writes nothing into a VM's config. A record belongs to a VM by its VMID
and PVE's creation time (meta: ctime=): a VM deleted in the web UI, or its VMID used again, is never taken
for the one recorded. {"vms": {vmid: record}, "templates": {vmid: record}}; a record's fields:

  vms        name, net, ip, user, password, owner | role ("shared"), template, prefix, blocks (a list)
  templates  kind ("built" | "copy" | "saved"), image, built, orig, stamp, from

Writes hold a lock (REGISTRY_LOCK, a directory: pmxcfs makes mkdir atomic across the cluster) and replace
the file whole; records of VMs that are gone are dropped then."""

import json
import os
import re
import time
from contextlib import contextmanager

from .util import die

REGISTRY = "/etc/pve/priv/pvelab/vms.json"
REGISTRY_LOCK = "/etc/pve/priv/lock/pvelab-registry"
LOCK_WAIT = 30                                  # s to wait for another pvelab run's write
KINDS = ("vms", "templates")

_cache = None                                   # (mtime, data) of the file last read


def ctime_of(conf):
    """PVE's creation time of a VM config (its meta line's ctime=), or None (a config made before PVE had it)."""
    m = re.search(r"(?:^|,)ctime=(\d+)", (conf or {}).get("meta", "") or "")
    return int(m.group(1)) if m else None


def _read():
    try:
        mtime = os.stat(REGISTRY).st_mtime_ns
    except OSError:
        return {k: {} for k in KINDS}
    global _cache
    if _cache and _cache[0] == mtime:
        return _cache[1]
    try:
        data = json.loads(open(REGISTRY).read())
        if not isinstance(data, dict) or not all(isinstance(data.get(k, {}), dict) for k in KINDS):
            raise ValueError("not pvelab's records")
    except (OSError, ValueError) as e:
        die(f"{REGISTRY} can't be read ({e}): fix it or move it away; pvelab migrate writes the records "
            "of the VMs that still have them in their description again")
    data = {k: data.get(k, {}) for k in KINDS}
    _cache = (mtime, data)
    return data


def load():
    """{"vms": {str(vmid): record}, "templates": {...}} as the file has it (records of gone VMs too)."""
    return _read()


def lookup(vmid, conf, kind="vms"):
    """VM vmid's record (a copy, without its ctime check) when it was made for this config, else None."""
    rec = _read()[kind].get(str(vmid))
    if rec is None or rec.get("ctime") != ctime_of(conf):
        return None
    return dict(rec)


@contextmanager
def locked():
    """Hold REGISTRY_LOCK: one writer at a time in the whole cluster."""
    os.makedirs(os.path.dirname(REGISTRY_LOCK), exist_ok=True)
    end = time.time() + LOCK_WAIT
    while True:
        try:
            os.mkdir(REGISTRY_LOCK)
            break
        except FileExistsError:
            if time.time() >= end:
                die(f"the pvelab registry is locked by another pvelab run ({REGISTRY_LOCK}): wait for it to "
                    "finish, or remove that directory if no pvelab runs on any host")
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            os.rmdir(REGISTRY_LOCK)
        except OSError:
            pass


def _confs(vmids, confs):
    """{vmid: config} for vmids: confs' own, the others read from PVE (None: no such VM)."""
    from .inventory import read_conf             # inventory reads records through this module
    return {v: confs[v] if v in confs else read_conf(v) for v in vmids}


def update(changes, confs, kind="vms"):
    """Records changed: {vmid: {field: value}} merged in (a value None drops the field), {vmid: None}
    forgets the VM. confs: {vmid: config} of the VMs changed (their ctime is recorded); other records are
    checked against PVE and dropped when their VM is gone or is another one now."""
    with locked():
        global _cache
        _cache = None
        data = {k: dict(v) for k, v in _read().items()}
        recs = data[kind]
        for vmid, fields in changes.items():
            key = str(vmid)
            if fields is None:
                recs.pop(key, None)
                continue
            rec = dict(recs.get(key) or {})
            ctime = ctime_of(confs.get(vmid))
            if rec.get("ctime") != ctime:        # another VM by that VMID before: nothing of it carries over
                rec = {}
            rec["ctime"] = ctime
            for k, v in fields.items():
                if v is None:
                    rec.pop(k, None)
                else:
                    rec[k] = v
            recs[key] = rec
        for k in KINDS:                          # records of VMs gone (or another VM by that VMID now)
            have = {int(v): r for v, r in data[k].items()}
            now = _confs([v for v in have if v not in changes], confs)
            for v, conf in now.items():
                if conf is None or ctime_of(conf) != have[v].get("ctime"):
                    data[k].pop(str(v))
        os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
        new = REGISTRY + ".new"
        with open(os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(json.dumps({k: dict(sorted(data[k].items(), key=lambda kv: int(kv[0]))) for k in KINDS},
                               indent=1) + "\n")
        os.chmod(new, 0o600)
        os.replace(new, REGISTRY)
        _cache = None


def forget(vmids, kind="vms"):
    """Drop the records of vmids (destroyed)."""
    update({v: None for v in vmids}, {}, kind)
