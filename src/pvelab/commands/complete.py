"""Tab completion for bash: `pvelab completion` prints the script, `pvelab __complete` answers it."""

import os
import subprocess

from ..catalogue import TEMPLATES
from ..config import BLOCK_KINDS
from ..library import library

COMMANDS = ["create", "list", "start", "stop", "reboot", "destroy", "resize", "ssh", "rollback", "snapshot",
            "run", "expose", "isolate", "block", "unblock", "top", "handout", "templates", "pull", "save-template",
            "delete-template", "upgrade", "scripts",
            "version", "completion", "help"]
VM_COMMANDS = {"start", "stop", "reboot", "destroy", "resize", "rollback", "snapshot", "run", "block",
               "unblock", "top", "handout"}
OPTIONS = {               # options per command; those taking a value are listed in VALUE_OPTIONS
    "create": ["--net", "--expose", "--no-isolate", "--host", "--script", "--template", "--cores", "--memory",
               "--disk"],
    "isolate": ["--net", "--off"],
    "list": ["--ssh", "--net", "--passwords"],
    "destroy": ["--net", "--dry-run"],
    "resize": ["--net", "--cores", "--memory", "--disk", "--reboot", "--dry-run"],
    "rollback": ["--net", "--to", "--initial", "--dry-run"],
    "run": ["--net", "--timeout"],
    "expose": ["--net", "--host", "--off"],
    "top": ["--net", "--watch", "--check"],
    "handout": ["--net", "--output"],
    "scripts": [],
}
for c in ("start", "stop", "reboot", "snapshot", "block", "unblock"):
    OPTIONS[c] = ["--net"]
ALIASES = [a for t in TEMPLATES for a in t.aliases]
VALUE_OPTIONS = {"--net", "--host", "--script", "--template", "-t", "--cores", "--memory", "--disk", "--to",
                 "--timeout", "--output", "-o"}

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


def chain_candidates(cur):
    """Library names for the last part after a + or a comma: 'harden+d' -> 'harden+docker',
    'harden,d' -> 'harden,docker'."""
    i = max(cur.rfind("+"), cur.rfind(","))
    head, rest = cur[:i + 1], cur[i + 1:]
    return [head + n for n in library() if n.startswith(rest)]


def candidates(words, cword, data):
    """What may come at position cword (1 = the command) of `pvelab words...`.
    data(key) gives "vms", "nets" or "snaps" - asked for only when needed."""
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
            out = data("nets") + (["auto"] if cmd == "create" else []) + (["dhcp"] if cmd == "list" else [])
        elif prev == "--to":
            out = data("snaps")
        elif prev in ("--template", "-t"):
            out = ALIASES
        elif prev == "--script":
            out = chain_candidates(cur)      # nothing matches: bash falls back to file names
        elif prev in VALUE_OPTIONS:
            out = []                         # free text or a file: bash falls back to file names
        elif cmd == "help":
            out = COMMANDS if cword == 2 else []
        elif cmd == "pull":
            out = ALIASES + ["all"]
        elif cmd in ("block", "unblock") and cword == 2:
            out = [*BLOCK_KINDS, "all"]
        elif cmd == "create":
            out = OPTIONS["create"] + (ALIASES if cword == 3 else [])
        elif cmd in ("ssh", "save-template"):
            out = data("vms") if cword == 2 else []
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
    if not args or not args[0].isdigit():
        return
    cword, words = int(args[0]), args[1:]
    wanted = set()
    found = candidates(words, cword, lambda k: wanted.add(k) or [])       # which data does it need?
    if wanted and os.geteuid() == 0:
        from ..inventory import lab_data
        found = candidates(words, cword, lab_data(snaps="snaps" in wanted).get)
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
