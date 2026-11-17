"""pvelab net check BRIDGE: does a VM on this bridge get a network? Tried as a VM would: a network namespace
on the bridge with a new MAC (a veth pair), which asks DHCP for an address, then reaches the gateway, a name
server and the package server its first boot uses. What blocks a VM blocks this too - no DHCP, a host on
OpenStack (its port security drops other MACs), a switch that allows one MAC per port. The lease is
released and the namespace removed after."""

import json
import os
import random
import secrets
import subprocess

from .util import run

# Run in the namespace (python3 -c): one DHCP exchange (discover, offer, request, ack) on interface argv[1]
# with MAC argv[2]; prints {"ip", "prefix", "router", "dns", "server"} or {"error"}.
DHCP = r'''
import json, socket, struct, sys, time
dev, mac = sys.argv[1], bytes.fromhex(sys.argv[2].replace(":", ""))
xid = int.from_bytes(__import__("os").urandom(4), "big")

def packet(kind, extra=b""):
    head = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, xid, 0, 0x8000, b"\0" * 4, b"\0" * 4,
                       b"\0" * 4, b"\0" * 4, mac + b"\0" * 10, b"", b"")
    return head + b"\x63\x82\x53\x63" + bytes([53, 1, kind]) + bytes([55, 4, 1, 3, 6, 54]) + extra + b"\xff"

def options(data):
    out, i = {}, 240
    while i < len(data) and data[i] != 255:
        if data[i] == 0:
            i += 1
            continue
        out[data[i]] = data[i + 2:i + 2 + data[i + 1]]
        i += 2 + data[i + 1]
    return out

def ask(kind, want, extra=b"", tries=3, wait=2.0):
    for _ in range(tries):
        s.sendto(packet(kind, extra), ("255.255.255.255", 67))
        end = time.time() + wait
        while time.time() < end:
            r.settimeout(max(0.05, end - time.time()))
            try:
                pkt = r.recv(2048)               # an IP packet: UDP to port 68 is DHCP's answer
            except socket.timeout:
                break
            ihl = (pkt[0] & 15) * 4
            if len(pkt) < ihl + 8 or pkt[9] != 17 or pkt[ihl + 2:ihl + 4] != b"\x00\x44":
                continue
            data = pkt[ihl + 8:]
            if len(data) > 240 and data[4:8] == xid.to_bytes(4, "big"):
                o = options(data)
                if o.get(53) == bytes([want]):
                    return data, o
    return None, None

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.setsockopt(socket.SOL_SOCKET, 25, dev.encode())          # SO_BINDTODEVICE
s.bind(("0.0.0.0", 68))
# The answers are read from the link: with no address yet the IP layer may drop them (dhclient does the same)
r = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(0x0800))
r.bind((dev, 0x0800))
offer, o = ask(1, 2)
if not offer:
    print(json.dumps({"error": "no DHCP answer"}))
    sys.exit()
ip, server = offer[16:20], o.get(54, b"")
ack, a = ask(3, 5, bytes([50, 4]) + ip + (bytes([54, 4]) + server if server else b""))
if not ack:
    print(json.dumps({"error": "a DHCP offer, but no acknowledgement"}))
    sys.exit()
mask = a.get(1, b"\xff\xff\xff\0")
print(json.dumps({"ip": socket.inet_ntoa(ack[16:20]), "prefix": bin(int.from_bytes(mask, "big")).count("1"),
                  "router": socket.inet_ntoa(a[3][:4]) if a.get(3) else "",
                  "dns": [socket.inet_ntoa(a[6][k:k + 4]) for k in range(0, len(a.get(6, b"")), 4)],
                  "server": socket.inet_ntoa(server) if server else ""}))
'''

# Run in the namespace: argv[1] host name -> its first IPv4 (or "error: ..."); argv[2] given: also an HTTP
# HEAD there, printing the status line.
REACH = r'''
import socket, sys
try:
    addr = socket.getaddrinfo(sys.argv[1], 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
except OSError as e:
    print("error: name: " + str(e))
    sys.exit()
print(addr)
if len(sys.argv) > 2:
    try:
        c = socket.create_connection((addr, 80), timeout=5)
        c.sendall(f"HEAD / HTTP/1.1\r\nHost: {sys.argv[1]}\r\nConnection: close\r\n\r\n".encode())
        print(c.recv(200).decode("latin-1").splitlines()[0])
    except OSError as e:
        print("error: http: " + str(e))
'''

