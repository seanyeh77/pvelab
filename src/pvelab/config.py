"""Settings: paths, defaults, names (most can be overridden with LAB_* environment variables)."""

import os
import re
import shutil


TAG = os.environ.get("LAB_TAG", "lab-student")
MARKER = "managed-by=lab.sh"                  # kept identical to lab.sh: VMs made by either are managed
PREFIX = os.environ.get("LAB_PREFIX", "student")
ID_START = int(os.environ.get("LAB_ID_START", "201"))
MEM = int(os.environ.get("LAB_MEM", "2048"))
CORES = int(os.environ.get("LAB_CORES", "2"))
DEFAULT_TEMPLATE = os.environ.get("LAB_TEMPLATE", "9002")
STORAGE = os.environ.get("LAB_STORAGE", "local-lvm")
TPL_DISK = os.environ.get("LAB_TPL_DISK", "10G")   # template root disk (clones can grow it)
SAVED_ID_START = 9100                         # save-template: VMIDs of templates made from VMs
YES = os.environ.get("LAB_YES") == "1"
FORCE = os.environ.get("LAB_FORCE") == "1"
DHCP_BRIDGE = "vmbr1"
DHCP_NET = "10.10.10"                         # vmbr1 = .1/24, dnsmasq DHCP .100-.200
UPLINK = "vmbr0"

IP_FIRST = 11
GUEST_USER = "student"
LAB_DIR = "/root/lab"
CRED = f"{LAB_DIR}/credentials.tsv"           # vmid name user password net ip (tab separated)
IFACES = "/etc/network/interfaces"
LEASES = "/var/lib/misc/dnsmasq.leases"
CONF_DIR = "/etc/pve/qemu-server"
NODES_DIR = "/etc/pve/nodes"                  # <node>/qemu-server/<vmid>.conf: every node's VMs
MEMBERS = "/etc/pve/.members"                 # the cluster's nodes: JSON with name, IP, online
IMG_DIR = "/var/lib/vz/template/cloudimg"     # downloaded cloud images (cache)
SNIPPET = "qga-vendor.yaml"                   # in local:snippets, used by every template
SNIPPET_DIR = "/var/lib/vz/snippets"         # local:snippets (on each node's own disk)
STORAGE_CFG = "/etc/pve/storage.cfg"
EST_DISK_GB = 3
SSH_PORT_BASE = 22000                         # expose: public port = SSH_PORT_BASE + VMID
SSH_CTL = f"{LAB_DIR}/ssh-%C"                  # ssh to other nodes: one shared connection per node
PUBLIC_HOST_FILE = f"{LAB_DIR}/public-host"   # address students ssh to (expose --host)
PRE = "before-"                               # run FILE: snapshot before-<name> first
SNAP_INITIAL = "initial"                      # snapshot before first boot (never changed)
SNAP = "clean"                                # snapshot after first boot; rollback's default,
                                              # replaced by "pvelab snapshot"
READY_TIMEOUT = 900                           # s to wait for first boot (cloud-init) in create
RUN_LOG_DIR = f"{LAB_DIR}/run-logs"           # output of scripts run in VMs (create --script, run)
RUN_TIMEOUT_MIN = 60                          # default limit for one script run
AGENT_WAIT = 300                              # s to wait for a booting VM's guest agent before run
BLOCKS_FILE = f"{LAB_DIR}/blocks.json"        # {vmid: ["internet", "ssh"]} - see block/unblock
BLOCK_KINDS = ("internet", "ssh")
MINING_PORTS = "3333,4444,5555,6666,7777,9999,14433,14444,45560,45700"   # stratum; dropped on every net
HOST_RESERVE_MB = 2048                        # memory create/resize leave for the host itself
CPU_HIGH = 0.9                                # list/top: a VM this busy (of its cores) is flagged
STORAGE_FULL_PCT = 80                         # warn when the VM storage is this full
UPGRADE_URL = os.environ.get("PVELAB_URL", "https://seanyeh77.github.io/pvelab/pvelab")
PUBLIC_IP_URL = "https://ifconfig.me/ip"      # otherwise: this host's public IP as seen from outside
INTERNAL_NETS = ("10.0.0.0/24", "10.10.0.0/16")   # status: logins from here (and the hosts) are inside
LOGIN_IP_HIGH = 20                            # status: an outside IP with this many failures is highlighted
LOGIN_RATE_HIGH = 100                         # status: a host or VM with this many failures per hour is red
STATUS_EVERY = 5                              # status -f: s between reads of the VMs and the journals
KNOWN_EVERY = 300                             # status -f: s between reads of who logged in to a VM in 7 days
INTERNET_PROBE = "1.1.1.1/443"                # status: a VM reaches the internet if it can connect here


SDN = os.environ.get("LAB_SDN", "auto")       # auto: new nets are PVE SDN vnets where SDN works; 0: never
SDN_ZONE = "pvelab"                           # the VXLAN zone of pvelab's vnets (one network across nodes)
SDN_VNETS = "/etc/pve/sdn/vnets.cfg"
SDN_SUBNETS = "/etc/pve/sdn/subnets.cfg"
SDN_TAG_BASE = 4200000                        # VXLAN id of net N's vnet: SDN_TAG_BASE + N
SDN_PLUGIN = "/usr/share/perl5/PVE/Network/SDN/Zones/VxlanPlugin.pm"


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def sdn_available():
    """Whether new nets can be SDN vnets here: PVE has SDN with VXLAN zones, ifupdown2 applies it, and
    /etc/network/interfaces reads the file SDN writes. Otherwise (older PVE, LAB_SDN=0) a vmbrN bridge."""
    return (SDN != "0" and os.path.exists(SDN_PLUGIN) and shutil.which("ifreload") is not None
            and re.search(r"^source /etc/network/interfaces\.d/\*", read_text(IFACES), re.M) is not None)


def net_bridge(net, sdn):
    """Net N's bridge name as an SDN vnet (labN, on every node) or as a bridge on this node (vmbrN)."""
    return f"lab{net}" if sdn else f"vmbr{net}"


def bridge(net):
    """The bridge of net N: what /etc/network/interfaces has for it (a net pvelab made carries its
    stanza, vmbrN or labN), else what a new net gets here (labN where SDN works). vmbr1 for "dhcp"."""
    if net == "dhcp":
        return DHCP_BRIDGE
    m = re.search(rf"^iface ((?:vmbr|lab){net}) ", read_text(IFACES), re.M)
    return m.group(1) if m else net_bridge(net, sdn_available())


def is_sdn(net):
    return bridge(net) == net_bridge(net, True)


def subnet(net):
    """The first three parts of net N's addresses: 10.10.N (its /24), or vmbr1's for "dhcp"."""
    return DHCP_NET if net == "dhcp" else f"10.10.{net}"
