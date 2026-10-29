#!/bin/bash
# EXP-83b-d, on the source VM: the WAL summarizer's CPU seconds (user + system, from /proc, 10 ms resolution, where
# Linux ps gives whole seconds) and, since EXP-83d, its read_bytes (from storage) and rchar (from any source, the page
# cache included) from /proc/<pid>/io, as "<seconds> <read_bytes> <rchar> walsummarizer" for bench.py's --ps; nothing if
# no summarizer runs.
for pid in $(pgrep -f '[w]alsummarizer'); do
  io=$(awk '/^read_bytes:/ {r = $2} /^rchar:/ {c = $2} END {print r + 0, c + 0}' "/proc/$pid/io")
  awk -v t="$(getconf CLK_TCK)" -v io="$io" '{printf "%.2f %s walsummarizer\n", ($14 + $15) / t, io}' "/proc/$pid/stat"
done
