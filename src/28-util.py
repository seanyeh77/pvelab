# ================================================================ running commands
CMD = ""                                      # the command being run, for bad_usage()


def bad_usage(problem):
    """Wrong arguments: say what is wrong, then the command's help."""
    raise LabError(f"{problem}\n\n{command_help(CMD) or ''}".rstrip())


def run(*cmd, check=True, quiet=False, capture=True):
    """Run a command; return stdout. close_fds (default) keeps the lock out of VM processes."""
    r = subprocess.run([str(c) for c in cmd], text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.DEVNULL if quiet else None)
    if check and r.returncode:
        die(f"command failed ({r.returncode}): {' '.join(map(str, cmd))}")
    return r.stdout or ""


def ok(*cmd):
    return subprocess.run([str(c) for c in cmd], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


