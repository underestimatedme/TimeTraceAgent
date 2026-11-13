#!/usr/bin/env python3
"""`python tests/eperm_cli.py ARGS...` is `python -m timetrace ARGS...` in a kernel that answers EPERM to the
existence probe `killpg(pgid, 0)`, for every group. Real signals, real processes, real SQLite: the only thing
that differs is that probe, which is what a group that exists but may not be signalled looks like.

Used by tests/test_local_stop.py to show that EPERM never counts as "the tool's process group is gone": the
scheduler must say the exit is not confirmed (待核对) and keep the slot fenced. Signals that really stop the tool
(SIGTERM, SIGKILL) are passed through untouched, so the tool itself does end.
"""
import errno
import os
import sys

_real_killpg = os.killpg


def _killpg(pgid, sig):
    if sig == 0:
        raise PermissionError(errno.EPERM, "Operation not permitted")
    return _real_killpg(pgid, sig)


os.killpg = _killpg

from timetrace.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
