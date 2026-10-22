"""pvelab - student VMs on a Proxmox VE node."""

try:
    from ._version import VERSION              # written into the single-file build by build.py
except ImportError:
    VERSION = "dev (run from a source checkout)"
