"""pvelab node upgrade [HOST...] [--major] [--dry-run]: bring PVE on the cluster's hosts to the newest
version, one host at a time, reboots included. The steps (also in `pvelab help node`):
  1. check every host, read-only: online and quorate, free space, apt-get update, what would be
     upgraded; with --major also PVE's checker (pveXtoY --full) and that the new release is out
  2. the plan, with every warning, and one question
  3. one host after another (this host last): who is logged in to its students' VMs (asks if anyone
     is), with --major the apt sources moved to the new suites (old ones kept as *.pvelab-bak),
     apt-get update and dist-upgrade, a reboot when needed, and a wait until it is back
A host already current is skipped, so a rerun goes on where the last one stopped. When this host
reboots, pvelab ends with it: it says how to go on first."""

import re
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from .config import BACK_SECS, YES
from . import guest
from .inventory import did_you_mean, inventory, managed, vm_names, vm_statuses
from .nodes import (cluster_ok, drop_master, here_first, in_cluster, is_local, is_online, members, offline_nodes,
                    parse_pveversion)
from .nodes import shell as sh       # tests replace sh and drop_master
from . import ui
from .ui import Board, check_line, dry_run, Ok, paint, Plan, Spinner, step
from .util import LabError, die, http_open, wait_for

DEBIAN = ["buster", "bullseye", "bookworm", "trixie", "forky", "duke"]   # in release order
DEBIAN_URI = "http://deb.debian.org/debian"
MIN_ROOT_GB = {False: 2, True: 5}             # free on / for an update, for a major upgrade
MIN_BOOT_MB = 200                             # free on /boot (a new kernel and its initrd)
UPDATE_SECS = 600                             # apt-get update
UPGRADE_SECS = 3600                           # apt-get dist-upgrade (a major one takes a while)
CHECKER_SECS = 600                            # pveXtoY --full
BAK = ".pvelab-bak"
SOURCES = ("/etc/apt/sources.list", "/etc/apt/sources.list.d/*.list", "/etc/apt/sources.list.d/*.sources")
APT = "LC_ALL=C DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none apt-get -q"
UPGRADE = (f"{APT} -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade")
KERNEL = re.compile(r"^(proxmox-kernel-|pve-kernel-|linux-image-)\d")     # not proxmox-kernel-helper
UNIT = "pvelab-upgrade"                       # dist-upgrade runs in a systemd unit of its own on the host,
HOST_LOG = "/var/log/pvelab-upgrade.log"      # its output in this file: not a child of pvelab, so a
# dropped ssh session, Ctrl-C or the timeout only stop the watching, never apt itself
START_UPGRADE = (f"systemctl -q is-active {UNIT} && {{ echo 'E: an upgrade is still running there "
                 f"(systemctl status {UNIT})'; exit 1; }}; : > {HOST_LOG} && systemd-run --quiet --collect "
                 f"--unit={UNIT} sh -c " + shlex.quote(f"{UPGRADE} >>{HOST_LOG} 2>&1; echo ==pvelab-rc=$? >>{HOST_LOG}"))
FOLLOW_UPGRADE = (f"tail -n +1 -f {HOST_LOG} & t=$!; while systemctl -q is-active {UNIT}; do sleep 2; done; "
                  f"sleep 1; kill $t")


@lru_cache(maxsize=None)
def fetch_release(uri, suite):
    """The head of URI/dists/SUITE/Release (its fields), read from this host; None if it is not there."""
    try:
        with http_open(f"{uri.rstrip('/')}/dists/{suite}/Release", timeout=30) as r:
            return r.read(16384).decode(errors="replace")
    except (OSError, ValueError):
        return None


# --- parsers (pure) ---

def major(version):
    return int(version.split(".")[0]) if version else None


def parse_facts(text):
    """The output of FACTS -> {"version", "codename", "root_kb", "boot_kb", "reboot", "boot_id"}."""
    rows = [l.split() for l in (text or "").splitlines()]
    df = [r for r in rows if len(r) >= 6 and r[-1].startswith("/") and r[3].isdigit()]
    field = lambda k: next((l[len(k) + 1:].strip() for l in (text or "").splitlines() if l.startswith(k + "=")), "")
    return {"version": parse_pveversion(text), "codename": field("codename") or None,
            "root_kb": int(df[0][3]) if df else None, "boot_kb": int(df[1][3]) if len(df) > 1 else None,
            "reboot": "reboot-required" in (text or "").split(), "boot_id": field("boot") or None}


