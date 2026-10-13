# ================================================================ expose (ssh from outside)
def ssh_port(vmid):
    return SSH_PORT_BASE + int(vmid)


def expose_re(n):
    return re.compile(rf"^        # expose net {n} begin\n.*?^        # expose net {n} end\n", re.M | re.S)


def is_exposed(n):
    return expose_re(n).search(open(IFACES).read()) is not None


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
    on=False removes them. Called by expose, and by create/destroy to follow the VM list."""
    s = open(IFACES).read()
    old = expose_re(n).search(s)
    vms = {}
    if on:
        vms = {v: r["ip"] for v, r in cred_rows().items() if r["net"] == n and r["ip"] != "-"}
        busy = {int(l.split()[3].rsplit(":", 1)[1]) for l in run("ss", "-Hltn").splitlines() if l.split()}
        clash = sorted(ssh_port(v) for v in vms if ssh_port(v) in busy)
        if clash:
            die(f"port(s) {clash} already used by a service on this host")
        if any(ssh_port(v) > 65535 for v in vms):
            die(f"VMIDs above {65535 - SSH_PORT_BASE} can't be exposed")
    if old:
        for line in old.group(0).splitlines():
            if line.strip().startswith("post-down"):
                subprocess.run(shlex.split(line.strip()[len("post-down"):]), stderr=subprocess.DEVNULL)
        s = s.replace(old.group(0), "")
    if on:
        up, down = expose_cmds(n, vms)
        section = (f"        # expose net {n} begin\n"
                   + "".join(f"        post-up   {c}\n" for c in up)
                   + "".join(f"        post-down {c}\n" for c in down)
                   + f"        # expose net {n} end\n")
        s = s.replace(f"# lab.sh net {n} end\n", section + f"# lab.sh net {n} end\n")
    run("cp", "-a", IFACES, IFACES + ".lab-bak")
    with open(IFACES, "w") as f:
        f.write(s)
    if on:
        for c in up:
            run(*shlex.split(c))


