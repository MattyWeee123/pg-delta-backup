#!/bin/bash
# EXP-83b-d, on the source VM: the WAL summarizer's CPU seconds (user + system, from /proc, 10 ms resolution, where
# Linux ps gives whole seconds) and, since EXP-83d, its read_bytes (from storage) and rchar (from any source, the page
# cache included) from /proc/<pid>/io, as "<seconds> <read_bytes> <rchar> walsummarizer" for bench.py's --ps; nothing if
# no summarizer runs. The counters pass through as text: awk would print large ones in scientific notation.
for pid in $(pgrep -f '[w]alsummarizer'); do
  rb=$(sed -n 's/^read_bytes: //p' "/proc/$pid/io")
  rc=$(sed -n 's/^rchar: //p' "/proc/$pid/io")
  awk -v t="$(getconf CLK_TCK)" -v io="${rb:-0} ${rc:-0}" '{printf "%.2f %s walsummarizer\n", ($14 + $15) / t, io}' \
    "/proc/$pid/stat"
done