FACTS = ("pveversion; . /etc/os-release; echo codename=$VERSION_CODENAME; df -Pk / /boot; "
         "[ -e /run/reboot-required ] && echo reboot-required; echo boot=$(cat /proc/sys/kernel/random/boot_id)")


def parse_simulation(text):
    """`apt-get -s dist-upgrade` -> [(package, new version)] of what it would install or upgrade."""
    return re.findall(r"^Inst (\S+)(?: \[[^\]]*\])? \((\S+)", text or "", re.M)


def parse_checker(text):
    """pveXtoY's summary -> the number of FAILURES, or None (no summary)."""
    m = re.search(r"^FAILURES:\s*(\d+)", ui.ANSI.sub("", text or ""), re.M)
    return int(m.group(1)) if m else None


def apt_error(text):
    """The last E: (or Err:) line of apt's output, for a short reason."""
    lines = [l.strip() for l in (text or "").splitlines() if l.startswith(("E:", "Err:"))]
    return lines[-1] if lines else ""


def apt_fail(label, rc, out, fallback=""):
    """'LABEL failed (RC): <apt's last error>' - or fallback when apt named none (nothing: no reason)."""
    why = apt_error(out) or fallback
    return f"{label} failed ({rc})" + (f": {why}" if why else "")


def next_codename(codename):
    """The Debian release after codename, or None (unknown: then Debian's testing, if it is newer)."""
    if codename in DEBIAN[:-1]:
        return DEBIAN[DEBIAN.index(codename) + 1]
    testing = release_field(fetch_release(DEBIAN_URI, "testing"), "Codename")
    return testing if testing and testing != codename else None


def release_field(text, name):
    m = re.search(rf"^{name}:[ \t]*(.*)$", text or "", re.M)
    return m.group(1).strip() if m else ""


def parse_sources_dump(text):
    """The output of DUMP_SOURCES -> {path: content}, each content as in its file (without the newline
    DUMP_SOURCES adds after it)."""
    out, path = {}, None
    for line in (text or "").splitlines(keepends=True):
        if line.startswith("==pvelab== "):
            path = line[11:].strip()
            out[path] = ""
        elif path:
            out[path] += line
    return {p: t[:-1] if t.endswith("\n") else t for p, t in out.items()}


# a newline after each file: one without a last newline would glue the next marker to its last line
DUMP_SOURCES = (f"for f in {' '.join(SOURCES)}; do [ -f \"$f\" ] && {{ echo \"==pvelab== $f\"; cat \"$f\"; echo; }}; "
                f"done; true")


def new_suite(word, old, new):
    """trixie -> forky, trixie-updates -> forky-updates, ...; any other suite stays."""
    if word == old or word.startswith(old + "-"):
        return new + word[len(old):]
    return word


def list_entry(line):
    """A one-line source ('deb [opts] URI SUITE COMP...') -> (tokens, index of SUITE), or None."""
    t = line.split()
    if not t or t[0] not in ("deb", "deb-src"):
        return None
    i = 1
    if len(t) > 1 and t[1].startswith("["):
        while i < len(t) and not t[i].endswith("]"):
            i += 1
        i += 1
    return (t, i + 1) if i + 1 < len(t) else None


def stanzas(text):
    """deb822 text -> its stanzas, each a list of lines (blank lines between them kept apart as None)."""
    out, cur = [], []
    for line in text.splitlines(keepends=True):
        if line.strip():
            cur.append(line)
        else:
            if cur:
                out.append(cur)
                cur = []
            out.append([line])
    if cur:
        out.append(cur)
    return out


def fields(lines):
    return {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip()
            for l in lines if ":" in l and not l[0].isspace() and not l.startswith("#")}


def enabled(f):
    """A deb822 stanza's fields -> is it enabled? Any of apt's false words disables it (PVE writes 'false')."""
    return f.get("enabled", "yes").lower() not in ("no", "false", "without", "off", "disable", "0")


