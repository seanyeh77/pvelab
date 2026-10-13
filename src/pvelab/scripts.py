"""Running instructor scripts in VMs as root through the guest agent."""

import json
import os
import re
import shlex
import subprocess
import time

from .config import AGENT_WAIT, PRE, RUN_LOG_DIR, RUN_TIMEOUT_MIN, SNAP, SNAP_INITIAL
from .inventory import read_conf, vm_status
from .lifecycle import take_snapshot
from .util import die, warn


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


def load_script(path):
    try:
        data = open(path, "rb").read()
    except OSError as e:
        die(f"cannot read script {path}: {e.strerror}")
    if len(data) > 1 << 20:
        die(f"{path} is over 1 MiB (the guest agent limit) - put big files on a web server instead")
    if not data.startswith(b"#!"):
        die(f"{path} must start with a #! line (e.g. #!/bin/bash)")
    return data


def agent_up(vmid):
    try:
        return subprocess.run(["qm", "guest", "cmd", str(vmid), "ping"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def guest_json(*cmd, data=None, timeout=60):
    r = subprocess.run(["qm", "guest", *map(str, cmd)], input=data, capture_output=True, timeout=timeout)
    return json.loads(r.stdout.decode() or "{}") if r.returncode == 0 else None


def run_script(ids, path, timeout_min=RUN_TIMEOUT_MIN, pre_snap=False):
    """Run the script as root in every VM at once; wait; log. Returns {vmid: exitcode or None}.
    pre_snap: first snapshot before-<name> on every VM it will run on."""
    data, base = load_script(path), os.path.basename(path)
    names = {i: read_conf(i)["name"] for i in ids}
    logdir = f"{RUN_LOG_DIR}/{time.strftime('%Y%m%d-%H%M%S')}-{script_snapshot_name(path)}"
    os.makedirs(logdir, exist_ok=True)
    inner = (f"mkdir -p /root/.pvelab && cat > /root/.pvelab/{shlex.quote(base)} && "
             f"chmod 700 /root/.pvelab/{shlex.quote(base)} && cd /root && exec /root/.pvelab/{shlex.quote(base)}")
    print(f">> running {base} as root on {len(ids)} VM(s) (logs: {logdir})")
    started, result = {}, {}
    # VMs that are still booting: wait for their guest agent (stopped VMs fail right away)
    waiting = {i for i in ids if vm_status(i) == "running"}
    for i in set(ids) - waiting:
        warn(f"{names[i]}: VM is not running - not run (start it first)")
    t0 = time.time()
    while True:
        waiting = {i for i in waiting if not agent_up(i)}
        if not waiting or time.time() - t0 > AGENT_WAIT:
            break
        if time.time() - t0 < 4:
            print(f">> waiting for the guest agent on {len(waiting)} VM(s) (still booting?)")
        time.sleep(5)
    ready = [i for i in ids if vm_status(i) == "running" and i not in waiting]
    for i in set(ids) & waiting:
        warn(f"{names[i]}: guest agent not answering after {AGENT_WAIT // 60} min - not run")
    if pre_snap and ready:                   # all taken before the script starts anywhere
        pre = PRE + script_snapshot_name(path)
        print(f">> '{pre}' snapshot on {len(ready)} VM(s) first")
        for i in ready:
            take_snapshot(i, pre, f"pvelab: before {base}")
    for i in ids:
        if i not in ready:
            result[i] = None
            continue
        try:
            r = guest_json("exec", i, "--pass-stdin", 1, "--synchronous", 0, "--", "sh", "-c", inner, data=data)
        except (subprocess.TimeoutExpired, ValueError):
            r = None
        if r is None:
            result[i] = None
            warn(f"{names[i]}: guest agent not answering (VM stopped or still booting?) - not run")
        elif "exited" in r:
            result[i] = r
        else:
            started[i] = r.get("pid")
    t0 = time.time()
    while started and time.time() - t0 < timeout_min * 60:
        time.sleep(3)
        for i, pid in list(started.items()):
            try:
                r = guest_json("exec-status", i, pid, timeout=30)
            except (subprocess.TimeoutExpired, ValueError):
                continue
            if r and r.get("exited"):
                result[i] = r
                del started[i]
    for i in started:
        result[i] = None
        warn(f"{names[i]}: {base} still running after {timeout_min} min - counted as failed")
    codes = {}
    for i in ids:
        r = result.get(i)
        codes[i] = r.get("exitcode") if isinstance(r, dict) else None
        with open(f"{logdir}/{names[i]}.log", "w") as f:
            f.write(f"# {base} on {names[i]} (VMID {i}): exit code {codes[i]}\n")
            if isinstance(r, dict):
                f.write(r.get("out-data", "") + ("\n[stdout truncated]\n" if r.get("out-truncated") else ""))
                if r.get("err-data"):
                    f.write("\n# stderr\n" + r["err-data"])
    good = [i for i in ids if codes[i] == 0]
    bad = [i for i in ids if codes[i] != 0]
    print(f">> {base}: {len(good)}/{len(ids)} OK" + (
        "; failed: " + " ".join(f"{names[i]}(exit {codes[i] if codes[i] is not None else '-'})" for i in bad)
        if bad else ""))
    return codes


def run_and_snapshot(ids, path, timeout_min=RUN_TIMEOUT_MIN, pre_snap=False):
    """run_script, then snapshot <script name> on the VMs where it succeeded."""
    snap = script_snapshot_name(path)
    codes = run_script(ids, path, timeout_min, pre_snap)
    ok_ids = [i for i in ids if codes[i] == 0]
    for i in ok_ids:
        take_snapshot(i, snap, f"pvelab: after {os.path.basename(path)}")
    if ok_ids:
        print(f">> '{snap}' snapshot taken on {len(ok_ids)} VM(s) - back to it: pvelab rollback <VM> --to {snap}")
    if len(ok_ids) < len(ids):
        warn(f"no '{snap}' snapshot where {os.path.basename(path)} failed; see the logs, fix, "
             "and run it again there with: pvelab run <VM> <script>")
    if pre_snap and any(c is not None for c in codes.values()):
        print(f">> to undo {os.path.basename(path)}: pvelab rollback <VM> --to {PRE}{snap}")
    return codes
