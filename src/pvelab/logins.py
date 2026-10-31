"""Login attempts, from journal lines of sshd and pvedaemon (journalctl -o short-unix): what they say,
and telling attacks from students who mistyped their password. Only parsing and counting here;
pvelab status reads the journals and shows the result."""

import ipaddress
import re
from collections import Counter, namedtuple

from .config import INTERNAL_NETS, LOGIN_IP_HIGH, LOGIN_RATE_HIGH

# '1730371200.123456 workshops sshd-session[1234]: Failed password for root from 1.2.3.4 port 5 ssh2'
LINE = re.compile(r"^(\d+(?:\.\d+)?) \S+ [^:\s]+: (.*)$")
FAILED = re.compile(r"Failed \S+ for (?:invalid user )?(\S*) from (\S+) port")
INVALID = re.compile(r"Invalid user (\S*) from (\S+) port")
ACCEPTED = re.compile(r"Accepted \S+ for (\S+) from (\S+) port")
WEB = re.compile(r"authentication failure; rhost=(\S+) user=(\S*)")       # pvedaemon (PVE::API2::AccessControl)

# what journalctl prints for these (the rest is left out in the host or VM, before it is sent here)
SSH_LOG = "journalctl -q --no-pager -o short-unix -t sshd -t sshd-session"
WANTED = "Failed |Invalid user |Accepted |authentication failure"

Event = namedtuple("Event", "ts kind user ip")         # kind: fail, invalid, accept, web


def stamp(line):
    """The time (epoch seconds) of a journal line, or 0."""
    m = LINE.match(line)
    return float(m.group(1)) if m else 0.0


def plain_ip(ip):
    """'::ffff:1.2.3.4' (IPv4 seen by an IPv6 socket) and '[::1]' -> the address alone."""
    return ip.strip("[]").removeprefix("::ffff:")


def parse(lines):
    """Journal lines -> [Event] in the order given; lines that are no login attempt are skipped."""
    out = []
    for line in lines:
        m = LINE.match(line)
        if not m:
            continue
        ts, msg = float(m.group(1)), m.group(2)
        for kind, rx in (("fail", FAILED), ("invalid", INVALID), ("accept", ACCEPTED)):
            if f := rx.search(msg):
                out.append(Event(ts, kind, f.group(1), plain_ip(f.group(2))))
                break
        else:
            if f := WEB.search(msg):
                out.append(Event(ts, "web", f.group(2), plain_ip(f.group(1))))
    return out


def is_internal(ip, hosts=()):
    """Inside: the hosts' LAN and the lab nets (INTERNAL_NETS), the hosts themselves, loopback."""
    if ip in hosts:
        return True
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False                         # not an address (a host name): count it as outside
    return a.is_loopback or any(a in ipaddress.ip_network(n) for n in INTERNAL_NETS)


def tally(events, start, window, hosts=(), student=None, known=()):
    """What happened from start on (window: its length in s), for one host (student=None) or one VM:
      fails      failed logins in all                    rate_high  LOGIN_RATE_HIGH or more per hour
      outside    Counter {ip: failures} from outside     inside     the same from inside
      own        failures from the student's own IPs ("wrong password (student)"; VMs only)
      users      Counter of the user names tried
      guessed    [(ip, failures, user)]: LOGIN_IP_HIGH+ failures from an IP not known before, then a login
      odd        [(ip, user)]: a VM login from outside as root or as someone else than its student
      web        Counter {ip: failures} of the web UI / API (pvedaemon)
    known: IPs that logged in as the VM's student before the window (their failures are typos)."""
    win = [e for e in events if e.ts >= start]
    fails = [e for e in win if e.kind == "fail"]
    by_ip = Counter(e.ip for e in fails)
    first = {}
    for e in fails:
        first.setdefault(e.ip, e.ts)
    guessed, seen = [], set()
    for e in win:
        if (e.kind == "accept" and e.ip not in known and e.ip not in seen and by_ip[e.ip] >= LOGIN_IP_HIGH
                and e.ts > first[e.ip]):
            guessed.append((e.ip, by_ip[e.ip], e.user))
            seen.add(e.ip)
    own = set(known)
    if student:                              # the student logging in today (not after many failures)
        own |= {e.ip for e in win if e.kind == "accept" and e.user == student and e.ip not in seen}
    odd = []
    if student:
        odd = sorted({(e.ip, e.user) for e in win if e.kind == "accept" and not is_internal(e.ip, hosts)
                      and e.user != student})
    split = {"outside": Counter(), "inside": Counter(), "own": Counter()}
    for ip, n in by_ip.items():
        where = "own" if ip in own and ip not in seen else "inside" if is_internal(ip, hosts) else "outside"
        split[where][ip] = n
    return {"fails": len(fails), "rate_high": len(fails) * 3600 / max(window, 1) >= LOGIN_RATE_HIGH,
            "users": Counter(e.user for e in win if e.kind in ("fail", "invalid") and e.user),
            "guessed": guessed, "odd": odd, "web": Counter(e.ip for e in win if e.kind == "web"), **split}
