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

import os
import re
import shlex
import subprocess
import threading
from functools import lru_cache

from .config import LAB_DIR, SSH_CTL, YES
from .guest import guest_json
from .inventory import did_you_mean, inventory, managed, vm_names, vm_statuses
from .joining import COROSYNC_CONF, Ok, quorate
from .nodes import is_local, members, on_node
from .templates import wait_for
from . import ui
from .ui import Board, Plan, Spinner, dry_run, paint, step
from .util import LabError, die, http_open, parallel

LOG_DIR = f"{LAB_DIR}/logs"
DEBIAN = ["buster", "bullseye", "bookworm", "trixie", "forky", "duke"]   # in release order
DEBIAN_URI = "http://deb.debian.org/debian"
MIN_ROOT_GB = {False: 2, True: 5}             # free on / for an update, for a major upgrade
MIN_BOOT_MB = 200                             # free on /boot (a new kernel and its initrd)
UPDATE_SECS = 600                             # apt-get update
UPGRADE_SECS = 3600                           # apt-get dist-upgrade (a major one takes a while)
CHECKER_SECS = 600                            # pveXtoY --full
BACK_SECS = 900                               # a rebooted host: online, quorate, new version
BAK = ".pvelab-bak"
SOURCES = ("/etc/apt/sources.list", "/etc/apt/sources.list.d/*.list", "/etc/apt/sources.list.d/*.sources")
APT = "LC_ALL=C DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none apt-get -q"
UPGRADE = (f"{APT} -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade")
KERNEL = re.compile(r"^(proxmox-kernel-|pve-kernel-|linux-image-)")


# --- running a shell line on a host (tests replace sh and drop_master) ---

def sh(node, script, timeout=60, show=None, data=None):
    """(exit code, output) of shell line script on node: here, or over the cluster's root ssh. stdout and
    stderr together; each line goes to the log, or to show(line) (a board's tail, which logs it).
    data: text for its stdin. Killed after timeout s (exit 124); an offline node gives 255."""
    ui.log(f"{node}$ {script[:300]}")
    try:
        argv = on_node(node, "sh", "-c", script)
    except LabError as e:                    # offline, or no address
        ui.log(f"{node}: {e}")
        return 255, str(e)
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    killed = []
    timer = threading.Timer(timeout, lambda: (killed.append(1), p.kill()))
    timer.start()
    out = []
    try:
        if data is not None:
            p.stdin.write(data)
            p.stdin.close()
        for line in p.stdout:
            out.append(line)
            if show:
                show(line)
            elif line.strip():
                ui.log(f"{node} | {line.rstrip()}")
        rc = p.wait()
    finally:
        timer.cancel()
        if p.poll() is None:
            p.kill()
            p.wait()
    if killed:
        ui.log(f"{node}: timed out after {timeout}s")
        return 124, "".join(out)
    return rc, "".join(out)