def rewrite(path, text, old, new):
    """text of source file path with every enabled entry moved from Debian release old to new."""
    if path.endswith(".sources"):
        out = []
        for st in stanzas(text):
            f = fields(st)
            if "suites" in f and enabled(f):
                st = [re.sub(r"^(Suites:[ \t]*)(.*)$",
                             lambda m: m.group(1) + " ".join(new_suite(w, old, new) for w in m.group(2).split()),
                             l, flags=re.I) if l.lower().startswith("suites:") else l for l in st]
            out += st
        return "".join(out)
    out = []
    for line in text.splitlines(keepends=True):
        e = list_entry(line)
        if e and new_suite(e[0][e[1]], old, new) != e[0][e[1]]:
            t, i = e
            line = " ".join(t[:i] + [new_suite(t[i], old, new)] + t[i + 1:]) + "\n"
        out.append(line)
    return "".join(out)


def entries(path, text):
    """The enabled entries of a source file: [(URIs, suites, components)]."""
    if path.endswith(".sources"):
        return [(f.get("uris", "").split(), f.get("suites", "").split(), f.get("components", "").split())
                for f in map(fields, stanzas(text)) if "suites" in f and enabled(f)]
    return [([e[0][e[1] - 1]], [e[0][e[1]]], e[0][e[1] + 1:]) for e in map(list_entry, text.splitlines()) if e]


def unpublished(sources, codename):
    """Why the new suites are not usable yet (the first problem), or None: every enabled entry's
    Release file must be there with its components, and Debian's codename must be stable, not testing."""
    for path, text in sorted(sources.items()):
        for uris, suites, comps in entries(path, text):
            for uri in uris:
                for suite in suites:
                    rel = fetch_release(uri, suite)
                    if rel is None:
                        return f"{uri} has no {suite} yet"
                    if suite == codename and release_field(rel, "Suite") in ("testing", "unstable", "experimental"):
                        return f"Debian {codename} is not released yet (still {release_field(rel, 'Suite')})"
                    have = {c.split("/")[-1] for c in release_field(rel, "Components").split()}
                    missing = [c for c in comps if have and c not in have]
                    if missing:
                        return f"{uri} {suite} has no {' '.join(missing)} yet"
    return None


# --- step 1: the checks, read-only ---

def gb(kb):
    return f"{kb / 1048576:.1f} GB"


def check_cluster(job):
    off = offline_nodes()
    if off:
        return f"offline: {', '.join(off)} (every host must be up)"
    if not cluster_ok():
        return "the cluster is not quorate (pvecm status)"
    return Ok(f"{len(job['all'])} host(s) online" + (", cluster quorate" if in_cluster() else ""))


def check_facts(job, h):
    rc, out = sh(h["name"], FACTS)
    h.update(parse_facts(out))
    h["before"] = h["after"] = h["version"]
    if not h["version"]:
        return "pveversion did not answer"
    need = MIN_ROOT_GB[job["major"]]
    if h["root_kb"] is None or h["root_kb"] < need * 1048576:
        return f"only {gb(h['root_kb'] or 0)} free on / (needs {need} GB): apt-get clean, old kernels"
    if h["boot_kb"] is not None and h["boot_kb"] < MIN_BOOT_MB * 1024:
        return f"only {h['boot_kb'] // 1024} MB free on /boot (needs {MIN_BOOT_MB} MB): remove old kernels"
    return Ok(f"{h['name']}: PVE {h['version']} on Debian {h['codename']}, {gb(h['root_kb'])} free on /")


def check_update(job, h):
    rc, out = sh(h["name"], f"{APT} update", UPDATE_SECS)
    if rc:
        return apt_fail("apt-get update", rc, out, "see the log")
    return Ok(f"{h['name']}: apt-get update works")


def check_packages(job, h):
    rc, out = sh(h["name"], f"{APT} -s dist-upgrade", 300)
    if rc:
        return apt_fail("apt-get -s dist-upgrade", rc, out, "see the log")
    h["pkgs"] = parse_simulation(out)
    h["kernel"] = any(KERNEL.match(p) for p, _ in h["pkgs"])
    return Ok(f"{h['name']}: {what(h)}")


def what(h):
    """'12 packages to upgrade (a new kernel, pve-manager 9.2.21)', or 'up to date'."""
    if not h["pkgs"]:
        return "up to date"
    pm = [v for p, v in h["pkgs"] if p == "pve-manager"]
    extra = (["a new kernel"] if h["kernel"] else []) + ([f"pve-manager {pm[0]}"] if pm else [])
    return f"{len(h['pkgs'])} package(s) to upgrade" + (f" ({', '.join(extra)})" if extra else "")


