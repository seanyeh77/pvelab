"""pvelab's marked sections in /etc/network/interfaces."""

import re

from .config import IFACES


def bridge_is_ours(n):
    return re.search(rf"^# lab\.sh net {n} begin", open(IFACES).read(), re.M) is not None


def expose_re(n):
    return re.compile(rf"^        # expose net {n} begin\n.*?^        # expose net {n} end\n", re.M | re.S)


def block_re(n):
    return re.compile(rf"^        # block net {n} begin\n.*?^        # block net {n} end\n", re.M | re.S)
