"""A command that changes things goes on when its terminal goes away (SIGHUP), its output to its log only."""

import os
import subprocess
import sys

CHILD = """
import os, signal, sys
from pvelab import cli, ui
ui.use_log(sys.argv[1])
signal.signal(signal.SIGHUP, cli.keep_going)
print("before", flush=True)
os.kill(os.getpid(), signal.SIGHUP)
print("after", flush=True)
ui.log("still running")
"""


def test_hangup_goes_on_with_output_to_the_log(tmp_path):
    logf = tmp_path / "cmd.log"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    r = subprocess.run([sys.executable, "-c", CHILD, str(logf)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "before\n"                        # after the hangup: nothing more to the terminal
    text = logf.read_text()
    assert "terminal closed (hangup)" in text and "still running" in text     # and it went on to the end


def test_write_commands_take_the_hangup():
    src = open(os.path.join(os.path.dirname(__file__), "../../src/pvelab/cli.py")).read()
    assert "signal.signal(signal.SIGHUP, keep_going)" in src
