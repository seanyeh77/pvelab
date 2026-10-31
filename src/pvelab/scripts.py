"""Running instructor scripts in VMs as root through the guest agent: chains of library scripts
(pvelab scripts) and files, e.g. harden+./setup.sh."""

import hashlib
import os
import re
import shlex
import time
from collections import namedtuple

from .config import AGENT_WAIT, PRE, RUN_LOG_DIR, RUN_TIMEOUT_MIN, SNAP, SNAP_INITIAL
from .guest import agent_up, guest_json
from .helptext import bad_usage
from .inventory import vm_names, vm_statuses
from .library import library
from .lifecycle import take_snapshot
from .ui import Progress
from .util import die, parallel, warn

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


def wait_ready(ids, names):
    """The VMs of ids that are running and whose guest agent answers (waiting up to AGENT_WAIT for
    VMs that are still booting), in ids' order. Warns about the others."""
    running = {i for i, st in vm_statuses(ids).items() if st == "running"}
    for i in [i for i in ids if i not in running]:
        warn(f"{names[i]}: VM is not running - not run (start it first)")
    t0 = time.time()
    waiting = {i for i, up in parallel(agent_up, running).items() if not up}
    if waiting:
        print(f">> waiting for the guest agent on {len(waiting)} VM(s) (still booting?)")
    while waiting and time.time() - t0 < AGENT_WAIT:
        time.sleep(5)
        waiting = {i for i, up in parallel(agent_up, waiting).items() if not up}
    for i in waiting:
        warn(f"{names[i]}: guest agent not answering after {AGENT_WAIT // 60} min - not run")
    return [i for i in ids if i in running - waiting]


def start_and_wait(scripts, names, timeout_min, logdir, step):
    """One step of the chains: scripts[vmid] runs as root in every VM at once; wait up to timeout_min;
    log to <logdir>/<vm>/<step+1>-<name>.log. Returns {vmid: exitcode, or None if it never finished}."""
    ids = list(scripts)

    def start(i):
        base = shlex.quote(scripts[i].base)
        inner = (f"mkdir -p /root/.pvelab && cat > /root/.pvelab/{base} && "
                 f"chmod 700 /root/.pvelab/{base} && cd /root && exec /root/.pvelab/{base}")
        return guest_json("exec", i, "--pass-stdin", 1, "--synchronous", 0, "--", "sh", "-c", inner,
                          data=scripts[i].data)
    started, result = {}, {}             # result: {vmid: exec-status} of the runs that finished
    for i, r in parallel(start, ids).items():
        if r is None:
            warn(f"{names[i]}: guest agent not answering (VM stopped or still booting?) - not run")
        elif "exited" in r:
            result[i] = r
        else:
            started[i] = r.get("pid")
    bases = {s.base for s in scripts.values()}
    t0, bar = time.time(), Progress(bases.pop() if len(bases) == 1 else f"step {step + 1}", len(ids))

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
        warn(f"{names[i]}: {scripts[i].base} still running after {timeout_min} min - counted as failed")
    codes = {}
    for i in ids:
        r, s = result.get(i), scripts[i]
        codes[i] = r.get("exitcode") if r else None
        os.makedirs(f"{logdir}/{names[i]}", exist_ok=True)
        with open(f"{logdir}/{names[i]}/{step + 1}-{s.name}.log", "w") as f:
            f.write(f"# {s.base} on {names[i]} (VMID {i}): exit code {codes[i]}\n")
            if r:
                f.write(r.get("out-data", "") + ("\n[stdout truncated]\n" if r.get("out-truncated") else ""))
                if r.get("err-data"):
                    f.write("\n# stderr\n" + r["err-data"])
    return codes


def by_name(ids, snap_of):
    """{snapshot name: [vmids]}, so snapshots can be taken (and shown) one name at a time."""
    groups = {}
    for i in ids:
        groups.setdefault(snap_of[i], []).append(i)
    return groups


def run_chains(chains, snap_of, names, timeout_min=RUN_TIMEOUT_MIN, pre_snap=False):
    """Run each VM's chain ({vmid: (Script, ...)}, none empty) as root: the k-th scripts of all VMs
    at once, step after step. A failed script (exit not 0, timeout, no agent) skips the rest of that
    VM's chain. pre_snap: first snapshot before-<snap_of[vmid]> on every VM that will run.
    Returns ({vmid: None if the whole chain succeeded, else (failed Script, exitcode or None)},
    the VMs that ran)."""
    ids = list(chains)
    ready = wait_ready(ids, names)
    if pre_snap:                             # all taken before any script starts anywhere
        for snap, group in by_name(ready, snap_of).items():
            Progress.each(f"snapshot '{PRE}{snap}'", group, names.get,
                          lambda i: take_snapshot(i, PRE + snap, f"pvelab: before {chain_text(chains[i])}"))
    tag = snap_of[ids[0]] if ids and len(set(snap_of.values())) == 1 else "scripts"
    logdir = f"{RUN_LOG_DIR}/{time.strftime('%Y%m%d-%H%M%S')}-{tag}"
    os.makedirs(logdir, exist_ok=True)
    print(f">> running scripts as root on {len(ready)} VM(s) (logs: {logdir})")
    result = {i: (chains[i][0], None) for i in ids if i not in ready}     # never run
    ok = list(ready)
    for k in range(max((len(c) for c in chains.values()), default=0)):
        step = {i: chains[i][k] for i in ok if k < len(chains[i])}
        if not step:
            break
        for i, code in start_and_wait(step, names, timeout_min, logdir, k).items():
            if code != 0:
                result[i] = (step[i], code)
                ok.remove(i)
    result.update({i: None for i in ok})
    return {i: result[i] for i in ids}, ready


def run_and_snapshot(chains, timeout_min=RUN_TIMEOUT_MIN, pre_snap=False):
    """run_chains, then snapshot <chain name> on the VMs where the whole chain succeeded.
    Returns {vmid: (failed Script, exitcode or None)} of the VMs where it did not."""
    ids = list(chains)
    names = vm_names(ids)
    snap_of = {i: chain_snapshot_name(chains[i]) for i in ids}
    result, ready = run_chains(chains, snap_of, names, timeout_min, pre_snap)
    ok_ids = [i for i in ids if result[i] is None]
    for snap, group in by_name(ok_ids, snap_of).items():
        Progress.each(f"snapshot '{snap}'", group, names.get,
                      lambda i: take_snapshot(i, snap, f"pvelab: after {chain_text(chains[i])}"))
        print(f">> '{snap}' snapshot taken on {len(group)} VM(s) - back to it: pvelab rollback <VM> --to {snap}")
    failed = {i: r for i, r in result.items() if r is not None}
    print(f">> scripts: {len(ok_ids)}/{len(ids)} OK" + (
        "; failed: " + " ".join(f"{names[i]} ({s.name}, exit {c if c is not None else '-'})"
                                for i, (s, c) in failed.items()) if failed else ""))
    if failed:
        texts = sorted({chain_text(chains[i]) for i in failed})
        warn("no snapshot where a script failed; see the logs, fix, and run it again there with: "
             + "  or  ".join(f"pvelab run <VM> {t}" for t in texts))
    if pre_snap:                             # before-<name> is on every VM that ran
        for snap in by_name(ready, snap_of):
            print(f">> to undo {snap}: pvelab rollback <VM> --to {PRE}{snap}")
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
            "add --script harden (only 'student' may ssh in, passwords need 10+ characters)"]
