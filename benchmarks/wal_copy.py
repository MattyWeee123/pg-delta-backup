"""Atomic WAL archive/restore copy for disposable benchmark clusters."""
from pathlib import Path
import os
import shutil
import sys
import tempfile


def copy_wal(mode, source: Path, destination: Path):
    if mode not in ("archive", "restore"):
        raise ValueError("Unknown WAL copy mode")
    if not source.is_file():
        return 1
    if mode == "archive" and destination.exists():
        if source.read_bytes() != destination.read_bytes():
            return 1
        return 0
    fd, temporary = tempfile.mkstemp(prefix=".wal-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(copy_wal(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])))
