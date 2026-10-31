"""Running instructor scripts in VMs as root through the guest agent: chains of library scripts
(pvelab scripts) and files, e.g. harden+./setup.sh."""

import hashlib
import os
import re
import shlex
import time
from collections import namedtuple

from .config import AGENT_WAIT, PRE, RUN_LOG_DIR, RUN_TIMEOUT_MIN, SNAP, SNAP_INITIAL
from .guest import agent_out, agent_up, guest_json, last_line
from .helptext import bad_usage
from .inventory import vm_names, vm_statuses
from .library import library
from .lifecycle import take_snapshot
from .nodes import board_hosts, node_of
from .ui import Board
from .util import die, parallel

class Script(namedtuple("Script", "text base data name source")):
    """text: the entry as typed (harden, ./setup.sh); base: the file name in the VM; name: its part
    of the snapshot name; source: 'library' or 'file'."""
    __slots__ = ()

    @property
    def label(self):
        """What the output shows, e.g. 'harden (library)' or './setup.sh (file)'."""
        return f"{self.text} ({self.source})"


def snap_stem(path):
    """setup.sh -> 'setup': a file's part of the snapshot name (PVE: letter first, [A-Za-z0-9_-])."""
    n = re.sub(r"[^A-Za-z0-9_-]", "-", re.sub(r"\.[A-Za-z0-9]+$", "", os.path.basename(path))).strip("-")
    return n if n[:1].isalpha() else "s-" + n


def chain_snapshot_name(chain):
    """harden+docker -> 'harden-docker': the snapshot taken after the whole chain ran. A long name is
    cut and ends in a hash of the full name, so it fits with 'before-' in PVE's 40 characters and the
    same chain always gives the same name."""
    n = "-".join(s.name for s in chain)
    if len(PRE + n) > 40:
        n = n[:33 - len(PRE)].rstrip("-") + "-" + hashlib.sha1(n.encode()).hexdigest()[:6]
    if len(n) < 2 or n in ("current", SNAP, SNAP_INITIAL) or n.startswith(PRE):
        die(f"script(s) {' + '.join(s.label for s in chain)} give snapshot name '{n}', which can't be used "
            "- rename the script")
    return n


def check_bytes(what, data):
    """The checks every script gets before anything is changed: at most 1 MiB, starts with #!."""
    if len(data) > 1 << 20:
        die(f"{what} is over 1 MiB (the guest agent limit) - put big files on a web server instead")
    if not data.startswith(b"#!"):
        die(f"{what} must start with a #! line (e.g. #!/bin/bash)")


def check_script(path):
    """The instructor's script file, checked: readable, at most 1 MiB, starts with #!."""
    try:
        data = open(os.path.expanduser(path), "rb").read()
    except OSError as e:
        die(f"cannot read script {path}: {e.strerror}")
    check_bytes(path, data)
    return Script(path, os.path.basename(path), data, snap_stem(path), "file")


def resolve_entry(entry):
    """One entry of a chain: an existing file wins, else a library script, else an error."""
    if os.path.isfile(os.path.expanduser(entry)):
        return check_script(entry)
    lib = library()
    if entry in lib:
        data = lib[entry][1]
        check_bytes(f"library script {entry}", data)
        return Script(entry, f"{entry}.sh", data, entry, "library")
    die(f"unknown script '{entry}': not a file, not in the library ({', '.join(lib) or 'empty'}) "
        "- see: pvelab scripts")


def parse_chain(text):
    """'harden+./setup.sh' -> (Script, Script); '' -> (). A whole text that is a file is one entry
    (so a path with + in it still works). The chain's snapshot name is checked too."""
    text = (text or "").strip()
    if not text:
        return ()
    if os.path.isfile(os.path.expanduser(text)):
        parts = [text]
    else:
        parts = [p.strip() for p in text.split("+")]
        if not all(parts):
            bad_usage(f"'{text}': an empty script name in the chain (join scripts with one +)")
    chain = tuple(resolve_entry(p) for p in parts)
    chain_snapshot_name(chain)
    return chain


def chain_text(chain):
    """The chain as it is typed: harden+./setup.sh."""
    return "+".join(s.text for s in chain)


def print_chain(chain, where):
    """'>> scripts for VM -1: harden (library) + ./x.sh (file)': the source is always shown."""
    print(f">> scripts for {where}: {' + '.join(s.label for s in chain)}")


