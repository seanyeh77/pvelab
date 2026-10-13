"""OS templates pvelab can build. Add a Debian/Ubuntu release here."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    vmid: int
    name: str        # VM name of the template on PVE
    base: str        # URL of the directory holding the image and its checksum file
    image: str       # image file name in that directory
    sums: str        # checksum file in that directory: SHA256SUMS or SHA512SUMS
    aliases: tuple = ()


def debian(vmid, version, codename, *aliases):
    return Template(vmid, f"debian-{version}-cloud",
                    f"https://cloud.debian.org/images/cloud/{codename}/latest",
                    f"debian-{version}-genericcloud-amd64.qcow2", "SHA512SUMS", aliases)


def ubuntu(vmid, version, *aliases):
    return Template(vmid, f"ubuntu-{version.replace('.', '')}-cloud",
                    f"https://cloud-images.ubuntu.com/releases/{version}/release",
                    f"ubuntu-{version}-server-cloudimg-amd64.img", "SHA256SUMS", aliases)


# One line per template. To add a system, append a line with an unused VMID, e.g.
#   ubuntu(9006, "25.10", "ubuntu25"),
#   Template(9010, "rocky-9-cloud", "https://dl.rockylinux.org/pub/rocky/9/images/x86_64",
#            "Rocky-9-GenericCloud-Base.latest.x86_64.qcow2", "CHECKSUM", ("rocky9",)),
TEMPLATES = [
    debian(9001, 12, "bookworm", "debian12"),
    debian(9002, 13, "trixie", "debian13", "debian"),
    ubuntu(9003, "26.04", "ubuntu26", "ubuntu"),
    ubuntu(9004, "24.04", "ubuntu24"),
    ubuntu(9005, "22.04", "ubuntu22"),
]
