"""Why a student VM is not running, from what PVE and its host recorded: the VM's own tasks (who
stopped it, a failed start), the host journal (storage full, out of memory, powered off inside) and
the storage now. One short reason and a fix for each, for pvelab list and pvelab status."""
import re
import time

from .config import STORAGE
from .nodes import on_node
from .resources import pvesh_json, storage
from .util import LabError, parallel, run

DAYS = 14                                # how far back the journal is read
TASKS = {"qmstart", "qmstop", "qmshutdown", "qmreboot", "qmresume", "qmsuspend"}
# QEMU's run state (PVE shows it as the VM's status) when the VM is on but not running
PAUSED = {"paused": "paused (suspended)", "suspended": "hibernated (suspended to disk)",
          "internal-error": "paused: QEMU hit an internal error", "guest-panicked": "paused: the guest kernel panicked",
          "prelaunch": "created, but its CPUs never started", "unknown": "no news from its host (offline?)"}
RESUMABLE = {"io-error", "paused"}       # pvelab start resumes these (qm resume)
JOURNAL = (f"journalctl -q --no-pager -o short-unix --since -{DAYS}d _TRANSPORT=kernel + SYSLOG_IDENTIFIER=qmeventd"
           " + _PID=1 -g 'out-of-data-space \\(error|Starting cleanup for|killed by the OOM killer'")
LINE = re.compile(r"(\d+)\.\d+ \S+ (\S+?)(?:\[\d+\])?: (.*)")


def when(ts, now=None):
    """MM/DD HH:MM this year, YYYY/MM/DD HH:MM another."""
    t, same = time.localtime(ts), time.localtime(now or time.time()).tm_year == time.localtime(ts).tm_year
    return time.strftime("%m/%d %H:%M" if same else "%Y/%m/%d %H:%M", t)


def parse_journal(lines):
    """JOURNAL's lines -> {"full": [ts the storage filled up], "cleanup": {vmid: [ts QEMU ended]},
    "oom": {vmid: [ts killed for memory]}}."""
    out = {"full": [], "cleanup": {}, "oom": {}}
    for line in lines:
        m = LINE.match(line)
        if not m:
            continue
        ts, msg = int(m.group(1)), m.group(3)
        if "out-of-data-space (error" in msg:
            out["full"].append(ts)
        elif (c := re.match(r"Starting cleanup for (\d+)", msg)):
            out["cleanup"].setdefault(int(c.group(1)), []).append(ts)
        elif (c := re.match(r"(\d+)\.scope: .*killed by the OOM killer", msg)):
            out["oom"].setdefault(int(c.group(1)), []).append(ts)
    return out


def reason(vmid, status, tasks, jr, sto, now=None):
    """(reason, fix) for VM vmid in status (not running). tasks: its PVE tasks (any order); jr:
    parse_journal() of its host; sto: storage() of its host. fix may hold {name}, or is None."""
    tasks = sorted((t for t in tasks if t.get("type") in TASKS), key=lambda t: t.get("endtime", 0), reverse=True)
    started = next((t.get("endtime", 0) for t in tasks if t["type"] == "qmstart" and t.get("status") == "OK"), 0)
    after = lambda stamps, pick=max: pick((s for s in stamps if s >= started), default=None)
    start = "pvelab start {name}"
    if status == "io-error":
        pct, _ = sto
        if pct is not None and pct >= 99:
            return (f"paused: disk write failed, {STORAGE} is full ({pct}% used)",
                    f"free space on {STORAGE}, then: {start}")
        if (t := after(jr["full"], min)):       # the first time it filled up: when the write failed
            return (f"paused {when(t, now)}: disk write failed while {STORAGE} was full"
                    + (f" (now {pct}% used)" if pct is not None else ""), start)
        return "paused: disk write failed (I/O error on its disk)", "see journalctl -k on its host"
    if status in PAUSED:
        return PAUSED[status], start if status in RESUMABLE else None
    if status != "stopped":
        return status, None
    last = tasks[0] if tasks else {}
    if last.get("type") in ("qmstop", "qmshutdown") and last.get("status") == "OK":
        verb = "stopped" if last["type"] == "qmstop" else "shut down"
        return f"{verb} by {last.get('user', '?')} {when(last['endtime'], now)}", start
    if last.get("type") == "qmstart" and last.get("status") != "OK":
        err = re.sub(r"^TASK ERROR: ", "", last.get("status") or "?")
        return f"start failed {when(last['endtime'], now)}: {err}", start
    if (t := after(jr["oom"].get(vmid, []))):
        return f"killed {when(t, now)}: the host ran out of memory", start
    if (t := after(jr["cleanup"].get(vmid, []))):
        return f"powered off from inside the VM {when(t, now)}", start
    return f"stopped (no reason in the logs of the last {DAYS} days)", start


def node_tasks(node, vmid):
    rows = pvesh_json(f"/nodes/{node}/tasks", "--vmid", vmid, "--limit", 50, "--source", "all")
    return rows if isinstance(rows, list) else []


def reasons(down):
    """{vmid: (reason, fix)} for down = {vmid: (status, node)}: one journal and storage read per host,
    the VMs' tasks in parallel."""
    if not down:
        return {}
    hosts = {node for _, node in down.values()}

    def host(n):                             # an offline host: no journal, its VMs are told apart by status
        try:
            lines = run(*on_node(n, "sh", "-c", JOURNAL), check=False, quiet=True).splitlines()
        except LabError:
            lines = []
        return parse_journal(lines), storage(n)
    per_host = parallel(host, hosts)
    tasks = parallel(lambda i: node_tasks(down[i][1], i), down)
    return {i: reason(i, st, tasks[i], *per_host[node]) for i, (st, node) in down.items()}

