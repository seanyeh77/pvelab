"""A VM's login (vmid name user password net ip): its registry record, or (a VM not migrated yet)
credentials.tsv; and the host ssh key given to VMs."""

import glob
import os
import re
import secrets

from . import registry
from .config import CRED
from .util import die, replace_file


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


def cred_save(rows):
    replace_file(CRED, "".join("\t".join([str(i), r["name"], r["user"], r["password"], r["net"], r["ip"]]) + "\n"
                               for i, r in sorted(rows.items())), 0o600)


def gen_password():
    a = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(a) for _ in range(12))


def ssh_keys():
    """root's authorized_keys + root's own public keys (the text, one per line), so 'pvelab ssh' works."""
    keys = set()
    for f in ["/root/.ssh/authorized_keys", *glob.glob("/root/.ssh/id_*.pub")]:
        try:
            keys |= {l.strip() for l in open(f) if re.match(r"(ssh-|ecdsa-|sk-)", l)}
        except OSError:
            pass
    if not keys:
        die("no SSH public keys found for root")
    return "\n".join(sorted(keys)) + "\n"