def wait_ready(ids, board):
    """The VMs of ids that are running and whose guest agent answers (waiting up to AGENT_WAIT for
    VMs that are still booting), in ids' order. The others are marked failed on the board."""
    running = {i for i, st in vm_statuses(ids).items() if st == "running"}
    for i in ids:
        if i in running:
            board.stage(i, "waiting for agent")
        else:
            board.fail(i, "VM is not running - not run (start it first)")
    t0, nodes = time.time(), {i: node_of(i) for i in running}     # each VM's node, looked up once

    def up(i):
        return agent_up(i, node=nodes[i])
    waiting = {i for i, ok in parallel(up, running).items() if not ok}
    while waiting and time.time() - t0 < AGENT_WAIT:
        time.sleep(5)
        waiting = {i for i, ok in parallel(up, waiting).items() if not ok}
    for i in waiting:
        board.fail(i, f"guest agent not answering after {AGENT_WAIT // 60} min - not run")
    return [i for i in ids if i in running - waiting]


EXITED = "@pvelab-exited"           # printed by the poll once the script's process is gone


def out_file(script):
    return f"/root/.pvelab/{script.base}.out"


def start_and_wait(scripts, board, timeout_min, logdir, step, steps):
    """One step of the chains: scripts[vmid] runs as root in every VM at once; its output also goes to
    a file in the VM, whose newest line the board shows while it runs. Waits up to timeout_min; logs to
    <logdir>/<vm>/<step+1>-<name>.log. steps: {vmid: length of its chain}. Returns {vmid: exitcode, or
    None if it never finished}."""
    ids = list(scripts)
    names = {i: board.rows[i]["name"] for i in ids}
    nodes = {i: node_of(i) for i in ids}     # each VM's node, looked up once (not on every poll)

    def start(i):
        base, out = shlex.quote(scripts[i].base), shlex.quote(out_file(scripts[i]))
        inner = (f"mkdir -p /root/.pvelab && cat > /root/.pvelab/{base} && chmod 700 /root/.pvelab/{base} && "
                 f"cd /root && /root/.pvelab/{base} > {out} 2>&1; rc=$?; cat {out}; exit $rc")
        return guest_json("exec", i, "--pass-stdin", 1, "--synchronous", 0, "--", "sh", "-c", inner,
                          data=scripts[i].data, node=nodes[i])
    for i in ids:
        board.stage(i, f"script {step + 1}/{steps[i]} {scripts[i].name}", live=True)
    started, result = {}, {}             # result: {vmid: exec-status} of the runs that finished
    for i, r in parallel(start, ids).items():
        if r is None:
            result[i] = None
        elif "exited" in r:
            result[i] = r
        else:
            started[i] = r.get("pid")
    t0 = time.time()

    def poll(i):
        """One agent call while it runs: the newest line of its output, and whether its process is gone
        (or a zombie). Only then exec-status, for its exit code and output: (exec-status or None, line)."""
        pid, out = started[i], shlex.quote(out_file(scripts[i]))
        check = (f"tail -n 5 {out} 2>/dev/null; [ -d /proc/{pid} ] && ! grep -qs '^State:.*Z' /proc/{pid}/status "
                 f"|| printf '\\n{EXITED}\\n'")
        text = agent_out(i, "sh", "-c", check, wait=5, timeout=20, node=nodes[i])
        if text is not None and text.endswith(f"\n{EXITED}\n"):
            return guest_json("exec-status", i, pid, timeout=30, node=nodes[i]), ""
        return None, last_line(text)
    while started and time.time() - t0 < timeout_min * 60:
        time.sleep(3)
        for i, (r, line) in parallel(poll, list(started)).items():
            if r and r.get("exited"):
                result[i] = r
                del started[i]
            elif line:
                board.tail(i, line)
    codes = {}
    for i in ids:
        r, s = result.get(i), scripts[i]
        codes[i] = r.get("exitcode") if r else None
        os.makedirs(f"{logdir}/{names[i]}", exist_ok=True)
        path = f"{logdir}/{names[i]}/{step + 1}-{s.name}.log"
        with open(path, "w") as f:
            f.write(f"# {s.base} on {names[i]} (VMID {i}): exit code {codes[i]}\n")
            if r:
                f.write(r.get("out-data", "") + ("\n[output truncated]\n" if r.get("out-truncated") else ""))
                if r.get("err-data"):
                    f.write("\n# stderr\n" + r["err-data"])
        if codes[i] != 0:
            why = (f"still running after {timeout_min} min" if i in started
                   else "guest agent not answering" if r is None else f"exit {codes[i]}")
            board.fail(i, f"{s.name}: {why}   log: {path}")
    return codes


def by_name(ids, snap_of):
    """{snapshot name: [vmids]}, so snapshots can be taken (and shown) one name at a time."""
    groups = {}
    for i in ids:
        groups.setdefault(snap_of[i], []).append(i)
    return groups


def snapshot_all(ids, board, snap, desc):
    """Snapshot snap (desc(vmid) describes it) on every VM of ids, a few at a time, shown on the board."""
    def one(i):
        board.stage(i, f"snapshot {snap}")
        take_snapshot(i, snap, desc(i))
    parallel(one, ids, workers=4)