def check_checker(job, h):
    tool = f"pve{job['target'] - 1}to{job['target']}"
    rc, out = sh(h["name"], f"command -v {tool} >/dev/null || {{ echo 'pvelab: no {tool}'; exit 127; }}; "
                            f"{tool} --full", CHECKER_SECS)
    if f"pvelab: no {tool}" in out:
        return (f"no {tool} on {h['name']}: PVE {job['target']} is not out yet (or {h['name']} needs the newest "
                f"PVE {job['target'] - 1}.x first: pvelab node upgrade)")
    fails = parse_checker(out)
    if fails is None:
        return f"{tool} --full did not finish ({rc}): see the log"
    if fails:
        return f"{tool} --full reports {fails} FAILURE(S): fix them as it says (its output is in the log)"
    return Ok(f"{h['name']}: {tool} --full, no failures")


def check_published(job, h):
    new = next_codename(h["codename"])
    if not new:
        return f"no Debian release after {h['codename']} is known yet"
    h["new_codename"] = new
    rc, out = sh(h["name"], DUMP_SOURCES)
    moved = {p: rewrite(p, t, h["codename"], new) for p, t in parse_sources_dump(out).items()}
    if not any(new in t for t in moved.values()):
        return f"no enabled apt source on {h['name']} uses {h['codename']}"
    why = unpublished(moved, new)
    return why or Ok(f"{h['name']}: PVE {job['target']} and Debian {new} are published")


def run_checks(job):
    """Every check on a spinner line (✔ or ✘); [(label, problem)] of those that failed. Each step runs on
    every host at once (apt and the checker are slow), then shows each host's line in order."""
    failed = []

    def one(label, fn, *a):
        problem = check_line(label, fn, *a)
        if problem:
            failed.append((label, problem))
        return not problem

    def each(what, fn, hosts):
        """fn(job, h) on every host of hosts at once; the hosts whose check passed."""
        passed = []
        with ThreadPoolExecutor(max(len(hosts), 1)) as pool:
            running = [pool.submit(fn, job, h) for h in hosts]
            for h, r in zip(hosts, running):     # a line per host, in order: it waits for that host's check
                if one(f"{h['name']}: {what}", r.result):
                    passed.append(h)
        return passed
    one("hosts online, cluster quorate", check_cluster, job)
    hosts = [job["hosts"][n] for n in job["order"]]
    each("version, free space", check_facts, hosts)
    if job["major"]:
        majors = {major(h["version"]) for h in job["hosts"].values() if h["version"]}
        if majors:                           # after a stop half-way some hosts are on the new one already
            job["target"] = max(majors) if len(majors) > 1 else max(majors) + 1
    updated = each("apt-get update", check_update, [h for h in hosts if h["version"]])
    each("what would be upgraded", check_packages, updated)
    if job["major"]:
        old = [h for h in updated if major(h["version"]) < job["target"]]
        checked = each(f"pve{job['target'] - 1}to{job['target']} --full", check_checker, old)
        each(f"PVE {job['target']} published", check_published, checked)
    return failed


# --- step 3: one host ---

def logged_in(node):
    """{VM name: [users]} of node's running pvelab VMs with someone logged in (the guest agent's `who`),
    and how many running VMs did not answer."""
    ids = [i for i, c in managed(inventory()).items() if c.get("_node") == node]
    running = [i for i, st in vm_statuses(ids).items() if st == "running"]
    found, silent = guest.logged_in(running, vm_names(running))
    return found, len(silent)


def students(h):
    """Who is logged in to the host's students' VMs: a warning and a question (default no) if anyone is."""
    with Spinner(f"{h['name']}: who is logged in to its students' VMs") as line:
        found, silent = logged_in(h["name"])
        line.ok(f"{h['name']}: " + (f"{len(found)} VM(s) with someone logged in" if found else "nobody logged in"))
    if silent:
        print(paint(f"    note: {silent} running VM(s) gave no answer (no guest agent): can't tell who is on them", "dim"))
    if found:
        plan = Plan()
        for name, users in sorted(found.items()):
            plan.warn(f"{name}: {', '.join(users)} logged in (the upgrade may restart or reboot it)")
        plan.ask()


