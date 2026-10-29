#!/bin/bash
# EXP-83d, on the source VM: the /proc/diskstats line of the device behind /data, for bench.py's --diskstats.
awk -v d="$(basename "$(findmnt -no SOURCE /data)")" '$3 == d' /proc/diskstats