def drop_master(node):
    """Close the shared ssh connection to node (after a reboot it would hang on the old one)."""
    ip = members().get(node, {}).get("ip")
    if ip and not is_local(node):
        try:
            subprocess.run(["ssh", "-o", f"ControlPath={SSH_CTL}", "-O", "exit", f"root@{ip}"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


@lru_cache(maxsize=None)
def fetch_release(uri, suite):
    """The head of URI/dists/SUITE/Release (its fields), read from this host; None if it is not there."""
    try:
        with http_open(f"{uri.rstrip('/')}/dists/{suite}/Release", timeout=30) as r:
            return r.read(16384).decode(errors="replace")
    except (OSError, ValueError):
        return None


# --- parsers (pure) ---

def parse_pveversion(text):
    """`pveversion` ('pve-manager/9.2.20/abc (running kernel: ...)') -> '9.2.20', or None."""
    m = re.search(r"pve-manager/(\d+(?:\.\d+)*)", text or "")
    return m.group(1) if m else None


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
    """The output of DUMP_SOURCES -> {path: content}."""
    out, path = {}, None
    for line in (text or "").splitlines(keepends=True):
        if line.startswith("==pvelab== "):
            path = line[11:].strip()
            out[path] = ""
        elif path:
            out[path] += line
    return out


DUMP_SOURCES = f"for f in {' '.join(SOURCES)}; do [ -f \"$f\" ] && echo \"==pvelab== $f\" && cat \"$f\"; done; true"


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


def rewrite(path, text, old, new):
    """text of source file path with every enabled entry moved from Debian release old to new."""
    if path.endswith(".sources"):
        out = []
        for st in stanzas(text):
            f = fields(st)
            if "suites" in f and f.get("enabled", "yes").lower() != "no":
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
                for f in map(fields, stanzas(text)) if "suites" in f and f.get("enabled", "yes").lower() != "no"]
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


def who_users(text):
    """`who` output -> the sorted user names logged in."""
    return sorted({l.split()[0] for l in (text or "").splitlines() if l.split()})


# --- step 1: the checks, read-only ---

def gb(kb):
    return f"{kb / 1048576:.1f} GB"


def check_cluster(job):
    off = [n for n in job["all"] if not members()[n]["online"]]
    if off:
        return f"offline: {', '.join(off)} (every host must be up)"
    if os.path.exists(COROSYNC_CONF) and not quorate():
        return "the cluster is not quorate (pvecm status)"
    return Ok(f"{len(job['all'])} host(s) online" + (", cluster quorate" if os.path.exists(COROSYNC_CONF) else ""))


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
        return f"apt-get update failed ({rc}): {apt_error(out) or 'see the log'}"
    return Ok(f"{h['name']}: apt-get update works")


def check_packages(job, h):
    rc, out = sh(h["name"], f"{APT} -s dist-upgrade", 300)
    if rc:
        return f"apt-get -s dist-upgrade failed ({rc}): {apt_error(out) or 'see the log'}"
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
    """Every check on a spinner line (✔ or ✘); [(label, problem)] of those that failed."""
    failed = []

    def one(label, fn, *a):
        with Spinner(label, ui.LOG) as line:
            try:
                r = fn(job, *a)
            except (LabError, OSError) as e:
                r = f"the check itself failed: {e}"
            if r is None or isinstance(r, Ok):
                line.ok(r)
            else:
                line.fail(r)
        if line.problem:
            failed.append((label, line.problem))
        return not line.problem
    one("hosts online, cluster quorate", check_cluster)
    hosts = [job["hosts"][n] for n in job["order"]]
    for h in hosts:
        one(f"{h['name']}: version, free space", check_facts, h)
    if job["major"]:
        majors = {major(h["version"]) for h in job["hosts"].values() if h["version"]}
        if majors:                           # after a stop half-way some hosts are on the new one already
            job["target"] = max(majors) if len(majors) > 1 else max(majors) + 1
    for h in hosts:
        if not h["version"]:
            continue
        if not one(f"{h['name']}: apt-get update", check_update, h):
            continue
        one(f"{h['name']}: what would be upgraded", check_packages, h)
        if job["major"] and major(h["version"]) < job["target"]:
            if one(f"{h['name']}: pve{job['target'] - 1}to{job['target']} --full", check_checker, h):
                one(f"{h['name']}: PVE {job['target']} published", check_published, h)
    return failed


# --- step 3: one host ---

def logged_in(node):
    """{VM name: [users]} of node's running pvelab VMs with someone logged in (the guest agent's `who`),
    and how many running VMs did not answer."""
    ids = [i for i, c in managed(inventory()).items() if c.get("_node") == node]
    running = [i for i, st in (vm_statuses(ids) if ids else {}).items() if st == "running"]
    answers = parallel(lambda i: guest_json("exec", i, "--timeout", 10, "--", "who", timeout=30), running)
    names = vm_names(running)
    users = {names[i]: who_users(r.get("out-data", "")) for i, r in answers.items() if r}
    return {n: u for n, u in users.items() if u}, sum(1 for r in answers.values() if not r)


def students(h):
    """Who is logged in to the host's students' VMs: a warning and a question (default no) if anyone is."""
    with Spinner(f"{h['name']}: who is logged in to its students' VMs", ui.LOG) as line:
        found, silent = logged_in(h["name"])
        line.ok(f"{h['name']}: " + (f"{len(found)} VM(s) with someone logged in" if found else "nobody logged in"))
    if silent:
        print(paint(f"    note: {silent} running VM(s) gave no answer (no guest agent): can't tell who is on them", "dim"))
    if found:
        plan = Plan()
        for name, users in sorted(found.items()):
            plan.warn(f"{name}: {', '.join(users)} logged in (the upgrade may restart or reboot it)", YES, "LAB_YES=1")
        plan.ask(die)


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


def apt(h, board, label, script, secs):
    board.stage(h["name"], label, live=True)
    rc, out = sh(h["name"], script, secs, show=lambda line: board.tail(h["name"], line))
    if rc:
        die(f"{label} failed ({'timed out' if rc == 124 else rc})" + (f": {apt_error(out)}" if apt_error(out) else ""))


def back(h):
    """The rebooted host is up again: online, the cluster quorate, a new boot, pveversion as after the upgrade."""
    if not members().get(h["name"], {}).get("online"):
        return False
    if os.path.exists(COROSYNC_CONF) and not quorate():
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
            apt(h, board, "apt-get update", f"{APT} update", UPDATE_SECS)
            h["step"] = "upgrade"
            apt(h, board, "dist-upgrade", UPGRADE, UPGRADE_SECS)
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
    "upgrade": ("on {host} as root: dpkg --configure -a; apt-get -f install{finish}; then run `{again}` again"
                "{restore}"),
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
    for h in todo:
        reboot = job["major"] or h["kernel"] or h["reboot"]
        what_ = (f" to PVE {job['target']}: apt sources {h['codename']} -> {h['new_codename']} (old ones kept as "
                 f"*{BAK}), apt-get dist-upgrade" if job["major"] else f": {what(h)} (apt-get dist-upgrade)")
        last = ", last: pvelab runs here and ends with the reboot" if is_local(h["name"]) and reboot else ""
        plan.do(f"upgrade {h['name']}{what_}" + (", then reboot it and wait until it is back" if reboot else "")
                + last)
        if reboot:
            plan.warn(f"{h['name']} reboots: its VMs stop; those with onboot=1 start again by themselves",
                      YES, "LAB_YES=1")
    plan.warn("apt-get dist-upgrade restarts PVE's services on each host (running VMs keep running)",
              YES, "LAB_YES=1")
    if len(job["all"]) == 2 and any(job["major"] or h["kernel"] or h["reboot"] for h in todo):
        plan.warn("with two hosts, while one reboots the other has no quorum: its /etc/pve is read-only (no VM can "
                  "be started or changed there; running ones keep running). A QDevice avoids this: "
                  "pvecm qdevice setup IP", YES, "LAB_YES=1")
    return plan


def node_upgrade(hosts, major_=False, dry=False):
    """Steps 1-3 (see the module's doc). A failure stops: which host and step, each host's state, how to
    recover. Every command's output goes to one log under LOG_DIR."""
    every = sorted(members())
    for n in hosts:
        if n not in every:
            die(f"no host '{n}' in this cluster" + did_you_mean(n, every))
    chosen = sorted(set(hosts) or every, key=lambda n: (is_local(n), n))     # this host last
    job = {"major": major_, "all": every, "order": chosen, "target": None,
           "again": " ".join(["pvelab node upgrade", *hosts] + (["--major"] if major_ else [])),
           "hosts": {n: {"name": n, "before": None, "after": None, "codename": None, "new_codename": None,
                         "pkgs": [], "kernel": False, "reboot": False, "boot_id": None, "state": "not changed",
                         "step": "checks"} for n in chosen}}
    if not dry:
        ui.start_log(LOG_DIR, "node-upgrade")
    step(1, 3, "check the hosts - nothing is changed")
    failed = run_checks(job)
    if failed:
        die(f"{len(failed)} check(s) failed: nothing was changed. Fix {'it' if len(failed) == 1 else 'them'} "
            f"as shown above" + (f"\n  log: {ui.LOG}" if ui.LOG else ""))
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
    plan.ask(die)
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
    if here:
        print(paint(f"\n{here['name']} reboots now: pvelab runs here, so this run ends with it.\n"
                    f"When it is back (a few minutes), log in again and run:\n"
                    f"  {job['again']}\n"
                    f"It checks every host again and skips those already current.", "yellow"))
        print(paint(f"log: {ui.LOG}", "dim"), flush=True)
        ui.log(f"{here['name']}: rebooting this host")
        sh(here["name"], "systemctl reboot", 30)
        return
    print(paint(f"\nlog: {ui.LOG}", "dim"))
