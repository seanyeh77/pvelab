"""A PVE pool per net (pvelab-NAME) and an API token that may only read the pool's VMs (net pool, net token).

pvelab records nothing: a net has a pool when PVE has one of that name, a token when PVE has its user's
token, read from /etc/pve/user.cfg. The token's role is pvelab's own (ROLE, VM.Audit only); its user and the
token each have it on /pool/pvelab-NAME only (a token with privilege separation has the intersection of its
user's rights and its own). Its secret goes to TOKEN_DIR/NAME.token (root only) and is never printed."""

import json
import os

from . import config, nets
from .config import is_plain
from .util import die, run

PREFIX = "pvelab-"
ROLE = "LabAudit"                        # PVE refuses role IDs that start with PVE (any case)
PRIVS = ("VM.Audit",)                     # GET status/current; GET /version needs only a valid token
TOKEN = "api"
TOKEN_DIR = "/etc/pve/priv/pvelab/tokens"     # cluster-wide, root only (like vms.json)


def user_cfg():
    return config.read_text(config.USER_CFG)


def lines(text, kind):
    """The fields after kind of each user.cfg line of that kind (pool:NAME:COMMENT:VMIDS:STORAGES: ...)."""
    return [f[1:] for f in (line.strip().split(":") for line in text.splitlines()) if f[0] == kind]


def ids(field):
    return [v for v in field.split(",") if v.strip()]


def pools(text=None):
    """{pool: {"vms": [vmid, ...], "storage": [id, ...]}} of user.cfg's text."""
    text = user_cfg() if text is None else text
    return {f[0]: {"vms": [int(v) for v in ids(f[2]) if v.isdigit()] if len(f) > 2 else [],
                   "storage": ids(f[3]) if len(f) > 3 else []} for f in lines(text, "pool") if f}


def vm_pools(text):
    """{vmid: pool} from user.cfg's text (pool:NAME:COMMENT:VMID,VMID:STORAGE,...:)."""
    return {v: p for p, m in pools(text).items() for v in m["vms"]}


def roles(text=None):
    """{role: set of privileges} of user.cfg's own roles (PVE's built-in ones are not in it)."""
    return {f[0]: set(ids(f[1])) if len(f) > 1 else set() for f in lines(user_cfg() if text is None else text, "role")}


def acls(text=None):
    """[(path, user or token, role)] of user.cfg's ACL lines (acl:PROPAGATE:PATH:WHO,...:ROLE,...:)."""
    return [(f[1], who, role) for f in lines(user_cfg() if text is None else text, "acl") if len(f) > 3
            for who in ids(f[2]) for role in ids(f[3])]


def users(text=None):
    return {f[0] for f in lines(user_cfg() if text is None else text, "user") if f}


def tokens(text=None):
    return {f[0] for f in lines(user_cfg() if text is None else text, "token") if f}


def suffix(n):
    """Net n's name as a pool, user and file name hold it (a vnet's alias may hold other characters)."""
    from .lifecycle import tag_name          # lifecycle imports this module
    return tag_name(nets.net_name(n))


def pool_name(n):
    return PREFIX + suffix(n)


def user_of(n):
    return f"{PREFIX}{suffix(n)}@pve"


def token_of(n):
    return f"{user_of(n)}!{TOKEN}"


def token_file(n):
    return f"{TOKEN_DIR}/{suffix(n)}.token"


def path_of(n):
    return f"/pool/{pool_name(n)}"


def has_pool(n, text=None):
    return not is_plain(n) and pool_name(n) in pools(text)


def has_token(n, text=None):
    """Whether net n's token user (or its token) is in PVE, even half made."""
    return not is_plain(n) and (user_of(n) in users(text) or token_of(n) in tokens(text))


def clone_args(net):
    """qm clone's options for a new VM of net: --pool when the net has one."""
    return ["--pool", pool_name(net)] if has_pool(net) else []


def pveum(*args, **kw):
    return run("pveum", *args, **kw)


# --- the pool

def make_pool(n, vmids):
    pveum("pool", "add", pool_name(n), "--comment", f"pvelab net {nets.net_name(n)}")
    add_vms(n, vmids)


def add_vms(n, vmids):
    if vmids:
        pveum("pool", "modify", pool_name(n), "--vms", ",".join(map(str, sorted(vmids))))


