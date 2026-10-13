"""The single-file build: reproducible, runnable, and accepted by `pvelab upgrade`."""
import pathlib
import subprocess
import sys

from pvelab.commands.upgrade import build_problem

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "dist" / "pvelab"


def build():
    subprocess.run([sys.executable, str(ROOT / "build.py")], check=True, capture_output=True)
    return OUT.read_bytes()


def test_build_is_reproducible():
    assert build() == build()


def test_build_runs_and_is_accepted_by_upgrade():
    data = build()
    assert build_problem(data) is None
    r = subprocess.run([str(OUT), "help", "create"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("usage: pvelab create")


def test_upgrade_refuses_other_downloads():
    assert build_problem(b"<!doctype html><html>") is not None
    assert build_problem(b"#!/usr/bin/env python3\nprint('old single-file pvelab')\n") is not None
