"""Why a VM is not running: the reason from PVE's tasks, the host journal and the storage. No host
commands: the reads are given."""
import time

from pvelab import downs

T = 1_793_700_000                       # a time in 2026


def when(ts):
    return downs.when(ts, now=T + 3600)


def jr(*lines):
    return downs.parse_journal(lines)


def test_when_this_year_and_other_years():
    assert downs.when(T, now=T + 3600) == time.strftime("%m/%d %H:%M", time.localtime(T))
    assert downs.when(T - 400 * 86400, now=T).startswith(time.strftime("%Y/", time.localtime(T - 400 * 86400)))


def test_parse_journal_lines():
    j = jr(f"{T}.1 workshop1 kernel: device-mapper: thin: 252:4: switching pool to out-of-data-space (error IO) mode",
           f"{T + 5}.2 workshop1 qmeventd[9]: Starting cleanup for 222",
           f"{T + 6}.2 workshop1 qmeventd[9]: Finished cleanup for 222",
           f"{T + 7}.0 workshop1 systemd[1]: 230.scope: A process of this unit has been killed by the OOM killer.",
           "-- No entries --", "garbage")
    assert j["full"] == [T] and j["cleanup"] == {222: [T + 5]} and j["oom"] == {230: [T + 7]}


def test_io_error_while_storage_full():
    r, fix = downs.reason(222, "io-error", [], jr(), (100, 0))
    assert "disk write failed" in r and "local-lvm is full (100% used)" in r
    assert fix == "free space on local-lvm, then: pvelab start {name}"


def test_io_error_after_storage_was_full():
    j = jr(f"{T}.0 workshop1 kernel: device-mapper: thin: 252:4: switching pool to out-of-data-space (error IO) mode")
    r, fix = downs.reason(222, "io-error", [{"type": "qmstart", "status": "OK", "endtime": T - 99}], j, (30, 69),
                          now=T + 3600)
    assert r == f"paused {when(T)}: disk write failed while local-lvm was full (now 30% used)"
    assert fix == "pvelab start {name}"


def test_io_error_without_a_full_storage():
    r, fix = downs.reason(222, "io-error", [], jr(), (30, 69))
    assert r == "paused: disk write failed (I/O error on its disk)" and "journalctl -k" in fix


def test_stopped_by_someone():
    tasks = [{"type": "qmstop", "status": "OK", "endtime": T, "user": "root@pam"},
             {"type": "qmstart", "status": "OK", "endtime": T - 50}]
    assert downs.reason(222, "stopped", tasks, jr(), (30, 69), now=T + 3600) == (
        f"stopped by root@pam {when(T)}", "pvelab start {name}")
    tasks[0]["type"] = "qmshutdown"
    assert downs.reason(222, "stopped", tasks, jr(), (30, 69), now=T + 3600)[0] == f"shut down by root@pam {when(T)}"


def test_start_failed():
    tasks = [{"type": "qmstart", "status": "TASK ERROR: storage 'local-lvm' is full", "endtime": T}]
    r, fix = downs.reason(222, "stopped", tasks, jr(), (30, 69), now=T + 3600)
    assert r == f"start failed {when(T)}: storage 'local-lvm' is full" and fix == "pvelab start {name}"


def test_killed_for_memory_beats_powered_off():
    tasks = [{"type": "qmstart", "status": "OK", "endtime": T - 100}]
    j = jr(f"{T}.0 h systemd[1]: 222.scope: A process of this unit has been killed by the OOM killer.",
           f"{T + 1}.0 h qmeventd[9]: Starting cleanup for 222")
    r, _ = downs.reason(222, "stopped", tasks, j, (30, 69), now=T + 3600)
    assert r == f"killed {when(T)}: the host ran out of memory"


def test_powered_off_inside():
    tasks = [{"type": "qmstart", "status": "OK", "endtime": T - 100}]
    j = jr(f"{T}.0 h qmeventd[9]: Starting cleanup for 222", f"{T - 500}.0 h qmeventd[9]: Starting cleanup for 222")
    assert downs.reason(222, "stopped", tasks, j, (30, 69), now=T + 3600)[0] == (
        f"powered off from inside the VM {when(T)}")


def test_events_before_the_last_start_do_not_count():
    tasks = [{"type": "qmstart", "status": "OK", "endtime": T}]
    j = jr(f"{T - 10}.0 h qmeventd[9]: Starting cleanup for 222",
           f"{T - 10}.0 h kernel: device-mapper: thin: 252:4: switching pool to out-of-data-space (error IO) mode")
    assert downs.reason(222, "stopped", tasks, j, (30, 69))[0] == "stopped (no reason in the logs of the last 14 days)"
    assert downs.reason(222, "io-error", tasks, j, (30, 69))[0] == "paused: disk write failed (I/O error on its disk)"


def test_other_paused_states_and_unknown():
    assert downs.reason(222, "paused", [], jr(), (30, 69)) == ("paused (suspended)", "pvelab start {name}")
    assert downs.reason(222, "guest-panicked", [], jr(), (30, 69))[0] == "paused: the guest kernel panicked"
    assert downs.reason(222, "unknown", [], jr(), (30, 69))[0] == "no news from its host (offline?)"
    assert downs.reason(222, "weird", [], jr(), (30, 69))[0] == "weird"


class FakeBoard:
    def __init__(self):
        self.done = {}

    def stage(self, i, text):
        pass


def start_one(monkeypatch, qmp_states, resumed_reason=("paused: disk write failed", None)):
    """pvelab start on one VM that qm list calls running; qmp_states: what qm status --verbose says, in order."""
    from pvelab.commands import power
    calls, states = [], list(qmp_states)
    monkeypatch.setattr(power, "targets", lambda args: [222])
    monkeypatch.setattr(power, "vm_statuses", lambda ids: {222: "running"})
    monkeypatch.setattr(power, "vm_names", lambda ids: {222: "net40-student01"})
    monkeypatch.setattr(power, "node_of", lambda i: "workshop1")
    monkeypatch.setattr(power, "qmp_status", lambda i, n: states.pop(0))
    monkeypatch.setattr(power, "run", lambda *cmd, **k: calls.append(cmd[-3:]) or "")
    monkeypatch.setattr(power, "reasons", lambda down: {i: resumed_reason for i in down})
    out = {}

    def each(title, ids, names, work, **k):
        for i in ids:
            try:
                out[i] = work(i, FakeBoard())
            except power.LabError as e:
                out[i] = "FAILED " + str(e)
    monkeypatch.setattr(power, "each", each)
    power.cmd_power("start", ["net40-student01"])
    return out[222], calls


def test_start_resumes_a_vm_paused_by_a_disk_error(monkeypatch):
    out, calls = start_one(monkeypatch, ["io-error", "running"])
    assert out == "resumed, running" and calls == [("qm", "resume", 222)]


def test_start_says_why_it_is_still_paused(monkeypatch):
    out, _ = start_one(monkeypatch, ["io-error", "io-error"])
    assert out == "FAILED still io-error: paused: disk write failed"


def test_start_leaves_a_running_vm_alone(monkeypatch):
    out, calls = start_one(monkeypatch, ["running"])
    assert out == "already running" and calls == []
