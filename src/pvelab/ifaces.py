"""pvelab's marked sections in /etc/network/interfaces.

A net made by pvelab is one block, "# lab.sh net N begin" .. "# lab.sh net N end". Inside it, each
feature that adds rules (networks.SETTERS) keeps its own section of post-up / post-down lines, so the rules come
back after a reboot and go away with the net.
"""

import re
import shlex
import subprocess

from . import config
from .nodes import this_node
from .util import die, run

def block_begin(n):
    return f"# lab.sh net {n} begin"


def block_end(n):
    return f"# lab.sh net {n} end\n"


_BEGIN = re.escape(block_begin("NET")).replace("NET", r"(?P<n>\S+)")
_END = re.escape(block_end("NET").strip()).replace("NET", r"(?P=n)")


def ifaces_text(text=None):
    """The interfaces file: text when the caller read it already, else read now."""
    if text is not None:
        return text
    with open(config.IFACES) as f:
        return f.read()


def our_nets(text=None):
    """The nets pvelab made (a block each), in file order. text: the interfaces file, if read already."""
    return re.findall(rf"^{_BEGIN}", ifaces_text(text), re.M)


def blocks(text):
    """{n: its whole block} of every net's block in text (the interfaces file)."""
    return {m["n"]: m.group(0) for m in re.finditer(rf"^{_BEGIN}.*?^{_END}", text, re.M | re.S)}


def bridge_is_ours(n):
    return re.search(rf"^{re.escape(block_begin(n))}", open(config.IFACES).read(), re.M) is not None


def gateway_elsewhere(n):
    """Where net n's gateway (and its isolation, ssh forwards) is when it is on another host, for a message:
    the host a take-over record names, else "another host"; None when it is here, or the net is no vnet in
    PVE's SDN config (bridge() names one a new net would get)."""
    if bridge_is_ours(n):
        return None
    if entry := config.taken_entry(n):       # a vnet it took over: on the host its record names (here: block lost)
        host = entry[1].get("host")
        return None if host == this_node() else host or "another host"
    return "another host" if config.net_bridge(n, True) in config.sdn_vnet_ids() else None     # pvelab's labN


def add_net_block(n, body):
    """Append net n's block (its bridge stanza); feature sections go in later with write_section."""
    save_ifaces(open(config.IFACES).read() + f"\n{block_begin(n)} (managed by lab - removed automatically with its "
                f"last VM)\n{body}{block_end(n)}")


def remove_net_block(n):
    save_ifaces(re.sub(rf"\n?{re.escape(block_begin(n))}.*?{re.escape(block_end(n))}", "", open(config.IFACES).read(),
                       flags=re.S))


def section_marks(kind, n):
    """The lines that open and close net n's <kind> section inside its block."""
    return f"        # {kind} net {n} begin\n", f"        # {kind} net {n} end\n"


def section_re(kind, n):
    """Net n's kind section (its begin and end marks and what is between them)."""
    begin, end = map(re.escape, section_marks(kind, n))
    return re.compile(rf"^{begin}.*?^{end}", re.M | re.S)


def any_section_re(kind):
    """Every net's kind section, its net number as group 1 (the marks of section_marks)."""
    begin, end = (re.escape(m) for m in section_marks(kind, "NUMBER"))
    begin, end = begin.replace("NUMBER", "([0-9]+)"), end.replace("NUMBER", r"\1")   # end: the same number
    return re.compile(f"^{begin}.*?^{end}", re.M | re.S)


def has_section(kind, n, text=None):
    """True if net n has a kind section (text: the interfaces file, if read already)."""
    return section_re(kind, n).search(ifaces_text(text)) is not None


def save_ifaces(text):
    run("cp", "-a", config.IFACES, config.IFACES + ".lab-bak")
    with open(config.IFACES, "w") as f:
        f.write(text)


PLAIN_ADD = re.compile(r"^(\s+post-up\s+)iptables ((?:-t \S+ )?)(-[AI]) (.+)$", re.M)
POST = re.compile(r"^(\s+post-(?:up|down)\s+.*)$", re.M)
WAITS = re.compile(r" (?:-w|--wait)(?:[ =]?\d+)?(?= )")     # a wait option of an iptables command


