"""ssh from outside: DNAT port SSH_PORT_BASE+VMID -> VM:22, kept in /etc/network/interfaces."""

import functools
import ipaddress
import re

from .config import GUEST_USER, PUBLIC_HOST_FILE, PUBLIC_IP_URL, SSH_PORT_BASE, UPLINK
from .credentials import cred_rows
from .ifaces import has_section, write_section
from .util import die, http_open, run, warn


def ssh_port(vmid):
    return SSH_PORT_BASE + int(vmid)


def ssh_command(vmid, row, host):
    return f"ssh -p {ssh_port(vmid)} {row.get('user', GUEST_USER)}@{host}"


def is_exposed(n):
    return has_section("expose", n)


@functools.cache
def public_host():
    """Address students ssh to: the saved --host, else the public IP from ifconfig.me,
    else vmbr0's own IP."""
    try:
        h = open(PUBLIC_HOST_FILE).read().strip()
        if h:
            return h
    except OSError:
        pass
    try:
        ip = http_open(PUBLIC_IP_URL, timeout=5).read().decode().strip()
        return str(ipaddress.ip_address(ip))
    except (OSError, ValueError):
        warn(f"could not get the public IP from {PUBLIC_IP_URL}, showing {UPLINK}'s address")
    m = re.search(r"inet (\S+)/", run("ip", "-4", "-o", "addr", "show", "dev", UPLINK, check=False))
    return m.group(1) if m else "<this host>"


def expose_cmds(n, vms):
    """iptables commands (up, down) forwarding port 22000+VMID on the uplink to each VM's ssh.
    vms: {vmid: ip}. Only DNATed traffic may enter the isolated bridge."""
    fwd = f"FORWARD -i {UPLINK} -o vmbr{n} -p tcp --dport 22 -m conntrack --ctstate DNAT -j ACCEPT"
    nat = [f"PREROUTING -i {UPLINK} -p tcp --dport {ssh_port(v)} -j DNAT --to-destination {ip}:22"
           for v, ip in sorted(vms.items())]
    up = [f"iptables -I {fwd}"] + [f"iptables -t nat -A {r}" for r in nat]
    down = [f"iptables -D {fwd}"] + [f"iptables -t nat -D {r}" for r in nat]
    return up, down


def set_expose(n, on):
    """(Re)write net n's port forwards inside its interfaces block and apply them now.
    on=False removes them. Called by expose, and through refresh_net when VMs come and go."""
    if not on:
        return write_section("expose", n, [], [])
    vms = {v: r["ip"] for v, r in cred_rows().items() if r["net"] == n and r["ip"] != "-"}
    busy = {int(l.split()[3].rsplit(":", 1)[1]) for l in run("ss", "-Hltn").splitlines() if l.split()}
    clash = sorted(ssh_port(v) for v in vms if ssh_port(v) in busy)
    if clash:
        die(f"port(s) {clash} already used by a service on this host")
    if any(ssh_port(v) > 65535 for v in vms):
        die(f"VMIDs above {65535 - SSH_PORT_BASE} can't be exposed")
    write_section("expose", n, *expose_cmds(n, vms))
