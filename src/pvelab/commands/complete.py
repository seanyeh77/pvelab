"""Tab completion for bash: `pvelab completion` prints the script, `pvelab __complete` answers it."""

import os
import subprocess

from ..catalogue import TEMPLATES
from ..config import BLOCK_KINDS
from ..library import library
from ..util import number

COMMANDS = ["create", "list", "start", "stop", "reboot", "destroy", "resize", "ssh", "rollback", "snapshot",
            "run", "expose", "isolate", "block", "unblock", "top", "status", "handout", "templates", "pull",
            "save-template", "delete-template", "node", "nodes", "net", "nets", "zone", "zones", "move", "passwd", "rename", "owner", "adopt", "forward", "reach", "migrate", "upgrade", "scripts",
            "version", "completion", "help"]
VM_COMMANDS = {"start", "stop", "reboot", "destroy", "resize", "rollback", "snapshot", "run", "block",
               "unblock", "top", "handout", "move", "passwd"}
OPTIONS = {               # options per command; those taking a value are listed in VALUE_OPTIONS
    "create": ["--net", "--cidr", "--gateway", "--accounts", "--name", "--prefix", "--user", "--password", "--expose", "--no-isolate", "--no-check", "--shared", "--host", "--script", "--template", "--cores", "--memory",
               "--disk", "--profile", "--node", "--zone", "--nodes"],
    "isolate": ["--net", "--off"],
    "migrate": ["--net", "--dry-run"],
    "forward": ["--off", "--dry-run"],
    "reach": ["--off", "--dry-run"],
    "adopt": ["--shared", "--owner", "--user", "--password", "--ip", "--dry-run"],
    "list": ["--ssh", "--exposed", "--net", "--passwords", "--json"],
    "destroy": ["--net", "--dry-run"],
    "resize": ["--net", "--cores", "--memory", "--disk", "--reboot", "--dry-run"],
    "rollback": ["--net", "--to", "--initial", "--dry-run"],
    "run": ["--net", "--timeout"],
    "expose": ["--net", "--host", "--off"],
    "top": ["--net", "--watch", "--check"],
    "status": ["--net", "--since", "-f"],
    "handout": ["--net", "--output"],
    "move": ["--net", "--to", "--dry-run"],
    "scripts": [],
}
for c in ("start", "stop", "reboot", "snapshot", "block", "unblock"):
    OPTIONS[c] = ["--net"]
ALIASES = [a for t in TEMPLATES for a in t.aliases]
VALUE_OPTIONS = {"--net", "--cidr", "--gateway", "--accounts", "--name", "--prefix", "--user", "--password", "--host", "--script", "--template", "-t", "--cores", "--memory", "--disk", "--profile", "--to",
                 "--timeout", "--output", "-o", "--node", "--since", "--zone", "--nodes", "--peers", "--owner", "--ip"}

SCRIPT = """# bash completion for pvelab (pvelab completion > /etc/bash_completion.d/pvelab)
_pvelab() {
    local IFS=$'\\n'
    COMPREPLY=($(pvelab __complete "$COMP_CWORD" "${COMP_WORDS[@]:1}" 2>/dev/null))
    [ ${#COMPREPLY[@]} -eq 0 ] && compopt -o default 2>/dev/null   # nothing known: file names
}
complete -F _pvelab pvelab
"""


def cmd_completion():
    print(SCRIPT, end="")


def chain_candidates(cur, names=None):
    """Library names (or names) for the last part after a + or a comma: 'harden+d' -> 'harden+docker',
    'harden,d' -> 'harden,docker'."""
    i = max(cur.rfind("+"), cur.rfind(","))
    head, rest = cur[:i + 1], cur[i + 1:]
    return [head + n for n in (library() if names is None else names) if n.startswith(rest)]


def args_of(words):
    """The words that are no option nor an option's value (bash splits --owner=x into --owner, =, x)."""
    out, it = [], iter(words)
    for w in it:
        if w in VALUE_OPTIONS:
            if next(it, None) == "=":
                next(it, None)
        elif not w.startswith("-") and w != "=":
            out.append(w)
    return out


