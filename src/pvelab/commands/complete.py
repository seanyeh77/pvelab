"""Tab completion for bash: `pvelab completion` prints the script, `pvelab __complete` answers it."""

import os
import subprocess
import sys

from ..catalogue import TEMPLATES
from ..config import BLOCK_KINDS

COMMANDS = ["create", "list", "start", "stop", "reboot", "destroy", "resize", "ssh", "rollback", "snapshot",
            "run", "expose", "block", "unblock", "top", "handout", "templates", "pull", "upgrade",
            "version", "completion", "help"]
VM_COMMANDS = {"start", "stop", "reboot", "destroy", "resize", "rollback", "snapshot", "run", "block",
               "unblock", "top", "handout"}
OPTIONS = {               # options per command; those taking a value are listed in VALUE_OPTIONS
    "create": ["--net", "--expose", "--host", "--script", "--template", "--cores", "--memory", "--disk"],
    "list": ["--ssh", "--net", "--passwords"],
    "destroy": ["--net", "--dry-run"],
    "resize": ["--net", "--cores", "--memory", "--disk", "--reboot", "--dry-run"],
    "rollback": ["--net", "--to", "--initial", "--dry-run"],
    "run": ["--net", "--timeout"],
    "expose": ["--net", "--host", "--off"],
    "top": ["--net", "--watch", "--check"],
    "handout": ["--net", "--output"],
}
for c in ("start", "stop", "reboot", "snapshot", "block", "unblock"):
    OPTIONS[c] = ["--net"]
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


def lab_data():
    """{"vms": [...], "nets": [...], "snaps": [...]} from /etc/pve (root only), else empty lists."""
    from ..inventory import managed, desc_value
    from ..lifecycle import snapshot_names
    try:
        man = managed()
    except OSError:
        return {"vms": [], "nets": [], "snaps": []}
    nets = sorted({desc_value(c, "net") for c in man.values()} - {"", "dhcp"}, key=int)
    snaps = sorted({s for i in man for s in snapshot_names(i)} - {"pending"})
    return {"vms": sorted(c.get("name", "") for c in man.values()), "nets": nets, "snaps": snaps}


def candidates(words, cword, data):
    """What may come at position cword (1 = the command) of `pvelab words...`."""
    cur = words[cword - 1] if len(words) >= cword else ""
    prev = words[cword - 2] if cword >= 2 else ""
    if prev == "=" and cword >= 3:           # bash splits --net=3 into --net, =, 3
        prev, cur = words[cword - 3], cur
    if cur == "=":
        prev, cur = prev, ""
    if cword == 1:
        out = COMMANDS
    else:
        cmd = words[0]
        if prev == "--net":
            out = data["nets"] + (["auto"] if cmd == "create" else []) + (["dhcp"] if cmd == "list" else [])
        elif prev == "--to":
            out = data["snaps"]
        elif prev in ("--template", "-t"):
            out = [a for t in TEMPLATES for a in t.aliases]
        elif prev in VALUE_OPTIONS:
            out = []                         # free text or a file: bash falls back to file names
        elif cmd == "help":
            out = COMMANDS if cword == 2 else []
        elif cmd == "pull":
            out = [a for t in TEMPLATES for a in t.aliases] + ["all"]
        elif cmd in ("block", "unblock") and cword == 2:
            out = [*BLOCK_KINDS, "all"]
        elif cmd == "create":
            out = OPTIONS["create"] + ([a for t in TEMPLATES for a in t.aliases] if cword == 3 else [])
        elif cmd == "ssh":
            out = data["vms"] if cword == 2 else []
        elif cmd in VM_COMMANDS:
            out = OPTIONS.get(cmd, []) + data["vms"] + ["all"]
        else:
            out = OPTIONS.get(cmd, [])
    return [w for w in out if w.startswith(cur)]


def cmd_complete(args):
    """pvelab __complete CWORD WORD...: one candidate per line. Never asks anything; a normal user
    gets VM names only if sudo works without a password."""
    if not args or not args[0].isdigit():
        return
    cword, words = int(args[0]), args[1:]
    if os.geteuid() != 0 and words[:1] and words[0] != "help":
        from ..util import package_root
        try:
            r = subprocess.run(["sudo", "-n", "env", f"PYTHONPATH={package_root()}", sys.executable, "-m",
                                "pvelab", "__complete", *args], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                print(r.stdout, end="")
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        data = {"vms": [], "nets": [], "snaps": []}
    else:
        data = lab_data() if os.geteuid() == 0 else {"vms": [], "nets": [], "snaps": []}
    for w in candidates(words, cword, data):
        print(w)
