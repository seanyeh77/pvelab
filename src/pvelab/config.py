"""Settings: paths, defaults, names (most can be overridden with LAB_* environment variables)."""

import os


TAG = os.environ.get("LAB_TAG", "lab-student")
MARKER = "managed-by=lab.sh"                  # kept identical to lab.sh: VMs made by either are managed
PREFIX = os.environ.get("LAB_PREFIX", "student")
ID_START = int(os.environ.get("LAB_ID_START", "201"))
MEM = int(os.environ.get("LAB_MEM", "2048"))
CORES = int(os.environ.get("LAB_CORES", "2"))
DEFAULT_TEMPLATE = os.environ.get("LAB_TEMPLATE", "9002")
STORAGE = os.environ.get("LAB_STORAGE", "local-lvm")
TPL_DISK = os.environ.get("LAB_TPL_DISK", "10G")   # template root disk (clones can grow it)
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
IMG_DIR = "/var/lib/vz/template/cloudimg"     # downloaded cloud images (cache)
SNIPPET = "qga-vendor.yaml"                   # in local:snippets, used by every template
EST_DISK_GB = 3
SSH_PORT_BASE = 22000                         # expose: public port = SSH_PORT_BASE + VMID
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
PW_MINLEN = 10                                # passwords set inside the VMs (pam_pwquality)
UPGRADE_URL = os.environ.get("PVELAB_URL", "https://seanyeh77.github.io/pvelab/pvelab")
PUBLIC_IP_URL = "https://ifconfig.me/ip"      # otherwise: this host's public IP as seen from outside


def bridge(net):
    """The bridge of net N (vmbrN), or vmbr1 for "dhcp"."""
    return DHCP_BRIDGE if net == "dhcp" else f"vmbr{net}"


def subnet(net):
    """The first three parts of net N's addresses: 10.10.N (its /24), or vmbr1's for "dhcp"."""
    return DHCP_NET if net == "dhcp" else f"10.10.{net}"