def candidates(words, cword, data):
    """What may come at position cword (1 = the command) of `pvelab words...`.
    data(key) gives "vms", "nets", "snaps", "nodes", "zones", "profiles" or "strays" (VMs adopt takes) - asked for only when
    needed (move --to: the hosts)."""
    cur = words[cword - 1] if len(words) >= cword else ""
    prev = words[cword - 2] if cword >= 2 else ""
    if prev == "=" and cword >= 3:           # bash splits --net=3 into --net, =, 3
        prev = words[cword - 3]
    if cur == "=":
        cur = ""
    if cword == 1:
        out = COMMANDS
    else:
        cmd = words[0]
        if prev == "--net":
            out = (data("all_nets") + ["auto"] if cmd == "create" else data("nets")) \
                + (["dhcp"] if cmd in ("list", "status") else [])
        elif prev == "--to":
            out = data("nodes") if cmd == "move" else data("snaps")
        elif prev == "--node":
            out = data("nodes") + ["auto"]
        elif prev == "--nodes":                  # a list: the host names (zone set: or all)
            out = data("nodes") + (["all"] if cmd in ("zone", "zones") else [])
        elif prev == "--zone":
            out = data("zones")
        elif prev == "--peers":
            out = ["auto"]
        elif prev in ("--template", "-t"):
            out = ALIASES
        elif prev == "--profile":            # one per VM of an owner: bench,,big
            out = chain_candidates(cur, data("profiles"))
        elif prev == "--script":
            out = chain_candidates(cur)      # nothing matches: bash falls back to file names
        elif prev in VALUE_OPTIONS:
            out = []                         # free text or a file: bash falls back to file names
        elif cmd == "help":
            out = COMMANDS if cword == 2 else []
        elif cmd in ("node", "nodes"):
            out = (["list", "add", "upgrade", "rename", "remove", "-v", "-j"] if cword == 2
                   else ["-v", "-j"] if words[1:2] == ["list"]
                   else data("nodes") + ["--major", "--minor", "--dry-run"] if words[1:2] == ["upgrade"]
                   else data("nodes") if cword == 3 and words[1] in ("rename", "remove")
                   else ["--dry-run"] if words[1] in ("rename", "remove") else [])
        elif cmd in ("net", "nets"):
            out = (["list", "create", "set", "rename", "remove", "check", "fix"] if cword == 2
                   else ["--cidr", "--gateway", "--zone", "--nodes", "--no-isolate"] if words[1:2] == ["create"] and cword > 3
                   else data("all_nets") if cword == 3 and words[1] in ("set", "rename", "remove", "fix")
                   else ["--cidr", "--gateway", "--dry-run"] if words[1:2] == ["set"]
                   else ["--dry-run"] + (["--rename-vms"] if words[1] == "rename" else [])
                   if words[1:2] in (["remove"], ["rename"]) else [])
        elif cmd in ("zone", "zones"):
            out = (["list", "set", "remove"] if cword == 2
                   else data("zones") if cword == 3 and words[1] in ("set", "remove")
                   else ["--nodes", "--peers", "--dry-run"] if words[1:2] == ["set"]
                   else ["--dry-run"] if words[1:2] == ["remove"] else [])
        elif cmd == "pull":
            out = ALIASES + ["all"]
        elif cmd in ("block", "unblock") and cword == 2:
            out = [*BLOCK_KINDS, "all"]
        elif cmd == "create":
            out = OPTIONS["create"] + (ALIASES if cword == 3 else [])
        elif cmd == "owner":
            out = data("vms")
        elif cmd == "adopt":
            out = ([] if args_of(words[1:cword - 1]) else data("strays")) + OPTIONS["adopt"]    # one VM, anywhere
        elif cmd == "reach":
            out = data("vms") if cword == 2 else OPTIONS["reach"]
        elif cmd == "forward":
            out = data("vms") if cword == 2 else OPTIONS["forward"] + ["icmp"]
        elif cmd in ("ssh", "save-template", "rename"):
            out = data("vms") if cword == 2 else ["--dry-run"] if cmd == "rename" else []
        elif cmd == "scripts":
            out = list(library()) if cword == 2 else []
        elif cmd in VM_COMMANDS:
            out = OPTIONS.get(cmd, []) + data("vms") + ["all"] + (chain_candidates(cur) if cmd == "run" else [])
        else:
            out = OPTIONS.get(cmd, [])
    return [w for w in out if w.startswith(cur)]


def cmd_complete(args):
    """pvelab __complete CWORD WORD...: one candidate per line. Never asks anything; a normal user
    gets VM names only if sudo works without a password."""
    if not args or (cword := number(args[0], 1, len(args))) is None:     # the word being completed
        return
    words = args[1:]
    wanted = set()
    found = candidates(words, cword, lambda k: wanted.add(k) or [])       # which data does it need?
    if wanted and os.geteuid() == 0:
        from ..inventory import lab_data
        found = candidates(words, cword, lab_data(snaps="snaps" in wanted, strays="strays" in wanted).get)
    elif wanted:                             # /etc/pve is root's: ask through sudo, never prompting
        from ..util import pvelab_argv
        try:
            r = subprocess.run(["sudo", "-n", *pvelab_argv("__complete", *args)], capture_output=True, text=True,
                               timeout=5)
            if r.returncode == 0:
                print(r.stdout, end="")
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    for w in found:
        print(w)
