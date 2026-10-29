"""ssh from outside: DNAT port SSH_PORT_BASE+VMID -> VM:22, kept in /etc/network/interfaces."""

import functools
import ipaddress
import re

from .config import bridge, GUEST_USER, IFACES, PUBLIC_HOST_FILE, PUBLIC_IP_URL, SSH_PORT_BASE, UPLINK
from .credentials import cred_rows
from .ifaces import has_section, iptables, section_re, write_section
from .util import http_open, run, uplink_ip, warn, write_file


def ssh_port(vmid):
    return SSH_PORT_BASE + int(vmid)


def ssh_command(vmid, row, host):
    return f"ssh -p {ssh_port(vmid)} {row.get('user', GUEST_USER)}@{host}"


def expose_net(n, host=None):
    """Open net n's ssh port forwards (host: the address to show students, saved for later) and
    print each VM's ssh command and the port range to allow in front of this host."""
    if host:
        write_file(PUBLIC_HOST_FILE, host + "\n", 0o600)
    ids, rows = sorted(set_expose(n, True)), cred_rows()
    h = public_host()
    for v in ids:
        print(f"{rows[v]['name']:<18} {ssh_command(v, rows[v], h)}")
    if ids:
        print(f"\nnet{n} exposed. Allow TCP {ssh_port(ids[0])}-{ssh_port(ids[-1])} in the cloud "
              "security group / firewall in front of this host.")
    else:
        print(f"net{n} exposed (no VMs yet; ports follow create/destroy)")


def forwarded_on(nets):
    """VMIDs whose ssh port is forwarded, on any of nets."""
    return set().union(*map(forwarded, nets))


def is_exposed(n):
    return has_section("expose", n)


def forwarded(n):
    """VMIDs whose ssh net n's expose section really forwards (none if the net isn't exposed)."""
    m = section_re("expose", n).search(open(IFACES).read())
    return {int(p) - SSH_PORT_BASE for p in re.findall(r"--dport (\d+) -j DNAT", m.group(0))} if m else set()


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
    return uplink_ip() or "<this host>"


def expose_cmds(n, vms):
    """iptables commands (up, down) forwarding port 22000+VMID on the uplink to each VM's ssh.
    vms: {vmid: ip}. Only DNATed traffic may enter the isolated bridge."""
    fwd = f"FORWARD -i {UPLINK} -o {bridge(n)} -p tcp --dport 22 -m conntrack --ctstate DNAT -j ACCEPT"
    nat = [f"PREROUTING -i {UPLINK} -p tcp --dport {ssh_port(v)} -j DNAT --to-destination {ip}:22"
           for v, ip in sorted(vms.items())]
    fwd_up, fwd_down = iptables([fwd], add="-I")
    nat_up, nat_down = iptables([f"-t nat {r}" for r in nat])
    return fwd_up + nat_up, fwd_down + nat_down


def set_expose(n, on):
    """(Re)write net n's port forwards inside its interfaces block and apply them now.
    on=False removes them. Called by expose, and through refresh_net when VMs come and go - so a VM
    whose port can't be used is left out with a warning instead of stopping the others' rules.
    Returns {vmid: ip} of the VMs it forwards."""
    if not on:
        return write_section("expose", n, [], [])
    vms = {v: r["ip"] for v, r in cred_rows().items() if r["net"] == n and r["ip"] != "-"}
    busy = {int(l.split()[3].rsplit(":", 1)[1]) for l in run("ss", "-Hltn").splitlines() if l.split()}
    for v in sorted(vms):
        p = ssh_port(v)
        why = "is used by a service on this host" if p in busy else "doesn't exist" if p > 65535 else None
        if why:
            warn(f"VMID {v}: port {p} {why} - its ssh is not reachable from outside")
            del vms[v]
    write_section("expose", n, *expose_cmds(n, vms))
    return vms
