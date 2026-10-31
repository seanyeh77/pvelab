"""pvelab run"""

import sys

from ..args import opt
from ..config import RUN_TIMEOUT_MIN
from ..helptext import bad_usage
from ..inventory import targets
from ..scripts import parse_chain, print_chain, run_and_snapshot


def cmd_run(args):
    timeout, rest = RUN_TIMEOUT_MIN, []
    it = iter(args)
    for a in it:
        if (v := opt(a, it, "--timeout")) is not None:
            if not v.isdigit() or int(v) < 1:
                bad_usage("--timeout needs minutes (a number)")
            timeout = int(v)
        else:
            rest.append(a)
    if len(rest) < 2:
        bad_usage("which VM(s), and which script?")
    *vms, text = rest
    chain = parse_chain(text)                # library names and files, e.g. harden+./setup.sh
    if not chain:
        bad_usage("which script(s)? library names or files, joined with + (see: pvelab scripts)")
    chains = {vmid: chain for vmid in targets(vms)}
    print_chain(chain, f"{len(chains)} VM(s)")
    if run_and_snapshot(chains, timeout, pre_snap=True):
        sys.exit(1)
