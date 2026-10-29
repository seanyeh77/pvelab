"""pvelab templates / pull / save-template / delete-template"""

from ..catalogue import TEMPLATES
from ..config import DEFAULT_TEMPLATE
from ..helptext import bad_usage
from ..inventory import inventory, resolve
from ..templates import (build_template, catalogue_find, check_template_name, delete_template, is_saved,
                         latest_sum, node_templates, save_template, SAVED, template_status)
from ..util import die, parallel


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
            d = inv[i].get("description", "")
            print(fmt.format(i, n, "", d.removeprefix(SAVED).strip() if is_saved(inv[i]) else ""))


def cmd_pull(args):
    if not args:
        bad_usage("which template(s)?")
    todo = TEMPLATES if args == ["all"] else [catalogue_find(a) or die(f"unknown template '{a}' "
                                                                     "(see: pvelab templates)") for a in args]
    sums = parallel(latest_sum, todo)            # the checksum files, all at once
    for t in todo:
        build_template(t, sums[t])


def cmd_save_template(args):
    if len(args) != 2:
        bad_usage("save-template takes a VM and a name for the template")
    src = resolve(args[0])
    check_template_name(args[1], inventory())
    save_template(src, args[1])


def cmd_delete_template(args):
    if len(args) != 1:
        bad_usage("which template?")
    delete_template(args[0])
