"""pvelab passwd VM... [--password PW] [--keep-changed] [--dry-run] | pvelab passwd --check VM...

An owner's password, new (given, or random), on all of that owner's VMs - users may have changed
theirs in the VM, so each VM is looked at first:

- running, guest agent answers: chpasswd in the VM, now. PVE's cloud-init password (cipassword) of the
  current config stays as it was: a change there would make cloud-init set it again at the next boot,
  and undo a password the user chooses later. The snapshots get it: after a rollback the VM boots with
  the snapshot's config, cloud-init sets the new password then, and the handout is right again.
- stopped, or no guest agent: cipassword (config and snapshots): cloud-init sets it at the next start.

--check only looks: for each running VM, whether the password pvelab has still works there (perl's crypt
against the user's hash in /etc/shadow, in the VM), or the user changed it."""

import re

from ..args import opt
from .. import accounts
from ..credentials import cred_rows, cred_save, gen_password
from ..guest import agent_out, guest_json
from ..helptext import bad_usage
from ..inventory import conf_path, login_of, targets, vm_statuses
from ..owners import confs_of, groups_of, is_shared_vm
from ..lifecycle import with_snapshots
from ..nodes import node_of, on_node
from ..ui import each, paint, Plan, some
from ..util import die, LabError, parallel, run

CHECK = r'my $p = <STDIN>; chomp $p; my $h = (getpwnam($ARGV[0]))[1]; print !defined $h ? "nouser" : crypt($p, $h) eq $h ? "same" : "changed"'
STATES = {"same": "as pvelab has it", "changed": "changed in the VM (by its user?)",
          "nouser": "its user is not in the VM", None: "no answer (no guest agent?)", "stopped": "not running"}


def check_one(vmid, row, conf, node, running):
    """same / changed / nouser / stopped, or None (no answer): whether pvelab's password still works there."""
    if not running:
        return "stopped"
    out = agent_out(vmid, "perl", "-e", CHECK, login_of(vmid, row, conf), node=node, wait=20, timeout=30,
                    data=(row["password"] + "\n").encode())
    out = (out or "").strip()
    return out if out in STATES else None


def set_in_vm(vmid, user, pw, node):
    """chpasswd in the running VM: None when it worked, else why not."""
    r = guest_json("exec", vmid, "--pass-stdin", 1, "--timeout", 20, "--", "chpasswd",
                   data=f"{user}:{pw}\n".encode(), timeout=30, node=node)
    if r is None:
        return "no answer from its guest agent"
    if r.get("exitcode") != 0:
        return (r.get("err-data") or r.get("out-data") or "chpasswd failed").strip().splitlines()[-1]
    return None


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
    if pw_opt is not None and not accounts.password_ok(pw_opt):
        bad_usage("--password: a password, without tabs or line breaks")
    rows = cred_rows()
    picked = [i for i in targets(rest) if i in rows]
    on_nets = {rows[p]["net"] for p in picked}
    confs = confs_of({i: r for i, r in rows.items() if r["net"] in on_nets})     # each read once
    shared = [i for i in picked if is_shared_vm(confs[i], rows[i]["net"])]
    if shared and len(shared) < len(picked):     # its password is the admin's: only when named alone
        picked = [i for i in picked if i not in shared]
        print("left out (a shared VM; on its own: pvelab passwd " + " ".join(rows[i]["name"] for i in shared) + ")")
    if not picked:
        die("no VM pvelab has a password of among those")
    groups = groups_of({i: rows[i]["name"] for i in confs}, confs)
    key = {i: g for g, ids in groups.items() for i in ids}
    sel = {key[i]: groups[key[i]] for i in picked}           # each picked VM's owner: all their VMs
    ids = sorted(i for vms in sel.values() for i in vms)
    st, nodes = vm_statuses(ids), {i: confs[i].get("_node") or node_of(i) for i in ids}
    state = parallel(lambda i: check_one(i, rows[i], confs[i], nodes[i], st[i] == "running"), ids, workers=8)
    if check:
        for i in ids:
            print(f"{rows[i]['name']:<24} {login_of(i, rows[i], confs[i]):<12} {STATES[state[i]]}")
        changed = [rows[i]["name"] for i in ids if state[i] == "changed"]
        if changed:
            print(paint(f"\nchanged in the VM: {some(changed, 8)} - pvelab passwd sets a new one (it replaces "
                        "the user's), pvelab list --passwords shows the old", "yellow"))
        return
    kept = {s for s, vms in sel.items() if keep and any(state[v] == "changed" for v in vms)}
    todo = [i for s, vms in sorted(sel.items()) if s not in kept for i in vms]
    if not todo:
        die("nothing to change")
    now = {i for i in todo if state[i] in ("same", "changed")}
    later = [i for i in todo if i not in now]
    changed = [i for i in todo if state[i] == "changed"]
    plan = Plan()
    plan.do(f"new password for {len(sel) - len(kept)} owner(s), {len(todo)} VM(s), "
            + ("the one given" if pw_opt else "a random one each") + ": " + some([rows[i]["name"] for i in todo], 8))
    if now:
        plan.do(f"set in the VM now (running): {len(now)} VM(s)")
    if kept:
        plan.do(f"left alone (--keep-changed): {some([owner for _, owner in sorted(kept)], 6)}")
    for line in accounts.short_warnings({"--password": pw_opt}):
        plan.warn(line)
    if later:
        plan.warn(f"not running or no guest agent: {some([rows[i]['name'] for i in later], 6)} - they take it at "
                  "their next start; whether their user changed it can't be seen" + (" (--keep-changed can't "
                  "leave them alone)" if keep else ""))
    if changed:
        plan.warn(f"the user changed the password of {some([rows[i]['name'] for i in changed], 6)}: it is "
                  "replaced (--keep-changed leaves those owners alone)")
    if not plan.go(dry):
        return
    pw_of = {i: p for s, vms in sel.items() if s not in kept for p in [pw_opt or gen_password()] for i in vms}

    def one(i, board):
        if i in now:
            board.stage(i, "in the VM")
            if why := set_in_vm(i, login_of(i, rows[i], confs[i]), pw_of[i], nodes[i]):
                raise LabError(f"chpasswd in the VM: {why}")
        board.stage(i, "PVE's cloud-init password")
        set_cipassword(i, pw_of[i], nodes[i], current=i not in now)
        rows[i]["password"] = pw_of[i]
        return "set now" if i in now else "set at its next start"
    board = each(f"passwd: {len(todo)} VM(s)", todo, {i: rows[i]["name"] for i in todo}, one, workers=8)
    cred_save(rows)
    if board.counts()[0]:
        print(paint("new passwords: pvelab list --passwords; login cards: pvelab handout", "dim"))
