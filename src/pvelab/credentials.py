"""credentials.tsv (vmid name user password net ip) and the host ssh key given to VMs."""

import glob
import os
import re
import secrets

from .config import CRED, LAB_DIR
from .util import die


def cred_rows():
    rows = {}
    if os.path.exists(CRED):
        for line in open(CRED):
            f = line.rstrip("\n").split("\t")
            if len(f) >= 4 and f[0].isdigit():
                f += ["", ""][: max(0, 6 - len(f))]
                rows[int(f[0])] = {"name": f[1], "user": f[2], "password": f[3],
                                   "net": f[4] or "dhcp", "ip": f[5] or "-"}
    return rows


def cred_save(rows):
    tmp = CRED + ".tmp"
    with open(tmp, "w") as f:
        for i in sorted(rows):
            r = rows[i]
            f.write("\t".join([str(i), r["name"], r["user"], r["password"], r["net"], r["ip"]]) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CRED)


def gen_password():
    a = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(a) for _ in range(12))


def keyfile():
    """root's authorized_keys + root's own public keys, so 'pvelab ssh' works."""
    keys = set()
    for f in ["/root/.ssh/authorized_keys", *glob.glob("/root/.ssh/id_*.pub")]:
        try:
            keys |= {l.strip() for l in open(f) if re.match(r"(ssh-|ecdsa-|sk-)", l)}
        except OSError:
            pass
    if not keys:
        die("no SSH public keys found for root")
    path = f"{LAB_DIR}/.sshkeys"
    with open(path, "w") as f:
        f.write("\n".join(sorted(keys)) + "\n")
    return path