# Run in the namespace: hand the address back (DHCPRELEASE from ip to server).
RELEASE = r'''
import socket, struct, sys
ip, server, mac = sys.argv[1], sys.argv[2], bytes.fromhex(sys.argv[3].replace(":", ""))
head = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, 0, 0, 0, socket.inet_aton(ip), b"\0" * 4,
                   b"\0" * 4, b"\0" * 4, mac + b"\0" * 10, b"", b"")
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind((ip, 68))
s.sendto(head + b"\x63\x82\x53\x63" + bytes([53, 1, 7, 54, 4]) + socket.inet_aton(server) + b"\xff", (server, 67))
'''


def mirror_of(template):
    """The package server a template's first boot reaches (its guest agent comes from there)."""
    return "archive.ubuntu.com" if "ubuntu" in str(template).lower() else "deb.debian.org"


def new_mac():
    """A random locally administered unicast MAC (as a new VM's would be)."""
    return ":".join(["02"] + [f"{secrets.randbelow(256):02x}" for _ in range(5)])


def check(bridge, mirror="deb.debian.org"):
    """[(step, ok, detail)] for a VM on bridge: DHCP, gateway (ping), DNS, package server; it stops at the
    first of DHCP, DNS that fails. Run as root on this host."""
    tag = f"{os.getpid() % 10000}{random.randrange(100):02d}"
    ns, host_end, ns_end, mac = f"pvelab-check-{tag}", f"pvck{tag}", f"pvcn{tag}", new_mac()

    def inside(*cmd, **kw):
        return run("ip", "netns", "exec", ns, *cmd, **kw)
    steps = []
    try:
        run("ip", "netns", "add", ns)
        run("ip", "link", "add", host_end, "type", "veth", "peer", "name", ns_end)
        run("ip", "link", "set", ns_end, "netns", ns)
        run("ip", "link", "set", host_end, "master", bridge, "up")
        inside("ip", "link", "set", "lo", "up")
        inside("ip", "link", "set", ns_end, "address", mac, "up")
        lease = json.loads(inside("python3", "-c", DHCP, ns_end, mac, timeout=30) or '{"error": "no answer"}')
        if "error" in lease:
            return steps + [("DHCP", False, lease["error"])]
        steps.append(("DHCP", True, f"{lease['ip']}/{lease['prefix']}" + (f", gateway {lease['router']}"
                                                                         if lease["router"] else "")))
        inside("ip", "addr", "add", f"{lease['ip']}/{lease['prefix']}", "dev", ns_end)
        try:
            if not lease["router"]:
                return steps + [("gateway", False, "DHCP gave no gateway")]
            inside("ip", "route", "add", "default", "via", lease["router"])
            pinged = subprocess.run(["ip", "netns", "exec", ns, "ping", "-c", "2", "-W", "2", lease["router"]],
                                    capture_output=True).returncode == 0
            steps.append(("gateway", pinged, lease["router"] + ("" if pinged else " does not answer ping")))
            os.makedirs(f"/etc/netns/{ns}", exist_ok=True)       # ip netns exec mounts it as /etc/resolv.conf
            with open(f"/etc/netns/{ns}/resolv.conf", "w") as f:
                f.write("".join(f"nameserver {d}\n" for d in lease["dns"]) or "nameserver 1.1.1.1\n")
            out = inside("python3", "-c", REACH, mirror, "http", timeout=30).splitlines() or ["error: no answer"]
            if out[0].startswith("error"):
                return steps + [("DNS", False, f"{mirror}: {out[0][7:]}" + ("" if lease["dns"] else
                                                                           " (DHCP gave no name server)"))]
            steps.append(("DNS", True, f"{mirror} is {out[0]}"))
            web = out[1] if len(out) > 1 else "error: no answer"
            steps.append(("packages", not web.startswith("error"), f"{mirror}: {web.removeprefix('error: ')}"))
            return steps
        finally:
            if lease.get("server"):
                inside("python3", "-c", RELEASE, lease["ip"], lease["server"], mac, check=False, quiet=True)
    finally:
        run("ip", "link", "del", host_end, check=False, quiet=True)          # its peer goes with it
        run("ip", "netns", "del", ns, check=False, quiet=True)
        if os.path.isdir(f"/etc/netns/{ns}"):
            for f in os.listdir(f"/etc/netns/{ns}"):
                os.remove(f"/etc/netns/{ns}/{f}")
            os.rmdir(f"/etc/netns/{ns}")


def problem(steps):
    """The first failed step as one line ("DHCP: no DHCP answer"), or None when the package server is
    reached (a gateway may ignore ping and still route)."""
    if steps and steps[-1][0] == "packages" and steps[-1][1]:
        return None
    return next((f"{s}: {d}" for s, ok, d in steps if not ok), None)