def move_sources(h, board):
    """--major: every enabled apt source from the old Debian release to the new one, each changed file
    backed up first next to it (*.pvelab-bak)."""
    old, new = h["codename"], h["new_codename"]
    rc, out = sh(h["name"], DUMP_SOURCES)
    if rc:
        die(f"can't read the apt sources ({rc})")
    for path, text in sorted(parse_sources_dump(out).items()):
        moved = rewrite(path, text, old, new)
        if moved == text:
            continue
        q = shlex.quote(path)
        rc, _ = sh(h["name"], f"cp -p {q} {q}{BAK} && cat > {q}.pvelab-new && mv {q}.pvelab-new {q}", data=moved)
        if rc:
            die(f"could not rewrite {path} ({rc})")
        board.tail(h["name"], f"{path}: {old} -> {new} (old one: {path}{BAK})")


def dist_upgrade(h, board):
    """apt-get dist-upgrade, started in its own systemd unit on the host (UNIT); the board follows its log
    until the unit ends. If pvelab stops watching (timeout, Ctrl-C, a dropped session) the upgrade goes on."""
    name = h["name"]
    board.stage(name, "dist-upgrade", live=True)
    rc, out = sh(name, START_UPGRADE)
    if rc:
        die(apt_fail("dist-upgrade", rc, out))
    show = lambda line: None if line.startswith("==pvelab-rc=") else board.tail(name, line)
    rc, out = sh(name, FOLLOW_UPGRADE, UPGRADE_SECS, show=show)
    if rc == 124:
        die(f"dist-upgrade still runs after {UPGRADE_SECS // 60} min: pvelab stopped watching, the upgrade goes "
            f"on (on {name}: systemctl status {UNIT}; its output: {HOST_LOG})")
    codes = re.findall(r"^==pvelab-rc=(\d+)", out, re.M)
    if not codes or codes[-1] != "0":
        die(apt_fail("dist-upgrade", codes[-1] if codes else "no exit code", out, f"see {HOST_LOG} on {name}"))


def back(h):
    """The rebooted host is up again: online, the cluster quorate, a new boot, pveversion as after the upgrade."""
    if not is_online(h["name"]):
        return False
    if not cluster_ok():
        return False
    rc, out = sh(h["name"], FACTS, 20)
    if rc:
        drop_master(h["name"])               # a connection from before the reboot may hang
        return False
    f = parse_facts(out)
    return f["boot_id"] not in (None, h["boot_id"]) and f["version"] == h["after"]


def upgrade_host(job, h, k, n):
    """Step 3 for one host. Sets h["step"] (the step running) and h["state"]."""
    name = h["name"]
    h["step"] = "students"
    students(h)
    with Board(f"[3/3] {k}/{n} {name}", {name: name}) as board:
        try:
            if job["major"]:
                h["step"] = "sources"
                board.stage(name, "apt sources")
                move_sources(h, board)
            h["step"] = "update"
            board.stage(name, "apt-get update", live=True)
            rc, out = sh(name, f"{APT} update", UPDATE_SECS, show=lambda line: board.tail(name, line))
            if rc:
                die(apt_fail("apt-get update", "timed out" if rc == 124 else rc, out))
            h["step"] = "upgrade"
            dist_upgrade(h, board)
            rc, out = sh(name, f"dpkg --audit; echo ==facts; {FACTS}")
            if rc or "==facts" not in out:
                die(f"can't read {name}'s packages after the upgrade ({rc})")
            audit, _, facts = out.partition("==facts\n")
            f = parse_facts(facts)
            if audit.strip():                # dpkg --audit names half-installed packages
                die(f"dpkg reports half-installed packages: {audit.strip().splitlines()[0]}")
            h["after"] = f["version"]
            if job["major"] and major(f["version"]) != job["target"]:
                die(f"pveversion shows {f['version']}, not PVE {job['target']}")
            h["state"] = f"upgraded {h['before']} -> {h['after']}"
            if not (job["major"] or h["kernel"] or f["reboot"]):
                board.ok(name, f"{h['before']} -> {h['after']}, no reboot needed")
                return
            if is_local(name):
                h["reboot_here"] = True
                board.ok(name, f"{h['before']} -> {h['after']}, reboots last")
                return
            h["step"] = "reboot"
            board.stage(name, "reboot")
            sh(name, "systemctl reboot", 30)         # the connection drops: its exit code says nothing
            drop_master(name)
            h["step"] = "back"
            board.stage(name, "coming back")
            wait_for(f"{name} online, quorate, with PVE {h['after']}", lambda: back(h), BACK_SECS)
            h["state"] += ", rebooted"
            board.ok(name, f"{h['before']} -> {h['after']}, rebooted and back")
        except (LabError, KeyboardInterrupt) as e:
            board.fail(name, f"{h['step']}: {e or 'interrupted'}")
            raise


