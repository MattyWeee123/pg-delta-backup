# A Linux image that can run the *whole* test suite, real PostgreSQL clusters included.
#
# Why this exists: tests/pgdelta_cluster.py skips every real-cluster test unless initdb, pg_ctl, psql and
# pg_controldata are present, and CI deliberately ships no PostgreSQL ("the tests that start real clusters run
# on the Mac before each push and in the VM experiments" - .github/workflows/experiment-tests.yml). So the
# engine's most important paths - in-place patching, live source reads, the WAL-summary fast path, datadir
# handling - are covered by tests that neither CI nor a Windows checkout ever executes. This image runs them.
#
#   docker build -t pgdelta-test tests/docker
#   docker run --rm --init --user postgres -v "$PWD:/w" -w /w pgdelta-test \
#       python3 -m unittest discover -s tests
#
# Notes on the pieces, each one learned from a failure:
#   procps, git, time  several tests shell out to ps, git and /usr/bin/time; without them they error rather
#                      than skip.
#   --init             the worker-lifetime tests assert a killed process group leaves nothing behind. With no
#                      reaper as PID 1, orphans reparent to PID 1 and are never reaped, so they fail.
#   --user postgres    initdb refuses to run as root.
#   zstandard          the --compress zstd path; the VMs' Python 3.11 uses this binding.
#   blake3             --hash auto prefers it; without it the tests exercise only the BLAKE2b fallback.
FROM postgres:17-bookworm

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        procps git time \
    && rm -rf /var/lib/apt/lists/*

# PEP 668 marks Debian's Python as externally managed; this image is disposable, so install into it directly.
RUN pip3 install --no-cache-dir --break-system-packages \
        'numpy>=2.0' 'matplotlib>=3.9' zstandard blake3

# Where tests/pgdelta_cluster.py looks for initdb, pg_ctl, psql and pg_controldata.
ENV PGDELTA_TEST_PG_BIN=/usr/lib/postgresql/17/bin
# matplotlib needs a writable home for its font cache; the postgres user's home is writable.
ENV MPLCONFIGDIR=/tmp/mpl

WORKDIR /w
