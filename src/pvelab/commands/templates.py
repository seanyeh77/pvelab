"""pvelab templates / pull"""

from ..catalogue import TEMPLATES
from ..config import DEFAULT_TEMPLATE
from ..helptext import bad_usage
from ..inventory import vm_exists
from ..templates import build_template, catalogue_find, node_templates
from ..util import die


def cmd_templates(_):
    on_node = node_templates()
    fmt = "  {:<6} {:<20} {:<20} {}"
    print("Catalogue:")
    print(fmt.format("VMID", "NAME", "ALIASES", "STATUS"))
    for t in TEMPLATES:
        if t.vmid in on_node:
            st = "on this node"
        elif vm_exists(t.vmid):
            st = "VMID in use by something else!"
        else:
            st = f"not built (pvelab pull {t.short})"
        if str(t.vmid) == DEFAULT_TEMPLATE or DEFAULT_TEMPLATE in (t.name, *t.aliases):
            st += "  (default)"
        print(fmt.format(t.vmid, t.name, ",".join(t.aliases), st))
    other = {i: n for i, n in on_node.items() if not catalogue_find(i)}
    if other:
        print("\nOther templates on this node (usable by VMID or name):")
        for i, n in other.items():
            print(fmt.format(i, n, "", ""))


def cmd_pull(args):
    if not args:
        bad_usage("which template(s)?")
    todo = TEMPLATES if args == ["all"] else [catalogue_find(a) or die(f"unknown template '{a}' "
                                                                     "(see: pvelab templates)") for a in args]
    for t in todo:
        build_template(t)
