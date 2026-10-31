"""pvelab's marked sections in /etc/network/interfaces.

A net made by pvelab is one block, "# lab.sh net N begin" .. "# lab.sh net N end". Inside it, each
feature that adds rules (networks.SETTERS) keeps its own section of post-up / post-down lines, so the rules come
back after a reboot and go away with the net.
"""

import re
import shlex
import subprocess

from .config import IFACES
from .util import die, run

def block_begin(n):
    return f"# lab.sh net {n} begin"


def block_end(n):
    return f"# lab.sh net {n} end\n"


def our_nets():
    """The nets pvelab made (a block each), in file order."""
    return re.findall(r"^# lab\.sh net (\S+) begin", open(IFACES).read(), re.M)


def bridge_is_ours(n):
    return re.search(rf"^{re.escape(block_begin(n))}", open(IFACES).read(), re.M) is not None


def add_net_block(n, body):
    """Append net n's block (its bridge stanza); feature sections go in later with write_section."""
    save_ifaces(open(IFACES).read() + f"\n{block_begin(n)} (managed by lab - removed automatically with its "
                f"last VM)\n{body}{block_end(n)}")


def remove_net_block(n):
    save_ifaces(re.sub(rf"\n?{re.escape(block_begin(n))}.*?{re.escape(block_end(n))}", "", open(IFACES).read(),
                       flags=re.S))


def section_marks(kind, n):
    """The lines that open and close net n's <kind> section inside its block."""
    return f"        # {kind} net {n} begin\n", f"        # {kind} net {n} end\n"


def section_re(kind, n):
    begin, end = section_marks(kind, n)
    return re.compile(rf"^{re.escape(begin)}.*?^{re.escape(end)}", re.M | re.S)


def has_section(kind, n):
    return section_re(kind, n).search(open(IFACES).read()) is not None


def save_ifaces(text):
    run("cp", "-a", IFACES, IFACES + ".lab-bak")
    with open(IFACES, "w") as f:
        f.write(text)


PLAIN_ADD = re.compile(r"^(\s+post-up\s+)iptables ((?:-t \S+ )?)(-[AI]) (.+)$", re.M)


def make_rules_add_once():
    """Before the whole file is applied again (ifreload -a, which every SDN apply runs on all nodes):
    rewrite its plain "post-up iptables -A/-I" lines (pvelab's older ones, or anyone's) into the
    add-once form iptables() writes - else each such rule would be added once more."""
    s = open(IFACES).read()
    new = PLAIN_ADD.sub(lambda m: m[1] + iptables([m[2] + m[4]], m[3])[0][0], s)
    if new != s:
        save_ifaces(new)


def iptables(rules, add="-A"):
    """Shell lines (up, down) that add and remove iptables rules, each "[-t TABLE] CHAIN MATCH...";
    add: -A (append) or -I (insert first). Adding is a no-op when the rule is there already, so
    applying the interfaces file again doesn't duplicate rules."""
    def line(rule, op):
        table, rest = rule[3:].split(" ", 1) if rule.startswith("-t ") else ("", rule)
        return f"iptables {'-t ' + table + ' ' if table else ''}{op} {rest}"
    return ([f"{line(r, '-C')} 2>/dev/null || {line(r, add)}" for r in rules],
            [line(r, "-D") for r in rules])


def post_lines(up, down):
    """An interfaces stanza's post-up / post-down lines."""
    return "".join(f"        post-up   {c}\n" for c in up) + "".join(f"        post-down {c}\n" for c in down)


def write_section(kind, n, up, down):
    """Replace net n's <kind> section: undo the old one's rules (its post-down lines), then write and
    run the new commands. up/down: shell command lines; no up = just remove the section."""
    s = open(IFACES).read()
    old = section_re(kind, n).search(s)
    if old:
        for line in old.group(0).splitlines():
            if line.strip().startswith("post-down"):
                subprocess.run(shlex.split(line.strip()[len("post-down"):]), stderr=subprocess.DEVNULL)
        s = s.replace(old.group(0), "")
    if up:
        end = block_end(n)
        if end not in s:                         # rules that no section holds could never be removed
            die(f"net{n} has no pvelab block in {IFACES} - not adding its {kind} rules")
        begin, close = section_marks(kind, n)
        s = s.replace(end, f"{begin}{post_lines(up, down)}{close}{end}")
    save_ifaces(s)
    if up:
        run("sh", "-ec", "\n".join(up))          # one shell for the section; stops at a failing line
