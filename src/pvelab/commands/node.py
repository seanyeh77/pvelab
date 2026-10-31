"""pvelab node list | add ADDR | upgrade [HOST...] [--major] [--dry-run] | rename OLD NEW [--dry-run] | rename
(also: pvelab nodes)"""

from .. import hosts
from ..helptext import bad_usage
from ..joining import node_add
from ..renaming import node_rename, pending
from ..upgrading import node_upgrade


USAGE = "node takes: list, add ADDR, upgrade [HOST...] [--major] [--dry-run], or rename OLD NEW [--dry-run]"


# cli runs one of these with the words after `node` (the first is the subcommand's own name). It refuses
# --dry-run for list and add; upgrade and rename get dry=True for it.

def cmd_node_list(args):
    """node, node list [-v] [-j] (also: pvelab nodes); any other word after node: the usage."""
    flags = {a for a in args if a in ("-v", "--verbose", "-j", "--json")}
    if [a for a in args if a not in flags] not in ([], ["list"]):
        bad_usage(USAGE)
    hosts.node_list(verbose=bool(flags & {"-v", "--verbose"}), as_json=bool(flags & {"-j", "--json"}))


def cmd_node_add(args):
    if len(args) != 2:
        bad_usage(USAGE)
    node_add(args[1])


def cmd_node_upgrade(args, dry=False):
    hosts = [a for a in args[1:] if a not in ("--major", "--minor")]
    if any(h.startswith("-") for h in hosts):
        bad_usage(f"node upgrade: unknown option {next(h for h in hosts if h.startswith('-'))}")
    if "--major" in args and "--minor" in args:
        bad_usage("node upgrade: --major or --minor, not both")
    node_upgrade(hosts, major_="--major" in args, dry=dry, minor="--minor" in args)


def cmd_node_rename(args, dry=False):
    """node rename OLD NEW; alone: finishes the unfinished rename."""
    if len(args) == 3:
        return node_rename(args[1], args[2], dry=dry)
    if args == ["rename"] and pending():
        return node_rename(None, None, dry=dry)
    bad_usage("--dry-run works with node upgrade and node rename" if dry else USAGE)
