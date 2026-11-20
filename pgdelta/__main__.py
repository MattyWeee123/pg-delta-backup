import sys
import time

# The receiver's publish_s counts from here (EXP-13), before pgdelta's own modules load.
LAUNCHED = time.perf_counter()

from pgdelta.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main(launched=LAUNCHED))
