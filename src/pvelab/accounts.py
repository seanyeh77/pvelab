"""pvelab create --accounts: each owner's login (and VM name) given, from a CSV file or inline.

    # comments at the top
    name,user,password         (a header line; name and password may be left out, or empty)
    ,s1101001,                 -> VM <net>-s1101001, user s1101001, a random password
    alice01,s1101002,Abc-123   -> VM alice01, user s1101002, password Abc-123

Inline: --accounts 's1101001:Pass-1,s1101002,s1101003:Pass-3' (user:password, comma separated).
An owner's VMs (20x3) share the name (alice01-1 .. -3), the user and the password."""

import csv
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


def vm_name_problem(name):
    """Why name can't be a VM's name, or None: a host name (lowercase letters, digits and -, at most 63), and
    not a word VM arguments read otherwise (all, netN, ...)."""
    if not NAME.fullmatch(name) or len(name) > 63:
        return f"'{name}' is not a host name (lowercase letters, digits and -, at most 63)"
    if name in ("all", "dhcp", "auto") or re.fullmatch(r"(net|lab|vmbr)\d+", name):
        return f"'{name}' means something else to pvelab (all VMs, a net, a bridge)"
    return None


def password_ok(p):
    """Whether p can be a password: not empty, only printable characters (the credentials file is tab separated,
    one VM per line)."""
    return bool(p) and p.isprintable()


@dataclass
class Account:
    name: str            # the owner's VM name (with -1 .. -k for several VMs each)
    user: str
    password: str | None     # None: a random one


def rows_of(v):
    """[(name, user, password)] from a CSV file (path v) or the inline form, each field "" when not given."""
    path = os.path.expanduser(v)
    if os.path.isfile(path):
        lines = [l for l in read_text(path, v).splitlines(keepends=True) if l.strip()]   # a cell's line break stays
        records = [[f.strip() for f in r] for r in csv.reader(lines, skipinitialspace=True)]   # quoted fields too
        for r in records:                                  # a spreadsheet's empty columns on the right (a,b,)
            while r and not r[-1]:
                r.pop()
        records = [r for r in records if r]                # and its empty rows (,,)
        while records and records[0][0].startswith("#"):   # comments: at the top only (a cell may start with #),
            records.pop(0)                                 # also one a spreadsheet saved quoted ("# a, b",,)
        cols = ["user", "password"]
        if records and "user" in [c.lower() for c in records[0]]:
            header = [c.lower() for c in records.pop(0)]
            keep = [j for j, c in enumerate(header) if c]          # columns with no header: left out
            for i, r in enumerate(records, 1):
                if bad := [j + 1 for j, f in enumerate(r) if f and (j >= len(header) or not header[j])]:
                    die(f"--accounts {v}, account {i}: a value in column {bad[0]}, which has no header")
            cols, records = [header[j] for j in keep], [[r[j] for j in keep if j < len(r)] for r in records]
            if unknown := set(cols) - {"name", "user", "password"}:
                die(f"--accounts {v}: unknown column(s) {', '.join(sorted(unknown))} (name, user, password)")
            if twice := sorted({c for c in cols if cols.count(c) > 1}):
                die(f"--accounts {v}: column(s) twice: {', '.join(twice)}")
        out = []
        for i, fields in enumerate(records, 1):
            if len(fields) > len(cols):
                die(f"--accounts {v}, account {i}: {len(fields)} fields, the columns are {','.join(cols)}")
            row = dict(zip(cols, fields + [""] * (len(cols) - len(fields))))
            out.append((row.get("name", ""), row.get("user", ""), row.get("password", "")))
        return out
    users = [p.split(":", 1)[0].strip() for p in v.split(",")]    # a user part with / or .csv: a path
    if any("/" in u or u.endswith(".csv") for u in users):
        die(f"--accounts {v}: no such file")
    return [("", *[f.strip() for f in (p.split(":", 1) + [""])[:2]]) for p in v.split(",") if p.strip()]


def read_text(path, v):
    """The file's text: UTF-8 (with or without a BOM), else Big5 (cp950: Excel's "CSV" on zh-TW Windows)."""
    data = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp950"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    die(f"--accounts {v}: neither UTF-8 nor Big5 text - save it as 'CSV UTF-8'")


def parse(v, net_name, per):
    """--accounts V -> [Account], checked: users and VM names valid, names unique (with -k for per > 1)."""
    out = []
    for i, (name, user, password) in enumerate(rows_of(v), 1):
        where = f"--accounts, account {i}"
        if why := user_problem(user):
            die(f"{where}: user {why}")
        name = name or f"{net_name}-{user}"
        if name == f"{net_name}-shared":
            die(f"{where}: {name} is the net's shared VM's name (pvelab create --shared): give another")
        longest = name + (f"-{per}" if per > 1 else "")
        if why := vm_name_problem(name) or vm_name_problem(longest):
            die(f"{where}: VM name {why}")
        if password and not password_ok(password) or ":" in password and not os.path.isfile(os.path.expanduser(v)):
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
