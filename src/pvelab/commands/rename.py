"""pvelab rename VM NEW"""

from ..helptext import bad_usage
from ..readdress import vm_rename


def cmd_rename(args, dry=False):
    if len(args) != 2 or any(a.startswith("-") for a in args):
        bad_usage("rename takes a VM and its new name: pvelab rename VM NEW")
    vm_rename(*args, dry=dry)
