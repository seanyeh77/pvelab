"""Command-line options, parsed the same way by every command."""


from .helptext import bad_usage
from .util import die, net_number


def valid_net(v):
    """'30' or 'net30' -> '30', or die."""
    if (n := net_number(v)) is None:
        die("--net must be a number")
    if not 2 <= n <= 254:
        die("--net must be 2..254")
    if n == 10:
        die("--net 10 would overlap vmbr1's 10.10.10.0/24")
    return str(n)


def net_arg(v):
    """A --net value: a number, netN or a net's name -> the net's number, or die."""
    from . import nets                            # nets imports this module
    n = nets.resolve(v)
    if n is None:
        die(f"no net called '{v}' (pvelab list shows the nets)")
    return n


def opt(a, it, *names):
    """Value of an option that takes one, given as "--x V" or "--x=V" (also for each alias in
    names, e.g. "-t V"); None if a is none of them. a: this argument, it: the remaining ones."""
    for n in names:
        if a == n:
            v = next(it, None)
        elif n.startswith("--") and a.startswith(n + "="):
            v = a[len(n) + 1:]
        else:
            continue
        if not v:
            bad_usage(f"{n} needs a value")
        return v
    return None


def no_bare_net(a):
    """The old form 'net30' for a whole net: point at --net 30."""
    if a.startswith("net") and net_number(a) is not None:
        bad_usage(f"a whole net is given as --net {a[3:]} (not '{a}')")
