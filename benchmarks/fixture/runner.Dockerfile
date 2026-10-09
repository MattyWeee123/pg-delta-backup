# Image for running benchmarks/benchmark.py with real PostgreSQL 17 client tools.
#
# Build this once rather than installing packages at run time: apt-get inside a
# measurement window competes for CPU and network with the thing being measured.
#
#   docker build -f benchmarks/fixture/runner.Dockerfile -t pg-delta-runner .
#
# git is included so versions.txt records a real commit instead of
# "git_commit: unavailable".

FROM postgres:17

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /repo
