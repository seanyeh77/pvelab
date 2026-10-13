#!/usr/bin/env python3
"""Join src/*.py (in file-name order) into the single-file dist/pvelab and check it.

The parts are not separate modules: they are one script cut into pieces, so any part can
use anything defined in another. pvelab is shipped as one file so that install and
`pvelab upgrade` only ever download one thing.
"""
import ast
import glob
import os
import sys

root = os.path.dirname(os.path.abspath(__file__))
parts = sorted(glob.glob(os.path.join(root, "src", "*.py")))
code = "".join(open(p).read() for p in parts)
try:
    ast.parse(code)
except SyntaxError as e:
    # map the line back to its part
    n = e.lineno or 0
    for p in parts:
        k = open(p).read().count("\n")
        if n <= k:
            break
        n -= k
    sys.exit(f"build: syntax error in {os.path.relpath(p, root)} line {n}: {e.msg}")
out = os.path.join(root, "dist", "pvelab")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    f.write(code)
os.chmod(out, 0o755)
print(f"built {os.path.relpath(out, root)} from {len(parts)} parts ({code.count(chr(10))} lines)")
