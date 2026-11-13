# viz: watch a refresh, or replay one, in a browser

A local web page showing a run as it happens, or replaying a recorded one. S and the destination are drawn as boxes, chunks fly across, each lane has a running timer, and VM runs add link and disk gauges. Several lanes race side by side, each clock starting at zero. Python's standard library only: nothing to install.

```bash
python -m viz serve          # then open http://localhost:8765 and pick a run
python -m viz race           # a laptop race, watched live at the URL it prints
```

## Launching runs from the page

**New run →** (at http://localhost:8765/launch) is a form:
- **What to run:** laptop or Google Cloud, methods, database size, change pattern, repeats; for the cloud, also machine type, disk, link cap and zones.
- **Check & estimate:** shows the cost, the quota and anything that would stop the run. **Launch** stays disabled until the check passes.
- **The run itself** goes through `python -m bench run` (see [bench/README.md](../bench/README.md)). Watch it live on the main page.
- **Publishing:** with the checkbox on, the run is committed and a pull request opened when it finishes. The page lists the runs started from it, with their logs and pull request links.

One run at a time. The page only accepts requests from itself.

## The laptop race

`python -m viz race [--scale 50] [--change hot|uniform]` builds a PostgreSQL database (PostgreSQL 17 or 18), copies it as the old copy, and changes 1% of the rows. Then it runs two lanes one after the other, so they don't compete for the laptop's disk:
1. `pg_basebackup` copies everything;
2. pgdelta refreshes the old copy.

Finally it starts the refreshed copy and compares its rows with the source. The page shows each lane live as it runs, then replays both side by side. Runs are saved in `runs/` (not committed).

On a laptop there's no slow network, so the naive pgdelta loses on time: in one run at scale 50, 8.7 s against `pg_basebackup`'s 1.1 s, because of its 168,340 round trips. It wins on bytes: 22 MB sent against 817 MB. Closing that time gap is what the roadmap is for.

## Replaying recorded VM runs

`python -m viz import-sampler NAME RUN_DIR:ID:LABEL ... [--limits link=119.6,disk=205.5]` turns recorded runs into lanes with gauges. Each run directory needs `meta.json` (with `t0` and `t1`) and the 0.1-second samples from `experiments/network/util_series.py` on both VMs (`util-source.txt`, `util-dest.txt`). Gauges at 90% of a given limit turn red.

`viz/examples/exp92-uniform-1gbit/` is the first phase's EXP-92 (18 GB, 28% of pages changed, 1 Gbit/s). `pg_basebackup` keeps the link at its limit for 153 s. Hashing before the fixes stalls in bursts for 204 s. With the fixes it takes 109 s, while the destination's disk reads at its limit and the link carries about 40 MB/s.

## Adding a lane from your own tool

A lane is a file of JSON lines (see `pgdelta/events.py`):
- `start`, with `total_bytes`;
- `progress`, about every 0.1 s, with any of `bytes_read`, `bytes_sent`, `round_trips`, `chunks_same`, `chunks_changed` and `chunks_new`;
- optionally `gauges`, with `link_MBps`, `src_read_MBps`, `dst_read_MBps` and `dst_write_MBps`;
- `done`, with `elapsed`.

`t` is seconds since the lane started. A run is a directory with `meta.json` (`title`, `lanes: [{id, label, events}]`, `finished`) and those files. pgdelta writes its lane with `--events FILE`.
