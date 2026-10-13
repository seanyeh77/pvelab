#!/usr/bin/env python3
"""pvelab - student VMs on this Proxmox VE node

usage: pvelab <command> [args]          details and examples: pvelab help <command>

VMs
  create COUNT [TEMPLATE] [--net N|auto] [--expose] [--script FILE]
         [--cores N] [--memory MB] [--disk GB]
  list [--ssh] [--net N]                 names, sizes, IPs, ssh ports, passwords
  start | stop | reboot VM...
  destroy VM...                          asks before deleting
  resize VM... [--cores N] [--memory MB] [--disk GB|+GB] [--reboot]
  ssh NAME [COMMAND]                     ssh into a VM from this host

Snapshots and scripts
  rollback VM... [--to NAME | --initial] undo the students' changes
  snapshot VM...                         current state becomes "clean"
  run VM... FILE                         run a script as root in the VMs

Access
  expose N [--off]                       students ssh in from outside (port 22000+VMID)
  block | unblock internet|ssh|all VM... cut internet or ssh; kept across reboots

Templates and pvelab itself
  templates                              available OS templates
  pull TEMPLATE... | all                 build templates ahead of time
  upgrade                                download the latest pvelab

VM: a name (net40-student01), netN (the whole net), 'net40-student1*', or all
Runs as root; other users are asked before it uses sudo.

example:
  pvelab create 20 ubuntu26 --net auto --expose --script setup.sh
  pvelab list --ssh
"""
import fcntl
import fnmatch
import glob
import functools
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass


