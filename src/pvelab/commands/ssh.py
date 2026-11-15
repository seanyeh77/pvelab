"""pvelab ssh"""

import os

from ..config import GUEST_USER
from ..credentials import cred_rows
from ..helptext import bad_usage
from ..inventory import resolve
from ..networks import vm_ip
from ..util import die


def cmd_ssh(args):
    if not args:
        bad_usage("which VM?")
    vmid = resolve(args[0])
    rows = cred_rows()
    ip = vm_ip(vmid, rows)
    user = rows.get(vmid, {}).get("user") or GUEST_USER          # its login (--accounts / --user)
    if ip == "-":
        die(f"no IP yet for VMID {vmid} (still booting?)")
    # Private NAT networks whose VMs get rolled back or re-created with reused IPs: don't pin host keys.
    os.execvp("ssh", ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                      "-o", "LogLevel=ERROR", f"{user}@{ip}", *args[1:]])
