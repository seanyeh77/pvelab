"""pvelab create --accounts: each student's login (and VM name) given, from a CSV file or inline.

    name,user,password         (a header line; name and password may be left out, or empty)
    ,s1101001,                 -> VM <net>-s1101001, user s1101001, a random password
    alice01,s1101002,Abc-123   -> VM alice01, user s1101002, password Abc-123

Inline: --accounts 's1101001:Pass-1,s1101002,s1101003:Pass-3' (user:password, comma separated).
A student's VMs (20x3) share the name (alice01-1 .. -3), the user and the password."""

import os
import re
from dataclasses import dataclass

from .ui import some
from .util import die

USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")


def user_problem(u):
    """Why u is no login name, or None."""
    return None if USER.fullmatch(u) else (f"'{u}' is not a login name (lowercase letters, digits, _ and -, at most "
                                           "32, not starting with a digit)")


def password_ok(p):
    """Whether p can be a password: not empty, without tabs or line breaks (the credentials file is tab separated)."""
    return bool(p) and not any(c in p for c in "\t\n")


@dataclass
class Account:
    name: str            # the student's VM name (with -1 .. -k for several VMs each)
    user: str
    password: str | None     # None: a random one


def rows_of(v):
    """[(name, user, password)] from a CSV file (path v) or the inline form, each field "" when not given."""
    if os.path.isfile(v):
        lines = [l.strip() for l in open(v, encoding="utf-8-sig") if l.strip() and not l.lstrip().startswith("#")]
        cols = ["user", "password"]
        if lines and "user" in [c.strip().lower() for c in lines[0].split(",")]:
            cols = [c.strip().lower() for c in lines.pop(0).split(",")]
            if unknown := set(cols) - {"name", "user", "password"}:
                die(f"--accounts {v}: unknown column(s) {', '.join(sorted(unknown))} (name, user, password)")
        out = []
        for i, line in enumerate(lines, 1):
            fields = [f.strip() for f in line.split(",")]
            if len(fields) > len(cols):
                die(f"--accounts {v}, line {i}: {len(fields)} fields, the columns are {','.join(cols)}")
            row = dict(zip(cols, fields + [""] * (len(cols) - len(fields))))
            out.append((row.get("name", ""), row.get("user", ""), row.get("password", "")))
        return out
    if "/" in v or v.endswith(".csv"):
        die(f"--accounts {v}: no such file")
    return [("", *(p.split(":", 1) + [""])[:2]) for p in v.split(",") if p.strip()]


def parse(v, net_name, per):
    """--accounts V -> [Account], checked: users and VM names valid, names unique (with -k for per > 1)."""
    out = []
    for i, (name, user, password) in enumerate(rows_of(v), 1):
        where = f"--accounts, student {i}"
        if why := user_problem(user):
            die(f"{where}: user {why}")
        name = name or f"{net_name}-{user}"
        if name == f"{net_name}-shared":
            die(f"{where}: {name} is the net's shared VM's name (pvelab create --shared): give another")
        longest = name + (f"-{per}" if per > 1 else "")
        if not NAME.fullmatch(name) or len(longest) > 63:
            die(f"{where}: VM name '{longest}' is not a host name (lowercase letters, digits and -, at most 63)")
        if password and not password_ok(password) or ":" in password and not os.path.isfile(v):
            die(f"{where}: a password can't have a tab or a line break (or, inline, a ':')")
        out.append(Account(name, user, password or None))
    if not out:
        die(f"--accounts {v}: no accounts in it")
    names = [a.name for a in out]
    if dup := sorted({n for n in names if names.count(n) > 1}):
        die(f"--accounts: VM name(s) twice: {', '.join(dup)} (give a name column)")
    return out


def short_warnings(passwords):
    """Warnings about passwords given that are short ({VM name: password or None}; harden asks for 10
    characters: chpasswd refuses a shorter one in a VM that ran it)."""
    short = [n for n, p in passwords.items() if p and len(p) < 10]
    return [f"short passwords (under 10 characters; refused in VMs that ran harden): {some(short, 6)}"] if short else []