def remove_pool(n):
    """Its members out (the VMs stay), then the pool."""
    p = pool_name(n)
    m = pools().get(p)
    if m is None:
        return
    if m["vms"]:
        pveum("pool", "modify", p, "--vms", ",".join(map(str, m["vms"])), "--delete", "1")
    if m["storage"]:
        pveum("pool", "modify", p, "--storage", ",".join(m["storage"]), "--delete", "1")
    pveum("pool", "delete", p)


# --- the token

def role_problem(text=None):
    """Why ROLE cannot be used (it exists with other privileges), else None."""
    have = roles(text).get(ROLE)
    if have is not None and have != set(PRIVS):
        return (f"PVE role {ROLE} exists with other privileges ({', '.join(sorted(have)) or 'none'}), not "
                f"{', '.join(PRIVS)} only: pvelab does not change it - remove or rename it in PVE first")
    return None


def token_steps(n, text=None, rotate=False):
    """[(what, argv of pveum)] that make net n's token (rotate: its token again), each one only when what it
    makes is not there yet. The token's own add is ("token", ...): its answer holds the secret."""
    text = user_cfg() if text is None else text
    user, tok, path = user_of(n), token_of(n), path_of(n)
    have = set(acls(text))
    out = []
    if ROLE not in roles(text):
        out.append(("role", ["role", "add", ROLE, "--privs", ",".join(PRIVS)]))
    if user not in users(text):
        out.append(("user", ["user", "add", user, "--comment", f"pvelab net {nets.net_name(n)}: API only"]))
    if (path, user, ROLE) not in have:
        out.append(("user acl", ["acl", "modify", path, "--users", user, "--roles", ROLE]))
    if rotate and tok in tokens(text):
        out.append(("old token", ["user", "token", "remove", user, TOKEN]))
    out.append(("token", ["user", "token", "add", user, TOKEN, "--privsep", "1", "--output-format", "json"]))
    out.append(("token acl", ["acl", "modify", path, "--tokens", tok, "--roles", ROLE]))
    return out


def off_steps(n, text=None):
    """[(what, argv of pveum)] that remove net n's token, its ACLs and its user; the role too when no other
    ACL uses it then."""
    text = user_cfg() if text is None else text
    user, tok = user_of(n), token_of(n)
    out = [(f"acl {who}", ["acl", "delete", path, "--" + ("tokens" if "!" in who else "users"), who, "--roles", role])
           for path, who, role in acls(text) if who in (user, tok)]
    if tok in tokens(text):
        out.append(("token", ["user", "token", "remove", user, TOKEN]))
    if user in users(text):
        out.append(("user", ["user", "delete", user]))
    if ROLE in roles(text) and not any(r == ROLE and who not in (user, tok) for _, who, r in acls(text)):
        out.append(("role", ["role", "delete", ROLE]))
    return out


def make_token(n, steps):
    """Run token_steps(); the secret goes to token_file(n) only (never printed, logged or an argument)."""
    for what, argv in steps:
        if what != "token":
            pveum(*argv)
            continue
        out = pveum(*argv, capture=True)     # captured: stdout stays out of the command's log
        try:
            secret = json.loads(out)["value"]
        except (ValueError, KeyError, TypeError):
            die(f"pveum user token add gave no token value for {token_of(n)}")
        save_secret(n, secret)


def save_secret(n, secret):
    """TOKEN_DIR/NAME.token (mode 0600): ID=SECRET, what follows PVEAPIToken= in an Authorization header.
    Written into a new file, then moved over the old one (pmxcfs: no chmod of a file in place)."""
    path = token_file(n)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = path + ".new"
    with open(os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(f"{token_of(n)}={secret}\n")
    os.replace(new, path)


def with_net(n):
    """What goes with net n when it is removed: [its token's words, its pool's words] (those it has)."""
    text = user_cfg()
    return ([f"API token {token_of(n)} with its user"] if has_token(n, text) else []) \
        + ([f"PVE pool {pool_name(n)}"] if has_pool(n, text) else [])


def remove_with_net(n):
    """Net n is removed: its token, its user and their ACLs (the role with the last net's), then its pool."""
    if has_token(n):
        remove_token(n, off_steps(n))
    remove_pool(n)


def remove_token(n, steps):
    for _, argv in steps:
        pveum(*argv)
    try:
        os.remove(token_file(n))
    except FileNotFoundError:
        pass