def run_chains(chains, snap_of, board, timeout_min=RUN_TIMEOUT_MIN, pre_snap=False):
    """Run each VM's chain ({vmid: (Script, ...)}, none empty) as root: the k-th scripts of all VMs
    at once, step after step. A failed script (exit not 0, timeout, no agent) skips the rest of that
    VM's chain. pre_snap: first snapshot before-<snap_of[vmid]> on every VM that will run.
    Returns ({vmid: None if the whole chain succeeded, else (failed Script, exitcode or None)},
    the VMs that ran, the log directory)."""
    ids = list(chains)
    ready = wait_ready(ids, board)
    if pre_snap:                             # all taken before any script starts anywhere
        for snap, group in by_name(ready, snap_of).items():
            snapshot_all(group, board, PRE + snap, lambda i: f"pvelab: before {chain_text(chains[i])}")
    tag = snap_of[ids[0]] if ids and len(set(snap_of.values())) == 1 else "scripts"
    logdir = f"{RUN_LOG_DIR}/{time.strftime('%Y%m%d-%H%M%S')}-{tag}"
    os.makedirs(logdir, exist_ok=True)
    result = {i: (chains[i][0], None) for i in ids if i not in ready}     # never run
    ok = list(ready)
    steps = {i: len(chains[i]) for i in ids}
    for k in range(max((len(c) for c in chains.values()), default=0)):
        step = {i: chains[i][k] for i in ok if k < len(chains[i])}
        if not step:
            break
        for i, code in start_and_wait(step, board, timeout_min, logdir, k, steps).items():
            if code != 0:
                result[i] = (step[i], code)
                ok.remove(i)
    result.update({i: None for i in ok})
    return {i: result[i] for i in ids}, ready, logdir


def run_and_snapshot(chains, timeout_min=RUN_TIMEOUT_MIN, pre_snap=False, board=None):
    """run_chains, then snapshot <chain name> on the VMs where the whole chain succeeded, all on a live
    board (board: one that is on screen already, as in create; else one is made here).
    Returns {vmid: (failed Script, exitcode or None)} of the VMs where it did not."""
    ids = list(chains)
    names = vm_names(ids)
    snap_of = {i: chain_snapshot_name(chains[i]) for i in ids}
    if board is None:
        title = "scripts: " + " / ".join(sorted({chain_text(c) for c in chains.values()}))
        with Board(title, names, board_hosts(ids)) as b:
            return finish_chains(chains, snap_of, names, b, timeout_min, pre_snap)
    return finish_chains(chains, snap_of, names, board, timeout_min, pre_snap)


def finish_chains(chains, snap_of, names, board, timeout_min, pre_snap):
    """The work of run_and_snapshot; the hints are printed after the board."""
    ids = list(chains)
    result, ready, logdir = run_chains(chains, snap_of, board, timeout_min, pre_snap)
    ok_ids = [i for i in ids if result[i] is None]
    for snap, group in by_name(ok_ids, snap_of).items():
        snapshot_all(group, board, snap, lambda i: f"pvelab: after {chain_text(chains[i])}")
    for i in ok_ids:
        board.ok(i, f"done (snapshot '{snap_of[i]}')")
    failed = {i: r for i, r in result.items() if r is not None}
    board.after.append((f"scripts: {len(ok_ids)}/{len(ids)} OK; logs: {logdir}", "dim"))
    for snap in by_name(ok_ids, snap_of):
        board.after.append((f"back to it: pvelab rollback <VM> --to {snap}", "dim"))
    if failed:
        texts = sorted({chain_text(chains[i]) for i in failed})
        board.after.append(("failed: " + " ".join(f"{names[i]} ({s.name}, exit {c if c is not None else '-'})"
                                                  for i, (s, c) in failed.items()), "red"))
        board.after.append(("no snapshot where a script failed; see the logs, fix, and run it again there with: "
                            + "  or  ".join(f"pvelab run <VM> {t}" for t in texts), "red"))
    if pre_snap:                             # before-<name> is on every VM that ran
        for snap in by_name(ready, snap_of):
            board.after.append((f"to undo {snap}: pvelab rollback <VM> --to {PRE}{snap}", "dim"))
    return failed


def expose_warning(chains, per):
    """create --expose: warning lines if some of a student's VMs (chains[k] is VM -k+1's chain) won't
    run the library's harden, else []."""
    def hardened(chain):
        return any(s.source == "library" and s.name == "harden" for s in chain)
    bare = [k for k in range(per) if not hardened(chains[k])]
    if not bare:
        return []
    who = "all VMs" if len(bare) == per else "VM " + ", ".join(f"-{k + 1}" for k in bare)
    return [f"--expose without the harden script ({who}): accounts students make with weak passwords "
            "could be logged into over ssh from outside",
            "add --script harden (passwords need 10+ characters)"]
