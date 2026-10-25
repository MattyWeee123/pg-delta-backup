"""Run a command and write the CPU it and its descendants used (user and system seconds) to a file (EXP-30).

    python3 experiments/network/cpu_time.py OUT_FILE COMMAND [ARGS...]

RUSAGE_CHILDREN counts every descendant that was waited for: pgdelta's worker processes are its children, and it
waits for them, so the sender's whole CPU is counted. Exits with the command's status.
"""
import resource
import subprocess
import sys

if __name__ == '__main__':
    status = subprocess.run(sys.argv[2:]).returncode
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    with open(sys.argv[1], 'w') as out:
        out.write(f'{usage.ru_utime:.3f} {usage.ru_stime:.3f}\n')
    sys.exit(status)
