"""pvelab templates / pull"""

from ..catalogue import TEMPLATES
from ..config import DEFAULT_TEMPLATE
from ..helptext import bad_usage
from ..inventory import inventory, vm_exists
from ..templates import build_template, built_on, catalogue_find, node_templates
from ..util import die


def template_status(t, inv, on_node):
    if t.vmid in on_node:
        day = built_on(inv[t.vmid])
        return "on this node" + (f", built {day}" if day else "")
    if vm_exists(t.vmid):
        return "VMID in use by something else!"
    return "not built"


def cmd_templates(_):
    inv = inventory()
    on_node = node_templates(inv)
    fmt = "  {:<6} {:<20} {:<20} {}"
    print("Catalogue:")
    print(fmt.format("VMID", "NAME", "ALIASES", "STATUS"))
    for t in TEMPLATES:
        st = template_status(t, inv, on_node)
        if st == "not built":
            st += f" (pvelab pull {t.short})"
        if str(t.vmid) == DEFAULT_TEMPLATE or DEFAULT_TEMPLATE in (t.name, *t.aliases):
            st += "  (default)"
        print(fmt.format(t.vmid, t.name, ",".join(t.aliases), st))
    print("\nTo get the newest image: pvelab pull NAME (or all) - VMs already made are not changed")
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