def make_rules_add_once():
    """Before the whole file is applied again (ifreload -a, which every SDN apply runs on all nodes):
    rewrite its plain "post-up iptables -A/-I" lines (pvelab's older ones, or anyone's) into the
    add-once form iptables() writes - else each such rule would be added once more; and in pvelab's
    blocks, give each iptables of a post-up / post-down line that does not wait -w, as iptables() does."""
    s = open(config.IFACES).read()
    new = PLAIN_ADD.sub(lambda m: m[1] + iptables([m[2] + m[4]], m[3])[0][0], s)
    for block in blocks(new).values():
        new = new.replace(block, POST.sub(lambda m: " || ".join(
            c if WAITS.search(c) else re.sub(r"(?:^|(?<=\s))iptables ", f"iptables {WAIT} ", c, count=1)
            for c in m[1].split(" || ")), block))
    if new != s:
        save_ifaces(new)


WAIT = "-w 10"         # each waits (up to 10 s) for the xtables lock another program holds, not failing at once


def split_table(rule):
    """(table, "CHAIN MATCH...") of rule "[-t TABLE ]CHAIN MATCH..." (no -t: filter)."""
    return tuple(rule[3:].split(" ", 1)) if rule.startswith("-t ") else ("filter", rule)


def rule_cmd(rule, op):
    """The iptables command line doing op (-A, -I, -C, -D) with rule "[-t TABLE ]CHAIN MATCH..."."""
    table, rest = split_table(rule)
    return f"iptables {WAIT} {'' if table == 'filter' else '-t ' + table + ' '}{op} {rest}"


def restore_lines(ops):
    """iptables-restore --noflush input doing ops [(rule "[-t TABLE ]CHAIN ...", op)] in order, each
    table in one commit (all or nothing)."""
    tables = {}
    for rule, op in ops:
        table, rest = split_table(rule)
        tables.setdefault(table, []).append(f"{op} {rest}")
    return "".join(f"*{t}\n" + "".join(f"{line}\n" for line in lines) + "COMMIT\n" for t, lines in tables.items())


def iptables(rules, add="-A"):
    """Shell lines (up, down) that add and remove iptables rules, each "[-t TABLE] CHAIN MATCH...";
    add: -A (append) or -I (insert first). Adding is a no-op when the rule is there already, so
    applying the interfaces file again doesn't duplicate rules."""
    return ([f"{rule_cmd(r, '-C')} 2>/dev/null || {rule_cmd(r, add)}" for r in rules],
            [rule_cmd(r, "-D") for r in rules])


def post_lines(up, down):
    """An interfaces stanza's post-up / post-down lines."""
    return "".join(f"        post-up   {c}\n" for c in up) + "".join(f"        post-down {c}\n" for c in down)


SECTION = re.compile(r"^\s+# \S+ net \S+ begin\n.*?^\s+# \S+ net \S+ end\n", re.M | re.S)
ADD = re.compile(r"(?:^|\|\| )iptables ((?:-t \S+ )?)(-[AI]) (.+)$")      # iptables()'s add, or a plain one (no wait option)


def own_rules(n):
    """Net n's own iptables rules [("[-t TABLE ]CHAIN MATCH...", "-A" or "-I")], in order: the post-up
    lines of its block outside its sections (expose, block, isolate) - as iptables() writes them, or
    older plain ones. [] when this host has no block for n."""
    block = SECTION.sub("", blocks(open(config.IFACES).read()).get(str(n), ""))
    found = (ADD.search(WAITS.sub("", c)) for c in re.findall(r"^\s*post-up\s+(.+)$", block, re.M))
    return [(m[1] + m[3], m[2]) for m in found if m]


def write_section(kind, n, up, down):
    """Replace net n's <kind> section: undo the old one's rules (its post-down lines), then write and
    run the new commands. up/down: shell command lines; no up = just remove the section."""
    s = open(config.IFACES).read()
    old = section_re(kind, n).search(s)
    if old:
        for line in old.group(0).splitlines():
            if line.strip().startswith("post-down"):
                subprocess.run(shlex.split(line.strip()[len("post-down"):]), stderr=subprocess.DEVNULL)
        s = s.replace(old.group(0), "")
    if up:
        end = block_end(n)
        if end not in s:                         # rules that no section holds could never be removed
            die(f"net{n} has no pvelab block in {config.IFACES} - not adding its {kind} rules")
        begin, close = section_marks(kind, n)
        s = s.replace(end, f"{begin}{post_lines(up, down)}{close}{end}")
    save_ifaces(s)
    if up:
        run("sh", "-ec", "\n".join(up))          # one shell for the section; stops at a failing line
