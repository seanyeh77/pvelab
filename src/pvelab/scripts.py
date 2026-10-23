"""Running instructor scripts in VMs as root through the guest agent."""

import os
import re
import shlex
import time
from collections import namedtuple

from .config import AGENT_WAIT, PRE, RUN_LOG_DIR, RUN_TIMEOUT_MIN, SNAP, SNAP_INITIAL
from .guest import agent_up, guest_json
from .inventory import vm_names, vm_statuses
from .lifecycle import take_snapshot
from .ui import Progress
from .util import die, parallel, warn


def script_snapshot_name(path):
    """setup.sh -> 'setup': the snapshot taken after the script ran (PVE: letter first, [A-Za-z0-9_-])."""
    n = re.sub(r"[^A-Za-z0-9_-]", "-", re.sub(r"\.[A-Za-z0-9]+$", "", os.path.basename(path))).strip("-")
    if not n[:1].isalpha():
        n = "s-" + n
    n = n[:40 - len(PRE)].rstrip("-")            # room for the "before-" snapshot
    if len(n) < 2 or n in ("current", SNAP, SNAP_INITIAL) or n.startswith(PRE):
        die(f"script name '{os.path.basename(path)}' gives snapshot name '{n}', which can't be used "
            "- rename the script")
    return n


Script = namedtuple("Script", "base data snap")     # snap: the snapshot taken after it ran


def check_script(path):
    """The instructor's script, checked before anything is changed: readable, at most 1 MiB, starts
    with #!, and its name gives a usable snapshot name."""
    try:
        data = open(path, "rb").read()
    except OSError as e:
        die(f"cannot read script {path}: {e.strerror}")
    if len(data) > 1 << 20:
        die(f"{path} is over 1 MiB (the guest agent limit) - put big files on a web server instead")
    if not data.startswith(b"#!"):
        die(f"{path} must start with a #! line (e.g. #!/bin/bash)")
    return Script(os.path.basename(path), data, script_snapshot_name(path))


def run_script(ids, script, names, timeout_min=RUN_TIMEOUT_MIN, pre_snap=False):
    """Run the script (check_script) as root in every VM at once; wait; log. Returns {vmid: exitcode
    or None}. pre_snap: first snapshot before-<name> on every VM it will run on."""
    data, snap, base = script.data, script.snap, script.base
    logdir = f"{RUN_LOG_DIR}/{time.strftime('%Y%m%d-%H%M%S')}-{snap}"
    os.makedirs(logdir, exist_ok=True)
    inner = (f"mkdir -p /root/.pvelab && cat > /root/.pvelab/{shlex.quote(base)} && "
             f"chmod 700 /root/.pvelab/{shlex.quote(base)} && cd /root && exec /root/.pvelab/{shlex.quote(base)}")
    print(f">> running {base} as root on {len(ids)} VM(s) (logs: {logdir})")
    started, result = {}, {}             # result: {vmid: exec-status} of the runs that finished
    # VMs that are still booting: wait for their guest agent (stopped VMs fail right away)
    running = {i for i, st in vm_statuses(ids).items() if st == "running"}
    for i in set(ids) - running:
        warn(f"{names[i]}: VM is not running - not run (start it first)")
    t0 = time.time()
    waiting = {i for i, up in parallel(agent_up, running).items() if not up}
    if waiting:
        print(f">> waiting for the guest agent on {len(waiting)} VM(s) (still booting?)")
    while waiting and time.time() - t0 < AGENT_WAIT:
        time.sleep(5)
        waiting = {i for i, up in parallel(agent_up, waiting).items() if not up}
    ready = [i for i in ids if i in running - waiting]
    for i in waiting:
        warn(f"{names[i]}: guest agent not answering after {AGENT_WAIT // 60} min - not run")
    if pre_snap and ready:                   # all taken before the script starts anywhere
        pre = PRE + snap
        Progress.each(f"snapshot '{pre}'", ready, names.get,
                      lambda i: take_snapshot(i, pre, f"pvelab: before {base}"))
    starts = parallel(lambda i: guest_json("exec", i, "--pass-stdin", 1, "--synchronous", 0, "--", "sh", "-c",
                                           inner, data=data), ready)
    for i in ready:
        r = starts[i]
        if r is None:
            warn(f"{names[i]}: guest agent not answering (VM stopped or still booting?) - not run")
        elif "exited" in r:
            result[i] = r
        else:
            started[i] = r.get("pid")
    t0, bar = time.time(), Progress(base, len(ready))

    def show():
        bad = sum(1 for r in result.values() if r.get("exitcode") != 0)
        bar.update(len(result) - bad, bad, [names[i] for i in started])
    show()
    while started and time.time() - t0 < timeout_min * 60:
        time.sleep(3)
        for i, r in parallel(lambda i: guest_json("exec-status", i, started[i], timeout=30), list(started)).items():
            if r and r.get("exited"):
                result[i] = r
                del started[i]
        show()
    bar.finish()
    for i in started:
        warn(f"{names[i]}: {base} still running after {timeout_min} min - counted as failed")
    codes = {}
    for i in ids:
        r = result.get(i)
        codes[i] = r.get("exitcode") if r else None
        with open(f"{logdir}/{names[i]}.log", "w") as f:
            f.write(f"# {base} on {names[i]} (VMID {i}): exit code {codes[i]}\n")
            if r:
                f.write(r.get("out-data", "") + ("\n[stdout truncated]\n" if r.get("out-truncated") else ""))
                if r.get("err-data"):
                    f.write("\n# stderr\n" + r["err-data"])
    good = [i for i in ids if codes[i] == 0]
    bad = [i for i in ids if codes[i] != 0]
    print(f">> {base}: {len(good)}/{len(ids)} OK" + (
        "; failed: " + " ".join(f"{names[i]}(exit {codes[i] if codes[i] is not None else '-'})" for i in bad)
        if bad else ""))
    return codes


def run_and_snapshot(ids, script, timeout_min=RUN_TIMEOUT_MIN, pre_snap=False):
    """run_script, then snapshot <script name> on the VMs where it succeeded."""
    snap, names = script.snap, vm_names(ids)
    codes = run_script(ids, script, names, timeout_min, pre_snap)
    ok_ids = [i for i in ids if codes[i] == 0]
    Progress.each(f"snapshot '{snap}'", ok_ids, names.get,
                  lambda i: take_snapshot(i, snap, f"pvelab: after {script.base}"))
    if ok_ids:
        print(f">> '{snap}' snapshot taken on {len(ok_ids)} VM(s) - back to it: pvelab rollback <VM> --to {snap}")
    if len(ok_ids) < len(ids):
        warn(f"no '{snap}' snapshot where {script.base} failed; see the logs, fix, "
             "and run it again there with: pvelab run <VM> <script>")
    if pre_snap and any(c is not None for c in codes.values()):
        print(f">> to undo {script.base}: pvelab rollback <VM> --to {PRE}{snap}")
    return codes
