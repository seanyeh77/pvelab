"""pvelab templates / pull"""

from ..catalogue import TEMPLATES
from ..config import DEFAULT_TEMPLATE
from ..helptext import bad_usage
from ..inventory import inventory
from ..templates import build_template, catalogue_find, node_templates, template_status
from ..util import die


def cmd_templates(_):
    inv = inventory()
    on_node = node_templates(inv)
    default = catalogue_find(DEFAULT_TEMPLATE)
    fmt = "  {:<6} {:<20} {:<20} {}"
    print("Catalogue:")
    print(fmt.format("VMID", "NAME", "ALIASES", "STATUS"))
    for t in TEMPLATES:
        st = template_status(t, inv) + ("  (default)" if t is default else "")
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