# --- the command ---

RECOVERY = {
    "students": "nothing was changed on {host}; run `{again}` again when the students are done",
    "sources": ("on {host} as root, put the old sources back: for f in /etc/apt/sources.list{bak} "
                "/etc/apt/sources.list.d/*{bak}; do [ -e \"$f\" ] && mv \"$f\" \"${{f%{bak}}}\"; done; apt-get update"),
    "update": ("check {host}'s network and apt sources (apt-get update there), then run `{again}` again"
               "{restore}"),
    "upgrade": ("on {host} as root, once the upgrade there has ended (systemctl is-active " + UNIT + ": inactive; "
                "its output: " + HOST_LOG + "): dpkg --configure -a; apt-get -f install{finish}; then run `{again}` "
                "again{restore}"),
    "reboot": "check {host}'s console; once it is up (pvelab node list), run `{again}` again",
    "back": "{host} is upgraded but not seen back yet: check its console; once it is up, run `{again}` again",
}
FINISH = "; apt-get dist-upgrade (the new sources stay); reboot"
RESTORE = ("\n    (with --major, if no package was upgraded yet: put the old sources back - every *{bak} file "
           "in /etc/apt and /etc/apt/sources.list.d over its original - then apt-get update; apt-get -f install)")


def summary(job):
    """Each host's version before and after, and what happened to it."""
    cols = [("HOST", 14), ("BEFORE", 12), ("AFTER", 12), ("", 0)]
    print("\n" + " ".join(f"{c:<{w}}" for c, w in cols).rstrip())
    for n in job["order"]:
        h = job["hosts"][n]
        color = "red" if h["state"].startswith("failed") else "green" if h["state"].startswith("upgraded") else None
        print(" ".join([f"{n:<14}", f"{h['before'] or '?':<12}", f"{h['after'] or '?':<12}", paint(h["state"], color)]))


def plan_for(job, todo):
    plan = Plan()
    reboots = [h["name"] for h in todo if job["major"] or h["kernel"] or h["reboot"]]
    for h in todo:
        reboot = h["name"] in reboots
        what_ = (f" to PVE {job['target']}: apt sources {h['codename']} -> {h['new_codename']} (old ones kept as "
                 f"*{BAK}), apt-get dist-upgrade" if job["major"] else f": {what(h)} (apt-get dist-upgrade)")
        last = ", last: pvelab runs here and ends with the reboot" if is_local(h["name"]) and reboot else ""
        plan.do(f"upgrade {h['name']}{what_}" + (", then reboot it and wait until it is back" if reboot else "")
                + last)
        if reboot:
            plan.warn(f"{h['name']} reboots: its VMs stop; those with onboot=1 start again by themselves")
    plan.warn("apt-get dist-upgrade restarts PVE's services on each host (running VMs keep running)")
    if len(job["all"]) == 2 and reboots:
        plan.warn("with two hosts, while one reboots the other has no quorum: its /etc/pve is read-only (no VM can "
                  "be started or changed there; running ones keep running). A QDevice avoids this: "
                  "pvecm qdevice setup IP")
    return plan


def major_out(job):
    """Without --major: 'PVE N+1 (Debian X -> Y)' if the next major version is published for the hosts
    (checked on the first one, read-only), else None."""
    h = next((h for h in job["hosts"].values() if h["version"] and h["codename"]), None)
    if not h:
        return None
    probe = dict(job, target=major(h["version"]) + 1)
    try:
        r = check_published(probe, dict(h))
    except (LabError, OSError):
        return None
    return f"PVE {probe['target']} (Debian {h['codename']} -> {next_codename(h['codename'])})" if isinstance(r, Ok) else None


