#!/bin/bash
# EXP-83b, on the source VM: the WAL summarizer's CPU seconds (user + system, from /proc, 10 ms resolution, where
# Linux ps gives whole seconds), as "<seconds> walsummarizer" for bench.py's --ps; nothing if no summarizer runs.
for pid in $(pgrep -f '[w]alsummarizer'); do
  awk -v t="$(getconf CLK_TCK)" '{printf "%.2f walsummarizer\n", ($14 + $15) / t}' "/proc/$pid/stat"
done
