"""A VM's login (vmid name user password net ip): its registry record, or (a VM not migrated yet)
credentials.tsv; and the ssh keys given to VMs (every host's root key, ADMIN_KEYS)."""

import functools
import glob
import os
import re
import secrets
import threading

from . import registry
from .config import ADMIN_KEYS, CRED, NODE_KEYS_DIR, ROOT_SSH_DIR
from .nodes import is_local, node_names, online_nodes, run_on, this_node
from .util import die, LabError, warn


def cred_rows():
    """{vmid: {name, user, password, net, ip}}: from the VMs' records, and credentials.tsv's rows of the VMs
    without one (not migrated yet)."""
    recs = registry.records()
    rows = {}
    if os.path.exists(CRED):
        for line in open(CRED):
            f = line.rstrip("\n").split("\t")
            if len(f) >= 4 and f[0].isdigit() and int(f[0]) not in recs:
                f += [""] * (6 - len(f))
                rows[int(f[0])] = {"name": f[1], "user": f[2], "password": f[3],
                                   "net": f[4] or "dhcp", "ip": f[5] or "-"}
    for v, rec in recs.items():
        rows[v] = {"name": str(rec.get("name", "")), "user": str(rec.get("user", "")),
                   "password": str(rec.get("password", "")), "net": str(rec.get("net") or "dhcp"),
                   "ip": str(rec.get("ip") or "-")}
    return dict(sorted(rows.items()))


FIELDS = ("name", "user", "password", "net", "ip")


def cred_save(rows):
    """cred_rows() rows changed by the caller (a new password, name or address): written to those VMs'
    records. The rows as they were stay where they are (a VM not migrated yet keeps its credentials.tsv
    row); a row taken out is not forgotten (remove_vm does that)."""
    from .legacy import write                    # legacy reads VMs through inventory, which imports this
    now = cred_rows()
    changed = {}
    for v, r in rows.items():
        row = {k: r[k] for k in FIELDS}
        if v not in now or row != {k: now[v][k] for k in FIELDS}:
            changed[v] = {**row, "ip": None if row["ip"] == "-" else row["ip"]}
    write(changed)


def gen_password():
    a = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(a) for _ in range(12))


KEY = re.compile(r"(?:^|\s)(?:ssh-|ecdsa-|sk-)\S+\s+\S+")   # a key's type and data: at the start, or after options
FETCH_TIMEOUT = 20                             # s to read another host's root key over ssh


def key_lines(text, said=None):
    """The public keys in text (authorized_keys / .pub lines, options such as from="..." kept), each stripped;
    comments and blank lines left out. said: a file name, to warn about the other lines (not keys)."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if KEY.search(line):
            out.append(line)
        elif line and not line.startswith("#") and said:
            warn(f"{said} line {n} is no ssh public key - left out")
    return out


def read_keys(path, said=False):
    try:
        with open(path) as f:
            return key_lines(f.read(), path if said else None)
    except OSError:
        return []


def keep_keys(path, keys):
    """keys written to path (cluster-wide, for the other hosts): a new file renamed over it, as pmxcfs needs. A
    failure is said, not fatal: the keys are in hand already."""
    if read_keys(path) == keys:
        return
    try:
        os.makedirs(NODE_KEYS_DIR, exist_ok=True)
        new = f"{path}.tmp.{os.getpid()}"
        with open(new, "w") as f:
            f.write("".join(k + "\n" for k in keys))
        os.replace(new, path)
    except OSError as e:
        warn(f"{path} not written ({e}): the other hosts read this key over ssh instead")


def own_root_keys():
    """This host's root public keys (id_*.pub), also kept in NODE_KEYS_DIR for the other hosts."""
    keys = sorted({k for f in glob.glob(f"{ROOT_SSH_DIR}/id_*.pub") for k in read_keys(f)})
    if keys:
        keep_keys(f"{NODE_KEYS_DIR}/{this_node()}.pub", keys)
    return keys


def node_root_keys(node):
    """Host node's root public keys: read over ssh while it is online (a host reinstalled or given a new key
    has its new one), and kept in NODE_KEYS_DIR; an offline host's kept ones. [] (said) when there are none."""
    path = f"{NODE_KEYS_DIR}/{node}.pub"
    if node in online_nodes():
        try:
            keys = sorted(set(key_lines(run_on(node, "sh", "-c", f"cat {ROOT_SSH_DIR}/id_*.pub", shared=False,
                                               quiet=True, timeout=FETCH_TIMEOUT))))
        except LabError as e:
            keys, why = [], str(e)
        else:
            why = "it has none"
        if keys:
            keep_keys(path, keys)
            return keys
        if kept := read_keys(path):
            warn(f"{node}'s root key not read ({why}): the one kept in {path} is used")
            return kept
        warn(f"{node}'s root key not read ({why}): pvelab ssh / run from {node} will not reach this VM")
        return []
    if kept := read_keys(path):
        return kept
    warn(f"{node} is offline and pvelab has no root key of it ({path}): pvelab ssh / run from {node} will not "
         "reach this VM")
    return []


_keys_lock = threading.Lock()                   # VMs are made by several threads: the keys read (and written) once


def ssh_keys():
    with _keys_lock:
        return _ssh_keys()


@functools.lru_cache(maxsize=1)
def _ssh_keys():
    """The keys a new VM's user gets (the text, one per line): every host's own root keys, so pvelab ssh / run
    work from any of them, and ADMIN_KEYS (the people who may log in). Not root's authorized_keys: that is
    whoever may log in to the hosts, old hosts' keys included (PVE only ever adds to it). Read once a run."""
    keys = set(own_root_keys())
    if not keys:
        die(f"no SSH public key of root on this host ({ROOT_SSH_DIR}/id_*.pub): ssh-keygen -t ed25519 makes one")
    for node in node_names():
        if not is_local(node):
            keys |= set(node_root_keys(node))
    if not os.path.exists(ADMIN_KEYS):
        warn(f"no {ADMIN_KEYS}: only pvelab ssh / run reach the new VMs - add the keys of the people who may log in "
             "to every VM there (one per line, as in authorized_keys)")
    keys |= set(read_keys(ADMIN_KEYS, said=True))
    return "\n".join(sorted(keys)) + "\n"