def node_upgrade(hosts, major_=False, dry=False, minor=False):
    """Steps 1-3 (see the module's doc). A failure stops: which host and step, each host's state, how to
    recover. Every command's output goes to this command's log (ui.LOG). Without --major a published new major
    version is offered (a question, default no); minor: never offered."""
    every = sorted(members())
    for n in hosts:
        if n not in every:
            die(f"no host '{n}' in this cluster" + did_you_mean(n, every))
    chosen = here_first(set(hosts) or every, last=True)
    job = {"major": major_, "all": every, "order": chosen, "target": None,
           "again": " ".join(["pvelab node upgrade", *hosts] + (["--major"] if major_ else [])),
           "hosts": {n: {"name": n, "before": None, "after": None, "codename": None, "new_codename": None,
                         "pkgs": [], "kernel": False, "reboot": False, "boot_id": None, "state": "not changed",
                         "step": "checks"} for n in chosen}}
    step(1, 3, "check the hosts - nothing is changed")
    failed = run_checks(job)
    if failed:
        die(f"{len(failed)} check(s) failed: nothing was changed. Fix {'it' if len(failed) == 1 else 'them'} "
            f"as shown above" + (f"\n  log: {ui.LOG}" if ui.LOG else ""))
    if not major_ and not minor:
        with Spinner("is a new major version of PVE out?") as line:
            out = major_out(job)
            line.ok(f"{out} is out" if out else "no new major version of PVE is out")
        if out:
            if sys.stdin.isatty() and not YES and not dry:
                if input(f"Upgrade to {out} as well (a major upgrade)? [y/N] ").strip().lower() == "y":
                    print(paint("  switching to a major upgrade: checking again with PVE's checker", "dim"))
                    return node_upgrade(hosts, major_=True, dry=dry)
                print(paint("  staying on this major version", "dim"))
            else:
                print(paint(f"  {out} is out; this run stays on this major version. For it: "
                            f"pvelab node upgrade --major", "yellow"))
    hs = [job["hosts"][n] for n in chosen]
    todo = [h for h in hs if (major(h["version"]) < job["target"] if major_ else h["pkgs"])]
    for h in hs:
        if h not in todo:
            h["state"] = f"skipped: already on PVE {job['target']}" if major_ else "skipped: up to date"
    if not todo:
        print(paint("\nEvery host is current: nothing to do.", "green"))
        summary(job)
        return
    step(2, 3, "plan")
    plan = plan_for(job, todo)
    if dry:
        dry_run("upgrade", plan.steps + [f"! {w}" for w, _, _ in plan.warnings])
        return
    plan.ask()
    h = todo[0]
    try:
        for k, h in enumerate(todo, 1):
            upgrade_host(job, h, k, len(todo))
    except (LabError, KeyboardInterrupt) as e:
        h["state"] = f"failed at '{h['step']}'" + (" - may be half-upgraded" if h["step"] == "upgrade" else "")
        summary(job)
        major_steps = major_ and h["step"] in ("update", "upgrade")
        fix = RECOVERY[h["step"]].format(host=h["name"], again=job["again"], bak=BAK,
                                          finish=FINISH if major_ else "",
                                          restore=RESTORE.format(bak=BAK) if major_steps else "")
        states = "; ".join(f"{x['name']}: {x['state']}" for x in hs)
        die(f"node upgrade: {h['name']}: step '{h['step']}' failed: {str(e) or 'interrupted'}\n"
            f"  state: {states}\n  recovery: {fix}\n  the other hosts were left as they are\n  log: {ui.LOG}")
    summary(job)
    here = next((h for h in todo if h.get("reboot_here")), None)
    if here:                                 # last of all: after it every host is done
        check = " ".join(["pvelab node upgrade", *hosts])     # not --major: that would look for the major after
        print(paint(f"\n{here['name']} reboots now: pvelab runs here, so this run ends with it.\n"
                    f"When it is back (a few minutes), log in again and check with:\n"
                    f"  {check}\n"
                    f"It checks every host again: there should be nothing left to do.", "yellow"))
        print(paint(f"log: {ui.LOG}", "dim"), flush=True)
        ui.log(f"{here['name']}: rebooting this host")
        sh(here["name"], "systemctl reboot", 30)
        return
    print(paint(f"\nlog: {ui.LOG}", "dim"))
