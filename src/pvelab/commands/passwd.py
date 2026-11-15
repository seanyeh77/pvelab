"""pvelab passwd VM... [--password PW] [--keep-changed] [--dry-run] | pvelab passwd --check VM...

A student's password, new (given, or random), on all of that student's VMs - students may have changed
theirs in the VM, so each VM is looked at first:

- running, guest agent answers: chpasswd in the VM, now. PVE's cloud-init password (cipassword) of the
  current config stays as it was: a change there would make cloud-init set it again at the next boot,
  and undo a password the student chooses later. The snapshots get it: after a rollback the VM boots with
  the snapshot's config, cloud-init sets the new password then, and the handout is right again.
- stopped, or no guest agent: cipassword (config and snapshots): cloud-init sets it at the next start.

--check only looks: for each running VM, whether the password pvelab has still works there (perl's crypt
against the user's hash in /etc/shadow, in the VM), or the student changed it."""

import re

from ..args import opt
from ..credentials import cred_rows, cred_save, gen_password
from ..guest import guest_json
from ..helptext import bad_usage
from ..inventory import conf_path, targets, vm_statuses
from ..isolation import by_student, student_key
from ..lifecycle import with_snapshots
from ..nodes import node_of, on_node
from ..ui import each, paint, Plan, some
from ..util import die, LabError, parallel, run

CHECK = r'my $p = <STDIN>; chomp $p; my $h = (getpwnam($ARGV[0]))[1]; print !defined $h ? "nouser" : crypt($p, $h) eq $h ? "same" : "changed"'
STATES = {"same": "as pvelab has it", "changed": "changed in the VM (by its student?)",
          "nouser": "its user is not in the VM", None: "no answer (no guest agent?)", "stopped": "not running"}


def check_one(vmid, row, node, running):
    """same / changed / nouser / stopped, or None (no answer): whether pvelab's password still works there."""
    if not running:
        return "stopped"
    r = guest_json("exec", vmid, "--pass-stdin", 1, "--timeout", 20, "--", "perl", "-e", CHECK, row["user"],
                   data=(row["password"] + "\n").encode(), timeout=30, node=node)
    out = (r or {}).get("out-data", "").strip()
    return out if out in STATES else None


def checks(ids, rows):
    st, nodes = vm_statuses(ids), {i: node_of(i) for i in ids}
    return parallel(lambda i: check_one(i, rows[i], nodes[i], st[i] == "running"), ids, workers=8), nodes


def set_in_vm(vmid, user, pw, node):
    """chpasswd in the running VM; True if the agent says it worked."""
    r = guest_json("exec", vmid, "--pass-stdin", 1, "--timeout", 20, "--", "chpasswd",
                   data=f"{user}:{pw}\n".encode(), timeout=30, node=node)
    return bool(r) and r.get("exitcode") == 0


def set_cipassword(vmid, pw, node, current):
    """PVE's cloud-init password: in the snapshots, and (current) in the config itself. qm set stores it
    the way PVE does (hashed); that line is copied to the snapshots, and the config's own is put back
    unless current."""
    path = conf_path(vmid)
    before = open(path).read()
    run(*on_node(node, "qm", "set", vmid, "--cipassword", pw))
    after = open(path).read()
    head = after.split("\n[", 1)[0]
    line = re.search(r"(?m)^cipassword: (.*)$", head)
    if not line:
        return
    text = with_snapshots(after, "cipassword", line.group(1))
    if not current:                          # the config keeps its own: no cloud-init run at the next boot
        old = re.search(r"(?m)^cipassword: .*$", before.split("\n[", 1)[0])
        new_head = re.sub(r"(?m)^cipassword: .*$", old.group(0), head) if old else \
            re.sub(r"(?m)^cipassword: .*\n?", "", head)
        text = new_head + text[len(head):]
    with open(path, "w") as f:               # /etc/pve: in place (pmxcfs)
        f.write(text)


def cmd_passwd(args, dry=False):
    pw_opt, keep, check, rest = None, False, False, []
    it = iter(args)
    for a in it:
        if a == "--check":
            check = True
        elif a == "--keep-changed":
            keep = True
        elif (v := opt(a, it, "--password")) is not None:
            pw_opt = v
        else:
            rest.append(a)
    if pw_opt is not None and (not pw_opt or "\n" in pw_opt):
        bad_usage("--password: a password, on one line")
    rows = cred_rows()
    picked = [i for i in targets(rest) if i in rows]
    if not picked:
        die("no VM pvelab has a password of among those")
    names = {i: rows[i]["name"] for i in rows if rows[i]["net"] in {rows[p]["net"] for p in picked}}
    groups = by_student(names)               # the rest of each student's VMs: one password each
    ids = sorted({i for i in picked for i in groups[student_key(i, rows[i]["name"])]})
    state, nodes = checks(ids, rows)
    if check:
        for i in ids:
            print(f"{rows[i]['name']:<24} {rows[i]['user']:<12} {STATES[state[i]]}")
        changed = [rows[i]["name"] for i in ids if state[i] == "changed"]
        if changed:
            print(paint(f"\nchanged in the VM: {some(changed, 8)} - pvelab passwd sets a new one (it replaces "
                        "the student's), pvelab list --passwords shows the old", "yellow"))
        return
    students = {}
    for i in ids:
        students.setdefault(student_key(i, rows[i]["name"]), []).append(i)
    changed = [i for i in ids if state[i] == "changed"]
    left = {s for s, vms in students.items() if keep and any(v in changed for v in vms)}
    skip = {v for s in left for v in students[s]}
    plan = Plan()
    todo = [i for i in ids if i not in skip]
    plan.do(f"new password for {len(students) - len(left)} student(s), {len(todo)} VM(s), "
            + ("the one given" if pw_opt else "a random one each") + ": " + some([rows[i]["name"] for i in todo], 8))
    now = [i for i in todo if state[i] in ("same", "changed")]
    later = [i for i in todo if i not in now]
    if now:
        plan.do(f"set in the VM now (running): {len(now)} VM(s)")
    if later:
        plan.warn(f"not running or no guest agent: {some([rows[i]['name'] for i in later], 6)} - they take it at "
                  "their next start")
    if changed and not keep:
        plan.warn(f"the student changed the password of {some([rows[i]['name'] for i in changed], 6)}: it is "
                  "replaced (--keep-changed leaves those students alone)")
    if skip:
        plan.do(f"left alone (--keep-changed): {some([rows[i]['name'] for i in sorted(skip)], 6)}")
    if not todo:
        die("nothing to change")
    if not plan.go(dry):
        return
    new = {s: pw_opt or gen_password() for s in students}

    def one(i, board):
        pw = new[student_key(i, rows[i]["name"])]
        if i in now:
            board.stage(i, "in the VM")
            if not set_in_vm(i, rows[i]["user"], pw, nodes[i]):
                raise LabError("chpasswd in the VM failed (its user missing? see the VM)")
        board.stage(i, "PVE's cloud-init password")
        set_cipassword(i, pw, nodes[i], current=i not in now)
        rows[i]["password"] = pw
        return "set now" if i in now else "set at its next start"
    board = each(f"passwd: {len(todo)} VM(s)", todo, {i: rows[i]["name"] for i in todo}, one, workers=1)
    cred_save(rows)
    if board.counts()[0]:
        print(paint("new passwords: pvelab list --passwords; login cards: pvelab handout", "dim"))
