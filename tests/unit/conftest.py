"""Every test sees an empty /etc/pve of its own (no cluster, no VMs) unless it writes one: the host
the tests run on must not decide where a command goes."""
import pytest

from pvelab import inventory, nodes, templates


@pytest.fixture(autouse=True)
def empty_pve(tmp_path_factory, monkeypatch):
    pve = tmp_path_factory.mktemp("pve")             # not tmp_path: tests list that themselves
    (pve / "nodes").mkdir(parents=True)
    monkeypatch.setattr(nodes, "NODES_DIR", str(pve / "nodes"))
    monkeypatch.setattr(nodes, "MEMBERS", str(pve / ".members"))
    monkeypatch.setattr(nodes, "COROSYNC_CONF", str(pve / "corosync.conf"))     # in_cluster(): not there
    monkeypatch.setattr(nodes, "SSH_CTL", str(pve / "ssh-%C"))
    monkeypatch.setattr(nodes, "uplink_ip", lambda: "192.0.2.1")
    monkeypatch.setattr(inventory, "NODES_DIR", str(pve / "nodes"))
    monkeypatch.setattr(inventory, "CONF_DIR", str(pve / "qemu-server"))
    monkeypatch.setattr(templates, "NODES_DIR", str(pve / "nodes"))
    return pve


DANGEROUS = {"qm", "pvesh", "pvecm", "pvesm", "pct", "vzdump", "qmrestore", "ssh", "scp", "iptables",
             "iptables-save", "iptables-restore", "nft", "ifreload", "ifup", "ifdown", "ip", "lvs", "sudo"}


@pytest.fixture(autouse=True)
def no_host_commands(monkeypatch):
    """A test whose fake run() is bypassed must fail, not act on the machine running the tests."""
    import os
    import subprocess
    for name in ("run", "Popen", "check_output", "call", "check_call"):
        real = getattr(subprocess, name)

        def guard(cmd, *a, _real=real, **k):
            argv = [cmd] if isinstance(cmd, str) else list(cmd)
            prog = os.path.basename(str(argv[0]).split()[0]) if argv else ""
            if prog in DANGEROUS:
                raise AssertionError(f"unit test tried to run a host command: {argv}")
            return _real(cmd, *a, **k)
        monkeypatch.setattr(subprocess, name, guard)


@pytest.fixture(autouse=True)
def rename_state(tmp_path_factory, monkeypatch):
    """node rename keeps its state and backups here, not under /root/lab."""
    from pvelab import renaming
    d = tmp_path_factory.mktemp("rename")
    monkeypatch.setattr(renaming, "STATE_FILE", str(d / "rename-state.json"))
    monkeypatch.setattr(renaming, "LAB_DIR", str(d))


@pytest.fixture(autouse=True)
def no_command_log(tmp_path_factory, monkeypatch):
    """No log unless a test starts one; cli's logs (LOG_DIR) go here, not under /root/lab."""
    from pvelab import cli, config, ui
    logs = str(tmp_path_factory.mktemp("logs"))
    monkeypatch.setattr(config, "LOG_DIR", logs)
    monkeypatch.setattr(cli, "LOG_DIR", logs)
    monkeypatch.setattr(ui, "LOG", None)
