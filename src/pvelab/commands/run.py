"""pvelab run"""

import sys

from ..args import opt
from ..config import RUN_TIMEOUT_MIN
from ..helptext import bad_usage
from ..inventory import targets
from ..scripts import load_script, run_and_snapshot, script_snapshot_name


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
    *vms, path = rest
    load_script(path)
    script_snapshot_name(path)
    codes = run_and_snapshot(targets(vms), path, timeout, pre_snap=True)
    if any(c != 0 for c in codes.values()):
        sys.exit(1)
