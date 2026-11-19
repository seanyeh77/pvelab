"""pvelab rollback / snapshot"""

from ..args import opt
from ..config import SNAP, SNAP_INITIAL
from ..inventory import read_conf, snapshot_names, targets, vm_names
from ..nodes import qm_argv
from ..legacy import strip_legacy
from ..lifecycle import has_snapshot, take_snapshot, wait_first_boot
from ..ui import dry_run, each, paint, some
from ..util import confirm_yes, die, LabError, run


def clean_legacy(vmid):
    """A VM with a record that a rollback gave its old managed-by line or pvelab tags (strip_legacy) again:
    those taken off; an admin's notes and tags stay. A VM not migrated yet keeps them (pvelab migrate)."""
    conf = read_conf(vmid) or {}
    if "_rec" not in conf:
        return
    desc, tags = conf.get("description", ""), conf.get("tags", "")
    new_desc, new_tags = strip_legacy(desc, tags)
    if (new_desc, new_tags) == (desc, tags):
        return
    args = (["--description", new_desc] if new_desc else []) + (["--tags", new_tags] if new_tags else [])
    gone = [k for k, old, new in (("description", desc, new_desc), ("tags", tags, new_tags)) if old and not new]
    if gone:
        args += ["--delete", ",".join(gone)]
    run(*qm_argv(vmid, "set", vmid, *args))


def cmd_rollback(args, dry=False):
    snap, rest = SNAP, []
    it = iter(args)
    for a in it:
        if a == "--initial":
            snap = SNAP_INITIAL
        elif (v := opt(a, it, "--to")) is not None:
            snap = v
        else:
            rest.append(a)
    ids = targets(rest)
    names = vm_names(ids)
    missing = [names[i] for i in ids if not has_snapshot(i, snap)]
    if missing:
        have = sorted({n for i in ids for n in snapshot_names(i)})
        die(f"no '{snap}' snapshot on: {some(missing, 10, ' ')}"
            + (f" (snapshots there: {', '.join(have)})" if have else " (no snapshots there; take one with 'pvelab snapshot')"))
    if dry:
        return dry_run(f"roll back to '{snap}' (everything done since is lost) and start",
                       [f"{names[i]} ({i})" for i in ids])
    confirm_yes(f"This will roll back {len(ids)} VM(s) to '{snap}' and start them - everything done since is lost:\n  "
                + some([f"{names[i]}({i})" for i in ids], 12, " "))

    started = {}                             # --initial: {vmid: a note}, ended once its first boot is

    def roll(i, board):
        board.stage(i, f"rollback {snap}")
        run(*qm_argv(i, "rollback", i, snap, "--start", "1"))
        note = ""
        try:                                 # a snapshot from before its record: the old line and tags again
            clean_legacy(i)
        except LabError as e:
            note = f"; the snapshot's managed-by line and tags not removed ({e}): pvelab migrate removes them"
        if snap == SNAP_INITIAL:             # before its first boot: ready once cloud-init has set it up again
            started[i] = note
            return None
        return f"back at '{snap}', started{note}"

    def first_boots(board):
        """All at once (create's wait): a command right after (a rename's reboot) finds them set up."""
        done = wait_first_boot(sorted(started), board)      # the ones not done: failed there
        for i, note in started.items():
            if i in done and done[i].startswith("done"):
                board.ok(i, f"back at '{snap}', first boot done{note}")
            elif i in done:
                board.fail(i, f"back at '{snap}', cloud-init ended with '{done[i]}'{note}")
    return each(f"rollback to '{snap}': {len(ids)} VM(s)", ids, names, roll,
                after=first_boots if snap == SNAP_INITIAL else None)


def cmd_snapshot(args):
    ids = targets(args)

    def snap(i, board):
        board.stage(i, f"snapshot {SNAP}")
        take_snapshot(i, SNAP, "pvelab: taken by 'pvelab snapshot'")
        return f"'{SNAP}' = current state"
    each(f"snapshot '{SNAP}': {len(ids)} VM(s)", ids, vm_names(ids), snap)
    print(paint(f"back to it later: pvelab rollback <VM>   (rolls back to '{SNAP}')", "dim"))
