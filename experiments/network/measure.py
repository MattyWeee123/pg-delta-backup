"""Pure helpers for EXP-04: counters, speeds, digests, pgBackRest's configuration and the CSV summary.

Nothing here runs a command. The agent (agent.py) and the orchestrator
(run_exp04.py) read /proc and the filesystem through these functions, and the
tests exercise them on fixed text.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import socket
import stat
import statistics
import struct

NETDEV_FIELDS = ('rx_bytes', 'rx_packets', 'rx_errs', 'rx_drop', 'rx_fifo', 'rx_frame',
                 'rx_compressed', 'rx_multicast', 'tx_bytes', 'tx_packets', 'tx_errs',
                 'tx_drop', 'tx_fifo', 'tx_colls', 'tx_carrier', 'tx_compressed')
CPU_FIELDS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal',
              'guest', 'guest_nice')
# guest and guest_nice are already included in user and nice (proc(5)).
CPU_BUSY_FIELDS = ('user', 'nice', 'system', 'irq', 'softirq', 'steal')
# /proc/meminfo's fields kept in each snapshot (kB): what is dirty or under write-back now (EXP-11's
# durability check (a)), and the page cache (the warm-source block).
MEMINFO_FIELDS = ('Dirty', 'Writeback', 'Cached')
# /proc/diskstats: the first 11 counters after major, minor and name (Documentation/admin-guide/
# iostats.rst). Sectors are always 512 bytes there, whatever the device's own sector size.
DISKSTATS_FIELDS = ('reads', 'reads_merged', 'sectors_read', 'read_ms', 'writes', 'writes_merged',
                    'sectors_written', 'write_ms', 'in_flight', 'io_ms', 'weighted_io_ms')
DISK_COUNTERS = ('reads', 'sectors_read', 'writes', 'sectors_written')
DISK_SECTOR_BYTES = 512
# Whole disks and md arrays, not partitions or loop devices. /data is the md array of the local
# SSDs (infra/gcp/startup.sh): EXP-11's check (b) reads its writes.
DISK_DEVICE = re.compile(r'^(md\d+|nvme\d+n\d+|sd[a-z]+|vd[a-z]+)$')
DATA_ARRAY = re.compile(r'^md\d+$')
UNTHROTTLED = 'unthrottled'
# The default sweep.
METHODS = ('full', 'incremental', 'pgdelta', 'rsync_8k', 'rsync_default')
# T3 (the destination's write path): staged like pgdelta, each with one switch changed.
T3_METHODS = ('pgdelta_gapfill_seq', 'pgdelta_seq', 'pgdelta_fsync_wb', 'pgdelta_fsync_worker',
              'pgdelta_inplace')
# T10a (EXP-12): v1's source side by proxy. Backup mode held on the live source around v1's own path,
# pgdelta_direct_inplace, reading the snapshot: a cleanly stopped copy of the data directory on the live
# source's disks, never the live files. v1proxy_s1 is the same with one sender job (and 4 on the receiver).
V1PROXY_METHODS = ('v1proxy', 'v1proxy_s1')
# T12 (EXP-13): v1's path at DR latency, each with one protocol setting changed: the sender's request
# window (--window 1 or 16; v1 pins 4, today's value), the high-churn fallback at 0.5, and v1 through a
# stunnel pair (TLS 1.3, mutual authentication; run_exp04 --conditions starts and stops the pair).
T12_V1_ARMS = ('pgdelta_direct_inplace_w1', 'pgdelta_direct_inplace_w16', 'pgdelta_direct_inplace_hc',
               'pgdelta_direct_inplace_tls')
# EXP-23's follow-up: v1's path asking for BBR on its data socket (pgdelta send --congestion-control bbr). Kept
# apart from EXP-13's registered side arms, which analysis/exp13.py reads as exactly four.
CC_V1_ARMS = ('pgdelta_direct_inplace_bbr',)
# EXP-45: v1 with BBR and its pages compressed with zstd (--compress zstd). EXP-56: the same with sub-page
# deltas (--subpage).
ZSTD_V1_ARMS = ('pgdelta_direct_inplace_bbr_z', 'pgdelta_direct_inplace_bbr_zs')
# EXP-60: v1 with BBR, its keyed hash pinned (--hash): BLAKE2b, which every DR arm before EXP-60 ran because the
# VMs lacked the blake3 package (auto falls back to it), and BLAKE3. Pinned, so installing the package can't
# change what either arm measures.
HASH_V1_ARMS = ('pgdelta_direct_inplace_bbr_b2', 'pgdelta_direct_inplace_bbr_b3')
# EXP-31: v1 verifying only what it wrote (--verify-scope written), without the second pass over B.
VS_V1_ARMS = ('pgdelta_direct_inplace_vw',)
TLS_METHODS = ('pgdelta_direct_inplace_tls',)
# T8a: the sender reads the cleanly stopped source's data directory; no capture. T2 (EXP-11) adds
# v1's path at 8 jobs on each end, pgdelta_direct_inplace_j8. T10a's proxies take v1's path through
# every dispatch too (they read the snapshot, method_paths' source_data, and never stop the source).
# T12's side arms are v1's path too.
PGDELTA_DIRECT_METHODS = ('pgdelta_direct', 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_j8',
                          *V1PROXY_METHODS, *T12_V1_ARMS, *CC_V1_ARMS, *VS_V1_ARMS, *ZSTD_V1_ARMS,
                          *HASH_V1_ARMS)
# T11: pgBackRest restore --delta from a repository on the source, its backup counted as paid.
PGBACKREST_METHODS = ('pgbackrest_zst', 'pgbackrest_none', 'pgbackrest_block')
# T3 (d): incremental, combined with pg_combinebackup --copy-file-range instead of --copy. On
# dest's XFS that clones B0's unchanged blocks (EXP-10); on ext4 it copies in the kernel.
INCREMENTAL_METHODS = ('incremental', 'incremental_cfr')
COMBINE_MODES = {'incremental': '--copy', 'incremental_cfr': '--copy-file-range'}
# T2 (EXP-11), the fastest durable full copy. pg_basebackup's arms, each the full copy with these
# options after the common ones (benchmarks.backup.basebackup_command's extra). Every one runs with
# --verbose, whose stderr lines the orchestrator timestamps as they arrive (basebackup_phases),
# never with --progress, which adds a sizing pass. full's own command line gains --verbose too.
BASEBACKUP_VERBOSE = ('--verbose',)
FULL_VARIANTS = {
    'full': BASEBACKUP_VERBOSE,
    # Not durable: a diagnostic, never a baseline, and the durability checks' negative control.
    'full_nosync': (*BASEBACKUP_VERBOSE, '--no-sync'),
    # PostgreSQL 17: one syncfs(2) of dest's /data instead of an fsync per file and directory.
    'full_syncfs': (*BASEBACKUP_VERBOSE, '--sync-method=syncfs'),
    # full on a host-tuned dest (HOST_TUNED_METHODS).
    'full_wb': BASEBACKUP_VERBOSE,
}
# Arms that change kernel settings on dest for their run: set before the cache drop, reset in
# run_method's finally. full_wb has the kernel write back while the stream arrives. Linux refuses
# 0 written to vm.dirty_background_bytes, so the reset writes vm.dirty_background_ratio back to its
# recorded default, which returns the bytes setting to 0.
DIRTY_BACKGROUND_RATIO = 'vm.dirty_background_ratio'
DIRTY_BACKGROUND_BYTES = 'vm.dirty_background_bytes'
HOST_TUNED_METHODS = {'full_wb': {DIRTY_BACKGROUND_BYTES: 64 << 20}}
# Not durable when the clock stops.
NON_DURABLE_METHODS = ('full_nosync',)
# The released parallel copy: pg_probackup catchup -b FULL from the live source over SSH, on dest,
# then a timed sync -f of the output in the same phase (its own flush is incomplete).
PROBACKUP_VARIANTS = {'probackup8': {'jobs': 8}, 'probackup4': {'jobs': 4}}
PROBACKUP_METHODS = tuple(PROBACKUP_VARIANTS)
# The bound: a range-parallel copier built in the harness (experiments/network/parcopy.py), reading
# the cleanly stopped source as v1 does, over 8 or 4 TCP streams. T12 (EXP-13) adds the same bound
# over 16 streams, for the one condition where eight default windows can't fill the link.
PARCOPY_VARIANTS = {'parcopy8': {'streams': 8}, 'parcopy4': {'streams': 4}, 'parcopy16': {'streams': 16}}
PARCOPY_METHODS = tuple(PARCOPY_VARIANTS)
# The arms EXP-11 adds. Selecting any of them makes a run T2's: it needs CRC32C manifests, and each
# trial takes the reference digests and runs the probes (run_exp04.Orchestrator.run_trial).
T2_METHODS = ('full_nosync', 'full_syncfs', 'full_wb', *PROBACKUP_METHODS, 'parcopy8', 'parcopy4',
              'pgdelta_direct_inplace_j8')
# The arms EXP-13 (T12) adds; its runs need --conditions (the emulated WAN) and CRC32C manifests.
T12_METHODS = (*T12_V1_ARMS, *CC_V1_ARMS, *ZSTD_V1_ARMS, *HASH_V1_ARMS, 'parcopy16')
# T10a (EXP-12), the source's slowdown under a fixed-rate load (run_exp04 --load). none: an empty timed
# phase of --none-window-s, the null's own windows; capture: the staged capture alone, a local
# pg_basebackup of S on the source; bracket: pg_backup_start and pg_backup_stop in one session.
T10A_ARMS = ('none', 'capture', 'bracket', *V1PROXY_METHODS)
# The controls (--controls), run at the blocks' starts and ends and never in a repeat: pause2 stops
# pgbench for 2.0 s (agent.py load-pause, the positive control), burn4 runs 4 busy processes for 2.0 s
# (agent.py burn, the CPU reference).
CONTROL_METHODS = ('pause2', 'burn4')
CONTROLS = {'pause2': {'agent': 'load-pause', 'seconds': 2.0},
            'burn4': {'agent': 'burn', 'procs': 4, 'seconds': 2.0}}
T10A_METHODS = (*T10A_ARMS, *CONTROL_METHODS)
# Variants run only when named in --methods.
OPTIONAL_METHODS = ('pgdelta_1thread', *T3_METHODS, 'incremental_cfr', *PGDELTA_DIRECT_METHODS,
                    *PGBACKREST_METHODS, 'full_nosync', 'full_syncfs', 'full_wb', *PROBACKUP_METHODS,
                    *PARCOPY_METHODS, *(method for method in T10A_ARMS if method not in V1PROXY_METHODS),
                    *CONTROL_METHODS)
ALL_METHODS = METHODS + OPTIONAL_METHODS
# pgdelta methods that capture S on the source first, with the receiver started beside the capture.
PGDELTA_STAGED_METHODS = ('pgdelta', 'pgdelta_1thread', *T3_METHODS)
PGDELTA_METHODS = PGDELTA_STAGED_METHODS + PGDELTA_DIRECT_METHODS
RSYNC_METHODS = ('rsync_8k', 'rsync_default')
STAGED_METHODS = PGDELTA_STAGED_METHODS + RSYNC_METHODS
# pgdelta refreshing B itself (--in-place) rather than writing a new copy O.
IN_PLACE_METHODS = ('pgdelta_inplace', 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_j8', *V1PROXY_METHODS,
                    *T12_V1_ARMS, *CC_V1_ARMS, *VS_V1_ARMS, *ZSTD_V1_ARMS, *HASH_V1_ARMS)
# Methods that patch B, a copy of B0 made before the cache drop and not timed: in a real
# refresh B already exists. The copy keeps B0 itself unchanged for the next method.
BASIS_COPY_METHODS = IN_PLACE_METHODS + PGBACKREST_METHODS
# Methods that read the source cluster stopped cleanly before the cache drop (untimed), and
# restart it afterwards, whatever happened. Never the proxies: they read the snapshot while the live
# source runs under its load (EXP-12).
SOURCE_STOP_METHODS = tuple(method for method in PGDELTA_DIRECT_METHODS
                            if method not in V1PROXY_METHODS) + PARCOPY_METHODS
# Methods that write a whole new copy: durability check (b) applies to them.
FULL_COPY_METHODS = (*FULL_VARIANTS, *PROBACKUP_METHODS, *PARCOPY_METHODS)
# EXP-12's check (b): the full copies and staged pgdelta's new O, which copy_file_range of B0's unchanged
# blocks writes in full on ext4. FULL_COPY_METHODS itself stays the full copies, as EXP-11's analysis reads it.
DISK_WRITE_CHECK_METHODS = (*FULL_COPY_METHODS, 'pgdelta')
# EXP-12: the live outputs the load's oracle checks (G4): started, recovering from their backup_label, then
# checked for the invariant, the consistent cut, coverage, the row counts, amcheck and the schema.
G4_METHODS = ('full', 'pgdelta')
# EXP-12: the arms with an output whose own tool aborting it (its timed command exiting non-zero or timing out) makes
# the run wrong, not unverified: the copy doesn't exist, so it doesn't start ("Wrong: ... or the output doesn't
# start"), and R3 and L5 read it. The live outputs, the capture's S (which pg_verifybackup would fail), the proxies'
# B and v1 itself's. Only the harness's own failures (SSH, a timed-out check, a missing binary) are unverified.
TOOL_ABORT_WRONG_METHODS = (*G4_METHODS, 'capture', *V1PROXY_METHODS, 'pgdelta_direct_inplace')
# EXP-12: arms that write nothing on dest, so durability check (a) says nothing about them.
NO_DEST_WRITE_METHODS = ('none', 'capture', 'bracket', *CONTROL_METHODS)
# Methods that move their bytes over several TCP streams: their time above link time is taken
# against the 8-stream link test, the others' against the single stream's.
MULTI_STREAM_METHODS = PROBACKUP_METHODS + PARCOPY_METHODS
# Methods whose verification starts the output on dest (on run_exp04.OUTPUT_PG_PORT). Every
# pg_basebackup arm of T2 is started too, recovering from its backup_label.
OUTPUT_START_METHODS = (PGDELTA_DIRECT_METHODS + PGBACKREST_METHODS + tuple(FULL_VARIANTS)
                        + PROBACKUP_METHODS + PARCOPY_METHODS)
# EXP-11's tiers (its Arms table). released: an operator can adopt it today through a released
# tool's documented options; host_tuned: a released arm with one kernel setting changed on dest;
# bound: built here from standard primitives. B_rel takes omni_today and released arms, B_fast adds
# bound, B_tuned adds host_tuned; diagnostic arms are never a baseline.
TIERS = {'full': 'omni_today', 'full_nosync': 'diagnostic', 'full_syncfs': 'released',
         'full_wb': 'host_tuned', 'probackup8': 'released', 'probackup4': 'released',
         'parcopy8': 'bound', 'parcopy4': 'bound', 'pgdelta_direct_inplace': 'v1',
         'pgdelta_direct_inplace_j8': 'v1_8_jobs',
         # EXP-12's (its Arms table); none of these is in any of EXP-11's envelopes.
         'none': 'no_backup', 'capture': 'staged_capture', 'pgdelta': 'staged_v0', 'bracket': 'backup_mode',
         'v1proxy': 'v1_proxy', 'v1proxy_s1': 'v1_proxy_one_core', 'pause2': 'positive_control',
         'burn4': 'cpu_reference',
         # EXP-13's (its Arms table): the side arms are protocol readings beside v1, never v1's place in a
         # row; parcopy16 is the bound with 16 streams, a B_fast candidate where it runs.
         'parcopy16': 'bound', 'pgdelta_direct_inplace_w1': 'protocol', 'pgdelta_direct_inplace_w16': 'protocol',
         'pgdelta_direct_inplace_hc': 'protocol', 'pgdelta_direct_inplace_tls': 'protocol_tls',
         # EXP-23's follow-up: v1 with BBR on its data socket.
         'pgdelta_direct_inplace_bbr': 'protocol',
         # EXP-45's: the same with zstd on the pages sent.
         'pgdelta_direct_inplace_bbr_z': 'protocol',
         # EXP-56's: and only the changed 512-byte blocks of each page.
         'pgdelta_direct_inplace_bbr_zs': 'protocol',
         # EXP-60's: v1 with BBR, its hash pinned to BLAKE2b or BLAKE3.
         'pgdelta_direct_inplace_bbr_b2': 'protocol', 'pgdelta_direct_inplace_bbr_b3': 'protocol'}
# The engine each pgdelta method runs, named by its flags, so that no change of pgdelta's
# defaults can change what a method measures (as compare_methods pins EXP-02's):
# - pgdelta: EXP-06's choice, which is also the command line's default on these 8-vCPU
#   VMs: 4 worker processes on each end, copy-then-patch, pipelined verification.
# - pgdelta_1thread: the single-threaded engine EXP-02 measured and EXP-04's smoke runs ran
#   (EXP-06's baseline): in-process hashing, gap-fill, sequential verification.
# --jobs goes to both ends; --rebuild and --verify only to the receiver, which builds O.
# prefetch_basis is the receiver's --prefetch-basis: reading B0 into the page cache while it
# waits for the sender (decided October 22, 2026; see EXP-04's decision note on pgdelta's
# receiver). It is on for both methods: it is a receiver feature that doesn't depend on how
# either end hashes, and with it off pgdelta_1thread would read B0 cold where pgdelta doesn't.
# The orchestrator checks that both VMs' pgdelta has these options before a run starts.
#
# T3's and T8a's methods (decided October 23, 2026) keep EXP-06's 4 jobs and the prefetch,
# and change one thing each. They pin the receiver's --fsync too, so that no change of its
# default can move them; pgdelta and pgdelta_1thread are left as they were, so they still run
# on a pgdelta without --fsync, and the mode their statistics report is recorded and checked.
# - fsync: the receiver's --fsync, where O's files are flushed: in the main loop (today's
#   write path), by early write-back, or on a worker thread.
# - in_place: the receiver's --in-place: B itself is refreshed, with no --output and no
#   --rebuild (a copy of B0 made before the cache drop stands in for B, untimed).
# - source_kind: the sender's --source-kind: 'datadir' reads the cleanly stopped source's data
#   directory instead of a capture S.
#
# T12 (EXP-13) pins the sender's request window on v1's path (--window, added for it): 4, today's value
# (pgdelta/sender.py's DEFAULT_WINDOW), so that no change of pgdelta's default can move v1 or its 8-job
# variant, and 1 or 16 on its side arms. Each side arm changes one thing:
# - window: the sender's --window, how many files it hashes ahead while it waits for leaf digests.
# - high_churn: the sender's --high-churn-node-fraction (the fallback: a file's mismatched nodes sent whole).
# - transport: 'tls' runs v1 through a stunnel pair: the receiver listens on localhost behind dest's
#   stunnel, and the sender connects to the source's (run_exp04's pgdelta_*_argv); no flag changes.
PGDELTA_ENGINES = {
    'pgdelta': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'pipelined', 'prefetch_basis': 'on'},
    'pgdelta_1thread': {'jobs': 1, 'rebuild': 'gap-fill', 'verify': 'sequential', 'prefetch_basis': 'on'},
    # T3 (a): gap-fill against copy-then-patch, both with sequential verification.
    'pgdelta_gapfill_seq': {'jobs': 4, 'rebuild': 'gap-fill', 'verify': 'sequential',
                            'prefetch_basis': 'on', 'fsync': 'main'},
    # T3 (a) and (c): copy-then-patch with sequential verification (against pgdelta's pipelined).
    'pgdelta_seq': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'sequential', 'prefetch_basis': 'on',
                    'fsync': 'main'},
    # T3 (b): pgdelta with the fsync moved out of the main loop.
    'pgdelta_fsync_wb': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'pipelined', 'prefetch_basis': 'on',
                         'fsync': 'writeback'},
    'pgdelta_fsync_worker': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'pipelined',
                             'prefetch_basis': 'on', 'fsync': 'worker'},
    # T3 (e): B patched in place, the rest as pgdelta.
    'pgdelta_inplace': {'jobs': 4, 'rebuild': None, 'verify': 'pipelined', 'prefetch_basis': 'on',
                        'fsync': 'main', 'in_place': True},
    # T8a: today's write path (a new copy O) and in place, both reading the stopped source directly.
    'pgdelta_direct': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'pipelined', 'prefetch_basis': 'on',
                       'fsync': 'main', 'source_kind': 'datadir'},
    'pgdelta_direct_inplace': {'jobs': 4, 'rebuild': None, 'verify': 'pipelined', 'prefetch_basis': 'on',
                               'fsync': 'main', 'in_place': True, 'source_kind': 'datadir', 'window': 4},
    # T2 (EXP-11): v1's path with 8 jobs on each end, the rest as pgdelta_direct_inplace.
    'pgdelta_direct_inplace_j8': {'jobs': 8, 'rebuild': None, 'verify': 'pipelined', 'prefetch_basis': 'on',
                                  'fsync': 'main', 'in_place': True, 'source_kind': 'datadir', 'window': 4},
    # T10a (EXP-12): v1's engine, pgdelta_direct_inplace's, reading the snapshot; and the same with one sender
    # job (send_jobs: the sender's --jobs, where it differs from the receiver's).
    'v1proxy': {'jobs': 4, 'rebuild': None, 'verify': 'pipelined', 'prefetch_basis': 'on', 'fsync': 'main',
                'in_place': True, 'source_kind': 'datadir', 'window': 4},
    'v1proxy_s1': {'jobs': 4, 'send_jobs': 1, 'rebuild': None, 'verify': 'pipelined', 'prefetch_basis': 'on',
                   'fsync': 'main', 'in_place': True, 'source_kind': 'datadir', 'window': 4},
}
# T12 (EXP-13): v1's engine with one setting changed each (T12_V1_ARMS).
_V1_ENGINE = PGDELTA_ENGINES['pgdelta_direct_inplace']
PGDELTA_ENGINES.update({
    'pgdelta_direct_inplace_w1': {**_V1_ENGINE, 'window': 1},
    'pgdelta_direct_inplace_w16': {**_V1_ENGINE, 'window': 16},
    'pgdelta_direct_inplace_hc': {**_V1_ENGINE, 'high_churn': 0.5},
    'pgdelta_direct_inplace_tls': {**_V1_ENGINE, 'transport': 'tls'},
    # EXP-23's follow-up: v1's engine asking for BBR on its data socket.
    'pgdelta_direct_inplace_bbr': {**_V1_ENGINE, 'congestion_control': 'bbr'},
    # EXP-45: and zstd on the pages it sends.
    'pgdelta_direct_inplace_bbr_z': {**_V1_ENGINE, 'congestion_control': 'bbr', 'compress': 'zstd'},
    'pgdelta_direct_inplace_bbr_zs': {**_V1_ENGINE, 'congestion_control': 'bbr', 'compress': 'zstd', 'subpage': True},
    # EXP-60: v1 with BBR, its keyed hash pinned.
    'pgdelta_direct_inplace_bbr_b2': {**_V1_ENGINE, 'congestion_control': 'bbr', 'hash': 'blake2b'},
    'pgdelta_direct_inplace_bbr_b3': {**_V1_ENGINE, 'congestion_control': 'bbr', 'hash': 'blake3'},
    # EXP-31: v1's engine verifying only the pages it wrote.
    'pgdelta_direct_inplace_vw': {**_V1_ENGINE, 'verify_scope': 'written'},
})
del _V1_ENGINE


def send_jobs(engine: dict) -> int:
    """The sender's --jobs: the engine's send_jobs where it pins one, its jobs otherwise."""
    return engine.get('send_jobs', engine['jobs'])


def pgdelta_engine_args(engine: dict) -> dict:
    """{'send': argv, 'receive': argv}: the hashing and rebuilding flags of `engine` on each end.

    Each end takes its own --jobs: the receiver the engine's jobs, the sender its send_jobs (send_jobs()).
    """
    rebuild = ('--rebuild', engine['rebuild']) if engine.get('rebuild') else ()
    return {'send': ('--jobs', str(send_jobs(engine))),
            'receive': ('--jobs', str(engine['jobs']), *rebuild, '--verify', engine['verify'])}


def pgdelta_args(engine: dict) -> dict:
    """{'send': argv, 'receive': argv}: every flag `engine` pins, the receiver's prefetch included."""
    args = pgdelta_engine_args(engine)
    receive = (*args['receive'], '--prefetch-basis', engine['prefetch_basis'])
    if engine.get('fsync'):
        receive += ('--fsync', engine['fsync'])
    if engine.get('in_place'):
        receive += ('--in-place',)
    if engine.get('verify_scope'):
        receive += ('--verify-scope', engine['verify_scope'])
    send = args['send']
    if engine.get('source_kind'):
        send += ('--source-kind', engine['source_kind'])
    if engine.get('window') is not None:
        send += ('--window', str(engine['window']))
    if engine.get('high_churn') is not None:
        send += ('--high-churn-node-fraction', f"{engine['high_churn']:g}")
    if engine.get('congestion_control'):
        send += ('--congestion-control', engine['congestion_control'])
    if engine.get('compress'):
        send += ('--compress', engine['compress'])
    if engine.get('subpage'):
        send += ('--subpage',)
    if engine.get('hash'):
        send += ('--hash', engine['hash'])
    return {'send': send, 'receive': receive}


def pgdelta_switches(engine: dict) -> dict:
    """The `switches` pgdelta's statistics report when `engine` is in effect.

    An in-place engine pins no --rebuild, so the receiver's rebuild is left out
    (pgdelta_switches_match compares only the switches named here).
    """
    def backend(jobs):
        return 'process' if jobs > 1 else 'serial'

    verify = engine['verify'] if engine['jobs'] > 1 else 'sequential'  # no workers: always sequential
    receiver = {'jobs': engine['jobs'], 'hash_backend': backend(engine['jobs']), 'rebuild': engine.get('rebuild'),
                'verify': verify}
    if receiver['rebuild'] is None:
        del receiver['rebuild']
    sender = send_jobs(engine)
    return {'sender': {'jobs': sender, 'hash_backend': backend(sender)}, 'receiver': receiver}


def pgdelta_switches_match(expected: dict, reported) -> bool:
    """Whether the reported switches include every switch `expected` names, with its value.

    Switches a newer pgdelta reports beyond these (its fsync mode, say) don't count against it.
    """
    if not isinstance(reported, dict):
        return False
    return all(isinstance(reported.get(side), dict)
               and all(reported[side].get(key) == value for key, value in values.items())
               for side, values in expected.items())


PGDELTA_ARGS = {method: pgdelta_args(engine) for method, engine in PGDELTA_ENGINES.items()}
# Options each end's `pgdelta <command> --help` must list for PGDELTA_ARGS to work.
PGDELTA_OPTIONS = {command: tuple(arg for arg in args if arg.startswith('--'))
                   for command, args in PGDELTA_ARGS['pgdelta'].items()}
# The module that lists what pgdelta's datadir mode reads (pgdelta_direct*'s verification):
# `python -m pgdelta.datadir list DIR --pg-controldata PATH` prints the selected relative paths.
PGDELTA_DATADIR_MODULE = 'pgdelta.datadir'


def pgdelta_required_options(methods) -> dict:
    """{'send': options, 'receive': options}: what `pgdelta <command> --help` must list for `methods`.

    The union of the flags every selected pgdelta method passes, so a run of the default
    methods asks for exactly PGDELTA_OPTIONS and a run without pgdelta asks for nothing.
    """
    required: dict[str, list] = {}
    for method in methods:
        for command, args in PGDELTA_ARGS.get(method, {}).items():
            options = required.setdefault(command, [])
            options += [arg for arg in args if arg.startswith('--') and arg not in options]
    return {command: tuple(options) for command, options in required.items()}


def pgdelta_modes(engine: dict) -> dict:
    """The read and write modes pgdelta's statistics should report for `engine`.

    pgdelta and pgdelta_1thread pin no mode: they expect today's write path (a new copy,
    fsync in the main loop) and a capture S.
    """
    return {'in_place': bool(engine.get('in_place')), 'fsync_mode': engine.get('fsync') or 'main',
            'source_kind': engine.get('source_kind') or 'backup'}


def pgdelta_reported(stats: dict, *keys, accept=None):
    """The first of `keys` that pgdelta's statistics report, wherever this version keeps it.

    The mode keys (in_place, fsync_mode, source_kind, fsync_wait_seconds) may be at the top
    level, in the receiver's part, in the switches or with the timings; None if absent.
    With `accept`, only a value it accepts counts (the timings' `fsync` is seconds, not a mode).
    """
    receiver = stats.get('receiver') if isinstance(stats.get('receiver'), dict) else {}
    places = (stats, receiver, _get(stats, 'switches', 'receiver'), _get(stats, 'switches', 'sender'),
              stats.get('timings_seconds'), receiver.get('timings_seconds'))
    for key in keys:
        for place in places:
            if isinstance(place, dict) and place.get(key) is not None \
                    and (accept is None or accept(place[key])):
                return place[key]
    return None


# The receiver's --fsync choices. pgdelta's receiver reports `fsync` (the mode asked for) and
# `fsync_effective` (the one in effect: writeback falls back to main where sync_file_range is
# missing or refused); the harness's contract named `fsync_mode`, which counts too.
FSYNC_MODES = ('main', 'writeback', 'worker')


def _fsync_mode(value) -> bool:
    return value in FSYNC_MODES


def pgdelta_modes_report(stats: dict, engine: dict) -> dict:
    """The modes pgdelta reported, the ones `engine` expects, and whether they agree.

    The fsync mode compared is the one in effect, so a writeback run that fell back to main
    fails modes_ok; the one asked for is kept as fsync_requested. modes_ok is None when this
    pgdelta reports none of the modes (before the modes existed).
    """
    expected = pgdelta_modes(engine)
    reported = {key: pgdelta_reported(stats, key) for key in expected if key != 'fsync_mode'}
    reported['fsync_mode'] = pgdelta_reported(stats, 'fsync_effective', 'fsync_mode', 'fsync',
                                              accept=_fsync_mode)
    reported = {key: reported[key] for key in expected}
    known = {key: value for key, value in reported.items() if value is not None}
    return {'modes': reported, 'modes_expected': expected,
            'modes_ok': None if not known else all(expected[key] == value for key, value in known.items()),
            'fsync_requested': pgdelta_reported(stats, 'fsync', 'fsync_mode', accept=_fsync_mode),
            'fsync_wait_seconds': pgdelta_reported(stats, 'fsync_wait_seconds', 'fsync_wait')}
# What the per-process CPU and peak-RSS columns cover. Every method's capture
# is served by a walsender, a PostgreSQL server process on the source that no
# per-process figure sees, and the methods launch different clients, so these
# columns are not comparable across methods; the VM-wide CPU columns are.
# Peak RSS is GNU time's %M: the peak of the largest single process in each
# tool's tree (getrusage(2) gives the largest child's ru_maxrss, not a sum), so
# tools that run several processes at once are reported by their largest one.
PEAK_RSS_SCOPE = ("GNU time's %M: the peak RSS of the largest single process in each tool's "
                  "process tree, not the tree's combined footprint. pg_basebackup -X stream "
                  "forks a WAL-streaming child that runs beside its main process, and the "
                  "rsync daemon forks a per-connection generator that forks the receiver; "
                  "those concurrent processes are not summed. pgdelta at --jobs 4 is not one "
                  "process: each end starts 4 hashing worker processes with spawn, as its own "
                  "children, beside its main process, and only the largest of the five is "
                  "reported. pgdelta_1thread (--jobs 1, no workers), pg_combinebackup, cp, sync "
                  "and the sending rsync client are single processes.")
PROC_SCOPE = {
    'full': 'dest: pg_basebackup; source: none (its walsender is not per-process). Peak RSS: '
            "largest single process (pg_basebackup's WAL-streaming child not added)",
    'incremental': 'dest: pg_basebackup, pg_combinebackup; source: none (walsender not '
                   "per-process). Peak RSS: largest single process (pg_basebackup's "
                   'WAL-streaming child not added)',
    'pgdelta': 'source: local pg_basebackup client, pgdelta send (capture walsender not per-process); '
               'dest: pgdelta receive. At --jobs 4 pgdelta is not one process on either end: each starts '
               '4 worker processes with spawn, as its own children, which it reaps when its pool closes, so CPU '
               "(wait4's rusage, and GNU time's user and system time) includes them. Peak RSS: GNU "
               "time's %M, the largest single process (pgdelta and its 4 workers not summed; "
               "pg_basebackup's WAL-streaming child not added)",
    'rsync_8k': 'source: local pg_basebackup client, rsync (capture walsender not '
                'per-process); dest: cp, sync, rsync daemon with its per-connection '
                'generator and receiver. Peak RSS: largest single process (the daemon, '
                "generator and receiver are not summed, nor pg_basebackup's WAL streamer)",
}
PROC_SCOPE['pgdelta_1thread'] = ('source: local pg_basebackup client, pgdelta send (capture walsender '
                                  'not per-process); dest: pgdelta receive. --jobs 1 starts no worker '
                                  'processes, so each pgdelta is one process. Peak RSS: largest single '
                                  "process (pg_basebackup's WAL-streaming child not added)")
PROC_SCOPE['rsync_default'] = PROC_SCOPE['rsync_8k']
PROC_SCOPE['incremental_cfr'] = PROC_SCOPE['incremental']
for _method in T3_METHODS:
    PROC_SCOPE[_method] = PROC_SCOPE['pgdelta']
for _method in PGDELTA_DIRECT_METHODS:
    _jobs = PGDELTA_ENGINES[_method]['jobs']
    PROC_SCOPE[_method] = ('source: pgdelta send, reading the stopped data directory (no capture, no '
                           f'walsender); dest: pgdelta receive. At --jobs {_jobs} each starts {_jobs} worker '
                           'processes with spawn, as its own children, so CPU includes them. Peak RSS: largest '
                           f'single process (pgdelta and its {_jobs} workers not summed)')
for _method in PGBACKREST_METHODS:
    PROC_SCOPE[_method] = ('dest: pgbackrest restore, with its 4 local processes and their SSH clients '
                           '(its children, reaped, so CPU includes them); source: none (no walsender '
                           'serves a restore, and the remote pgbackrest processes SSH starts there to read '
                           'the repository are not our children; VM-wide CPU covers them). Peak RSS: '
                           'largest single process (pgbackrest and its local processes not summed)')
for _method in FULL_VARIANTS:
    PROC_SCOPE[_method] = PROC_SCOPE['full']
for _method in PROBACKUP_METHODS:
    _jobs = PROBACKUP_VARIANTS[_method]['jobs']
    PROC_SCOPE[_method] = (f'dest: pg_probackup catchup with its {_jobs} threads and the SSH clients it '
                           'starts (its children, reaped, so CPU includes them), then sync -f; source: none '
                           '(the walsender that streams its WAL and the remote pg_probackup agents SSH starts '
                           'there are not our children; VM-wide CPU covers them). Peak RSS: largest single '
                           'process (pg_probackup and its SSH clients not summed)')
for _method in PARCOPY_METHODS:
    _streams = PARCOPY_VARIANTS[_method]['streams']
    PROC_SCOPE[_method] = ('source: parcopy send, reading the stopped data directory (no capture, no '
                           f'walsender); dest: parcopy receive. Each end runs its {_streams} streams as threads '
                           'of one process (the receiver its fsync threads too), so CPU includes them. Peak RSS: '
                           'largest single process (one per end)')
# T10a (EXP-12). The load's pgbench and its backends run beside every arm on the source; no per-process column
# covers them (the sampler's groups do), and the VM-wide CPU columns include them.
PROC_SCOPE['none'] = ('nothing runs: an empty timed phase with the counter reads every arm makes (no command, no '
                      'walsender). Peak RSS: none, there is no largest single process')
PROC_SCOPE['capture'] = ("source: agent.py run, then the local pg_basebackup S (its WAL-streaming child not added; "
                         'its walsender is not per-process). Peak RSS: the largest single process')
PROC_SCOPE['bracket'] = ('source: agent.py run, then psql (pg_backup_start and pg_backup_stop in one session); the '
                         "backend that runs them, and the checkpointer, are the server's, not per-process (no "
                         'walsender). Peak RSS: the largest single process, psql')
for _method in V1PROXY_METHODS:
    _engine = PGDELTA_ENGINES[_method]
    PROC_SCOPE[_method] = (f'source: pgdelta send at --jobs {send_jobs(_engine)}, reading the stopped snapshot, '
                           + (f'which starts {send_jobs(_engine)} worker processes with spawn, as its own children, '
                              if send_jobs(_engine) > 1 else 'which hashes in its own process, ')
                           + 'so CPU includes them; source_cluster.py backup-hold over SSH, holding backup mode in '
                           "one psql session (its backend and the checkpointer are the server's, not per-process; no "
                           f'walsender); dest: pgdelta receive at --jobs {_engine["jobs"]}, with its workers. Peak '
                           'RSS: largest single process')
PROC_SCOPE['pause2'] = ("source: agent.py load-pause, which sends pgbench SIGSTOP and SIGCONT (pgbench's own CPU is "
                        'not per-process here; no walsender). Peak RSS: none measured, no largest single process')
PROC_SCOPE['burn4'] = ('source: agent.py burn, 4 forked busy processes at nice 0 (its children, reaped; their CPU is '
                       'in the result, not in the per-process columns; no walsender). Peak RSS: none measured, no '
                       'largest single process')
# T12 (EXP-13): v1 through the stunnel pair. Both stunnels start before the clock and stop after it (agent.py
# stunnel), so neither is a child of a timed command.
PROC_SCOPE['pgdelta_direct_inplace_tls'] = (PROC_SCOPE['pgdelta_direct_inplace'] + '. stunnel on each end is not '
                                            'per-process (started before the clock by agent.py): the VM-wide CPU '
                                            "columns include it, and the record's stunnel holds each one's own CPU")
del _method, _jobs, _streams, _engine
# rsync's copy of B0 (prep, on dest) and its capture of S (on source) touch
# different machines and the link is idle during both, so they run at the same
# time in one timed phase, as a real cp-then-rsync refresh would.
CONCURRENT_PHASE = 'prep_capture'
# pgdelta's counterpart: its receiver starts on dest, and prefetches B0 while it
# waits for the sender, at the same time as the capture of S on source.
RECEIVER_PHASE = 'receiver_capture'
# pgdelta_direct*: no capture to start beside, so the receiver's start and the transfer are one
# timed phase, one counter window, with no untimed gap between them in which the receiver would
# prefetch B0 off the clock. Its steps, one after the other: receiver_start (from the launch until
# the receiver listens), then transfer (the sender, until both ends exit).
DIRECT_PHASE = 'receiver_transfer'
DIRECT_RECEIVER_STEP = 'receiver_start'
# pgbackrest_*: the one timed phase, on dest.
RESTORE_PHASE = 'restore'
# T2's arms are one timed phase each, one counter window, so no write-back can happen off the
# clock between two phases. The pg_basebackup arms: 'transfer', as full always was. probackup*:
# catchup_sync, whose steps are pg_probackup catchup and then sync -f of the output. parcopy*:
# pgdelta_direct*'s frame, receiver_start and then transfer.
FULL_PHASE = 'transfer'
PROBACKUP_PHASE = 'catchup_sync'
PROBACKUP_STEPS = ('catchup', 'sync')
PARCOPY_PHASE = DIRECT_PHASE
# T10a (EXP-12): one timed phase per arm, one counter window, staged pgdelta keeping its two. none: an empty
# phase; capture: the local pg_basebackup of S; bracket: backup mode alone; the controls: the agent's command;
# the proxies: one phase in four steps, backup_start (backup-hold's pg_backup_start, until it prints the start
# LSN), receiver_start, transfer, then backup_stop (pg_backup_stop in the same session, until the stop LSN).
NONE_PHASE = 'none'
CAPTURE_PHASE = 'capture'
BRACKET_PHASE = 'bracket'
CONTROL_PHASE = 'control'
V1PROXY_PHASE = 'backup_transfer'
V1PROXY_STEPS = ('backup_start', 'receiver_start', 'transfer', 'backup_stop')
SPEED_ORDERS = ('shuffle', 'alternate', 'ascending')
DIGEST_CHUNK = 1 << 20
DEFAULT_WAL_SEGMENT = 16 << 20
WAL_SEGMENT_NAME = re.compile(r'^[0-9A-F]{24}$')
# GNU time (Debian package `time`) reports the peak RSS of the command it runs.
# A child's own ru_maxrss is useless here: Linux carries the parent's high-water
# mark into a forked child, so a tool launched from the Python orchestrator or
# agent would report the launcher's memory. GNU time is a small C program, so
# the floor it passes on is about 1 MB (measured on pgd-dest from a 420 MB
# parent: /bin/true 1144 KiB under GNU time, 420644 KiB from wait4 directly).
GNU_TIME_PREFIX = 'EXP04-TIME'
GNU_TIME_FORMAT = (GNU_TIME_PREFIX + ' maxrss_kb=%M wall_seconds=%e user_seconds=%U '
                   'system_seconds=%S exit=%x')


# --- /proc parsing ----------------------------------------------------------

def parse_netdev(text: str) -> dict[str, dict[str, int]]:
    """Interface -> counters from /proc/net/dev (two header lines, then one per interface)."""
    interfaces = {}
    for line in text.splitlines():
        name, colon, rest = line.partition(':')
        if not colon or '|' in line:
            continue
        values = rest.split()
        if len(values) < len(NETDEV_FIELDS):
            raise ValueError(f'short /proc/net/dev line: {line!r}')
        interfaces[name.strip()] = {field: int(value)
                                    for field, value in zip(NETDEV_FIELDS, values)}
    if not interfaces:
        raise ValueError('no interfaces in /proc/net/dev text')
    return interfaces


def parse_proc_stat(text: str) -> dict[str, int]:
    """Aggregate CPU ticks (USER_HZ units) from the first 'cpu ' line of /proc/stat."""
    for line in text.splitlines():
        if line.startswith('cpu '):
            values = [int(value) for value in line.split()[1:]]
            # Older kernels print fewer columns; missing ones are zero.
            values += [0] * (len(CPU_FIELDS) - len(values))
            return dict(zip(CPU_FIELDS, values))
    raise ValueError("no aggregate 'cpu ' line in /proc/stat text")


def parse_meminfo(text: str) -> dict[str, int]:
    """Field -> value from /proc/meminfo, in kB (KiB) as the kernel prints them (counts stay counts)."""
    values = {}
    for line in text.splitlines():
        name, colon, rest = line.partition(':')
        words = rest.split()
        if colon and words and words[0].isdigit():
            values[name.strip()] = int(words[0])
    if not values:
        raise ValueError('no fields in /proc/meminfo text')
    return values


def parse_diskstats(text: str) -> dict[str, dict[str, int]]:
    """Device -> its first 11 counters from /proc/diskstats (major, minor, name, then the counters)."""
    devices = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3 + len(DISKSTATS_FIELDS):
            continue
        devices[fields[2]] = {field: int(value)
                              for field, value in zip(DISKSTATS_FIELDS, fields[3:3 + len(DISKSTATS_FIELDS)])}
    if not devices:
        raise ValueError('no devices in /proc/diskstats text')
    return devices


def counter_delta(before: dict, after: dict, fields) -> dict[str, int]:
    delta = {}
    for field in fields:
        change = after[field] - before[field]
        if change < 0:
            raise ValueError(f'counter {field} went backwards ({before[field]} -> {after[field]})')
        delta[field] = change
    return delta


def memory_delta(before: dict, after: dict) -> dict:
    """Dirty and Writeback (dirty_writeback_bytes: their sum) at each end of a window, in bytes."""
    def total(values):
        return (values['Dirty'] + values['Writeback']) * 1024
    return {'dirty_bytes_before': before['Dirty'] * 1024, 'writeback_bytes_before': before['Writeback'] * 1024,
            'dirty_bytes_after': after['Dirty'] * 1024, 'writeback_bytes_after': after['Writeback'] * 1024,
            'dirty_writeback_bytes_before': total(before), 'dirty_writeback_bytes_after': total(after),
            'dirty_writeback_increase_bytes': total(after) - total(before)}


def disk_delta(before: dict, after: dict) -> dict[str, dict[str, int]]:
    """Bytes read and written, and requests, per device present in both snapshots."""
    delta = {}
    for device in sorted(before.keys() & after.keys()):
        counts = counter_delta(before[device], after[device], DISK_COUNTERS)
        delta[device] = {'read_bytes': counts['sectors_read'] * DISK_SECTOR_BYTES,
                         'write_bytes': counts['sectors_written'] * DISK_SECTOR_BYTES,
                         'reads': counts['reads'], 'writes': counts['writes']}
    return delta


def snapshot_delta(before: dict, after: dict) -> dict:
    """Net bytes and packets and CPU seconds between two snapshot() results of one host.

    With meminfo and diskstats in both (snapshots from before EXP-11 have neither), also the
    window's Dirty and Writeback at each end (`memory`) and each disk's bytes (`disk`). T12 (EXP-13)
    snapshots also carry the TCP counters nstat reads (`nstat`) and the emulated WAN's qdisc counters
    (`qdisc`), whose deltas are kept under the same keys.
    """
    if before['iface'] != after['iface']:
        raise ValueError('snapshots are of different interfaces')
    net = counter_delta(before['net'], after['net'],
                        ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets'))
    ticks = counter_delta(before['cpu_ticks'], after['cpu_ticks'], CPU_FIELDS)
    hz = after['clk_tck']
    cpu = {f'{field}_seconds': round(value / hz, 3) for field, value in ticks.items()}
    cpu['busy_seconds'] = round(sum(ticks[field] for field in CPU_BUSY_FIELDS) / hz, 3)
    delta = {'net': net, 'cpu': cpu,
             'interval_seconds': round(after['monotonic'] - before['monotonic'], 6)}
    if before.get('meminfo') and after.get('meminfo'):
        delta['memory'] = memory_delta(before['meminfo'], after['meminfo'])
    if before.get('disks') is not None and after.get('disks') is not None:
        delta['disk'] = disk_delta(before['disks'], after['disks'])
    if before.get('nstat') and after.get('nstat'):
        delta['nstat'] = {name: after['nstat'][name] - before['nstat'][name]
                          for name in NSTAT_COUNTERS if name in before['nstat'] and name in after['nstat']}
    if before.get('qdisc') is not None and after.get('qdisc') is not None:
        delta['qdisc'] = qdisc_delta(before['qdisc'], after['qdisc'])
    return delta


def _read_proc(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def snapshot(iface: str, proc: Path = Path('/proc'), tcp: bool = False) -> dict:
    """Counters of one interface and the aggregate CPU line, with clocks.

    Also, where /proc has them: meminfo's Dirty, Writeback and Cached (kB), and the counters of
    every whole disk and md array (DISK_DEVICE) from diskstats. With tcp (T12, EXP-13), the TCP
    counters nstat reads from /proc/net/snmp and /proc/net/netstat (NSTAT_COUNTERS).
    """
    import time
    netdev = parse_netdev((proc / 'net' / 'dev').read_text())
    if iface not in netdev:
        raise ValueError(f'interface {iface} not in /proc/net/dev ({", ".join(netdev)})')
    result = {'iface': iface, 'net': netdev[iface],
              'cpu_ticks': parse_proc_stat((proc / 'stat').read_text()),
              'clk_tck': os.sysconf('SC_CLK_TCK'), 'monotonic': time.monotonic(),
              'utc': time.time()}
    meminfo = _read_proc(proc / 'meminfo')
    if meminfo:
        values = parse_meminfo(meminfo)
        result['meminfo'] = {field: values[field] for field in MEMINFO_FIELDS if field in values}
    diskstats = _read_proc(proc / 'diskstats')
    if diskstats:
        result['disks'] = {device: {field: counters[field] for field in DISK_COUNTERS}
                           for device, counters in parse_diskstats(diskstats).items()
                           if DISK_DEVICE.match(device)}
    if tcp:
        snmp, netstat = _read_proc(proc / 'net' / 'snmp'), _read_proc(proc / 'net' / 'netstat')
        if snmp or netstat:
            result['nstat'] = nstat_values(snmp or '', netstat or '')
    return result


# --- link speeds ------------------------------------------------------------

def parse_speed(text: str) -> dict:
    """'0.5' (Gbit/s) or 'unthrottled' -> label, target and tc rate."""
    text = text.strip().lower()
    if text in (UNTHROTTLED, 'off', 'none'):
        return {'label': UNTHROTTLED, 'gbit': None, 'tc_rate': 'off'}
    gbit = float(text.rstrip('g'))
    if not math.isfinite(gbit) or gbit <= 0:
        raise ValueError(f'link speed must be a positive Gbit/s value, got {text!r}')
    mbit = round(gbit * 1000)
    if mbit < 1 or abs(mbit - gbit * 1000) > 1e-6:
        raise ValueError(f'link speed {text} Gbit/s is not a whole number of Mbit/s')
    return {'label': f'{gbit:g}g', 'gbit': gbit, 'tc_rate': f'{mbit}mbit'}


def parse_speeds(text: str) -> list[dict]:
    speeds = [parse_speed(item) for item in text.split(',') if item.strip()]
    labels = [speed['label'] for speed in speeds]
    if len(set(labels)) != len(labels):
        raise ValueError(f'duplicate link speeds in {text!r}')
    if not speeds:
        raise ValueError('no link speeds given')
    return speeds


def parse_method_speeds(specs, methods, speeds) -> dict[str, list[str]]:
    """--method-speeds: method -> the labels of the speeds it runs at, for the methods it limits.

    Each spec is METHODS=SPEEDS, the methods joined with '+' and the speeds a comma list
    as in --speeds, for example 'pgdelta_seq+pgdelta_inplace=unthrottled'. Every method
    must be in `methods` and named once, and every speed must be one of `speeds`; a method
    no spec names runs at every speed.
    """
    labels = [speed['label'] for speed in speeds]
    limited: dict[str, list[str]] = {}
    for spec in specs:
        names, equals, values = spec.partition('=')
        names = [name.strip() for name in names.split('+') if name.strip()]
        if not equals or not names:
            raise ValueError(f'--method-speeds wants METHODS=SPEEDS, got {spec!r}')
        chosen = [speed['label'] for speed in parse_speeds(values)]
        unknown = [label for label in chosen if label not in labels]
        if unknown:
            raise ValueError(f'--method-speeds {spec!r}: {", ".join(unknown)} not in --speeds')
        for name in names:
            if name not in methods:
                raise ValueError(f'--method-speeds {spec!r}: {name} is not in --methods')
            if name in limited:
                raise ValueError(f'--method-speeds names {name} twice')
            limited[name] = [label for label in labels if label in chosen]  # in --speeds' order
    return limited


def methods_at(methods, limited: dict, label: str) -> list[str]:
    """The methods that run at the speed `label`, in their --methods order."""
    return [method for method in methods if method not in limited or label in limited[method]]


def gbit_per_second(byte_count: int, seconds: float) -> float | None:
    return round(byte_count * 8 / seconds / 1e9, 4) if seconds > 0 else None


def method_orders(methods, rng: random.Random, count: int) -> list[list[str]]:
    """count independent random orders of methods, drawn from rng in sequence."""
    orders = []
    for _ in range(count):
        order = list(methods)
        rng.shuffle(order)
        orders.append(order)
    return orders


def order_speeds(speeds, rng: random.Random, mode: str, repeat: int) -> list:
    """The speeds in the order one repeat visits them.

    'shuffle' (the default) draws a new order from rng for every repeat, so
    drift over a long sweep (cache or disk state, noisy neighbours, the
    source's growing WAL) is not confounded with link speed; 'alternate' runs
    the given order on even repeats and its reverse on odd ones; 'ascending'
    keeps the given order.
    """
    speeds = list(speeds)
    if mode == 'shuffle':
        rng.shuffle(speeds)
        return speeds
    if mode == 'alternate':
        return speeds if repeat % 2 == 0 else speeds[::-1]
    if mode == 'ascending':
        return speeds
    raise ValueError(f'unknown speed order {mode!r}')


# --- per-process peak memory (GNU time) ---------------------------------------

def gnu_time_argv(gnu_time, output, argv) -> list[str]:
    """argv run under GNU time, which writes GNU_TIME_FORMAT to output when it ends."""
    return [str(gnu_time), '-f', GNU_TIME_FORMAT, '-o', str(output), *map(str, argv)]


def group_members(pgid: int, proc: Path = Path('/proc')) -> list[int] | None:
    """PIDs in process group pgid, from /proc/*/stat; None without a /proc. A signal meant for a command run under GNU
    time goes to these but GNU time itself (run_exp04's Child.signal, agent.py run --timeout), so GNU time outlives
    the command and writes its report: GNU time ignores only SIGINT and SIGQUIT."""
    if not (proc / 'self').exists():
        return None
    members = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / 'stat').read_text()
        except OSError:
            continue
        # pid (comm) state ppid pgrp ...; comm may contain spaces and parentheses.
        fields = text[text.rindex(')') + 2:].split()
        if len(fields) > 2 and int(fields[2]) == pgid:
            members.append(int(entry.name))
    return members


def parse_gnu_time(text: str) -> dict | None:
    """The GNU_TIME_FORMAT line of a GNU time -o file, or None if it has none.

    GNU time writes 'Command exited with non-zero status N' (or 'terminated
    by signal') before the format line; only the format line is read.
    """
    for line in reversed(text.splitlines()):
        if not line.startswith(GNU_TIME_PREFIX + ' '):
            continue
        fields = dict(item.partition('=')[::2] for item in line.split()[1:])
        try:
            return {'maxrss_kb': int(fields['maxrss_kb']),
                    'wall_seconds': float(fields['wall_seconds']),
                    'user_seconds': float(fields['user_seconds']),
                    'system_seconds': float(fields['system_seconds']),
                    'exit_code': int(fields['exit'])}
        except (KeyError, ValueError):
            return None
    return None


def read_gnu_time(path) -> dict | None:
    try:
        return parse_gnu_time(Path(path).read_text(errors='replace'))
    except OSError:
        return None


# --- trees ------------------------------------------------------------------

def tree_digest(root: Path) -> dict:
    """Every entry below root: path -> [kind, size, sha256 or link target, mode, ino, nlink].

    Symlinks are not followed. 'dir' entries carry no size or hash.
    """
    root = Path(root)
    entries = {}
    total = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            info = os.lstat(path)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = ['link', 0, os.readlink(path), mode, info.st_ino, info.st_nlink]
            elif stat.S_ISDIR(info.st_mode):
                entries[rel] = ['dir', 0, None, mode, info.st_ino, info.st_nlink]
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                with open(path, 'rb') as stream:
                    while chunk := stream.read(DIGEST_CHUNK):
                        digest.update(chunk)
                entries[rel] = ['file', info.st_size, digest.hexdigest(), mode, info.st_ino,
                                info.st_nlink]
                total += info.st_size
            else:
                entries[rel] = ['other', 0, None, mode, info.st_ino, info.st_nlink]
    top = os.lstat(root)
    return {'root': str(root), 'device': top.st_dev, 'entries': entries, 'files': sum(
        1 for entry in entries.values() if entry[0] == 'file'), 'bytes': total}


def files_of(digest: dict) -> dict:
    """A tree digest with its regular files only (directories, links and the rest left out)."""
    entries = {path: entry for path, entry in digest['entries'].items() if entry[0] == 'file'}
    return {**digest, 'entries': entries, 'files': len(entries),
            'bytes': sum(entry[1] for entry in entries.values())}


def files_digest(root: Path, paths) -> dict:
    """tree_digest's entries for the listed relative paths only, each one a regular file.

    What pgdelta's datadir mode reads from a stopped data directory is a selection of its
    files (PostgreSQL's base-backup exclusions applied), so only those are hashed; a listed
    path that is missing or not a regular file goes in `missing`. `paths` may map each listed
    path to the path whose bytes pgdelta sends for it (an unlogged relation's main fork is sent
    with its init fork's bytes); each entry is then keyed by the listed path and hashes the
    other, and `read_from` names those that differ.
    """
    root = Path(root)
    sources = dict(paths) if isinstance(paths, dict) else {rel: rel for rel in paths}
    entries, missing, total = {}, [], 0
    for rel in sorted(sources):
        path = root / sources[rel]
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            missing.append(rel)
            continue
        if not stat.S_ISREG(info.st_mode):
            missing.append(rel)
            continue
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            while chunk := stream.read(DIGEST_CHUNK):
                digest.update(chunk)
        entries[rel] = ['file', info.st_size, digest.hexdigest(), stat.S_IMODE(info.st_mode),
                        info.st_ino, info.st_nlink]
        total += info.st_size
    return {'root': str(root), 'device': os.lstat(root).st_dev, 'entries': entries,
            'files': len(entries), 'bytes': total, 'missing': missing,
            'read_from': {rel: read for rel, read in sorted(sources.items()) if read != rel}}


def parse_datadir_sources(text: str) -> dict[str, str]:
    """{listed path: path read} from `python -m pgdelta.datadir list [--sources]`, as JSON.

    With --sources it prints [path, path read] pairs, which differ for the main fork of an
    unlogged relation: pgdelta sends it with its init fork's bytes, the empty relation that
    recovery's reset would leave. Without, a list of paths, each read as itself. Either may be
    held under 'files' or 'paths' in an object. If the whole output doesn't parse, its last
    non-empty line is tried.
    """
    try:
        value = json.loads(text)
    except ValueError:
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise ValueError('pgdelta.datadir list printed nothing') from None
        value = json.loads(lines[-1])
    if isinstance(value, dict):
        value = next((value[key] for key in ('files', 'paths') if isinstance(value.get(key), list)), None)
    if not isinstance(value, list):
        raise ValueError('pgdelta.datadir list must print a JSON list of relative paths')
    sources = {}
    for item in value:
        if isinstance(item, str):
            sources[item] = item
        elif isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item):
            sources[item[0]] = item[1]
        else:
            raise ValueError('pgdelta.datadir list must print relative paths or [path, path read] pairs')
    return sources


def parse_datadir_list(text: str) -> list[str]:
    """The relative paths `python -m pgdelta.datadir list` printed as JSON (see parse_datadir_sources)."""
    return list(parse_datadir_sources(text))


# --- shared extents (reflink clones) ----------------------------------------------

# FS_IOC_FIEMAP maps a file's extents and flags each one whose blocks another file shares too:
# a reflink clone on XFS. Since coreutils 9.0 (Debian 12 ships 9.1) cp defaults to
# --reflink=auto, and copy_file_range clones as well, so on dest's XFS a plain `cp -a B0 B`
# makes B share every block with B0. ext4 has no reflinks and never sets the flag.
FS_IOC_FIEMAP = 0xC020660B  # _IOWR('f', 11, struct fiemap)
FIEMAP_MAX_OFFSET = (1 << 64) - 1
FIEMAP_FLAG_SYNC = 0x1
FIEMAP_EXTENT_LAST = 0x1
FIEMAP_EXTENT_SHARED = 0x2000
# struct fiemap: fm_start, fm_length, fm_flags, fm_mapped_extents, fm_extent_count, fm_reserved.
FIEMAP_HEADER = struct.Struct('=QQIIII')
# struct fiemap_extent: fe_logical, fe_physical, fe_length, fe_reserved64[2], fe_flags, fe_reserved[3].
FIEMAP_EXTENT = struct.Struct('=QQQQQIIII')
FIEMAP_BATCH = 256


def parse_fiemap(buffer) -> list[tuple[int, int, int]]:
    """(logical offset, length, flags) of each extent an FS_IOC_FIEMAP call filled in."""
    mapped = FIEMAP_HEADER.unpack_from(buffer, 0)[3]
    extents = []
    for index in range(mapped):
        logical, _, length, _, _, flags, _, _, _ = FIEMAP_EXTENT.unpack_from(
            buffer, FIEMAP_HEADER.size + index * FIEMAP_EXTENT.size)
        extents.append((logical, length, flags))
    return extents


def shared_extent_bytes(path) -> int:
    """Bytes of `path` in extents that share their blocks with another file; OSError without FIEMAP."""
    shared, start = 0, 0
    with open(path, 'rb') as stream:
        while True:
            request = bytearray(FIEMAP_HEADER.size + FIEMAP_BATCH * FIEMAP_EXTENT.size)
            FIEMAP_HEADER.pack_into(request, 0, start, FIEMAP_MAX_OFFSET - start, FIEMAP_FLAG_SYNC, 0,
                                    FIEMAP_BATCH, 0)
            fcntl.ioctl(stream.fileno(), FS_IOC_FIEMAP, request)
            extents = parse_fiemap(request)
            shared += sum(length for _, length, flags in extents if flags & FIEMAP_EXTENT_SHARED)
            if not extents or extents[-1][2] & FIEMAP_EXTENT_LAST:
                return shared
            start = extents[-1][0] + extents[-1][1]


def shared_extents(root, limit: int = 20) -> dict:
    """How much of the regular files below root shares disk blocks with other files (FIEMAP).

    A copy made with `cp --reflink=never` must share nothing; one made with cp's default or
    copy_file_range on XFS shares the blocks of whatever it was cloned from, B0 here.
    `supported` is False where FIEMAP isn't available (macOS, some filesystems), and None
    when there was no file to ask about.
    """
    checked = shared_files = shared = 0
    examples = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                count = shared_extent_bytes(path)
            except OSError as exc:
                return {'supported': False, 'error': f'{type(exc).__name__}: {exc}', 'files': checked,
                        'shared_files': None, 'shared_bytes': None, 'examples': examples}
            checked += 1
            if count:
                shared_files += 1
                shared += count
                if len(examples) < limit:
                    examples.append(path.relative_to(root).as_posix())
    return {'supported': True if checked else None, 'error': None, 'files': checked,
            'shared_files': shared_files, 'shared_bytes': shared, 'examples': examples}


# What an in-place refresh writes into B while it runs, and must leave no trace of.
IN_PLACE_MARKER = 'pgdelta-incomplete.json'


def in_place_leftovers(digest: dict) -> list[str]:
    """What a finished in-place refresh must not leave in B.

    While it runs, pgdelta --in-place writes B/pgdelta-incomplete.json and keeps
    global/pg_control under another name, so PostgreSQL refuses to start on a half-done B.
    Afterwards neither may remain: no marker, no entry named after pgdelta, no other
    global/pg_control* file, and global/pg_control itself back in place.
    """
    leftovers = []
    for path in sorted(digest['entries']):
        name = path.rsplit('/', 1)[-1]
        renamed_control = (path.startswith('global/') and name.startswith('pg_control')
                           and path != 'global/pg_control')
        if name == IN_PLACE_MARKER or 'pgdelta' in name or renamed_control:
            leftovers.append(path)
    if 'global/pg_control' not in digest['entries']:
        leftovers.append('global/pg_control is missing')
    return leftovers


def tree_stat(root: Path) -> dict[str, list]:
    """A cheap fingerprint of a tree without reading it: path -> [kind, size, mtime_ns, mode].

    The orchestrator compares B0's before and after every method run, and
    hashes it fully (tree_digest) at the start and end of each trial.
    """
    root = Path(root)
    entries = {}
    for directory, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(directory) / name
            info = os.lstat(path)
            kind = ('link' if stat.S_ISLNK(info.st_mode) else 'dir' if stat.S_ISDIR(info.st_mode)
                    else 'file' if stat.S_ISREG(info.st_mode) else 'other')
            entries[path.relative_to(root).as_posix()] = [
                kind, info.st_size if kind == 'file' else 0, info.st_mtime_ns,
                stat.S_IMODE(info.st_mode)]
    return entries


def stat_changes(before: dict, after: dict, limit: int = 20) -> list[str]:
    changes = []
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) != after.get(path):
            changes.append(f'{path}: {before.get(path)} -> {after.get(path)}')
            if len(changes) >= limit:
                break
    return changes


def compare_digests(expected: dict, actual: dict, limit: int = 20) -> tuple[bool, list[str]]:
    """Same paths, kinds, sizes and contents (modes, inodes and link counts ignored)."""
    left, right = expected['entries'], actual['entries']
    differences = []
    for path in sorted(left.keys() | right.keys()):
        if path not in right:
            differences.append(f'missing in output: {path}')
        elif path not in left:
            differences.append(f'extra in output: {path}')
        elif left[path][:3] != right[path][:3]:
            differences.append(f'differs: {path} {left[path][:2]} vs {right[path][:2]}')
        if len(differences) >= limit:
            break
    return not differences, differences


def independence(output: dict, basis: dict, limit: int = 20) -> tuple[bool, list[str]]:
    """O shares no inode with B (same device) and none of its files has a second link."""
    problems = []
    basis_inodes = ({entry[4] for entry in basis['entries'].values()}
                    if basis['device'] == output['device'] else set())
    for path, entry in sorted(output['entries'].items()):
        if entry[4] in basis_inodes:
            problems.append(f'shares an inode with the basis: {path}')
        elif entry[0] == 'file' and entry[5] > 1:
            problems.append(f'has {entry[5]} hard links: {path}')
        if len(problems) >= limit:
            break
    return not problems, problems


# --- WAL in a backup ----------------------------------------------------------

def parse_lsn(text: str) -> int:
    high, _, low = text.partition('/')
    return (int(high, 16) << 32) | int(low, 16)


def wal_files(root: Path) -> dict[str, int]:
    """Relative path -> size of every regular file under root/pg_wal."""
    root = Path(root)
    files = {}
    for directory, _, filenames in os.walk(root / 'pg_wal'):
        for name in filenames:
            path = Path(directory) / name
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode):
                files[path.relative_to(root).as_posix()] = info.st_size
    return files


def wal_files_from_digest(digest: dict) -> dict[str, int]:
    return {path: entry[1] for path, entry in digest['entries'].items()
            if entry[0] == 'file' and path.startswith('pg_wal/')}


def manifest_wal_ranges(text: str) -> list[dict]:
    """The WAL-Ranges of a backup_manifest."""
    return [{'timeline': item['Timeline'], 'start_lsn': item['Start-LSN'],
             'end_lsn': item['End-LSN']} for item in json.loads(text).get('WAL-Ranges', [])]


def wal_summary(files: dict[str, int], manifest_text: str | None) -> dict:
    """The WAL a backup holds, and how much of it is real.

    A plain backup's pg_wal holds whole segment files (16 MiB), zero-padded
    past the backup's end LSN. pg_basebackup -X stream sends only the WAL from
    the start of the first segment to the end LSN (wal_stream_bytes, from the
    manifest's WAL-Ranges); pgdelta and rsync send S's new segment files whole,
    so a staged run moves about wal_padding_bytes more than a streaming copy.
    """
    segments = {path: size for path, size in files.items()
                if WAL_SEGMENT_NAME.match(path.rsplit('/', 1)[-1])}
    segment_bytes = max(segments.values()) if segments else DEFAULT_WAL_SEGMENT
    ranges = manifest_wal_ranges(manifest_text) if manifest_text else []
    stream = None
    if ranges:
        stream = 0
        for item in ranges:
            start = parse_lsn(item['start_lsn'])
            stream += parse_lsn(item['end_lsn']) - (start - start % segment_bytes)
    wal_bytes = sum(files.values())
    return {'wal_bytes': wal_bytes, 'wal_files': len(files), 'wal_segments': len(segments),
            'wal_segment_bytes': segment_bytes, 'wal_ranges': ranges,
            'wal_stream_bytes': stream,
            'wal_padding_bytes': None if stream is None else max(0, wal_bytes - stream)}


# --- logical digest (pgbackrest_*'s verification) ---------------------------------

# EXP-01's tables (run_study.USER_TABLES) with their primary keys; pgbench_history has none.
LOGICAL_TABLES = (('pgbench_accounts', 'aid'), ('pgbench_branches', 'bid'), ('pgbench_tellers', 'tid'),
                  ('pgbench_history', None), ('events', 'id'))
# Rows are hashed in blocks of this many key values, so no aggregated string nears
# PostgreSQL's 1 GB limit at any scale.
LOGICAL_BLOCK_KEYS = 65536
# PGOPTIONS for every digest session: a row's text must not depend on the server's settings
# (events.ts is a timestamptz).
LOGICAL_SESSION_OPTIONS = '-c TimeZone=UTC -c DateStyle=ISO,YMD -c extra_float_digits=3'


def table_digest_sql(table: str, key: str | None, block: int = LOGICAL_BLOCK_KEYS) -> str:
    """A scalar subquery: the table's row count and an md5 over its rows in a fixed order.

    With a primary key, each block of `block` key values hashes its rows' md5s in key
    order, and the table's md5 is that of the block hashes in block order: the rows in
    key order, in two levels. Without one, the rows' md5s are taken in the order of the
    rows' text (all rows sorted).
    """
    if key is None:
        return (f"(SELECT json_build_object('rows', count(*), 'md5', "
                f"md5(coalesce(string_agg(md5(r), '' ORDER BY r), ''))) "
                f"FROM (SELECT t::text AS r FROM {table} t) s)")
    return (f"(SELECT json_build_object('rows', coalesce(sum(n), 0), 'md5', "
            f"md5(coalesce(string_agg(h, '' ORDER BY b), ''))) "
            f"FROM (SELECT {key} / {block} AS b, count(*) AS n, "
            f"md5(string_agg(md5(t::text), '' ORDER BY {key})) AS h "
            f"FROM {table} t GROUP BY 1) s)")


def logical_digest_sql(tables=LOGICAL_TABLES, block: int = LOGICAL_BLOCK_KEYS) -> str:
    """One query printing {table: {rows, md5}} as JSON for every table in `tables`."""
    parts = ', '.join(f"'{table}', {table_digest_sql(table, key, block)}" for table, key in tables)
    return f'SELECT json_build_object({parts})::text;'


def compare_logical(expected: dict, actual: dict) -> tuple[bool, list[str]]:
    """Same tables with the same row counts and md5s."""
    differences = []
    for table in sorted(expected.keys() | actual.keys()):
        left, right = expected.get(table), actual.get(table)
        if left is None or right is None:
            differences.append(f'{table}: only in {"the output" if left is None else "the source"}')
        elif (int(left['rows']), left['md5']) != (int(right['rows']), right['md5']):
            differences.append(f'{table}: {left["rows"]} rows md5 {left["md5"]} on the source, '
                               f'{right["rows"]} rows md5 {right["md5"]} in the output')
    return not differences, differences


# --- pgBackRest (T11) -------------------------------------------------------------

# One stanza per variant, named after its method, each with its own repository on the
# source. What differs between them: compression, and block incremental with bundling.
# pgbackrest_none is uncompressed on the network too (EXP-08): with compress-type=none and the
# repository on another host, pgBackRest compresses what it sends at compress-level-network
# (default 3) unless that is 0. The zst variants send their files as stored, and pgBackRest
# ignores the network level for them.
PGBACKREST_VARIANTS = {
    'pgbackrest_zst': {'compress-type': 'zst'},
    'pgbackrest_none': {'compress-type': 'none', 'compress-level-network': '0'},
    'pgbackrest_block': {'compress-type': 'zst', 'repo1-bundle': 'y', 'repo1-block': 'y'},
}
# The variant options that act on dest, where the restore and the recovery's archive-get run and
# reach the repository over SSH: they go in dest's stanza sections too.
PGBACKREST_DEST_OPTIONS = ('compress-level-network',)
PGBACKREST_PROCESS_MAX = 4
PGBACKREST_LOG_LEVELS = {'log-level-console': 'info', 'log-level-file': 'detail'}


def pgbackrest_paths(work) -> dict:
    """Where pgBackRest's files go, at the same paths on both VMs (the work directory's)."""
    root = f'{work}/pgbackrest'
    return {'root': root, 'config': f'{root}/pgbackrest.conf', 'log': f'{root}/log',
            'lock': f'{root}/lock', 'spool': f'{root}/spool', 'ssh': f'{root}/ssh',
            'ssh_config': f'{root}/ssh_config'}


def pgbackrest_repo(work, stanza: str) -> str:
    return f'{pgbackrest_paths(work)["root"]}/repo-{stanza}'


def _ini(header: list[str], sections: dict) -> str:
    lines = list(header)
    for name, options in sections.items():
        lines += ['', f'[{name}]', *(f'{key}={value}' for key, value in options.items())]
    return '\n'.join(lines) + '\n'


def pgbackrest_common(work) -> dict:
    paths = pgbackrest_paths(work)
    return {'log-path': paths['log'], 'lock-path': paths['lock'], 'spool-path': paths['spool'],
            **PGBACKREST_LOG_LEVELS}


def pgbackrest_source_config(work, stanzas, *, pg_path, socket_path, port, user) -> str:
    """pgbackrest.conf on pgd-source, the repository host: one stanza per variant."""
    sections = {'global': {**pgbackrest_common(work), 'start-fast': 'y',
                           'process-max': str(PGBACKREST_PROCESS_MAX), 'repo1-retention-full': '1'}}
    for stanza in stanzas:
        sections[stanza] = {'pg1-path': pg_path, 'pg1-socket-path': socket_path, 'pg1-port': str(port),
                            'pg1-user': user, 'repo1-path': pgbackrest_repo(work, stanza),
                            **PGBACKREST_VARIANTS[stanza]}
    return _ini(['# EXP-04 (T11): pgBackRest on pgd-source, the repository host. One stanza per',
                 '# variant, each with its own repository under the work directory.'], sections)


def pgbackrest_dest_config(work, stanzas, *, source_ip, ssh_user) -> str:
    """pgbackrest.conf on pgd-dest: restores from the repositories on pgd-source over SSH.

    pgBackRest's SSH goes through cmd-ssh, a wrapper that uses its own SSH configuration
    with the harness key (pgbackrest_ssh_config), so ~/.ssh/config is left alone.
    """
    paths = pgbackrest_paths(work)
    sections = {'global': {**pgbackrest_common(work), 'cmd-ssh': paths['ssh'], 'repo1-host': source_ip,
                           'repo1-host-user': ssh_user, 'repo1-host-config': paths['config']}}
    for stanza in stanzas:
        sections[stanza] = {'repo1-path': pgbackrest_repo(work, stanza), **pgbackrest_dest_options(stanza)}
    return _ini(['# EXP-04 (T11): pgBackRest on pgd-dest, restoring from the repositories on',
                 '# pgd-source over SSH. pg1-path is given on each restore command line.'], sections)


def pgbackrest_dest_options(stanza: str) -> dict:
    """The variant's options that dest's own pgBackRest commands use (PGBACKREST_DEST_OPTIONS)."""
    return {key: value for key, value in PGBACKREST_VARIANTS[stanza].items() if key in PGBACKREST_DEST_OPTIONS}


def pgbackrest_ssh_config(source_ip: str, user: str, key: str) -> str:
    """The SSH configuration pgBackRest uses on pgd-dest: the harness key, never a prompt."""
    return '\n'.join([
        '# EXP-04 (T11): how pgBackRest on pgd-dest reaches the repository host, pgd-source.',
        f'Host {source_ip}', f'    User {user}', f'    IdentityFile {key}', '    IdentitiesOnly yes',
        '    BatchMode yes', '    StrictHostKeyChecking accept-new', '    ServerAliveInterval 15', ''])


def pgbackrest_ssh_wrapper(ssh_config: str) -> str:
    """pgBackRest's cmd-ssh on pgd-dest: ssh with its own configuration file."""
    return ('#!/bin/sh\n# EXP-04 (T11): pgBackRest\'s SSH client on pgd-dest (cmd-ssh).\n'
            f'exec ssh -F {ssh_config} "$@"\n')


def pgbackrest_archive_command(exe: str, config: str, stanzas) -> str:
    """archive_command pushing every WAL file to every configured stanza's repository."""
    return ' && '.join(f'{exe} --config={config} --stanza={stanza} archive-push %p' for stanza in stanzas)


def pgbackrest_restore_argv(exe: str, config: str, stanza: str, pg_path) -> list[str]:
    """The timed restore on pgd-dest: B patched from the repository (--delta), 4 processes."""
    return [exe, f'--config={config}', f'--stanza={stanza}', '--delta',
            f'--process-max={PGBACKREST_PROCESS_MAX}', f'--pg1-path={pg_path}', 'restore']


def parse_pgbackrest_restore(text: str) -> dict:
    """What pgBackRest's restore log says: size, files, how many matched, its end and statistics.

    The console log (info) has the summary line and the end line; the file log (detail)
    has one line per file. Counts are None when the log has no such lines.
    """
    size = re.search(r'restore size = ([^,\s]+), file total = (\d+)', text)
    end = re.search(r'restore command end: (completed successfully|aborted with exception \[\d+\])'
                    r'(?: \((\d+)ms\))?', text)
    begin = re.search(r'restore command begin ([0-9][^:\s]*)', text)
    backup = re.search(r'restore backup set (\S+)', text)
    file_lines = [line for line in text.splitlines() if 'restore file ' in line]
    statistics = None
    for line in reversed(text.splitlines()):
        found = re.search(r'statistics: (\{.*\})\s*$', line)
        if found:
            try:
                statistics = json.loads(found.group(1))
            except ValueError:
                pass
            break
    return {'version': begin.group(1) if begin else None,
            'backup_set': backup.group(1).rstrip(',') if backup else None,
            'restore_size': size.group(1) if size else None,
            'file_total': int(size.group(2)) if size else None,
            'file_lines': len(file_lines) if file_lines else None,
            'files_matched': (sum('exists and matches' in line for line in file_lines)
                              if file_lines else None),
            'completed': None if not end else end.group(1) == 'completed successfully',
            'reported_ms': int(end.group(2)) if end and end.group(2) else None,
            'statistics': statistics}


def pgbackrest_backup_summary(info_text: str) -> dict:
    """The latest backup of `pgbackrest info --output=json` for one stanza: sizes and times."""
    info = json.loads(info_text)
    stanza = info[0] if isinstance(info, list) and info else {}
    backups = stanza.get('backup') or []
    if not backups:
        return {'stanza': stanza.get('name'), 'status': stanza.get('status'), 'backup': None}
    latest = backups[-1]
    sizes = latest.get('info') or {}
    repository = sizes.get('repository') or {}
    return {'stanza': stanza.get('name'), 'label': latest.get('label'), 'type': latest.get('type'),
            'size_bytes': sizes.get('size'), 'delta_bytes': sizes.get('delta'),
            'repo_size_bytes': repository.get('size'), 'repo_delta_bytes': repository.get('delta'),
            'start': (latest.get('timestamp') or {}).get('start'),
            'stop': (latest.get('timestamp') or {}).get('stop')}


# PostgreSQL's server log, for each WAL file restore_command fetched (pgBackRest's archive-get).
RESTORED_WAL = re.compile(r'restored log file "([0-9A-F]{24}(?:\.partial)?|[0-9A-F]{8}\.history)" from archive')


def restored_wal_files(server_log: str) -> int:
    """How many WAL files (segments and timeline histories) the server's restore_command fetched."""
    return len(RESTORED_WAL.findall(server_log))


def with_recovery_bytes(totals: dict, recovery: dict) -> dict:
    """pgBackRest's totals with its recovery's bytes on the wire added, and kept apart too.

    The restore is timed; starting the restored copy, whose recovery fetches WAL from the
    repository through archive-get, is not. EXP-08 counts that WAL in pgBackRest's wire bytes,
    as pgdelta's WAL travels inside its timed transfer. Wall time and CPU stay the restore's.
    """
    net = recovery.get('net') or {}
    extra = {'wire_bytes_source_to_dest': _get(net, 'source', 'tx_bytes'),
             'wire_bytes_dest_to_source': _get(net, 'dest', 'tx_bytes'),
             'wire_bytes_source_to_dest_dest_rx': _get(net, 'dest', 'rx_bytes'),
             'wire_bytes_dest_to_source_source_rx': _get(net, 'source', 'rx_bytes')}
    result = dict(totals)
    for key, value in extra.items():
        result[key] = None if result.get(key) is None or value is None else result[key] + value
    result['recovery_wire_bytes_source_to_dest'] = extra['wire_bytes_source_to_dest']
    result['recovery_wire_bytes_dest_to_source'] = extra['wire_bytes_dest_to_source']
    result['wire_bytes_scope'] = ("the timed restore plus the untimed start of the restored copy, whose "
                                  "recovery fetches WAL through archive-get (recovery_wire_bytes_*)")
    return result


# --- T2 (EXP-11): phase timers from timestamped output lines ------------------------

# Child(stamp_lines=True) copies a command's output to its log line by line, as each arrives, each
# prefixed with the seconds since the command's launch on the orchestrator's clock: "[+1.234567] ".
STAMPED_LINE = re.compile(r'^\[\+(\d+(?:\.\d+)?)\] ?(.*)$')


def stamp_line(seconds: float, line: bytes) -> bytes:
    """One output line as Child(stamp_lines=True) logs it."""
    return f'[+{seconds:.6f}] '.encode() + line.rstrip(b'\r\n') + b'\n'


def parse_stamped_lines(text: str) -> list[tuple[float, str]]:
    """(seconds since launch, text) of every stamped line of a log, in order; others are skipped."""
    lines = []
    for line in text.splitlines():
        found = STAMPED_LINE.match(line)
        if found:
            lines.append((float(found.group(1)), found.group(2)))
    return lines


def _first_marks(lines, markers) -> dict[str, float]:
    """name -> the time of the first line containing that marker's text (case-insensitive)."""
    marks = {}
    for seconds, text in lines:
        lowered = text.lower()
        for name, needle in markers:
            if name not in marks and needle in lowered:
                marks[name] = seconds
    return marks


def _interval(marks: dict, start: str, *ends):
    """Seconds from mark `start` to the first of `ends` that is marked; None if either is missing."""
    end = next((marks[name] for name in ends if name in marks), None)
    if start not in marks or end is None:
        return None
    return round(end - marks[start], 6)


# pg_basebackup --verbose's progress messages (PostgreSQL 17, pg_log_info: "pg_basebackup: ..."),
# in the order it prints them. With --no-sync there is no 'sync' line; with no manifest no
# 'manifest' line.
BASEBACKUP_MARKERS = (
    ('initiating', 'initiating base backup, waiting for checkpoint to complete'),
    ('checkpoint_completed', 'checkpoint completed'),
    ('wal_start', 'write-ahead log start point:'),
    ('wal_receiver', 'starting background wal receiver'),
    ('wal_end', 'write-ahead log end point:'),
    ('waiting_for_streamer', 'waiting for background process to finish streaming'),
    ('sync', 'syncing data to disk'),
    ('manifest', 'renaming backup_manifest.tmp to backup_manifest'),
    ('completed', 'base backup completed'),
)


def basebackup_phases(lines, wall_seconds: float | None = None) -> dict:
    """Where a pg_basebackup --verbose run's time went, from its timestamped lines.

    - start: launch until it asks for the backup (connection);
    - checkpoint: from "initiating base backup, waiting for checkpoint" to "checkpoint completed";
    - stream: from there to "write-ahead log end point" (the files, and pg_backup_stop);
    - wal_end: from the end point until the next step starts: "syncing data to disk", or with
      --no-sync the manifest's rename or the end (the WAL streamer's finish);
    - sync: "syncing data to disk" until the manifest's rename (or the end): the flush;
    - manifest: the durable rename of backup_manifest.tmp until "base backup completed";
    - exit: "base backup completed" until the process ended (with wall_seconds).

    backup_mode_adder is checkpoint + wal_end: what a stopped-source copy leaves out of a live
    one (EXP-11's R_adj). A missing line leaves its intervals None.
    """
    marks = _first_marks(lines, BASEBACKUP_MARKERS)
    phases = {
        'start_s': round(marks['initiating'], 6) if 'initiating' in marks else None,
        'checkpoint_s': _interval(marks, 'initiating', 'checkpoint_completed'),
        'stream_s': _interval(marks, 'checkpoint_completed', 'wal_end'),
        'wal_end_s': _interval(marks, 'wal_end', 'sync', 'manifest', 'completed'),
        'sync_s': _interval(marks, 'sync', 'manifest', 'completed'),
        'manifest_s': _interval(marks, 'manifest', 'completed'),
        'exit_s': (round(wall_seconds - marks['completed'], 6)
                   if wall_seconds is not None and 'completed' in marks else None),
    }
    adder = (None if phases['checkpoint_s'] is None or phases['wal_end_s'] is None
             else round(phases['checkpoint_s'] + phases['wal_end_s'], 6))
    return {**phases, 'backup_mode_adder_s': adder, 'synced': 'sync' in marks,
            'complete': all(name in marks for name in ('initiating', 'checkpoint_completed', 'wal_end',
                                                       'completed')),
            'marks': marks}


# pg_probackup catchup's INFO lines on its console (stderr), matched as lowercase text, in the order
# 2.5.16 prints them (src/catchup.c's do_catchup and catchup_sync_destination_files, src/backup.c's
# pg_stop_backup_consume). Catchup prints nothing when its copy threads finish: the next line,
# "wait for pg_stop_backup()", follows the copy of pg_control and the query's send. The smoke run
# checks the wording (EXP-11's item 1); a line not found leaves its intervals None.
PROBACKUP_MARKERS = (
    ('started', 'catchup start'),
    ('copy', 'start transferring data files'),
    ('stop_backup', 'wait for pg_stop_backup'),
    ('stopped', 'pg_stop backup() successfully executed'),
    ('synchronized', 'databases synchronized'),
    ('sync', 'syncing copied files to disk'),
    ('synced', 'files are synced'),
)


def probackup_phases(lines, wall_seconds: float | None = None) -> dict:
    """Where a pg_probackup catchup run's time went, from its timestamped console lines.

    - start: launch until "Start transferring data files" (SSH, the backup's start, file lists);
    - copy: until "wait for pg_stop_backup()" (its threads, file by file, then pg_control);
    - stop: until "Syncing copied files to disk" (pg_backup_stop, the wait for the stop LSN's WAL,
      backup_label, the WAL stream's end);
    - files_sync: until "Files are synced" (its own fsync of each file of the source's list);
    - exit: from there until the process ended (with wall_seconds).

    The timed sync -f of the output that follows is a step of its own (probackup_sync_s).
    """
    marks = _first_marks(lines, PROBACKUP_MARKERS)
    return {'start_s': round(marks['copy'], 6) if 'copy' in marks else None,
            'copy_s': _interval(marks, 'copy', 'stop_backup'),
            'stop_s': _interval(marks, 'stop_backup', 'sync'),
            'files_sync_s': _interval(marks, 'sync', 'synced'),
            'exit_s': (round(wall_seconds - marks['synced'], 6)
                       if wall_seconds is not None and 'synced' in marks else None),
            'complete': all(name in marks for name in ('copy', 'stop_backup', 'sync', 'synced')),
            'marks': marks}


# --- T2 (EXP-11): pg_probackup's and parcopy's command lines --------------------------

PROBACKUP_EXE = '/usr/local/bin/pg_probackup-17'  # install_incumbents.sh's link, on both VMs
# How pg_probackup on dest reaches the source: the harness's key with IdentitiesOnly, BatchMode, no
# connection sharing, and the fastest cipher with AES-NI, a documented setting an operator would pick.
PROBACKUP_SSH_OPTIONS = (('IdentitiesOnly', 'yes'), ('BatchMode', 'yes'), ('ControlMaster', 'no'),
                         ('ControlPath', 'none'), ('Ciphers', 'aes128-gcm@openssh.com'),
                         ('StrictHostKeyChecking', 'accept-new'), ('ServerAliveInterval', '15'))


def probackup_paths(work) -> dict:
    """Where pg_probackup's own files go on dest: its SSH configuration and its file logs."""
    root = f'{work}/probackup'
    return {'root': root, 'ssh_config': f'{root}/ssh_config', 'log_dir': f'{root}/log'}


def probackup_ssh_config(source_ip: str, user: str, key: str) -> str:
    """The SSH configuration pg_probackup on dest uses for the source (--ssh-options '-F ...')."""
    return '\n'.join(['# EXP-11 (T2): how pg_probackup on pgd-dest reaches pgd-source.',
                      f'Host {source_ip}', f'    User {user}', f'    IdentityFile {key}',
                      *(f'    {option} {value}' for option, value in PROBACKUP_SSH_OPTIONS), ''])


def probackup_args(method: str) -> tuple:
    """The catchup flags a probackup* method pins: a full copy, WAL streamed over a temporary slot."""
    return ('-b', 'FULL', '--stream', '--temp-slot', '-j', str(PROBACKUP_VARIANTS[method]['jobs']))


def probackup_catchup_argv(exe: str, method: str, *, source_pgdata, dest_pgdata, source_ip: str, port: int,
                           user: str, database: str, ssh_user: str, ssh_config) -> list[str]:
    """pg_probackup catchup on dest, from the live source over SSH (remote mode) into an empty O."""
    return [str(exe), 'catchup', *probackup_args(method), f'--source-pgdata={source_pgdata}',
            f'--destination-pgdata={dest_pgdata}', f'--pghost={source_ip}', f'--pgport={port}',
            f'--pguser={user}', f'--pgdatabase={database}', '--remote-proto=ssh', f'--remote-host={source_ip}',
            f'--remote-user={ssh_user}', f'--remote-path={Path(str(exe)).parent}',
            f'--ssh-options=-F {ssh_config}', '--log-level-console=INFO']


# The release whose catchup EXP-11 read (install_incumbents.sh installs it): its own flush covers the
# source's file list and pg_control only, so probackup* end with a timed sync -f.
PROBACKUP_VERSION = '2.5.16'
_PROBACKUP_VERSION = re.compile(r'\b(\d+\.\d+\.\d+)\b')
_PROBACKUP_PG = re.compile(r'PostgreSQL (\d+)')
# What pg_probackup's remote mode always adds to its ssh command line (src/utils/remote.c, 2.5.16).
PROBACKUP_SSH_FIXED_OPTIONS = ('-o', 'PasswordAuthentication=no', '-o', 'Compression=no', '-o',
                               'ControlMaster=no', '-o', 'LogLevel=error')


def probackup_version(text) -> dict | None:
    """{'version', 'postgresql'} from `pg_probackup --version` ("pg_probackup-17 2.5.16 (PostgreSQL 17)");
    None without a version number."""
    found = _PROBACKUP_VERSION.search(text or '')
    if found is None:
        return None
    major = _PROBACKUP_PG.search(text)
    return {'version': found.group(1), 'postgresql': major.group(1) if major else None}


def probackup_help_problems(help_text: str, argv) -> list[str]:
    """What `pg_probackup catchup --help` lacks for a catchup command line: FULL among its catchup modes,
    -j, and every long option the command line passes."""
    text = help_text or ''
    problems = []
    modes = re.search(r'--backup-mode=\S+\s+catchup mode=([A-Z|]+)', text)
    if modes is None or 'FULL' not in modes.group(1).split('|'):
        problems.append('-b FULL is not among its catchup modes')
    if re.search(r'-j, --threads\b', text) is None:
        problems.append('-j (--threads) is not listed')
    for part in argv:
        option = str(part).split('=', 1)[0]
        if option.startswith('--') and re.search(rf'(^|[\s\[,]){re.escape(option)}\b', text, re.MULTILINE) is None:
            problems.append(f'{option} is not listed')
    return problems


def probackup_ssh_argv(exe, *, source_ip: str, ssh_user: str, ssh_config) -> list[str]:
    """The ssh command pg_probackup's remote mode runs to start its agent on the source, as 2.5.16 builds
    it (-l, our --ssh-options, its own options, the host, `<remote-path>/<its name> agent`), with
    --version in place of agent: the preflight's proof that dest reaches the source's binary that way."""
    exe = Path(str(exe))
    return ['ssh', '-l', ssh_user, '-F', str(ssh_config), *PROBACKUP_SSH_FIXED_OPTIONS, source_ip,
            f'{exe.parent}/{exe.name} --version']


def probackup_outcome(started: bool, logical_match, reference_match) -> str:
    """'pass', 'fail' or 'void' for a probackup* run at the trial's end (EXP-11's Verification, A4).

    An output that doesn't start fails. One whose per-table digest differs from the source's is void
    if the trial's two reference digests differ (the source changed during the trial, so the tool
    can't be blamed), and fails otherwise, an unknown comparison included.
    """
    if not started:
        return 'fail'
    if logical_match:
        return 'pass'
    return 'void' if reference_match is False else 'fail'


# probackup*'s byte diagnostic (EXP-11's Verification): every file the trial's reference digest selects
# under these directories, global/pg_control left out (reference_excluded), against the output's by
# SHA-256, and the output's extra files listed. It decides nothing.
BYTE_DIAGNOSTIC_DIRECTORIES = ('base', 'global')


def byte_view(digest: dict) -> dict:
    """A digest's regular files under BYTE_DIAGNOSTIC_DIRECTORIES, without what a clean stop rewrites."""
    entries = {path: entry for path, entry in digest['entries'].items()
               if entry[0] == 'file' and path.split('/', 1)[0] in BYTE_DIAGNOSTIC_DIRECTORIES
               and not reference_excluded(path)}
    return {**digest, 'entries': entries, 'files': len(entries),
            'bytes': sum(entry[1] for entry in entries.values())}


def byte_diagnostic_paths(root) -> list[str]:
    """The output's regular files under BYTE_DIAGNOSTIC_DIRECTORIES, relative, for files_digest."""
    root = Path(root)
    paths = []
    for top in BYTE_DIAGNOSTIC_DIRECTORIES:
        for directory, _, filenames in os.walk(root / top):
            for name in filenames:
                path = Path(directory) / name
                rel = path.relative_to(root).as_posix()
                if stat.S_ISREG(os.lstat(path).st_mode) and not reference_excluded(rel):
                    paths.append(rel)
    return sorted(paths)


def byte_diagnostic(reference: dict, output: dict, limit: int = 20) -> dict:
    """The reference's selected files under base/ and global/ against the output's (byte_view of each).

    identical: every selected file is in the output with the same size and SHA-256. The output's extra
    files are listed apart and don't count against it.
    """
    expected, actual = byte_view(reference)['entries'], byte_view(output)['entries']
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    differing = sorted(path for path in expected.keys() & actual.keys() if expected[path][:3] != actual[path][:3])
    return {'identical': not missing and not differing, 'selected_files': len(expected),
            'selected_bytes': sum(entry[1] for entry in expected.values()), 'output_files': len(actual),
            'missing_count': len(missing), 'differing_count': len(differing), 'extra_count': len(extra),
            'missing': missing[:limit], 'differing': differing[:limit], 'extra': extra[:limit]}


# parcopy (experiments/network/parcopy.py, run as a module from the repository on both VMs).
PARCOPY_MODULE = 'experiments.network.parcopy'
PARCOPY_CHUNK_BYTES = 64 << 20  # ranges of at most this, assigned largest first to the lightest stream
PARCOPY_WRITEBACK_BYTES = 8 << 20  # EXP-10's fixed write-back (pgdelta.flush.start_writeback)


def parcopy_args(method: str) -> dict:
    """{'send': argv, 'receive': argv}: the flags a parcopy* method pins on each end."""
    streams = ('--streams', str(PARCOPY_VARIANTS[method]['streams']))
    return {'send': (*streams, '--chunk-bytes', str(PARCOPY_CHUNK_BYTES)),
            'receive': (*streams, '--writeback-bytes', str(PARCOPY_WRITEBACK_BYTES))}


def parcopy_summary(stats) -> dict:
    """The CSV's parcopy_* columns from the receiver's --stats-json.

    Expected shape: {'listening_s', 'streams': [{'bytes', 'first_byte_s', 'last_byte_s', 'write_s'}, ...],
    'fsync_s' (the fsync tail), 'fsync_calls', 'fsync_expected'}; missing keys leave columns None.
    Times are seconds since the receiver started; the transfer step starts once it is listening
    (listening_s). balance is the busiest stream's bytes over the mean (EXP-11's A3: at most 1.1).
    """
    stats = stats if isinstance(stats, dict) else {}
    streams = [stream for stream in stats.get('streams') or () if isinstance(stream, dict)]
    sizes = [stream['bytes'] for stream in streams if isinstance(stream.get('bytes'), int)]
    mean = sum(sizes) / len(sizes) if sizes else None

    def extreme(key, pick):
        values = [stream[key] for stream in streams if isinstance(stream.get(key), (int, float))]
        return round(pick(values), 6) if values else None

    calls, expected = stats.get('fsync_calls'), stats.get('fsync_expected')
    return {'streams': len(streams) or None, 'bytes': sum(sizes) if sizes else None,
            'max_stream_bytes': max(sizes) if sizes else None,
            'balance': round(max(sizes) / mean, 4) if sizes and mean else None,
            'listening_s': stats.get('listening_s'),
            'first_byte_s': extreme('first_byte_s', min), 'last_byte_s': extreme('last_byte_s', max),
            'write_s': extreme('write_s', max), 'fsync_s': stats.get('fsync_s'),
            'fsync_calls': calls, 'fsync_expected': expected,
            'fsync_ok': None if calls is None or expected is None else calls == expected}


def fsyncs_expected(digest: dict) -> int:
    """The fsyncs a durable copy owes for a tree_digest of it: its files, its directories, and its top one."""
    kinds = [entry[0] for entry in digest['entries'].values()]
    return kinds.count('file') + kinds.count('dir') + 1


def parcopy_fsync_count(record: dict) -> dict:
    """EXP-11's A5 for a parcopy* run: its receiver's fsync calls against what they must be.

    Its inventory's files plus directories plus one for the output's top directory, as the receiver
    counted them (fsync_expected), and as the output holds them (verify_direct's
    fsync_expected_from_output, when there). ok is None while the receiver's counts are missing.
    """
    summary = _get(record, 'tool_reported', 'parcopy_summary') or {}
    calls, expected = summary.get('fsync_calls'), summary.get('fsync_expected')
    held = _get(record, 'verification', 'fsync_expected_from_output')
    ok = None if calls is None or expected is None else calls == expected and (held is None or calls == held)
    return {'calls': calls, 'expected': expected, 'expected_from_output': held, 'ok': ok}


# --- T2 (EXP-11): durability, reference digests and probes ---------------------------

# Check (a): Dirty + Writeback on dest may grow by at most this over the run, or by the idle noise
# the smoke run measures, whichever is larger. One unflushed 16 MiB WAL segment fails it.
DIRTY_LIMIT_BYTES = 4 << 20
# Check (b): a full copy's bytes written to dest's md array in the window, at least this share of
# the output's bytes.
DISK_WRITE_FRACTION = 0.98


def phase_disk_bytes(record: dict, host: str, key: str):
    """Bytes read or written (`key`) by the data arrays (DATA_ARRAY) of one VM over the timed phases.

    None if a phase has no disk counters or the VM has no md array.
    """
    phases = list((record.get('phases') or {}).values())
    if not phases:
        return None
    total = 0
    for phase in phases:
        disks = _get(phase, 'disk', host)
        if not isinstance(disks, dict):
            return None
        arrays = [counters[key] for device, counters in disks.items() if DATA_ARRAY.match(device)]
        if not arrays:
            return None
        total += sum(arrays)
    return total


def durability_check(record: dict, output_bytes, idle_noise=None, noise_table=None) -> dict:
    """EXP-11's checks (a) and (b) on one run's timed phases (one counter window each).

    (a) dest's Dirty + Writeback, read just before the clock starts and first after it stops, may
    grow by at most max(DIRTY_LIMIT_BYTES, idle_noise). (b) for a full copy, or staged pgdelta's new O
    (DISK_WRITE_CHECK_METHODS, EXP-12), the bytes dest's md array wrote in the window are at least
    DISK_WRITE_FRACTION of output_bytes. `ok` is whether both hold (b only where it applies); None while a
    needed counter is missing. `durable` says whether the arm is meant to be durable (full_nosync is not:
    it is the control); `a_required` whether it writes on dest at all (EXP-12's arms that don't,
    NO_DEST_WRITE_METHODS, are checked all the same). A parcopy* run whose fsync count is not its
    inventory's (A5, parcopy_fsync_count) fails (a).

    T12 (EXP-13): with noise_table (window length in seconds -> bytes, noise_table()), (a)'s limit follows the
    run's clock: max(DIRTY_LIMIT_BYTES, the noise of the shortest length that covers it; noise_limit), and
    the length is recorded (noise_window_s; noise_beyond_table when the clock outlasts every length).
    """
    method = record.get('method')
    phases = list((record.get('phases') or {}).values())
    before = _get(phases[0], 'memory', 'dest', 'dirty_writeback_bytes_before') if phases else None
    after = _get(phases[-1], 'memory', 'dest', 'dirty_writeback_bytes_after') if phases else None
    increase = None if before is None or after is None else after - before
    limit = max(DIRTY_LIMIT_BYTES, int(idle_noise or 0))
    by_clock = None
    if noise_table:
        by_clock = noise_limit(noise_table, (record.get('totals') or {}).get('wall_seconds'))
        limit = max(DIRTY_LIMIT_BYTES, by_clock['noise_bytes'])
    a_ok = None if increase is None else increase <= limit
    fsync_count = parcopy_fsync_count(record) if method in PARCOPY_METHODS else None
    if fsync_count is not None:
        if fsync_count['ok'] is False:
            a_ok = False
        elif fsync_count['ok'] is None and a_ok:
            a_ok = None
    written = phase_disk_bytes(record, 'dest', 'write_bytes')
    b_required = method in DISK_WRITE_CHECK_METHODS
    needed = None if not b_required or output_bytes is None else math.ceil(DISK_WRITE_FRACTION * output_bytes)
    b_ok = None if needed is None or written is None else written >= needed
    if a_ok is False or b_ok is False:
        ok = False
    elif a_ok is None or (b_required and b_ok is None):
        ok = None
    else:
        ok = True
    return {'durable': method not in NON_DURABLE_METHODS, 'ok': ok,
            'a_required': method not in NO_DEST_WRITE_METHODS,
            'md_write_ratio': (None if written is None or not output_bytes
                               else round(written / output_bytes, 4)),
            'dirty_writeback_before_bytes': before, 'dirty_writeback_after_bytes': after,
            'dirty_writeback_increase_bytes': increase, 'limit_bytes': limit,
            'idle_noise_bytes': None if idle_noise is None else int(idle_noise), 'a_ok': a_ok,
            'disk_write_bytes': written, 'output_bytes': output_bytes, 'b_required': b_required,
            'b_min_bytes': needed, 'b_ok': b_ok,
            **({'fsync_count': fsync_count} if fsync_count is not None else {}),
            **({'noise_window_s': by_clock['window_s'], 'noise_beyond_table': by_clock['beyond_table'],
                'noise_table_bytes': {str(length): value for length, value in sorted(noise_table.items())}}
               if by_clock is not None else {})}


# EXP-11's directory rule (a directory change is durable once a later fsync or syncfs on the same
# filesystem commits a journal transaction), and so pg_basebackup's and parcopy's not fsyncing the
# output's parent, rest on ext4's ordered journal: /data must be ext4 as startup.sh makes it, its journal
# in data=ordered mode (the default, which the mount options need not list), with write barriers on.
DATA_MOUNT_REFUSED_OPTIONS = ('data=writeback', 'data=journal', 'nobarrier', 'barrier=0')


def data_mount_problem(text) -> str | None:
    """Why /data, as `findmnt -n -o SOURCE,FSTYPE,OPTIONS,SIZE /data` reports it (agent.py env's
    data_mount), isn't the ext4 in data=ordered mode EXP-11's durability definition assumes; None if it is."""
    fields = (text or '').split()
    if len(fields) < 3:
        return f'findmnt reports no filesystem at /data ({text!r})'
    fstype, options = fields[1], fields[2].split(',')
    if fstype != 'ext4':
        return f'/data is {fstype}, not ext4'
    refused = [option for option in options if option in DATA_MOUNT_REFUSED_OPTIONS]
    if refused:
        return f'/data is ext4 mounted with {", ".join(refused)}'
    return None


# What every clean stop of the source rewrites, left out of the reference digests (EXP-11's steps 2
# and 7): pg_control, the WAL, and (since PostgreSQL 15) the cumulative statistics in pg_stat/.
REFERENCE_EXCLUDED_FILES = ('global/pg_control',)
REFERENCE_EXCLUDED_DIRECTORIES = ('pg_wal', 'pg_stat')


def reference_excluded(path: str) -> bool:
    return path in REFERENCE_EXCLUDED_FILES or path.split('/', 1)[0] in REFERENCE_EXCLUDED_DIRECTORIES


def reference_view(digest: dict) -> dict:
    """A files_digest of the stopped source without what a clean stop rewrites (REFERENCE_EXCLUDED_*)."""
    entries = {path: entry for path, entry in digest['entries'].items() if not reference_excluded(path)}
    return {**digest, 'entries': entries, 'files': len(entries),
            'bytes': sum(entry[1] for entry in entries.values()),
            'excluded': sorted(path for path in digest['entries'] if reference_excluded(path)),
            'missing': [path for path in digest.get('missing', ()) if not reference_excluded(path)]}


def split_files(files, parts: int) -> list[list]:
    """(size, path) pairs in lists of about equal bytes: largest first, each to the lightest list.

    EXP-07's C4 split its 4 readers' files this way; the read probes do too.
    """
    lists, loads = [[] for _ in range(parts)], [0] * parts
    for size, path in sorted(files, key=lambda item: (-item[0], str(item[1]))):
        lightest = loads.index(min(loads))
        lists[lightest].append((size, path))
        loads[lightest] += size
    return lists


PSQL_TIMING = re.compile(r'^Time: (\d+(?:\.\d+)?) ms', re.MULTILINE)


def psql_timings(text: str) -> list[float]:
    """Seconds of each statement psql's \\timing printed ("Time: 12.345 ms"), in order."""
    return [round(float(value) / 1000, 6) for value in PSQL_TIMING.findall(text)]


def digest_summary(digest: dict) -> dict:
    """A short, comparable form of a tree digest for the result records."""
    listing = hashlib.sha256()
    for path, entry in sorted(digest['entries'].items()):
        listing.update(f'{path}\0{entry[0]}\0{entry[1]}\0{entry[2]}\n'.encode())
    return {'files': digest['files'], 'bytes': digest['bytes'],
            'entries': len(digest['entries']), 'listing_sha256': listing.hexdigest()}


# --- T10a (EXP-12): the load, its logs and the oracle ----------------------------------

# The load's database, beside the benchmark's postgres in the same cluster: pgbench -i -s 2 -F 90 (200,000
# accounts, about 30 MB, 10% free per page so the updates are HOT) and load_progress, one row per client, each
# on its own page (fillfactor 10, a 900-byte filler). Autovacuum is off for all of them.
LOAD_DATABASE = 'loaddb'
LOAD_SCALE = 2
LOAD_FILLFACTOR = 90
LOAD_ACCOUNTS = 100_000 * LOAD_SCALE
LOAD_PROGRESS_FILLFACTOR = 10
LOAD_FILLER_BYTES = 900
LOAD_TABLES = ('pgbench_accounts', 'pgbench_branches', 'pgbench_tellers', 'pgbench_history', 'load_progress')
# Added to the source's postgresql.conf for every arm alike (EXP-12's Setup), then a restart: no checkpoint
# starts inside a window except one an arm asks for; WAL write and sync times; checkpoints and autovacuum
# runs logged. synchronous_commit and shared_buffers are EXP-01's, pinned here.
LOAD_SERVER_CONFIG = {'checkpoint_timeout': '30min', 'max_wal_size': '16GB', 'track_wal_io_timing': 'on',
                      'log_checkpoints': 'on', 'log_autovacuum_min_duration': '0', 'synchronous_commit': 'on',
                      'shared_buffers': '128MB'}
# The scripts (experiments/network/load/), in -f order: pgbench's script number in each log line.
LOAD_SCRIPTS = (('commit.sql', 99), ('abort.sql', 1))
COMMIT_SCRIPT, ABORT_SCRIPT = 0, 1
# The rolled-back change; a balance this large carries one (each commit moves a balance by at most 5,000).
LOAD_ABORT_DELTA = 1_000_000_000
LOAD_LEAK_THRESHOLD = LOAD_ABORT_DELTA // 2
# Where each pgbench run writes on the source (tmpfs, so logging adds no disk I/O), and its registry there.
LOAD_LOG_DIR = '/dev/shm/exp12'
LOAD_STATE = 'load.json'
# pgbench_load_argv's fixed options: no vacuum, prepared statements, per-transaction logs, 1-s progress lines
# with Unix timestamps. -R makes it open loop on a Poisson schedule, latency measured from the schedule.
PGBENCH_FIXED = ('-n', '-M', 'prepared')


def pgbench_load_argv(pgbench, *, rate, clients: int, threads: int, duration_s: int, seed: int, prefix,
                      scripts, host, port: int, user: str, database: str = LOAD_DATABASE) -> list[str]:
    """One pgbench run of the load: open loop at `rate` per second, or closed loop (C) when rate is 0 or None."""
    argv = [str(pgbench), *PGBENCH_FIXED, '-c', str(clients), '-j', str(threads)]
    if rate:
        argv += ['-R', str(rate)]
    argv += ['-T', str(duration_s), '--random-seed', str(seed)]
    for name, weight in LOAD_SCRIPTS:
        argv += ['-f', f'{scripts}/{name}@{weight}']
    return argv + ['-l', '--log-prefix', str(prefix), '-P', '1', '--progress-timestamp', '-h', str(host),
                   '-p', str(port), '-U', user, database]


def load_state_path(directory) -> Path:
    return Path(directory) / LOAD_STATE


def read_load_state(directory) -> dict:
    """The load's registry on the source: {'runs': [...], 'current': run_id or None}; empty if absent."""
    try:
        return json.loads(load_state_path(directory).read_text())
    except (OSError, ValueError):
        return {'runs': [], 'current': None}


def write_load_state(directory, state: dict) -> None:
    path = load_state_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=1, sort_keys=True))
    temporary.replace(path)


def load_run(state: dict, run_id=None) -> dict | None:
    """A run of the registry by its id, or the current one."""
    wanted = run_id or state.get('current')
    return next((run for run in state.get('runs', ()) if run['run_id'] == wanted), None)


def progress_path(directory, run_id: str) -> Path:
    """pgbench's -P 1 lines (its stderr) for one run."""
    return Path(directory) / f'{run_id}-progress.log'


def summary_path(directory, run_id: str) -> Path:
    """pgbench's closing summary (its stdout) for one run."""
    return Path(directory) / f'{run_id}-summary.log'


def pgbench_log_names(run: dict) -> list[str]:
    """The per-transaction log files of a run: PREFIX.PID for its first thread, PREFIX.PID.N for the others."""
    base = f'{run["run_id"]}.{run["pid"]}'
    return [base] + [f'{base}.{thread}' for thread in range(1, int(run['threads']))]


def log_lines(path):
    """The lines of a pgbench log, zstd-compressed (.zst, through `zstd -dc`) or not."""
    path = Path(path)
    if path.suffix != '.zst':
        with open(path, encoding='ascii', errors='replace') as stream:
            yield from stream
        return
    import subprocess
    process = subprocess.Popen(['zstd', '-dc', str(path)], stdout=subprocess.PIPE, text=True,
                               encoding='ascii', errors='replace')
    try:
        yield from process.stdout
    finally:
        process.stdout.close()
        if process.wait() != 0:
            raise ValueError(f'zstd -dc {path} failed ({process.returncode})')


def existing_log(directory, name: str) -> Path:
    """name in directory, or its compressed form name.zst where only that exists."""
    path = Path(directory) / name
    compressed = path.with_name(path.name + '.zst')
    return compressed if not path.exists() and compressed.exists() else path


def parse_pgbench_line(line: str):
    """(client, transaction, latency_us, script, end_us, lag_us) of one pgbench -l line, or None.

    PostgreSQL 17 writes `client_id transaction_no time script_no time_epoch time_us [schedule_lag]`: time is
    the latency in microseconds (from the scheduled start under -R), 'failed' for a failed transaction; the
    completion time is time_epoch seconds plus time_us. latency_us is None for a failed one, lag_us None
    without -R.
    """
    fields = line.split()
    if len(fields) < 6:
        return None
    try:
        latency = None if fields[2] in ('failed', 'skipped') else int(fields[2])
        lag = int(fields[6]) if len(fields) > 6 else None
        return (int(fields[0]), int(fields[1]), latency, int(fields[3]),
                int(fields[4]) * 1_000_000 + int(fields[5]), lag)
    except ValueError:
        return None


def transaction_times(parsed) -> tuple[int, int, int]:
    """(scheduled, started, ended) in microseconds of a parsed line: the schedule is the completion minus the
    latency, and the actual start the schedule plus the lag (without -R, both are the start)."""
    _, _, latency, _, end, lag = parsed
    scheduled = end - (latency or 0)
    return scheduled, scheduled + (lag or 0), end


def count_commits(paths) -> dict[int, int]:
    """client -> its committed transactions (commit.sql, not failed) over the given log files."""
    counts: dict[int, int] = {}
    for path in paths:
        for line in log_lines(path):
            parsed = parse_pgbench_line(line)
            if parsed and parsed[3] == COMMIT_SCRIPT and parsed[2] is not None:
                counts[parsed[0]] = counts.get(parsed[0], 0) + 1
    return counts


def log_tails(paths) -> dict[int, dict]:
    """client -> {'lines', 'contiguous', 'last_end_us'} over a run's log files: whether its transaction numbers (every
    line, failed ones too) run 1, 2, 3, ... without a gap, and its last completion. EXP-12's SIGINT fallback judges a
    run's log by them (load_log_complete)."""
    tails: dict[int, dict] = {}
    for path in paths:
        for line in log_lines(path):
            parsed = parse_pgbench_line(line)
            if not parsed:
                continue
            client, number, end = parsed[0], parsed[1], parsed[4]
            tail = tails.setdefault(client, {'lines': 0, 'contiguous': True, 'last_end_us': None})
            tail['lines'] += 1
            tail['contiguous'] = tail['contiguous'] and number == tail['lines']
            tail['last_end_us'] = end if tail['last_end_us'] is None else max(tail['last_end_us'], end)
    return tails


# EXP-12's SIGINT fallback (The load): every line up to this long before the signal must be in the log.
SIGINT_LOG_MARGIN_S = 1.0


def load_log_complete(tails: dict, clients, signalled_utc: float) -> dict:
    """The registered completeness rule for a run stopped with SIGINT, which pgbench 17 doesn't catch (only -T's
    SIGALRM ends it as -T would), so each thread's buffered last lines are lost and the counters can't match the
    log: every line up to 1 s before the signal present. Per client, its transaction numbers contiguous from 1 and
    its last completion at or after the signal less 1 s. {'complete', 'short': {client: why}}."""
    limit_us = int(round((signalled_utc - SIGINT_LOG_MARGIN_S) * 1_000_000))
    short = {}
    for client in clients:
        tail = tails.get(int(client))
        if tail is None:
            short[str(client)] = 'no line'
        elif not tail['contiguous']:
            short[str(client)] = 'a gap in its transaction numbers'
        elif tail['last_end_us'] < limit_us:
            short[str(client)] = (f'its last line {(limit_us - tail["last_end_us"]) / 1e6:.3f} s before the signal '
                                  f'less {SIGINT_LOG_MARGIN_S:g} s')
    return {'complete': not short, 'short': short}


def pick_commits(paths, wanted: dict) -> dict:
    """{(client, position): (scheduled, started, ended)} for the wanted commits of one run's logs.

    wanted maps a client to the positions (0-based, among its committed transactions in this run, in order) to
    find. Each client is in one thread's file, its lines in completion order, so one pass finds them all.
    """
    found, seen = {}, {}
    for path in paths:
        for line in log_lines(path):
            parsed = parse_pgbench_line(line)
            if not parsed or parsed[3] != COMMIT_SCRIPT or parsed[2] is None:
                continue
            client = parsed[0]
            position = seen.get(client, 0)
            seen[client] = position + 1
            if position in wanted.get(client, ()):
                found[(client, position)] = transaction_times(parsed)
    return found


PROGRESS_LINE = re.compile(r'^progress: (\d+(?:\.\d+)?) s, (\d+(?:\.\d+)?) tps, lat (\d+(?:\.\d+)?) ms '
                           r'stddev (\S+?)(?:, (\d+) failed)?(?:, lag (\d+(?:\.\d+)?) ms)?(?:, (\d+) skipped)?\s*$')


def parse_progress_line(line: str) -> dict | None:
    """One pgbench -P line with --progress-timestamp: its Unix time (the end of its second), tps, latency and
    schedule lag (ms; None without -R); None for another line."""
    found = PROGRESS_LINE.match(line.strip())
    if not found:
        return None
    stamp, tps, latency, stddev, failed, lag, skipped = found.groups()
    try:
        deviation = float(stddev)
    except ValueError:
        deviation = None
    return {'t': float(stamp), 'tps': float(tps), 'lat_ms': float(latency), 'stddev_ms': deviation,
            'failed': int(failed) if failed else 0, 'lag_ms': float(lag) if lag is not None else None,
            'skipped': int(skipped) if skipped else 0}


def parse_progress(text: str) -> list[dict]:
    return [parsed for line in text.splitlines() if (parsed := parse_progress_line(line))]


# EXP-12's settle gate, live part (A8): over the settle's last 10 s, every second's completions within +-5% of R
# and the mean schedule lag under 1 ms, from pgbench's -P 1 lines. Its p99 and late parts are the analysis's.
SETTLE_WINDOW_S = 10
SETTLE_TOLERANCE = 0.05
SETTLE_LAG_MS = 1.0


def settle_gate(lines, now: float, rate: float, *, window_s: int = SETTLE_WINDOW_S,
                tolerance: float = SETTLE_TOLERANCE, lag_ms: float = SETTLE_LAG_MS) -> dict:
    """Whether the progress lines of the last window_s seconds before `now` pass the gate.

    Each line covers the second that ends at its stamp, so the window holds the lines stamped in
    (now - window_s, now]; fewer than window_s of them fail it (pgbench just started, or stalled).
    """
    recent = [line for line in lines if now - window_s < line['t'] <= now + 1e-6]
    lags = [line['lag_ms'] for line in recent if line['lag_ms'] is not None]
    low, high = rate * (1 - tolerance), rate * (1 + tolerance)
    off_rate = [line['tps'] for line in recent if not low <= line['tps'] <= high]
    mean_lag = round(sum(lags) / len(lags), 4) if lags else None
    passed = (len(recent) >= window_s and not off_rate and mean_lag is not None and mean_lag < lag_ms)
    return {'passed': passed, 'lines': len(recent), 'off_rate': off_rate, 'mean_lag_ms': mean_lag,
            'tps_range': [low, high], 'evaluated_utc': now}


# The counters read around every pgbench start and stop, with the load stopped: each client's n, and the sums.
LOAD_COUNTERS_SQL = ("SELECT json_build_object('n', (SELECT json_object_agg(client, n ORDER BY client) FROM "
                     "load_progress), 'sum_n', (SELECT sum(n) FROM load_progress), 'sum_abalance', (SELECT "
                     "sum(abalance) FROM pgbench_accounts))::text;")


def load_oracle_sql() -> str:
    """(iii) on a copy's loaddb, as JSON: the sums of abalance and n, the accounts, how many balances carry a
    rolled-back change (|abalance| at least LOAD_LEAK_THRESHOLD), and each client's n (for the cut, (iv))."""
    return ("SELECT json_build_object('sum_abalance', a.s, 'accounts', a.c, 'leaked', a.l, 'sum_n', p.s, "
            "'clients', p.c, 'n', p.n)::text FROM (SELECT sum(abalance) AS s, count(*) AS c, count(*) FILTER "
            f"(WHERE abs(abalance) >= {LOAD_LEAK_THRESHOLD}) AS l FROM pgbench_accounts) a, (SELECT sum(n) AS s, "
            "count(*) AS c, json_object_agg(client, n ORDER BY client) AS n FROM load_progress) p;")


def atomicity(answer: dict) -> dict:
    """(iii): the two sums equal, and no balance carrying a rolled-back change."""
    sum_abalance, sum_n = answer.get('sum_abalance'), answer.get('sum_n')
    equal = sum_abalance is not None and sum_n is not None and int(sum_abalance) == int(sum_n)
    leaked = int(answer.get('leaked') or 0)
    return {'ok': equal and leaked == 0, 'sums_equal': equal, 'leaked': leaked,
            'difference': None if sum_abalance is None or sum_n is None else int(sum_abalance) - int(sum_n)}


def row_counts_sql(tables=LOGICAL_TABLES) -> str:
    """(vi) in every run: the benchmark database's tables' row counts, as JSON."""
    parts = ', '.join(f"'{table}', (SELECT count(*) FROM {table})" for table, _ in tables)
    return f'SELECT json_build_object({parts})::text;'


def compare_rows(reference: dict, counts: dict) -> tuple[bool, list[str]]:
    """Row counts against a logical digest's ({table: {rows, md5}}) or another count's."""
    differences = []
    for table in sorted(reference.keys() | counts.keys()):
        left, right = reference.get(table), counts.get(table)
        left = left.get('rows') if isinstance(left, dict) else left
        right = right.get('rows') if isinstance(right, dict) else right
        if left is None or right is None or int(left) != int(right):
            differences.append(f'{table}: {left} rows in the reference, {right} in the copy')
    return not differences, differences


# (vii): the schema dumps, with a fixed \restrict key (since PostgreSQL 17.6 a plain dump carries a random one
# otherwise), of the load's and the benchmark's databases.
SCHEMA_RESTRICT_KEY = 'exp12'
SCHEMA_DATABASES = (LOAD_DATABASE, 'postgres')


def schema_dump_argv(pg_dump, *, host, port: int, user: str, database: str) -> list[str]:
    return [str(pg_dump), '--schema-only', f'--restrict-key={SCHEMA_RESTRICT_KEY}', '-h', str(host), '-p',
            str(port), '-U', user, '-d', database]


def amcheck_argv(pg_amcheck, *, host, port: int, user: str, database: str = LOAD_DATABASE) -> list[str]:
    """pg_amcheck of every relation of loaddb, heap tuples against their index entries too; --verbose names
    each relation it checks, so a run that checked none is seen."""
    return [str(pg_amcheck), '--heapallindexed', '--verbose', '-h', str(host), '-p', str(port), '-U', user,
            '-d', database]


AMCHECK_CHECKING = re.compile(r'pg_amcheck: checking (heap table|btree index) "')
AMCHECK_SKIPPING = re.compile(r'pg_amcheck: warning: skipping database')


def amcheck_result(exit_code, text: str) -> dict:
    """pg_amcheck's verdict: 'ok', 'wrong' (it reported corruption or an error) or 'unverified' (it skipped the
    database, which lacks the amcheck extension, or checked no relation)."""
    lines = text.splitlines()
    checked = sum(1 for line in lines if AMCHECK_CHECKING.search(line))
    skipped = [line for line in lines if AMCHECK_SKIPPING.search(line)]
    problems = [line for line in lines if line.strip() and not AMCHECK_CHECKING.search(line)
                and not line.startswith('pg_amcheck: including database')]
    if skipped or checked == 0:
        status = 'unverified'
    elif exit_code != 0:
        status = 'wrong'
    else:
        status = 'ok'
    return {'status': status, 'exit_code': exit_code, 'relations_checked': checked, 'skipped': skipped,
            'problems': problems[:20]}


# The oracle's checks, in the order a started copy meets them: start (it starts and answers), atomic (iii), cut
# (iv), coverage (v), rows (vi, with the static digest in a cell's first repeat), amcheck, schema (vii).
ORACLE_CHECKS = ('start', 'atomic', 'cut', 'coverage', 'rows', 'amcheck', 'schema')
# EXP-12's smoke item 5: the oracle's seven negative controls, each the check it must fail through. The index
# page's also needs (iii) to pass, since no other check reads that page. The coverage control is the analysis's.
ORACLE_CONTROLS = {
    'no_wal': 'start',  # a copy made with -X none: it doesn't start
    'n_raised': 'atomic',  # one client's n raised alone, on a started copy
    'cut_extended': 'cut',  # one client's prefix extended by 100: its n and one balance both raised by 100
    'cut_back': 'cut',  # the same cut back by 100
    'heap_page': 'atomic',  # a heap page of loaddb's pgbench_accounts overwritten on a stopped copy: (iii) errors
    'index_page': 'amcheck',  # a leaf page of pgbench_accounts_pkey overwritten the same way
    'schema': 'schema',  # a column added to load_progress on a started copy
}
ORACLE_CONTROL_SHIFT = 100
ORACLE_CONTROL_PASSING = {'index_page': ('atomic',)}


def oracle_control_verdict(name: str, statuses: dict) -> dict:
    """Whether a negative control failed through its own check (and passed those it must pass): `ok`."""
    target = ORACLE_CONTROLS[name]
    failed = sorted(check for check, status in statuses.items() if status == 'wrong')
    passing = ORACLE_CONTROL_PASSING.get(name, ())
    ok = statuses.get(target) == 'wrong' and all(statuses.get(check) == 'ok' for check in passing)
    return {'control': name, 'target': target, 'must_pass': list(passing), 'failed': failed, 'ok': ok}


def oracle_status(checks: dict) -> str:
    """A live output's status from its checks' ('ok', 'wrong', 'unverified', or None while pending): any wrong is
    'fail'; then anything pending, 'pending'; then anything unverified, 'unverified'; else 'pass'."""
    values = [checks.get(check) for check in ORACLE_CHECKS] + [checks.get('bytes')]
    if 'wrong' in values:
        return 'fail'
    if None in values:
        return 'pending'
    if 'unverified' in values:
        return 'unverified'
    return 'pass'


# --- T10a (EXP-12): the consistent cut and coverage, from the load's logs -----------------

CUT_TOLERANCE_US = 1000  # (iv): within 1 ms
COVERAGE_MARGIN_S = 0.05  # (v): within the recorded offset plus 50 ms


def locate_commit(runs, client: int, index: int):
    """(run_id, position) of a client's index-th commit (0-based, counted over every logged run in order: a
    copy's n), with its position among that run's commits; None if no logged run holds it."""
    key = str(client)
    for run in runs:
        start, stop = (run.get('start') or {}).get('n', {}), (run.get('stop') or {}).get('n', {})
        if key in start and key in stop and int(start[key]) <= index < int(stop[key]):
            return run['run_id'], index - int(start[key])
    return None


def cut_queries(progress: dict, runs) -> dict:
    """client -> {'n', 'held': its last held commit, 'lacking': its first lacking one}, each (run_id, position)
    or None (nothing held; or nothing lacking, past every logged run's end), and 'unmapped' when the copy holds
    a commit no logged run made (n above every run's stop, or below the first run's start)."""
    total = {}
    for run in runs:
        for key, value in ((run.get('stop') or {}).get('n') or {}).items():
            total[key] = max(total.get(key, 0), int(value))
    first = {key: int(value) for key, value in ((runs[0].get('start') or {}).get('n') or {}).items()} if runs else {}
    queries = {}
    for key, n in sorted(progress.items(), key=lambda item: int(item[0])):
        client, n = int(key), int(n)
        held = locate_commit(runs, client, n - 1) if n > first.get(key, 0) else None
        lacking = locate_commit(runs, client, n) if n < total.get(key, 0) else None
        unmapped = n > total.get(key, 0) or n < first.get(key, 0) or (n > first.get(key, 0) and held is None)
        queries[client] = {'n': n, 'held': held, 'lacking': lacking, 'unmapped': unmapped}
    return queries


def wanted_commits(queries_list) -> dict:
    """{run_id: {client: {position, ...}}}: every commit the cut queries of several copies need."""
    wanted: dict = {}
    for queries in queries_list:
        for client, query in queries.items():
            for side in ('held', 'lacking'):
                if query[side] is not None:
                    run_id, position = query[side]
                    wanted.setdefault(run_id, {}).setdefault(client, set()).add(position)
    return wanted


def consistent_cut(queries: dict, commits: dict) -> dict:
    """(iv): no committed transaction the copy lacks completed before one it holds started, within 1 ms.

    commits maps (run_id, client, position) to (scheduled, started, ended) in microseconds (pick_commits per run).
    The cut lies in [lo, hi]: lo the latest start of a last held commit, hi the earliest end of a first lacking
    one; it is consistent if hi >= lo - 1 ms. A client whose n no logged run explains fails it.
    """
    starts, ends, missing = [], [], []
    for client, query in queries.items():
        for side, into, index in (('held', starts, 1), ('lacking', ends, 2)):
            if query[side] is None:
                continue
            times = commits.get((query[side][0], client, query[side][1]))
            if times is None:
                missing.append(f'client {client}: {side} commit {query[side]} not in its log')
            else:
                into.append((times[index], client))
    unmapped = sorted(client for client, query in queries.items() if query['unmapped'])
    lo, hi = (max(starts) if starts else None), (min(ends) if ends else None)
    ok = (not unmapped and not missing
          and (lo is None or hi is None or hi[0] >= lo[0] - CUT_TOLERANCE_US))
    return {'ok': ok, 'lo_us': lo[0] if lo else None, 'lo_client': lo[1] if lo else None,
            'hi_us': hi[0] if hi else None, 'hi_client': hi[1] if hi else None,
            'gap_us': None if lo is None or hi is None else hi[0] - lo[0],
            'unmapped_clients': unmapped, 'missing': missing[:20]}


def coverage(cut: dict, start_utc: float, stop_utc: float, offset_s: float) -> dict:
    """(v): the cut lies between the copy's backup start and its clock stop, both on dest's clock mapped to the
    source's (plus offset_s, source minus dest), within |offset| plus 50 ms. A cut that ends before the window
    opens (a copy of an old state) or begins after it closes fails it."""
    margin = abs(offset_s or 0.0) + COVERAGE_MARGIN_S
    start, stop = start_utc + (offset_s or 0.0), stop_utc + (offset_s or 0.0)
    lo = None if cut.get('lo_us') is None else cut['lo_us'] / 1e6
    hi = None if cut.get('hi_us') is None else cut['hi_us'] / 1e6
    ok = (lo is None or lo <= stop + margin) and (hi is None or hi >= start - margin)
    return {'ok': ok, 'window_source_utc': [round(start, 6), round(stop, 6)], 'margin_s': round(margin, 6),
            'cut_utc': [lo, hi]}


def cut_time(cut: dict) -> float | None:
    """One time for the cut (seconds, the source's clock): its interval's middle, or the end that is known."""
    lo, hi = cut.get('lo_us'), cut.get('hi_us')
    if lo is None and hi is None:
        return None
    if lo is None or hi is None:
        return (lo if hi is None else hi) / 1e6
    return (lo + hi) / 2e6


# --- T10a (EXP-12): the source's cache, its CPU, the sampler and the clocks --------------

def parse_fincore_json(text: str) -> list[dict]:
    """util-linux `fincore --json --bytes`: [{'res': resident bytes, 'size': bytes, 'file': path}, ...]."""
    value = json.loads(text)
    rows = value.get('fincore', []) if isinstance(value, dict) else []
    result = []
    for row in rows:
        result.append({'res': int(row.get('res') or 0), 'size': int(row.get('size') or 0), 'file': row.get('file')})
    return result


def residency(rows) -> dict:
    """Files, bytes and resident bytes of fincore rows, and the resident share (None for no bytes)."""
    size = sum(row['size'] for row in rows)
    resident = sum(row['res'] for row in rows)
    return {'files': len(rows), 'bytes': size, 'resident_bytes': resident,
            'resident_share': round(resident / size, 6) if size else None}


# The cache flag (A8): before a cold window more than 1% of the benchmark database's files or of the snapshot
# resident, or less than 99% of loaddb's; before a warm one, less than 95% of them.
CACHE_COLD_MAX, CACHE_LOAD_MIN, CACHE_WARM_MIN = 0.01, 0.99, 0.95


def cache_flag(fincore: dict, warm: bool) -> bool | None:
    """Whether the cache state before a window raises A8's cache flag; None without the readings."""
    shares = {name: (fincore.get(name) or {}).get('resident_share') for name in ('benchmark', 'snapshot', 'loaddb')}
    if shares['benchmark'] is None or shares['loaddb'] is None:
        return None
    statics = [shares['benchmark']] + ([shares['snapshot']] if shares['snapshot'] is not None else [])
    if warm:
        return any(share < CACHE_WARM_MIN for share in statics) or shares['loaddb'] < CACHE_LOAD_MIN
    return any(share > CACHE_COLD_MAX for share in statics) or shares['loaddb'] < CACHE_LOAD_MIN


# VM-wide busy CPU without steal, as EXP-12 counts the source's CPU-s (steal reported apart).
CPU_BUSY_NO_STEAL = ('user', 'nice', 'system', 'irq', 'softirq')


def busy_seconds(cpu: dict | None, fields=CPU_BUSY_NO_STEAL):
    """The busy seconds of a snapshot_delta's cpu dict over `fields`; None without it."""
    if not isinstance(cpu, dict):
        return None
    try:
        return round(sum(cpu[f'{field}_seconds'] for field in fields), 3)
    except KeyError:
        return None


def phases_cpu(record: dict, host: str = 'source', fields=CPU_BUSY_NO_STEAL):
    """Busy seconds of a host over a run's timed phases (without steal by default); None if any is missing."""
    values = [busy_seconds(_get(phase, 'cpu', host), fields) for phase in (record.get('phases') or {}).values()]
    if not values or any(value is None for value in values):
        return None
    return round(sum(values), 3)


def clock_offset(samples) -> dict:
    """The offset of the far clock (its time minus ours) from (sent, far, received) timestamp exchanges: the one
    with the shortest round trip, where the far stamp is taken half way."""
    best = min(samples, key=lambda sample: sample[2] - sample[0])
    sent, far, received = best
    return {'offset_s': round(far - (sent + received) / 2, 6), 'rtt_s': round(received - sent, 6),
            'samples': len(samples)}


# The sampler (agent.py sampler): the groups whose processes it follows every tick, by name.
SAMPLER_GROUPS = ('pgbench', 'backend', 'walsender', 'checkpointer', 'walwriter', 'bgwriter', 'autovacuum',
                  'postgres_other', 'pg_basebackup', 'pgdelta', 'burn', 'agent', 'sampler')
POSTGRES_AUXILIARY = (('walsender', 'walsender'), ('checkpointer', 'checkpointer'), ('walwriter', 'walwriter'),
                      ('background writer', 'bgwriter'), ('autovacuum worker', 'autovacuum'))


def sampler_group(comm: str, cmdline: str, parent_group: str | None = None) -> str | None:
    """The sampler's group of a process from its comm and command line (its title, for PostgreSQL's), or None for
    one it doesn't follow. A spawned worker of pgdelta is pgdelta's by its parent; burn's processes are forks of
    `agent.py burn`, with its command line."""
    if comm == 'pgbench':
        return 'pgbench'
    if comm == 'pg_basebackup':
        return 'pg_basebackup'
    if cmdline.startswith('postgres: '):
        title = cmdline[len('postgres: '):]
        for prefix, group in POSTGRES_AUXILIARY:
            if title.startswith(prefix):
                return group
        # A client backend's title: user, database, then [local] or host(port), then its state.
        words = title.split()
        return 'backend' if len(words) >= 3 and (words[2] == '[local]' or '(' in words[2]) else 'postgres_other'
    if comm == 'postgres':
        return 'postgres_other'
    if 'pgdelta' in cmdline or parent_group == 'pgdelta':
        return 'pgdelta'
    if 'agent.py' in cmdline:
        if ' burn' in cmdline:
            return 'burn'
        return 'sampler' if ' sampler' in cmdline else 'agent'
    return None


def parse_schedstat(text: str) -> tuple[int, int] | None:
    """(run ns, run-queue wait ns) from /proc/<pid>/task/<tid>/schedstat: time on a CPU, time waiting for one."""
    fields = text.split()
    if len(fields) < 2:
        return None
    return int(fields[0]), int(fields[1])


def parse_proc_running(text: str) -> int | None:
    """procs_running from /proc/stat: the run queue's length now."""
    for line in text.splitlines():
        if line.startswith('procs_running '):
            return int(line.split()[1])
    return None


# Once a second, over one held connection: WAL write and sync counts and times, the checkpointer, and the
# autovacuum workers with their databases. It shows a mechanism; no verdict uses it.
SAMPLER_SQL = ("SELECT json_build_object('t', extract(epoch FROM clock_timestamp()), 'wal', (SELECT row_to_json(w) "
               "FROM pg_stat_wal w), 'checkpointer', (SELECT row_to_json(c) FROM pg_stat_checkpointer c), "
               "'autovacuum', (SELECT coalesce(json_agg(json_build_object('pid', pid, 'datname', datname)), '[]') "
               "FROM pg_stat_activity WHERE backend_type = 'autovacuum worker'))::text")


# The capture's G4 boundary (EXP-12): pg_basebackup --verbose's "write-ahead log end point" line.
WAL_END_LINE = re.compile(r'write-ahead log end point: ([0-9A-Fa-f]+/[0-9A-Fa-f]+)')


def wal_end_point(lines, launched_utc: float | None = None) -> dict | None:
    """The LSN and stamp of a stamped pg_basebackup --verbose log's end point line, with its UTC on the stamping
    clock when the launch's is given; None without the line."""
    for seconds, text in lines:
        found = WAL_END_LINE.search(text)
        if found:
            return {'lsn': found.group(1), 'seconds': seconds,
                    'utc': None if launched_utc is None else round(launched_utc + seconds, 6)}
    return None


# --- T12 (EXP-13): conditions, the emulated WAN and what each visit and run checks -----------------------

# A condition is RATE:RTT_MS:HOST: the link rate in Gbit/s, the added round-trip time r in ms (r/2 on each
# VM's netem) and the host TCP setting: default (Debian's, as recorded at the start) or tuned (only the
# tcp_rmem and tcp_wmem maxima raised, on both VMs).
HOST_TCP_SETTINGS = ('default', 'tuned')
TCP_MEM_SYSCTLS = ('net.ipv4.tcp_rmem', 'net.ipv4.tcp_wmem')
# Every TCP setting each run records on both VMs; only the two maxima ever change.
TCP_SYSCTLS = (*TCP_MEM_SYSCTLS, 'net.ipv4.tcp_congestion_control', 'net.ipv4.tcp_adv_win_scale',
               'net.ipv4.tcp_slow_start_after_idle', 'net.ipv4.tcp_no_metrics_save', 'net.core.rmem_max',
               'net.core.wmem_max', 'net.core.default_qdisc')
TUNED_TCP_MAX_BYTES = 128 << 20
# Debian 12's tcp_rmem and tcp_wmem (kernel 6.1's), as EXP-11's run.json recorded them: the TCP settings table's
# default row. Every invocation must find them on both VMs (run_exp04's check_tcp_defaults).
DEBIAN_TCP_MEM = {'net.ipv4.tcp_rmem': (4096, 131072, 6291456), 'net.ipv4.tcp_wmem': (4096, 16384, 4194304)}
# infra/gcp/wan.sh's tree: the prio root's plain band 1:1, its delayed band 1:2 with netem (20:) and netem's
# tbf child (30:, parent 20:1).
WAN_NETEM_LIMIT = 1000000
WAN_QUEUE_FLOOR_MS = 50
WAN_BURST_FLOOR = 131072
WAN_PLAIN_BAND, WAN_DELAYED_BAND = '1:1', '1:2'
WAN_NETEM_HANDLE, WAN_TBF_HANDLE = '20:', '30:'
QDISC_COUNTERS = ('bytes', 'packets', 'drops', 'overlimits', 'requeues', 'backlog', 'qlen')
QDISC_CUMULATIVE = ('bytes', 'packets', 'drops', 'overlimits', 'requeues')
# The counters nstat reports (its names), from /proc/net/snmp (Tcp) and /proc/net/netstat (TcpExt).
NSTAT_COUNTERS = {'TcpRetransSegs': ('Tcp', 'RetransSegs'),
                  'TcpExtTCPLostRetransmit': ('TcpExt', 'TCPLostRetransmit'),
                  'TcpExtTCPTimeouts': ('TcpExt', 'TCPTimeouts')}
# The visit's checks: 20 pings 0.05 s apart, minimum in [r, r + 1 ms] and median at most minimum + 2 ms; 5
# control no-ops each within 5 ms of the anchor's; per run, the sending sockets' minrtt in [r, r + 2 ms] and
# netem's bytes on the source at least 0.98 x the run's wire bytes from source to dest.
PING_COUNT, PING_INTERVAL_S = 20, 0.05
PING_MIN_SLACK_MS, PING_MEDIAN_SLACK_MS = 1.0, 2.0
CONTROL_NOOPS, CONTROL_NOOP_SLACK_S = 5, 0.005
MINRTT_SLACK_MS = 2.0
# A real path's own round trip (run_exp04 --base-rtt-ms, EXP-15): the checks expect r plus it, with this share of
# it again as slack for the path's own jitter.
BASE_RTT_SLACK_SHARE = 0.05
NETEM_BYTES_FRACTION = 0.98
# The ss sampler: a sending socket carried at least this many bytes (acknowledged), and this share of all.
SS_SENDING_MIN_BYTES, SS_SENDING_SHARE = 1 << 20, 0.01
# The stunnel pair (TLS through a proxy): the sender connects to the source's stunnel on localhost, which
# opens TLS 1.3 to dest's stunnel, which forwards to the receiver listening on localhost.
STUNNEL_CLIENT_PORT, STUNNEL_SERVER_PORT = 7001, 7443
STUNNEL_SERVER_NAME = 'pgd-exp13-server'


def parse_condition(text: str) -> dict:
    """'2:60:tuned' -> a condition: its label (canonical), slug (for file names), the tbf rate (parse_speed's),
    the round trip and its half (each VM's netem delay), and the host setting. The anchor is r = 0."""
    parts = text.strip().lower().split(':')
    if len(parts) != 3:
        raise ValueError(f'a condition is RATE:RTT_MS:HOST, got {text!r}')
    speed = parse_speed(parts[0])
    if speed['gbit'] is None:
        raise ValueError(f'a condition needs a link rate, not {parts[0]!r}')
    try:
        rtt = float(parts[1])
    except ValueError:
        raise ValueError(f'bad round-trip time in {text!r}') from None
    if not math.isfinite(rtt) or rtt < 0:
        raise ValueError(f'the round-trip time in {text!r} must be 0 or more')
    if parts[2] not in HOST_TCP_SETTINGS:
        raise ValueError(f'the host setting in {text!r} must be one of {", ".join(HOST_TCP_SETTINGS)}')
    gbit = speed['gbit']
    return {'label': f'{gbit:g}:{rtt:g}:{parts[2]}', 'slug': f'{gbit:g}g-{rtt:g}ms-{parts[2]}', 'gbit': gbit,
            'tc_rate': speed['tc_rate'], 'rtt_ms': rtt, 'delay_ms': rtt / 2, 'host_tcp': parts[2]}


def parse_conditions(text: str) -> list[dict]:
    conditions = [parse_condition(item) for item in (text or '').split(',') if item.strip()]
    labels = [condition['label'] for condition in conditions]
    if len(set(labels)) != len(labels):
        raise ValueError(f'duplicate conditions in {text!r}')
    if not conditions:
        raise ValueError('no conditions given')
    return conditions


def parse_method_conditions(specs, methods, conditions) -> dict[str, list[str]]:
    """--method-conditions: method -> the labels of the conditions it runs at, for the methods it limits.

    As --method-speeds: each spec is METHODS=CONDITIONS, the methods joined with '+'; every method must be in
    `methods` and named once, every condition one of `conditions`; a method no spec names runs at every one.
    """
    labels = [condition['label'] for condition in conditions]
    limited: dict[str, list[str]] = {}
    for spec in specs:
        names, equals, values = spec.partition('=')
        names = [name.strip() for name in names.split('+') if name.strip()]
        if not equals or not names:
            raise ValueError(f'--method-conditions wants METHODS=CONDITIONS, got {spec!r}')
        chosen = [condition['label'] for condition in parse_conditions(values)]
        unknown = [label for label in chosen if label not in labels]
        if unknown:
            raise ValueError(f'--method-conditions {spec!r}: {", ".join(unknown)} not in --conditions')
        for name in names:
            if name not in methods:
                raise ValueError(f'--method-conditions {spec!r}: {name} is not in --methods')
            if name in limited:
                raise ValueError(f'--method-conditions names {name} twice')
            limited[name] = [label for label in labels if label in chosen]
    return limited


def parse_repeats(text: str) -> list[int]:
    """'2,4' -> [2, 4]: repeats counted from 1, as the specification counts them."""
    repeats = sorted({int(item) for item in (text or '').split(',') if item.strip()})
    if any(repeat < 1 for repeat in repeats):
        raise ValueError(f'repeats are counted from 1, got {text!r}')
    return repeats


def parse_method_repeats(specs, methods) -> dict[str, list[int]]:
    """--method-repeats: METHODS=REPEATS (methods joined with '+', repeats counted from 1), e.g. probackup8=1,3,5."""
    limited: dict[str, list[int]] = {}
    for spec in specs:
        names, equals, values = spec.partition('=')
        names = [name.strip() for name in names.split('+') if name.strip()]
        if not equals or not names:
            raise ValueError(f'--method-repeats wants METHODS=REPEATS, got {spec!r}')
        repeats = parse_repeats(values)
        for name in names:
            if name not in methods:
                raise ValueError(f'--method-repeats {spec!r}: {name} is not in --methods')
            if name in limited:
                raise ValueError(f'--method-repeats names {name} twice')
            limited[name] = repeats
    return limited


def parse_count_at(specs, conditions, what: str = '--first-visit-link-streams') -> dict[str, list[int]]:
    """N@CONDITIONS specs (e.g. 16@2:120:default; repeatable) -> condition label -> the counts asked for there."""
    labels = [condition['label'] for condition in conditions]
    found: dict[str, list[int]] = {}
    for spec in specs:
        count, at, values = spec.partition('@')
        if not at or not count.strip().isdigit() or int(count) < 1:
            raise ValueError(f'{what} wants N@CONDITIONS, got {spec!r}')
        for condition in parse_conditions(values):
            if condition['label'] not in labels:
                raise ValueError(f'{what} {spec!r}: {condition["label"]} not in --conditions')
            found.setdefault(condition['label'], []).append(int(count))
    return found


def parse_method_at(text: str, conditions) -> dict | None:
    """--negative-control METHOD@CONDITIONS -> {'method', 'conditions'}; None when not given."""
    if not text:
        return None
    method, at, values = text.partition('@')
    if not at or not method.strip():
        raise ValueError(f'--negative-control wants METHOD@CONDITIONS, got {text!r}')
    labels = [condition['label'] for condition in conditions]
    chosen = [condition['label'] for condition in parse_conditions(values)]
    unknown = [label for label in chosen if label not in labels]
    if unknown:
        raise ValueError(f'--negative-control {text!r}: {", ".join(unknown)} not in --conditions')
    return {'method': method.strip(), 'conditions': chosen}


def t12_methods_at(methods, label: str, repeat: int, plan: dict) -> list[str]:
    """The methods that run at condition `label` in repeat `repeat` (counted from 1), in --methods' order.

    - --method-conditions limits a method to its conditions;
    - --method-repeats limits a method to its repeats;
    - otherwise a --baseline-methods method runs in --baseline-repeats only, or in every repeat at the
      --baseline-every-repeat conditions;
    - the negative control (--negative-control METHOD@CONDITIONS) runs at its conditions in the first repeat
      only (it is last here; each visit draws its own order).
    """
    control = plan.get('negative_control') or {}
    here = []
    for method in methods:
        if method == control.get('method'):
            continue
        limited = (plan.get('method_conditions') or {}).get(method)
        if limited is not None and label not in limited:
            continue
        repeats = (plan.get('method_repeats') or {}).get(method)
        if repeats is not None:
            if repeat not in repeats:
                continue
        elif (method in (plan.get('baseline_methods') or ()) and label not in (plan.get('baseline_every') or ())
              and repeat not in (plan.get('baseline_repeats') or ())):
            continue
        here.append(method)
    if control and label in control['conditions'] and repeat == 1:
        here.append(control['method'])
    return here


def t12_schedule(methods, conditions, repeats: int, plan: dict) -> list[tuple[int, str, str]]:
    """Every (repeat, condition label, method) an invocation runs, repeats counted from 1, in condition order
    (the run draws each repeat's order of conditions, and each visit's order of arms)."""
    return [(repeat, condition['label'], method) for repeat in range(1, repeats + 1) for condition in conditions
            for method in t12_methods_at(methods, condition['label'], repeat, plan)]


# A15: a run that times out is a tool failure. Its bound is four times its arm's expected upper bound at the
# condition, and at least 60 s; probackup8's is 240 s. The upper bounds are the pre-registration's, as written (s):
# E1 at the anchor (the tuned anchor within 0.1 s of it), E4 for the full copies, E5 for the parcopy arms, and for
# v1 E6 added to E1's hot, E7 (uniform 1%, at 1 Gbit/s as at 2) and E8 (uniform 10%). `full` at 1 Gbit/s, which runs
# only in A16's variant, takes its 2 Gbit/s bound: a window-bound stream doesn't see the link rate.
T12_TIMEOUT_FACTOR, T12_TIMEOUT_FLOOR_S, T12_TIMEOUT_PROBACKUP_S = 4, 60.0, 240.0
_FULL_UPPER = {'2:0:default': 10.8, '2:0:tuned': 10.9, '2:20:default': 18, '2:60:default': 52, '2:120:default': 102,
               '2:60:tuned': 12.2, '2:120:tuned': 13.8, '1:60:default': 52, '1:120:default': 102}
T12_UPPER_S = {
    'full': {workload: _FULL_UPPER for workload in ('hot', 'uniform1', 'uniform10')},
    'parcopy8': {workload: {'2:0:default': 9.8, '2:20:default': 9.9, '2:60:default': 10.5, '2:60:tuned': 10.5,
                            '2:120:default': 14.5, '2:120:tuned': 11.2, '1:60:default': 19.3, '1:120:default': 20.0}
                 for workload in ('hot', 'uniform1', 'uniform10')},
    'parcopy16': {workload: {'2:120:default': 11.0} for workload in ('hot', 'uniform1', 'uniform10')},
    'v1': {'hot': {'2:0:default': 3.4, '2:0:tuned': 3.5, '2:20:default': 3.4 + 0.7, '2:60:default': 3.4 + 2.1,
                   '2:120:default': 3.4 + 4.4, '2:60:tuned': 3.4 + 1.8, '2:120:tuned': 3.4 + 3.8},
           'uniform1': {'2:0:default': 5.8, '2:0:tuned': 5.9, '2:20:default': 8.2, '2:60:default': 18.6,
                        '2:120:default': 34, '2:60:tuned': 7.7, '2:120:tuned': 10.0, '1:60:default': 18.6,
                        '1:120:default': 34},
           'uniform10': {'2:0:default': 8.7, '2:0:tuned': 8.8, '2:20:default': 12.5, '2:60:default': 31,
                         '2:60:tuned': 10.2, '2:120:tuned': 12.5}},
}
# v1's side arms: v1's bound plus the largest difference from v1 written for the arm (E9's window 1 in hot and 16,
# E10's fallback in uniform 1%, E11's TLS at 120 ms tuned).
T12_SIDE_EXTRA_S = {'pgdelta_direct_inplace_w1': 2.0, 'pgdelta_direct_inplace_w16': 0.1,
                    'pgdelta_direct_inplace_hc': 2.8, 'pgdelta_direct_inplace_tls': 0.5,
                    'pgdelta_direct_inplace_bbr': 0.1}


def t12_workload(pattern, fraction) -> str:
    """A trial's workload as EXP-13 names it: hot (1%), uniform1, uniform10 (analysis/exp13.workload_name)."""
    percent = f'{float(fraction) * 100:g}'
    return 'hot' if pattern == 'hot' and percent == '1' else f'{pattern}{percent}'


def t12_run_limit(method: str, workload: str, label: str) -> dict:
    """A15's bound on one EXP-13 run (T12_UPPER_S): limit_s, and the written upper bound it is four times (upper_s).
    A run with no written upper bound (another arm, workload or condition) gets limit_s None: run_exp04's
    --command-timeout then bounds each of its commands, as outside EXP-13."""
    if method in PROBACKUP_METHODS:
        return {'limit_s': T12_TIMEOUT_PROBACKUP_S, 'upper_s': None, 'from': 'probackup8: 240 s (A15)'}
    if method in FULL_VARIANTS:
        family, extra = 'full', 0.0
    elif method in PARCOPY_METHODS:
        family, extra = method, 0.0
    elif method == 'pgdelta_direct_inplace' or method in T12_SIDE_EXTRA_S:
        family, extra = 'v1', T12_SIDE_EXTRA_S.get(method, 0.0)
    else:
        family, extra = None, 0.0
    upper = ((T12_UPPER_S.get(family) or {}).get(workload) or {}).get(label)
    if upper is None:
        return {'limit_s': None, 'upper_s': None, 'from': f'no written upper bound for {method} in {workload} at {label}'}
    upper = round(upper + extra, 3)
    return {'limit_s': max(T12_TIMEOUT_FLOOR_S, round(T12_TIMEOUT_FACTOR * upper, 3)), 'upper_s': upper,
            'from': f'{T12_TIMEOUT_FACTOR} x {upper} s, at least {T12_TIMEOUT_FLOOR_S:g} s (A15)'}


def tcp_mem(value) -> tuple[int, ...] | None:
    """tcp_rmem or tcp_wmem as /proc/sys prints them ('4096\\t131072\\t6291456') or as written ('4096 131072
    6291456') -> (min, default, max); None for anything else."""
    words = str(value).split() if value is not None else []
    if len(words) != 3 or not all(word.isdigit() for word in words):
        return None
    return tuple(int(word) for word in words)


def condition_tcp_settings(host: str, defaults: dict, tuned_max: int = TUNED_TCP_MAX_BYTES) -> dict:
    """The tcp_rmem and tcp_wmem a host setting wants on one VM, as 'MIN DEFAULT MAX': that VM's recorded
    defaults, or (tuned) the same with only the maximum raised to tuned_max."""
    wanted = {}
    for name in TCP_MEM_SYSCTLS:
        values = tcp_mem(defaults.get(name))
        if values is None:
            raise ValueError(f'{name} has no recorded default ({defaults.get(name)!r})')
        if host == 'tuned':
            values = (*values[:2], int(tuned_max))
        wanted[name] = ' '.join(str(value) for value in values)
    return wanted


def tcp_defaults_problems(found: dict, tuned_max: int = TUNED_TCP_MAX_BYTES) -> list[str]:
    """How one VM's tcp_rmem and tcp_wmem differ from Debian's (DEBIAN_TCP_MEM); a maximum at or above the tuned
    one is named as such (what an interrupted run leaves)."""
    problems = []
    for name, debian in DEBIAN_TCP_MEM.items():
        values = tcp_mem(found.get(name))
        if values == debian:
            continue
        tuned = values is not None and values[2] >= tuned_max
        problems.append(f"{name} is {found.get(name)!r}, not Debian's {' '.join(map(str, debian))!r}"
                        + (' (a tuned maximum: an earlier run did not reset it)' if tuned else ''))
    return problems


def teardown_ok(teardown) -> bool:
    """Whether an EXP-13 invocation's teardown (run.json's) left both VMs as they came: Debian's qdisc rather than
    wan.sh's tree, and the default TCP settings back and checked."""
    if not isinstance(teardown, dict) or 'error' in teardown:
        return False
    if (teardown.get('tcp') or {}).get('ok') is not True:
        return False
    return all(isinstance(teardown.get(vm), dict) and teardown[vm].get('tree_is_wan') is False
               for vm in ('source', 'dest'))


def tcp_settings_problems(found: dict, wanted: dict) -> list[str]:
    """What differs between the TCP settings a VM has and those a condition wants (tcp_mem's comparison)."""
    return [f'{name} is {found.get(name)!r}, not {value!r}' for name, value in wanted.items()
            if tcp_mem(found.get(name)) != tcp_mem(value)]


def wan_expected(condition: dict, peer: str, control_port: int) -> dict:
    """What wan.sh sets for a condition on each VM: netem's delay (r/2) and limit, the tbf's rate (bytes per
    second, as tc reports it), burst (10 ms of tokens, at least 128 KiB) and queue (max(50 ms, r))."""
    rate = condition['gbit'] * 1e9 / 8
    return {'delay_ms': condition['delay_ms'], 'limit': WAN_NETEM_LIMIT, 'rate_bytes': rate,
            'burst_bytes': max(WAN_BURST_FLOOR, int(rate / 100)),
            'queue_ms': max(float(WAN_QUEUE_FLOOR_MS), float(condition['rtt_ms'])), 'peer': peer,
            'control_port': int(control_port)}


def parse_qdisc_json(text) -> list[dict]:
    """`tc -s -j qdisc show dev DEV` -> its qdiscs: kind, handle, parent ('root' for the root qdisc), options, and
    the counters it reports (QDISC_COUNTERS; None where absent)."""
    data = json.loads(text) if isinstance(text, (str, bytes)) else text
    qdiscs = []
    for item in data or ():
        if not isinstance(item, dict):
            continue
        entry = {'kind': item.get('kind'), 'handle': item.get('handle'),
                 'parent': 'root' if item.get('root') else item.get('parent'),
                 'options': item.get('options') if isinstance(item.get('options'), dict) else {}}
        for key in QDISC_COUNTERS:
            value = item.get(key)
            entry[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
        qdiscs.append(entry)
    return qdiscs


_TC_TIME = re.compile(r'^([\d.]+)(s|ms|us|ns)$')
_TC_RATE = re.compile(r'^([\d.]+)([KMGT]?)(i?)bit$')
_TC_SIZE = re.compile(r'^([\d.]+)([KMG]?)(i?)b$')


def _tc_time_ms(value, number_seconds: float):
    """A tc time in ms: a number in tc's JSON unit (`number_seconds` seconds each: 1 for netem's delay, 1e-6 for
    tbf's lat), or text with its unit ('30ms', '60.0ms'); None if neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value * number_seconds * 1000
    found = _TC_TIME.match(str(value or ''))
    if not found:
        return None
    return float(found.group(1)) * {'s': 1000, 'ms': 1, 'us': 1e-3, 'ns': 1e-6}[found.group(2)]


def _tc_rate_bytes(value):
    """tbf's rate in bytes per second: a number (tc's JSON) or text ('2Gbit')."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    found = _TC_RATE.match(str(value or ''))
    if not found:
        return None
    base = 1024 if found.group(3) else 1000
    return float(found.group(1)) * base ** ' KMGT'.index(found.group(2) or ' ') / 8


def _tc_size_bytes(value):
    """A tc size in bytes: a number (tc's JSON) or text ('2500000b', '2441Kb')."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    found = _TC_SIZE.match(str(value or '').split('/')[0])
    if not found:
        return None
    base = 1024 if found.group(3) or found.group(2) else 1000
    return float(found.group(1)) * base ** ' KMG'.index(found.group(2) or ' ')


def wan_tree(qdiscs) -> dict:
    """wan.sh's tree in parsed qdiscs (parse_qdisc_json): the prio root, the plain band's qdisc where one shows,
    netem on the delayed band and its tbf child (each None where missing), and whether that is the tree.

    netem_own_drops is netem's drop counter less its child tbf's, from the same snapshot: in kernel 6.1 netem
    counts a packet its child refuses as its own drop (sch_netem.c, netem_dequeue), and so does the prio root.
    The plain band's counters are its own qdisc's where tc shows one, else the root's less netem's.
    """
    root = next((qdisc for qdisc in qdiscs if qdisc['parent'] == 'root'), None)
    netem = next((qdisc for qdisc in qdiscs if qdisc['parent'] == WAN_DELAYED_BAND), None)
    tbf = next((qdisc for qdisc in qdiscs if netem is not None and qdisc['parent'] == f'{netem["handle"]}1'), None)
    plain = next((qdisc for qdisc in qdiscs if qdisc['parent'] == WAN_PLAIN_BAND), None)

    def counters(qdisc):
        return None if qdisc is None else {key: qdisc.get(key) for key in QDISC_COUNTERS}

    tree = {'is_wan': bool(root and root['kind'] == 'prio' and netem and netem['kind'] == 'netem'
                           and tbf and tbf['kind'] == 'tbf'),
            'root': None if root is None else {'kind': root['kind'], 'handle': root['handle'], **counters(root)},
            'netem': None, 'tbf': None, 'plain': None, 'netem_own_drops': None}
    if netem is not None and netem['kind'] == 'netem':
        options = netem['options']
        delay = options.get('delay')
        delay = delay.get('delay') if isinstance(delay, dict) else delay
        tree['netem'] = {'handle': netem['handle'], 'limit': options.get('limit'),
                         'delay_ms': 0.0 if delay is None else _tc_time_ms(delay, 1.0), **counters(netem)}
    if tbf is not None and tbf['kind'] == 'tbf':
        options = tbf['options']
        tree['tbf'] = {'handle': tbf['handle'], 'rate_bytes': _tc_rate_bytes(options.get('rate')),
                       'burst_bytes': _tc_size_bytes(options.get('burst')),
                       'latency_ms': _tc_time_ms(options.get('lat'), 1e-6), 'limit_bytes': options.get('limit'),
                       **counters(tbf)}
    if tree['netem'] and tree['tbf'] and None not in (tree['netem']['drops'], tree['tbf']['drops']):
        tree['netem_own_drops'] = tree['netem']['drops'] - tree['tbf']['drops']
    if plain is not None:
        tree['plain'] = {'kind': plain['kind'], 'derived': False, **counters(plain)}
    elif root is not None and tree['netem'] is not None:
        tree['plain'] = {'kind': None, 'derived': True,
                         **{key: (None if root.get(key) is None or tree['netem'].get(key) is None
                                  else root[key] - tree['netem'][key]) for key in ('bytes', 'packets')}}
    return tree


_U32_HEAD = re.compile(r'^filter .*?\bpref (\d+) u32\b')
_U32_FLOWID = re.compile(r'\*?(?:flowid|classid) (\S+)')
_U32_MATCH = re.compile(r'^\s*match ([0-9a-f]{8})/([0-9a-f]{8}) at (\d+)')


def parse_u32_filters(text: str) -> list[dict]:
    """`tc filter show dev DEV` -> its u32 rules (iproute2's text: a 'filter ... pref P u32 ... flowid C' line,
    then its '  match VALUE/MASK at OFFSET' lines): pref, flowid, and the fields wan.sh matches (dst, protocol,
    sport, dport), with any other match kept in `other`."""
    rules, current = [], None
    for line in (text or '').splitlines():
        head = _U32_HEAD.match(line)
        if head:
            flowid = _U32_FLOWID.search(line)
            current = {'pref': int(head.group(1)), 'flowid': flowid.group(1), 'matches': []} if flowid else None
            if current is not None:
                rules.append(current)
            continue
        found = _U32_MATCH.match(line)
        if found and current is not None:
            current['matches'].append((int(found.group(1), 16), int(found.group(2), 16), int(found.group(3))))
    parsed = []
    for rule in rules:
        fields = {'pref': rule['pref'], 'flowid': rule['flowid'], 'dst': None, 'protocol': None, 'sport': None,
                  'dport': None, 'other': []}
        for value, mask, offset in rule['matches']:
            if offset == 16 and mask == 0xffffffff:
                fields['dst'] = socket.inet_ntoa(value.to_bytes(4, 'big'))
            elif offset == 8 and mask == 0x00ff0000:
                fields['protocol'] = (value >> 16) & 0xff
            elif offset == 20 and mask == 0xffff0000:
                fields['sport'] = value >> 16
            elif offset == 20 and mask == 0x0000ffff:
                fields['dport'] = value & 0xffff
            else:
                fields['other'].append([f'{value:08x}', f'{mask:08x}', offset])
        parsed.append(fields)
    return parsed


def wan_filter_problems(filters, peer: str, control_port: int) -> list[str]:
    """What is wrong with wan.sh's filters (parse_u32_filters): the control port, as source and as destination
    port of TCP to the peer, to the plain band; then everything else to the peer to the delayed band; nothing
    else."""
    def plain(rule, key):
        other = 'dport' if key == 'sport' else 'sport'
        return (rule['flowid'] == WAN_PLAIN_BAND and rule['dst'] == peer and rule['protocol'] == 6
                and rule[key] == control_port and rule[other] is None and not rule['other'])

    exceptions = [rule for rule in filters if plain(rule, 'sport') or plain(rule, 'dport')]
    delayed = [rule for rule in filters if rule['flowid'] == WAN_DELAYED_BAND and rule['dst'] == peer
               and rule['protocol'] is None and rule['sport'] is None and rule['dport'] is None and not rule['other']]
    problems = []
    for key in ('sport', 'dport'):
        if not any(plain(rule, key) for rule in filters):
            problems.append(f'no filter sends TCP {key} {control_port} to {peer} to the plain band')
    if not delayed:
        problems.append(f'no filter sends traffic to {peer} to the delayed band')
    elif exceptions and min(rule['pref'] for rule in delayed) <= max(rule['pref'] for rule in exceptions):
        problems.append("the delayed band's filter comes before the control port's")
    unexpected = [rule for rule in filters if rule not in exceptions and rule not in delayed]
    if unexpected:
        problems.append(f'unexpected filters: {unexpected}')
    return problems


def _close(value, expected, relative: float, absolute: float = 0.0) -> bool:
    return value is not None and abs(value - expected) <= max(absolute, relative * abs(expected))


def wan_problems(tree: dict, filters, expected: dict) -> list[str]:
    """The visit's tree check: whether one VM's parsed tree (wan_tree) and filters are wan.sh's for `expected`
    (wan_expected): netem's delay (within 1%, at least 0.05 ms) and limit, the tbf's rate (1%), burst (2%) and
    queue (2%, at least 1 ms; tc prints back what it computed from the limit), and the filters."""
    if not tree.get('is_wan'):
        return [f"the qdiscs are not wan.sh's tree: root {tree.get('root')}, netem {tree.get('netem')}, "
                f"tbf {tree.get('tbf')}"]
    netem, tbf = tree['netem'], tree['tbf']
    problems = []
    if not _close(netem['delay_ms'], expected['delay_ms'], 0.01, 0.05):
        problems.append(f"netem's delay is {netem['delay_ms']} ms, not {expected['delay_ms']}")
    if netem['limit'] != expected['limit']:
        problems.append(f"netem's limit is {netem['limit']}, not {expected['limit']}")
    if not _close(tbf['rate_bytes'], expected['rate_bytes'], 0.01):
        problems.append(f"tbf's rate is {tbf['rate_bytes']} bytes/s, not {expected['rate_bytes']}")
    if not _close(tbf['burst_bytes'], expected['burst_bytes'], 0.02):
        problems.append(f"tbf's burst is {tbf['burst_bytes']} bytes, not {expected['burst_bytes']}")
    if not _close(tbf['latency_ms'], expected['queue_ms'], 0.02, 1.0):
        problems.append(f"tbf's queue is {tbf['latency_ms']} ms, not {expected['queue_ms']}")
    return problems + wan_filter_problems(filters, expected['peer'], expected['control_port'])


def qdisc_counters(tree: dict) -> dict:
    """A snapshot's qdisc counters (T12's snapshot --qdisc): the root's, netem's, the tbf's and the plain band's,
    from one `tc -s -j` reading (wan_tree), and whether it was wan.sh's tree."""
    return {'is_wan': tree.get('is_wan'),
            **{part: (None if tree.get(part) is None else {key: tree[part].get(key) for key in QDISC_COUNTERS
                                                            if key in tree[part]})
               for part in ('root', 'netem', 'tbf', 'plain')}}


def qdisc_delta(before: dict, after: dict) -> dict:
    """The qdisc counters' growth over a window (two qdisc_counters()), and netem's own drops over it: its drop
    counter's growth less its tbf child's. A part missing at either end, or a counter that went backwards (the
    tree replaced in between), leaves its values None."""
    delta: dict = {'same_tree': bool(before.get('is_wan') and after.get('is_wan'))}
    for part in ('root', 'netem', 'tbf', 'plain'):
        first, last = before.get(part), after.get(part)
        if not first or not last:
            delta[part] = None
            continue
        values = {}
        for key in QDISC_CUMULATIVE:
            if first.get(key) is None or last.get(key) is None or last[key] < first[key]:
                values[key] = None
            else:
                values[key] = last[key] - first[key]
        delta[part] = values
    netem, tbf = delta.get('netem') or {}, delta.get('tbf') or {}
    delta['netem_own_drops'] = (None if netem.get('drops') is None or tbf.get('drops') is None
                                else netem['drops'] - tbf['drops'])
    return delta


def qdisc_samples_summary(samples) -> dict:
    """The tc sampler's readings (each a qdisc_counters() with its time): netem's largest backlog in packets
    (qlen, against its limit, which counts packets) and in bytes, and how many readings had the tree."""
    netem = [sample['netem'] for sample in samples if isinstance(sample.get('netem'), dict)]
    qlens = [item['qlen'] for item in netem if item.get('qlen') is not None]
    backlogs = [item['backlog'] for item in netem if item.get('backlog') is not None]
    return {'samples': len(samples), 'with_tree': len(netem),
            'netem_qlen_max': max(qlens) if qlens else None,
            'netem_backlog_max_bytes': max(backlogs) if backlogs else None,
            'limit': WAN_NETEM_LIMIT, 'at_limit': None if not qlens else max(qlens) >= WAN_NETEM_LIMIT}


_PING_TIME = re.compile(r'\btime[=<]([\d.]+) ?ms\b')


def parse_ping(text: str, count: int = PING_COUNT) -> dict:
    """iputils ping's replies ('64 bytes from ...: icmp_seq=1 ttl=64 time=60.2 ms'): each round trip in ms,
    their minimum, median and maximum, and how many of `count` came back."""
    times = [float(value) for value in _PING_TIME.findall(text or '')]
    return {'count': count, 'replies': len(times), 'times_ms': times,
            'min_ms': min(times) if times else None, 'median_ms': statistics.median(times) if times else None,
            'max_ms': max(times) if times else None}


def ping_problems(pings: dict, rtt_ms: float, base_ms: float = 0.0) -> list[str]:
    """The visit's ping check: every reply back, the minimum in [r, r + 1 ms], the median at most the minimum
    + 2 ms. On a real path (base_ms, its own round trip) the window is [r + base, r + base + 1 ms + 5% of base]."""
    slack = PING_MIN_SLACK_MS + BASE_RTT_SLACK_SHARE * base_ms
    rtt_ms = rtt_ms + base_ms
    problems = []
    if pings.get('replies') != pings.get('count'):
        problems.append(f"{pings.get('replies')} of {pings.get('count')} pings came back")
    low, middle = pings.get('min_ms'), pings.get('median_ms')
    if low is None or not rtt_ms <= low <= rtt_ms + slack:
        problems.append(f'the ping minimum {low} ms is outside [{rtt_ms:g}, {rtt_ms + slack:g}] ms')
    if low is not None and (middle is None or middle > low + PING_MEDIAN_SLACK_MS):
        problems.append(f'the ping median {middle} ms is above the minimum + {PING_MEDIAN_SLACK_MS:g} ms')
    return problems


def control_noop_problems(seconds, reference) -> list[str]:
    """The visit's control-path check: each no-op over the control path at most 5 ms slower than the anchor's
    (the median of the reference no-ops, taken with the tree at the anchor). One-sided: a no-op faster than the
    anchor's says nothing about a delayed control path."""
    if not reference:
        return ['no reference no-ops at the anchor']
    limit = statistics.median(reference) + CONTROL_NOOP_SLACK_S
    slow = [round(value, 4) for value in seconds if value > limit]
    return [f'control no-ops {slow} s exceed the anchor\'s {statistics.median(reference):.4f} s + '
            f'{CONTROL_NOOP_SLACK_S:g} s'] if slow else []


def parse_snmp(text: str) -> dict[str, dict[str, int]]:
    """/proc/net/snmp or /proc/net/netstat: for each prefix ('Tcp', 'TcpExt'), its header line's names with the
    values of the line after it."""
    tables: dict[str, dict[str, int]] = {}
    names: dict[str, list[str]] = {}
    for line in (text or '').splitlines():
        prefix, colon, rest = line.partition(':')
        if not colon:
            continue
        words = rest.split()
        if prefix in names:
            header = names.pop(prefix)
            try:
                tables[prefix] = {name: int(value) for name, value in zip(header, words)}
            except ValueError:
                names[prefix] = words
        else:
            names[prefix] = words
    return tables


def nstat_values(snmp_text: str, netstat_text: str) -> dict[str, int]:
    """nstat's TCP counters (NSTAT_COUNTERS, by nstat's names) from /proc/net/snmp and /proc/net/netstat."""
    tables = {**parse_snmp(snmp_text), **parse_snmp(netstat_text)}
    return {name: tables[prefix][field] for name, (prefix, field) in NSTAT_COUNTERS.items()
            if field in tables.get(prefix, {})}


def _ss_endpoint(text: str):
    """'10.128.0.12:7000', '[::ffff:10.128.0.12]:5432' or '*:22' -> (address, port)."""
    host, _, port = text.rpartition(':')
    host = host.strip('[]')
    if host.startswith('::ffff:'):
        host = host[len('::ffff:'):]
    return host, int(port) if port.isdigit() else None


_SS_MS = re.compile(r'^(\d+)ms')


def _ss_info(words) -> dict:
    """One socket's `ss -i` line: the counters the samples keep (minrtt in ms; busy, rwnd_limited and
    sndbuf_limited in ms; notsent and unacked, which ss prints only when not 0)."""
    info: dict = {'notsent': 0, 'unacked': 0}
    words = list(words)
    for index, word in enumerate(words):
        if word in ('send', 'pacing_rate', 'delivery_rate') and index + 1 < len(words):
            info[word] = words[index + 1]
            continue
        key, colon, value = word.partition(':')
        if not colon:
            continue
        if key in ('minrtt',):
            info['minrtt_ms'] = float(value)
        elif key == 'rtt':
            mean, _, var = value.partition('/')
            info['rtt_ms'], info['rttvar_ms'] = float(mean), float(var) if var else None
        elif key in ('busy', 'rwnd_limited', 'sndbuf_limited'):
            found = _SS_MS.match(value)
            if found:
                info[f'{key}_ms'] = int(found.group(1))
        elif key == 'retrans':
            now, _, total = value.partition('/')
            info['retrans'], info['retrans_total'] = int(now), int(total) if total else None
        elif value.isdigit():
            info[key] = int(value)
    return info


def parse_ss(text: str, exclude_ports=()) -> list[dict]:
    """`ss -tin` (with or without its header) -> one dict per TCP socket: state, local and peer endpoints, the
    queues, and its info line's counters (_ss_info). Sockets with either port in exclude_ports (the harness's
    control path) are left out."""
    sockets, current = [], None
    for line in (text or '').splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if current is not None:
                current.update(_ss_info(line.split()))
            continue
        words = line.split()
        if words[0] in ('State', 'Netid', 'Recv-Q'):
            current = None
            continue
        endpoints = [index for index, word in enumerate(words) if ':' in word and not word.endswith(':')]
        if len(endpoints) < 2:
            current = None
            continue
        local, peer = _ss_endpoint(words[endpoints[-2]]), _ss_endpoint(words[endpoints[-1]])
        current = None
        if local[1] in exclude_ports or peer[1] in exclude_ports:
            continue
        numbers = [word for word in words[:endpoints[-2]] if word.isdigit()]
        current = {'state': None if words[0].isdigit() else words[0], 'local': f'{local[0]}:{local[1]}',
                   'peer': f'{peer[0]}:{peer[1]}', 'local_port': local[1], 'peer_port': peer[1],
                   'recv_q': int(numbers[-2]) if len(numbers) >= 2 else None,
                   'send_q': int(numbers[-1]) if numbers else None}
        sockets.append(current)
    return sockets


def ss_connections(samples) -> list[dict]:
    """The ss sampler's readings (each {'t', 'ss': parse_ss()}) per connection (local, peer): when it was first
    and last seen, in how many readings, its last reading (that connection's reading: ss's counters are
    cumulative), its smallest minrtt and its largest notsent and unacked."""
    found: dict = {}
    for sample in samples:
        for item in sample.get('ss') or ():
            key = (item['local'], item['peer'])
            entry = found.setdefault(key, {'local': item['local'], 'peer': item['peer'], 'first_t': sample.get('t'),
                                           'samples': 0, 'notsent_max': 0, 'unacked_max': 0, 'minrtt_ms': None})
            entry['samples'] += 1
            entry['last_t'] = sample.get('t')
            entry['last'] = item
            entry['notsent_max'] = max(entry['notsent_max'], item.get('notsent') or 0)
            entry['unacked_max'] = max(entry['unacked_max'], item.get('unacked') or 0)
            if item.get('minrtt_ms') is not None:
                entry['minrtt_ms'] = (item['minrtt_ms'] if entry['minrtt_ms'] is None
                                      else min(entry['minrtt_ms'], item['minrtt_ms']))
    return sorted(found.values(), key=lambda entry: (entry['first_t'] or 0, entry['local']))


def ss_summary(connections) -> dict:
    """A run's ss reading: its sending sockets (at least SS_SENDING_MIN_BYTES and SS_SENDING_SHARE of all bytes
    acknowledged), their minrtt range, busy, rwnd_limited and sndbuf_limited times summed (E15's share is the
    two limits over busy), retransmissions, and the main socket's (most bytes) readings with notsent and unacked
    (stunnel's WAN socket, for v1_tls)."""
    def acked(entry):
        return (entry.get('last') or {}).get('bytes_acked') or 0

    total = sum(acked(entry) for entry in connections)
    threshold = max(SS_SENDING_MIN_BYTES, SS_SENDING_SHARE * total)
    sending = [entry for entry in connections if acked(entry) >= threshold]

    def summed(key):
        return sum((entry['last'].get(key) or 0) for entry in sending) if sending else None

    minrtts = [entry['minrtt_ms'] for entry in sending if entry['minrtt_ms'] is not None]
    busy, rwnd, sndbuf = summed('busy_ms'), summed('rwnd_limited_ms'), summed('sndbuf_limited_ms')
    main = max(sending, key=acked) if sending else None
    last = (main or {}).get('last') or {}
    return {'connections': len(connections), 'sending': [[entry['local'], entry['peer']] for entry in sending],
            'bytes_acked': total, 'minrtt_ms_min': min(minrtts) if minrtts else None,
            'minrtt_ms_max': max(minrtts) if minrtts else None, 'busy_ms': busy, 'rwnd_limited_ms': rwnd,
            'sndbuf_limited_ms': sndbuf,
            'limited_fraction': round((rwnd + sndbuf) / busy, 4) if busy else None,
            'retrans_total': summed('retrans_total'),
            'main': None if main is None else {
                'local': main['local'], 'peer': main['peer'], 'minrtt_ms': main['minrtt_ms'],
                'cwnd': last.get('cwnd'), 'busy_ms': last.get('busy_ms'),
                'rwnd_limited_ms': last.get('rwnd_limited_ms'), 'sndbuf_limited_ms': last.get('sndbuf_limited_ms'),
                'retrans': last.get('retrans'), 'retrans_total': last.get('retrans_total'),
                'notsent': last.get('notsent'), 'unacked': last.get('unacked'),
                'notsent_max': main['notsent_max'], 'unacked_max': main['unacked_max'],
                'bytes_acked': last.get('bytes_acked')}}


STEADY_SECONDS = 3  # a link test's steady rate: the median of the last 3 whole one-second rates after the warmup
# The stunnel link test's sender counts its seconds from its connect to the source's stunnel on localhost, and its
# first byte reaches dest only after the WAN connect and TLS 1.3's handshake (about 2.5 r): it sends ceil(3 r) more,
# and at least 1 (at r = 0 the handshake still takes a few ms, which would cost a whole second), so the receiver
# keeps STEADY_SECONDS whole seconds after its warmup.
STUNNEL_SETUP_RTTS = 3


def stunnel_link_test_extra_s(rtt_ms) -> int:
    """The seconds the stunnel link test's sender adds to --link-test-seconds at a round trip of rtt_ms."""
    return max(1, math.ceil(STUNNEL_SETUP_RTTS * float(rtt_ms) / 1000))


def steady_last(per_second_bytes, warmup_s: float, count: int = STEADY_SECONDS) -> dict:
    """A link test's steady rate: the median of the last `count` complete one-second rates after the warmup
    (per_second_bytes[i] holds the bytes received in second i after the first byte; only whole seconds)."""
    first = math.ceil(warmup_s)
    eligible = [(index, value) for index, value in enumerate(per_second_bytes or ()) if index >= first]
    used = eligible[-count:]
    rates = [value * 8 / 1e9 for _, value in used]
    return {'gbit': round(statistics.median(rates), 4) if rates else None, 'seconds': [index for index, _ in used],
            'complete_seconds': len(per_second_bytes or ())}


def stunnel_config(role: str, *, accept: str, connect: str, cert, key, ca, log) -> str:
    """One end of the stunnel pair (role 'client' on the source, 'server' on dest).

    TLS 1.3 only, with OpenSSL's default suites; each end presents its certificate from the per-session CA and
    requires the other's (mutual authentication); TCP_NODELAY on both of its sockets; stunnel's default
    TIMEOUTclose (not set: dest's stunnel then waits for the source's close_notify, so the receiver's drain ends as
    in clear); the log at level info, which names each connection, its protocol and suite. In the foreground,
    with no pid file: agent.py stunnel starts and stops it.
    """
    lines = [f'; EXP-13 (T12): v1_tls\'s stunnel {role}', 'foreground = yes', 'pid =', f'output = {log}',
             'debug = info', 'socket = l:TCP_NODELAY=1', 'socket = r:TCP_NODELAY=1', '', '[pgdelta]']
    if role == 'client':
        lines.append('client = yes')
    lines += [f'accept = {accept}', f'connect = {connect}', f'cert = {cert}', f'key = {key}', f'CAfile = {ca}',
              'verifyChain = yes', 'requireCert = yes', 'sslVersionMin = TLSv1.3', 'sslVersionMax = TLSv1.3']
    if role == 'client':
        lines.append(f'checkHost = {STUNNEL_SERVER_NAME}')
    return '\n'.join(lines) + '\n'


_STUNNEL_SUITE = re.compile(r'\b(TLSv1(?:\.\d)?|SSLv3) ciphersuite: (\S+)')
_STUNNEL_ACCEPTED = re.compile(r'Service \[[^\]]+\] accepted connection from (\S+)')
_STUNNEL_CLOSED = re.compile(r'Connection closed: (\d+) byte\(s\) sent to TLS, (\d+) byte\(s\) sent to socket')
# What stunnel logs when it refuses a peer (smoke item 12's refusals; stunnel 5.68 and OpenSSL 3.0's messages).
STUNNEL_REFUSAL_MARKERS = ('peer did not return a certificate', 'CERT: Verification error',
                           'certificate verify failed', 'alert certificate required', 'unknown ca',
                           'Rejected by CERT')


def parse_stunnel_log(text: str) -> dict:
    """stunnel's log (debug = info): the connections it accepted, each TLS connection's protocol and suite, the
    bytes each closed connection moved, and any refusal (STUNNEL_REFUSAL_MARKERS)."""
    lines = (text or '').splitlines()
    suites = [found.groups() for found in map(_STUNNEL_SUITE.search, lines) if found]
    closed = [tuple(int(value) for value in found.groups()) for found in map(_STUNNEL_CLOSED.search, lines) if found]
    return {'lines': len(lines), 'connections': sum(1 for line in lines if _STUNNEL_ACCEPTED.search(line)),
            'protocols': sorted({protocol for protocol, _ in suites}), 'suites': sorted({suite for _, suite in suites}),
            'tls_version': suites[-1][0] if suites else None, 'tls_suite': suites[-1][1] if suites else None,
            'closed': [{'to_tls': to_tls, 'to_socket': to_socket} for to_tls, to_socket in closed],
            'refusals': [line for line in lines if any(marker in line for marker in STUNNEL_REFUSAL_MARKERS)]}


# pgdelta's protocol timers (EXP-13, built in pgdelta for it), read wherever its --stats-json keeps them. The
# sender's waits are each split into busy and idle parts, which add up to the wait.
PGDELTA_SENDER_WAITS = ('accept_wait', 'basis_wait', 'leaf_wait', 'complete_wait')
PGDELTA_SENDER_TIMERS = ('connect', 'accept_wait', 'basis_wait', 'leaf_wait', 'page_send', 'complete_wait')
PGDELTA_SENDER_COUNTS = ('leaf_requests', 'leaf_waits_over_1ms')
PGDELTA_RECEIVER_TIMERS = ('hello_wait', 'recv_wait', 'publish')


def find_stat(stats, *names, skip=('receiver',)):
    """The first scalar value under any of `names` anywhere in pgdelta's statistics, searched breadth first, the
    branches named in `skip` left out (the sender's search skips the receiver's statistics); None if absent."""
    queue = [stats]
    while queue:
        node = queue.pop(0)
        if not isinstance(node, dict):
            continue
        for name in names:
            value = node.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        queue += [value for key, value in node.items() if key not in skip and isinstance(value, dict)]
    return None


def _timer_names(base: str, part: str | None = None) -> tuple:
    stem = base if part is None else f'{base}_{part}'
    return (f'{stem}_s', f'{stem}_seconds', stem)


def pgdelta_timer(stats, base: str, part: str | None = None, skip=('receiver',)):
    """One of pgdelta's protocol timers (seconds): `base` (e.g. leaf_wait), or its busy or idle part, as
    <base>[_<part>]_s, _seconds or bare, or as part of a dict under the base's name ({'busy': ...})."""
    value = find_stat(stats, *_timer_names(base, part), skip=skip)
    if value is not None:
        return value
    queue = [stats]
    while queue:
        node = queue.pop(0)
        if not isinstance(node, dict):
            continue
        for name in _timer_names(base):
            nested = node.get(name)
            if isinstance(nested, dict):
                keys = (('total_s', 'total', 'seconds', 's') if part is None
                        else (f'{part}_s', f'{part}_seconds', part))
                found = next((nested[key] for key in keys if isinstance(nested.get(key), (int, float))), None)
                if found is not None:
                    return found
        queue += [value for key, value in node.items() if key not in skip and isinstance(value, dict)]
    return None


def pgdelta_protocol_report(reported: dict) -> dict:
    """pgdelta's protocol timers from its statistics, as tool_reported() carries them: the sender's (connect_s,
    each wait with its _busy_s and _idle_s, page_send_s), its leaf requests and waits over 1 ms, the window, the
    fallback's fraction and files; and the receiver's (hello_wait_s, recv_wait_s, publish_s) under `receiver`.
    Read defensively: a timer this pgdelta doesn't report is None. busy_idle_gap_s is, per wait, the wait less
    its two parts (smoke item 13: within 2 ms)."""
    reported = reported if isinstance(reported, dict) else {}
    report: dict = {}
    for base in PGDELTA_SENDER_TIMERS:
        report[f'{base}_s'] = pgdelta_timer(reported, base)
    gaps = {}
    for base in PGDELTA_SENDER_WAITS:
        busy, idle = pgdelta_timer(reported, base, 'busy'), pgdelta_timer(reported, base, 'idle')
        report[f'{base}_busy_s'], report[f'{base}_idle_s'] = busy, idle
        total = report[f'{base}_s']
        gaps[base] = None if None in (total, busy, idle) else round(total - busy - idle, 6)
    report['busy_idle_gap_s'] = gaps
    for name in PGDELTA_SENDER_COUNTS:
        report[name] = find_stat(reported, name)
    report['window'] = find_stat(reported, 'window')
    report['high_churn_node_fraction'] = reported.get('high_churn_node_fraction')
    report['high_churn_files'] = find_stat(reported, 'high_churn_files')
    receiver = reported.get('receiver') if isinstance(reported.get('receiver'), dict) else {}
    report['receiver'] = {f'{base}_s': pgdelta_timer(receiver, base, skip=()) for base in PGDELTA_RECEIVER_TIMERS}
    # The publication's moment on dest's wall clock, so the drain can be read against the receiver's exit there.
    report['receiver']['published_utc'] = find_stat(receiver, 'published_utc', skip=())
    return report


# A15 (EXP-13): a run is void only if a user-table file differs between the trial's reference digests. The user
# tables are pgbench's and EXP-01's events, with their TOAST tables and every index of either, read from the
# catalog (no user table is read): relation name and file path, as JSON.
USER_TABLE_FILES_SQL = (
    "WITH t AS (SELECT oid, reltoastrelid FROM pg_class WHERE relnamespace = 'public'::regnamespace "
    "AND relkind IN ('r', 'p') AND (relname LIKE 'pgbench\\_%' OR relname = 'events')), "
    'r AS (SELECT oid FROM t UNION SELECT reltoastrelid FROM t WHERE reltoastrelid <> 0 '
    'UNION SELECT indexrelid FROM pg_index WHERE indrelid IN (SELECT oid FROM t) '
    'OR indrelid IN (SELECT reltoastrelid FROM t WHERE reltoastrelid <> 0)) '
    "SELECT coalesce(json_agg(json_build_object('relation', c.oid::regclass::text, 'path', "
    "pg_relation_filepath(c.oid)) ORDER BY c.oid), '[]') FROM r JOIN pg_class c ON c.oid = r.oid "
    'WHERE pg_relation_filepath(c.oid) IS NOT NULL;')
_RELATION_FILE = re.compile(r'^(.*?/\d+)(?:_(?:fsm|vm|init))?(?:\.\d+)?$')
FILE_CLASSES = ('user_table', 'catalog', 'other')


def reference_changes(start: dict, end: dict) -> list[str]:
    """Every path whose entry (kind, size, SHA-256) differs between two reference views, or that one lacks."""
    first, last = start.get('entries') or {}, end.get('entries') or {}
    return sorted(path for path in set(first) | set(last)
                  if list((first.get(path) or [None] * 3)[:3]) != list((last.get(path) or [None] * 3)[:3]))


def file_class(path: str, user_files) -> str:
    """'user_table' for a user table's file (any fork or segment of a path in user_files), 'catalog' for any
    other relation file (base/, global/), 'other' for the rest."""
    found = _RELATION_FILE.match(path)
    if found and found.group(1) in user_files:
        return 'user_table'
    return 'catalog' if path.startswith(('base/', 'global/')) else 'other'


def reference_classes(start: dict, end: dict, user_files, limit: int = 50) -> dict:
    """A15: the reference digests' differences by file class (FILE_CLASSES): every count, the first `limit`
    paths of each, and whether the user tables match (None without the user tables' files)."""
    changes = reference_changes(start, end)
    known = user_files is not None
    user_files = set(user_files or ())
    classes = {name: [] for name in FILE_CLASSES}
    for path in changes:
        classes[file_class(path, user_files)].append(path)
    return {'counts': {name: len(paths) for name, paths in classes.items()},
            'paths': {name: paths[:limit] for name, paths in classes.items()},
            'user_tables_match': (not classes['user_table']) if known else None,
            'user_files': len(user_files) if known else None}


def parse_noise_plan(text: str) -> list[tuple[float, int]]:
    """--idle-noise-plan '5:20,30:6,120:3' -> [(window seconds, windows), ...]."""
    plan = []
    for item in (text or '').split(','):
        if not item.strip():
            continue
        seconds, colon, count = item.partition(':')
        if not colon or float(seconds) <= 0 or int(count) < 1:
            raise ValueError(f'--idle-noise-plan wants SECONDS:WINDOWS,..., got {text!r}')
        plan.append((float(seconds), int(count)))
    return plan


def parse_noise_table(text: str) -> dict[float, int]:
    """--dirty-noise-table '5:4,30:4,120:4' (window seconds: MiB) -> window seconds -> bytes."""
    table = {}
    for item in (text or '').split(','):
        if not item.strip():
            continue
        seconds, colon, mib = item.partition(':')
        if not colon or float(seconds) <= 0 or float(mib) < 0:
            raise ValueError(f'--dirty-noise-table wants SECONDS:MIB,..., got {text!r}')
        table[float(seconds)] = int(float(mib) * (1 << 20))
    return table


def noise_table(given: dict, measured: dict | None = None) -> dict[float, int]:
    """Check (a)'s noise per window length: the larger of the given and the measured (largest increase over that
    length's idle windows), then, since a length's noise is the largest increase over windows of that length or
    shorter, the running maximum from the shortest length up."""
    lengths = sorted(set(given) | set(measured or {}))
    table, running = {}, 0
    for length in lengths:
        running = max(running, int(given.get(length, 0)), int((measured or {}).get(length, 0)))
        table[length] = running
    return table


def noise_limit(table: dict, clock_s) -> dict:
    """The noise check (a) allows a run: that of the shortest window length that covers its clock, or the
    longest length's, flagged beyond_table, for a run that outlasts every length (or whose clock is unknown)."""
    lengths = sorted(table)
    covering = [length for length in lengths if clock_s is not None and length >= clock_s]
    window = covering[0] if covering else lengths[-1]
    return {'window_s': window, 'noise_bytes': table[window], 'beyond_table': not covering}


def _phase_sum(record: dict, *keys):
    values = [_get(phase, *keys) for phase in (record.get('phases') or {}).values()]
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def network_checks(record: dict) -> dict:
    """A11 (ii) for one run, from its record: netem's own drops grow by 0 on both VMs (each phase's qdisc delta);
    netem's sampled backlog stays below its limit on both; netem's bytes on the source are at least 0.98 x the
    run's wire bytes from source to dest; the sysctl guard passed; and the sending sockets' minrtt is in [r,
    r + 2 ms]. ok is False with any problem, None while a reading is missing, True otherwise."""
    rtt = _get(record, 'condition', 'rtt_ms')
    base = _get(record, 'condition', 'base_rtt_ms') or 0.0
    if rtt is not None:
        rtt += base
    problems, unknown = [], []
    values: dict = {}
    for host in ('source', 'dest'):
        own = _phase_sum(record, 'qdisc', host, 'netem_own_drops')
        values[f'netem_own_drops_{host}'] = own
        if own is None:
            unknown.append(f"netem's own drops on {host}")
        elif own > 0:
            problems.append(f"netem's own drops on {host}: {own}")
        samples = _get(record, 'qdisc', 'samples', host) or {}
        values[f'netem_qlen_max_{host}'] = samples.get('netem_qlen_max')
        if samples.get('at_limit'):
            problems.append(f"netem's backlog on {host} reached its limit ({samples.get('netem_qlen_max')} packets)")
        elif samples.get('at_limit') is None:
            unknown.append(f"netem's sampled backlog on {host}")
    netem_bytes = _phase_sum(record, 'qdisc', 'source', 'netem', 'bytes')
    wire = (record.get('totals') or {}).get('wire_bytes_source_to_dest')
    values['netem_bytes_source'], values['wire_bytes_source_to_dest'] = netem_bytes, wire
    if netem_bytes is None or wire is None:
        unknown.append("netem's bytes against the wire bytes")
    elif netem_bytes < NETEM_BYTES_FRACTION * wire:
        problems.append(f"netem's bytes on the source ({netem_bytes}) are under {NETEM_BYTES_FRACTION} x the wire "
                        f'bytes ({wire})')
    guard = _get(record, 'sysctl', 'tcp', 'ok')
    if guard is False:
        problems.append('the TCP sysctl guard failed')
    elif guard is None:
        unknown.append('the TCP sysctl guard')
    ss = record.get('ss') or {}
    low, high = ss.get('minrtt_ms_min'), ss.get('minrtt_ms_max')
    values['minrtt_ms_min'], values['minrtt_ms_max'] = low, high
    if rtt is None or low is None or high is None:
        unknown.append("the sending sockets' minrtt")
    elif low < rtt or high > rtt + MINRTT_SLACK_MS + BASE_RTT_SLACK_SHARE * base:
        problems.append(f"the sending sockets' minrtt [{low}, {high}] ms is outside [{rtt:g}, "
                        f'{rtt + MINRTT_SLACK_MS + BASE_RTT_SLACK_SHARE * base:g}] ms')
    return {'ok': False if problems else (None if unknown else True), 'problems': problems, 'unknown': unknown,
            **values}


def window_limited_bound(method: str, link: dict):
    """A9's window-limited bound for a parcopy* run: its stream count's link test at the condition (the trial's
    first visit) under 0.9 x the zero-delay rate at the same tbf rate (link['zero_delay_bound_gbit']: the default
    anchor's 8-stream test at 2 Gbit/s, the zero-delay probe at 1 Gbit/s). None for other arms or without both."""
    if method not in PARCOPY_METHODS:
        return None
    streams = PARCOPY_VARIANTS[method]['streams']
    rate = link.get(f'source_to_dest_{streams}streams_steady_gbit') or link.get(f'source_to_dest_{streams}streams_gbit')
    zero = link.get('zero_delay_bound_gbit')
    if not rate or not zero:
        return None
    return rate < 0.9 * zero


def excluded_reason(record: dict) -> str | None:
    """Why a T12 run is left out of every median, or None: an error (failure_kind), a failed verification, A11
    (ii)'s network checks, or A13 (a durable arm failing check (a) or (b))."""
    reasons = []
    if record.get('status') == 'error':
        reasons.append(f"error ({record.get('failure_kind')})")
    elif record.get('status') == 'fail':
        reasons.append('verification failed')
    network = record.get('network_checks') or {}
    if network.get('problems'):
        reasons.append('A11: ' + '; '.join(network['problems']))
    durability = record.get('durability') or {}
    if durability.get('durable') and durability.get('ok') is False:
        reasons.append('A13: durability check (a) or (b) failed')
    return '; '.join(reasons) or None


def t12_columns(record: dict) -> dict:
    """The T12 columns of one record (empty for a run that isn't EXP-13's)."""
    condition = record.get('condition') or {}
    if not condition:
        return {field: None for field in T12_FIELDS}
    reported = record.get('tool_reported') or {}
    receiver = reported.get('receiver') if isinstance(reported.get('receiver'), dict) else {}
    tcp = _get(record, 'sysctl', 'tcp') or {}
    ss = record.get('ss') or {}
    main = ss.get('main') or {}
    network = record.get('network_checks') or {}
    durability = record.get('durability') or {}
    samples = _get(record, 'qdisc', 'samples') or {}
    link = record.get('link') or {}
    tls = record.get('stunnel') or {}
    method = record.get('method')
    columns = {
        'rate_gbit': condition.get('gbit'), 'rtt_ms': condition.get('rtt_ms'), 'host_tcp': condition.get('host_tcp'),
        'ping_min_ms': condition.get('ping_min_ms'), 'visit_one_stream_gbit': condition.get('visit_one_stream_gbit'),
        'zero_delay_gbit': condition.get('zero_delay_gbit'), 'busy_inflight_bytes': condition.get('busy_inflight_bytes'),
        'sysctl_ok': tcp.get('ok'),
        'netem_own_drops': (None if None in (network.get('netem_own_drops_source'), network.get('netem_own_drops_dest'))
                            else network['netem_own_drops_source'] + network['netem_own_drops_dest']),
        'netem_backlog_max': max([value for value in (samples.get(host, {}).get('netem_qlen_max')
                                                      for host in ('source', 'dest')) if value is not None],
                                 default=None),
        'netem_backlog_max_bytes': max([value for value in (samples.get(host, {}).get('netem_backlog_max_bytes')
                                                            for host in ('source', 'dest')) if value is not None],
                                       default=None),
        'ss_sockets': ss.get('connections'), 'ss_sending_sockets': len(ss.get('sending') or ()) if ss else None,
        'minrtt_ms': main.get('minrtt_ms'), 'minrtt_ms_max': ss.get('minrtt_ms_max'), 'cwnd': main.get('cwnd'),
        'busy_ms': ss.get('busy_ms'), 'rwnd_limited_ms': ss.get('rwnd_limited_ms'),
        'sndbuf_limited_ms': ss.get('sndbuf_limited_ms'), 'limited_fraction': ss.get('limited_fraction'),
        'retrans': ss.get('retrans_total'), 'notsent': main.get('notsent'), 'unacked': main.get('unacked'),
        'notsent_max': main.get('notsent_max'), 'unacked_max': main.get('unacked_max'),
        'tls_version': tls.get('tls_version'), 'tls_suite': tls.get('tls_suite'),
        'tls_connections': tls.get('connections'),
        'noise_window_s': durability.get('noise_window_s'), 'dirty_limit_bytes': durability.get('limit_bytes'),
        'network_ok': network.get('ok'), 'excluded_reason': excluded_reason(record), 'inferred': False,
        'window_limited_bound': window_limited_bound(method, link),
        'receiver_wall_s': record.get('receiver_wall_s'),
        'busy_split_valid': (None if method not in PGDELTA_METHODS else method not in TLS_METHODS),
        # A15's bound on the run, and why it failed where the harness knows ('timeout': it reached that bound).
        'run_limit_s': (record.get('run_limit') or {}).get('limit_s'), 'failure_reason': record.get('failure_reason'),
        # parcopy's connection set-up: its sender's control and data connections, all opened at once.
        'parcopy_connect_s': parcopy_connect_s(reported.get('send_stats'))}
    for host in ('source', 'dest'):
        values = tcp.get(host) or {}
        columns[f'tcp_rmem_max_{host}'] = (tcp_mem(values.get('net.ipv4.tcp_rmem')) or (None,) * 3)[2]
        columns[f'tcp_wmem_max_{host}'] = (tcp_mem(values.get('net.ipv4.tcp_wmem')) or (None,) * 3)[2]
        for part, key in (('netem', 'bytes'), ('netem', 'drops'), ('tbf', 'drops'), ('tbf', 'overlimits')):
            columns[f'{part}_{key}_{host}'] = _phase_sum(record, 'qdisc', host, part, key)
        columns[f'netem_own_drops_{host}'] = network.get(f'netem_own_drops_{host}')
        for column, name in (('retrans_segs', 'TcpRetransSegs'), ('lost_retransmit', 'TcpExtTCPLostRetransmit'),
                             ('tcp_timeouts', 'TcpExtTCPTimeouts')):
            columns[f'{column}_{host}'] = _phase_sum(record, 'nstat', host, name)
    for base in PGDELTA_SENDER_TIMERS:
        columns[f'{base}_s'] = reported.get(f'{base}_s')
    for base in PGDELTA_SENDER_WAITS:
        columns[f'{base}_busy_s'] = reported.get(f'{base}_busy_s')
        columns[f'{base}_idle_s'] = reported.get(f'{base}_idle_s')
    for name in (*PGDELTA_SENDER_COUNTS, 'window', 'high_churn_node_fraction', 'high_churn_files'):
        columns[name] = reported.get(name)
    for base in PGDELTA_RECEIVER_TIMERS:
        columns[f'{base}_s'] = receiver.get(f'{base}_s')
    # publish_s, from the receiver's launch to its publication, and the drain (A3), from the publication to its exit:
    # on dest's clock where the record has both ends (its command's launch and wall; pgdelta's published_utc), so
    # they add up to its wall. Else pgdelta's own publish_s, which counts from the interpreter's start (python -m
    # pgdelta), and the wall less it, which then also holds the interpreter's start-up.
    published, command = receiver.get('published_utc'), receiver_command(record)
    if published is not None and command is not None:
        columns['publish_s'] = round(published - command['started_utc'], 6)
        columns['drain_s'] = round(command['started_utc'] + command['wall_seconds'] - published, 6)
    else:
        publish = columns.get('publish_s')
        columns['drain_s'] = (None if publish is None or columns['receiver_wall_s'] is None
                              else round(columns['receiver_wall_s'] - publish, 6))
    return columns


def parcopy_connect_s(send_stats) -> float | None:
    """parcopy's sender's connection set-up: its stats' connected_s less connect_start_s (the control connection and
    every data connection opened at once), or less select_s from a sender that predates connect_start_s (which
    opened them one after another); None without them."""
    if not isinstance(send_stats, dict):
        return None
    connected = send_stats.get('connected_s')
    start = send_stats.get('connect_start_s', send_stats.get('select_s'))
    if not isinstance(connected, (int, float)) or not isinstance(start, (int, float)):
        return None
    return round(connected - start, 6)


def receiver_command(record: dict):
    """pgdelta's receiver's command record on dest, if it has its launch (started_utc) and its wall; else None."""
    for phase in (record.get('phases') or {}).values():
        for command in phase.get('commands', ()) if isinstance(phase, dict) else ():
            if command.get('host') == 'dest' and 'receiver' in str(command.get('role', '')):
                if command.get('started_utc') is not None and command.get('wall_seconds') is not None:
                    return command
    return None


def receiver_exit_utc(record: dict):
    """pgdelta's receiver's exit on dest's clock: its command's launch plus its wall; None without."""
    command = receiver_command(record)
    return None if command is None else command['started_utc'] + command['wall_seconds']


T12_FIELDS = (
    'rate_gbit', 'rtt_ms', 'host_tcp', 'ping_min_ms', 'visit_one_stream_gbit', 'zero_delay_gbit',
    'busy_inflight_bytes', 'sysctl_ok', 'tcp_rmem_max_source', 'tcp_rmem_max_dest', 'tcp_wmem_max_source',
    'tcp_wmem_max_dest',
    'netem_bytes_source', 'netem_bytes_dest', 'netem_drops_source', 'netem_drops_dest', 'tbf_drops_source',
    'tbf_drops_dest', 'tbf_overlimits_source', 'tbf_overlimits_dest', 'netem_own_drops', 'netem_own_drops_source',
    'netem_own_drops_dest', 'netem_backlog_max', 'netem_backlog_max_bytes',
    'retrans_segs_source', 'retrans_segs_dest', 'lost_retransmit_source', 'lost_retransmit_dest',
    'tcp_timeouts_source', 'tcp_timeouts_dest',
    'ss_sockets', 'ss_sending_sockets', 'minrtt_ms', 'minrtt_ms_max', 'cwnd', 'busy_ms', 'rwnd_limited_ms',
    'sndbuf_limited_ms', 'limited_fraction', 'retrans', 'notsent', 'unacked', 'notsent_max', 'unacked_max',
    'window', 'high_churn_node_fraction', 'high_churn_files', 'leaf_requests', 'leaf_waits_over_1ms',
    'connect_s', 'accept_wait_s', 'accept_wait_busy_s', 'accept_wait_idle_s', 'basis_wait_s', 'basis_wait_busy_s',
    'basis_wait_idle_s', 'leaf_wait_s', 'leaf_wait_busy_s', 'leaf_wait_idle_s', 'page_send_s', 'complete_wait_s',
    'complete_wait_busy_s', 'complete_wait_idle_s', 'hello_wait_s', 'recv_wait_s', 'publish_s', 'receiver_wall_s',
    'drain_s', 'busy_split_valid',
    'tls_version', 'tls_suite', 'tls_connections',
    'noise_window_s', 'dirty_limit_bytes', 'network_ok', 'excluded_reason', 'inferred', 'window_limited_bound',
    'run_limit_s', 'failure_reason', 'parcopy_connect_s',
)


# --- results ----------------------------------------------------------------

CSV_FIELDS = (
    'run_name', 'trial_id', 'pattern', 'fraction', 'scale', 'rows_changed', 'repeat',
    'speed', 'speed_index', 'target_gbit', 'measured_s2d_gbit', 'measured_d2s_gbit',
    'method', 'order_index', 'status',
    'total_s', 'total_no_ssh_s', 'inplace_total_s', 'inplace_total_no_ssh_s',
    'serial_total_s', 'serial_total_no_ssh_s',
    'prep_s', 'capture_s', 'prep_capture_s', 'receiver_start_s', 'receiver_capture_s', 'transfer_s',
    'combine_s', 'restore_s', 'ssh_overhead_s', 'untimed_setup_s',
    'prefetch_bytes', 'prefetch_basis_bytes', 'prefetch_s', 'prefetch_wait_s', 'prefetch_stopped',
    'untimed_gap_s', 'prefetch_untimed_s', 'prefetch_untimed_bytes_est', 'hash_basis_s',
    'rebuild_s', 'fsync_s', 'fsync_wait_s', 'verify_wait_s', 'in_place', 'fsync_mode', 'source_kind',
    'wire_s2d_bytes', 'wire_d2s_bytes', 'wire_s2d_bytes_dest_rx', 'wire_d2s_bytes_source_rx',
    'recovery_wire_s2d_bytes', 'recovery_wire_d2s_bytes', 'recovery_wal_files',
    'tool_s2d_bytes', 'tool_d2s_bytes', 'output_bytes', 'output_minus_wal_padding_bytes',
    'output_shared_bytes', 'capture_bytes', 'wal_bytes', 'wal_stream_bytes', 'wal_padding_bytes',
    'cpu_source_s', 'cpu_dest_s', 'prep_capture_cpu_dest_s', 'receiver_capture_cpu_dest_s',
    'counter_windows',
    'proc_cpu_source_s', 'proc_cpu_dest_s', 'peak_rss_source_kb', 'peak_rss_dest_kb',
    'proc_scope',
    # T2 (EXP-11)
    'tier', 'block', 'source_cache', 'durable', 'durability_ok', 'dirty_increase_mb',
    'disk_write_dest_bytes', 'disk_read_dest_bytes',
    'pbb_checkpoint_s', 'pbb_stream_s', 'pbb_wal_end_s', 'pbb_sync_s', 'pbb_manifest_s', 'backup_mode_adder_s',
    'probackup_sync_s', 'probackup_copy_s', 'probackup_stop_s', 'probackup_files_sync_s',
    'parcopy_streams', 'parcopy_max_stream_bytes', 'parcopy_balance', 'parcopy_listening_s', 'parcopy_first_byte_s',
    'parcopy_last_byte_s', 'parcopy_write_s', 'parcopy_fsync_s', 'parcopy_fsync_calls', 'parcopy_fsync_expected',
    'usable_s', 'void', 'bytes_identical', 'measured_s2d_gbit_8streams', 'link_time_s', 'above_link_s',
    'sysctl_dirty_background_bytes', 'sysctl_dirty_background_ratio',
    'verified', 'identical_to_s', 'logical_match', 'output_start_s', 'caches_dropped', 'error', 'failure_kind',
    # T10a (EXP-12). The harness fills what a run itself knows: its block, the load, its window on both clocks, its
    # settle, the source's CPU, checks (a) and (b), the oracle's checks, G4's staleness and dest's wait. What only
    # the load's logs and the sampler give (W, D, latencies, the client's wait, most flags) the analysis fills from
    # each record's `slowdown` (analysis/exp12.py), and is empty here until it does.
    'block_repeat', 'control_slot', 'load_rate', 'load_run', 'load_void', 't0_utc', 't1_utc', 'clock_offset_s',
    'settle_s', 'settle_gate_passed', 'dest_wait_s', 'tail_verification_s',
    'window_buckets', 'worst_drop', 'work_lost_s', 'worst_drop_net', 'work_lost_net_s', 'late_count_s',
    'lat_p50_ms', 'lat_p95_ms', 'lat_p99_ms', 'lat_p999_ms', 'lat_max_ms', 'lat_p99_worst5s_ms', 'backlog_end',
    'cpu_source_busy_s', 'cpu_source_calibrated_s', 'cpu_source_excess_s', 'steal_s', 'client_wait_s',
    'client_wait_worst_bucket_s', 'dirty_increase_bytes', 'md_write_ratio',
    'flag_unsettled', 'flag_checkpoint', 'flag_steal', 'flag_cache', 'flag_client', 'flag_client_wait_w',
    'flag_client_wait_d', 'flag_autovacuum',
    'g4_bytes', 'g4_start', 'g4_atomic', 'g4_cut', 'g4_coverage', 'g4_rows', 'g4_static_digest', 'g4_schema',
    'amcheck_status', 'amcheck_relations', 'capture_end_lsn', 'capture_end_utc', 'cut_lo_utc', 'cut_hi_utc',
    'staleness_s', 'backup_start_lsn', 'backup_stop_lsn', 'proxy_backup_held', 'proxy_all_hashed',
    # T12 (EXP-13): the condition, the emulator's and TCP's counters, the ss reading, pgdelta's protocol timers,
    # TLS, check (a)'s limit by the run's clock, and A11's exclusions (t12_columns; empty outside EXP-13).
    *T12_FIELDS,
)
# The columns the analysis fills from a record's `slowdown` (T10a), by name.
SLOWDOWN_FIELDS = ('window_buckets', 'worst_drop', 'work_lost_s', 'worst_drop_net', 'work_lost_net_s',
                   'late_count_s', 'lat_p50_ms', 'lat_p95_ms', 'lat_p99_ms', 'lat_p999_ms', 'lat_max_ms',
                   'lat_p99_worst5s_ms', 'backlog_end', 'client_wait_s', 'client_wait_worst_bucket_s',
                   'flag_checkpoint', 'flag_steal', 'flag_client', 'flag_client_wait_w', 'flag_client_wait_d',
                   'flag_autovacuum')


def calibrated_cpu(record: dict):
    """EXP-12's calibrated CPU-s of a run without load: the source's VM-wide busy CPU over its timed phases (steal
    left out), minus the idle rate (step 6's) times their wall. None without either."""
    busy = phases_cpu(record)
    rate = record.get('idle_rate_no_steal')
    wall = (record.get('totals') or {}).get('wall_seconds')
    if busy is None or rate is None or wall is None:
        return None
    return round(busy - rate * wall, 3)


def excess_cpu(record: dict):
    """Under load: the timed phases' busy CPU (steal left out) minus the settle's rate times their wall. The timed
    phases only: EXP-12's registered view, over the window [t0, t1 + 10 s], is analysis/exp12.py's, from the
    sampler's ticks (it fills the curated CSV's cpu_source_excess_s with it)."""
    busy = phases_cpu(record)
    rate = _get(record, 'settle', 'busy_rate_no_steal')
    wall = (record.get('totals') or {}).get('wall_seconds')
    if busy is None or rate is None or wall is None:
        return None
    return round(busy - rate * wall, 3)


def load_columns(record: dict) -> dict:
    """The T10a columns of one record (empty for a run that isn't EXP-12's)."""
    window = record.get('window') or {}
    settle = record.get('settle') or {}
    oracle = _get(record, 'verification', 'oracle') or {}
    durability = record.get('durability') or {}
    g4 = record.get('g4') or {}
    backup = _get(record, 'proxy', 'backup') or {}
    slowdown = record.get('slowdown') or {}
    phases = record.get('phases') or {}
    steal = [_get(phase, 'cpu', 'source', 'steal_seconds') for phase in phases.values()]
    gate = settle.get('gate')
    cache = _get(record, 'source_untimed', 'cache') or {}
    columns = {
        'block_repeat': record.get('block_repeat'), 'control_slot': record.get('control_slot'),
        'load_rate': record.get('load_rate'), 'load_run': record.get('load_run'), 'load_void': record.get('void'),
        't0_utc': window.get('t0_utc'), 't1_utc': window.get('t1_utc'),
        'clock_offset_s': window.get('clock_offset_s', record.get('clock_offset_s')),
        'settle_s': settle.get('seconds'), 'settle_gate_passed': None if gate is None else gate.get('passed'),
        'dest_wait_s': settle.get('dest_wait_s'), 'tail_verification_s': _get(record, 'tail', 'verification_s'),
        'cpu_source_busy_s': phases_cpu(record),
        'cpu_source_calibrated_s': calibrated_cpu(record) if record.get('block') == 'calibration' else None,
        'cpu_source_excess_s': excess_cpu(record) if record.get('load_rate') else None,
        'steal_s': None if not steal or None in steal else round(sum(steal), 3),
        'dirty_increase_bytes': durability.get('dirty_writeback_increase_bytes'),
        'md_write_ratio': durability.get('md_write_ratio'),
        'flag_unsettled': settle.get('unsettled') if settle else None, 'flag_cache': cache.get('cache_flag'),
        'g4_bytes': oracle.get('bytes'), 'g4_start': oracle.get('start'), 'g4_atomic': oracle.get('atomic'),
        'g4_cut': oracle.get('cut'), 'g4_coverage': oracle.get('coverage'), 'g4_rows': oracle.get('rows'),
        'g4_static_digest': oracle.get('static_digest'), 'g4_schema': oracle.get('schema'),
        'amcheck_status': oracle.get('amcheck'), 'amcheck_relations': oracle.get('amcheck_relations'),
        'capture_end_lsn': (g4.get('capture_end') or {}).get('lsn'),
        'capture_end_utc': (g4.get('capture_end') or {}).get('utc'),
        'cut_lo_utc': (g4.get('cut_utc') or [None, None])[0], 'cut_hi_utc': (g4.get('cut_utc') or [None, None])[1],
        'staleness_s': g4.get('staleness_s'), 'backup_start_lsn': backup.get('start_lsn'),
        'backup_stop_lsn': backup.get('stop_lsn'), 'proxy_backup_held': backup.get('held'),
        'proxy_all_hashed': _get(record, 'proxy', 'hashed', 'all_hashed')}
    columns.update({field: slowdown.get(field) for field in SLOWDOWN_FIELDS})
    return columns


def _get(value, *keys):
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def phase_seconds(record: dict, name: str):
    """Wall seconds of a timed phase, of a part of a concurrent phase (prep, capture), or of a
    step of a sequential one (pgdelta_direct*'s receiver_start and transfer)."""
    phases = record.get('phases') or {}
    if name in phases:
        return _get(phases, name, 'wall_seconds')
    for phase in phases.values():
        for kind in ('parts', 'steps'):
            if name in (phase.get(kind) or {}):
                return _get(phase, kind, name, 'wall_seconds')
    return None


def link_seconds(record: dict):
    """The run's source-to-dest wire bytes at the link test's measured rate: its link time.

    Multi-stream arms (MULTI_STREAM_METHODS) take the 8-stream rate, the others the single stream's.
    None without the bytes or the rate.
    """
    link = record.get('link') or {}
    key = ('source_to_dest_8streams_gbit' if record.get('method') in MULTI_STREAM_METHODS
           else 'source_to_dest_gbit')
    rate = link.get(key)
    wire = (record.get('totals') or {}).get('wire_bytes_source_to_dest')
    if not rate or wire is None:
        return None
    return round(wire * 8 / (rate * 1e9), 3)


def summary_row(record: dict) -> dict:
    """One CSV row for one (trial, repeat, speed, method) record."""
    totals = record.get('totals') or {}
    in_place = record.get('totals_in_place') or {}
    serial = record.get('totals_serial') or {}
    verification = record.get('verification') or {}
    link = record.get('link') or {}
    speed = record.get('speed') or {}
    wal = record.get('wal') or {}
    prefetch = _get(record, 'tool_reported', 'prefetch') or {}
    untimed = record.get('prefetch_untimed') or {}
    method = record.get('method')
    durability = record.get('durability') or {}
    increase = durability.get('dirty_writeback_increase_bytes')
    basebackup = _get(record, 'tool_reported', 'basebackup_phases') or {}
    probackup = _get(record, 'tool_reported', 'probackup_phases') or {}
    parcopy = _get(record, 'tool_reported', 'parcopy_summary') or {}
    sysctl = record.get('sysctl') or {}
    in_effect = sysctl.get('during') or sysctl.get('before') or {}
    ready = _get(verification, 'output_start', 'ready_seconds')
    link_time = link_seconds(record)
    return {
        'run_name': record.get('run_name'),
        'trial_id': record.get('trial_id'),
        'pattern': record.get('pattern'),
        'fraction': record.get('fraction'),
        'scale': record.get('scale'),
        'rows_changed': record.get('rows_changed'),
        'repeat': record.get('repeat'),
        'speed': speed.get('label'),
        'speed_index': record.get('speed_index'),
        'target_gbit': speed.get('gbit'),
        'measured_s2d_gbit': link.get('source_to_dest_gbit'),
        'measured_d2s_gbit': link.get('dest_to_source_gbit'),
        'method': record.get('method'),
        'order_index': record.get('order_index'),
        'status': record.get('status'),
        # End to end: every timed phase, rsync's copy of B0 included. rsync's copy
        # runs on dest while its capture runs on source, so it counts only where it
        # is the longer of the two: max(prep, capture) + transfer. pgdelta's receiver
        # starts beside its capture the same way: max(receiver start, capture) + transfer.
        'total_s': totals.get('wall_seconds'),
        'total_no_ssh_s': totals.get('wall_seconds_less_remote_overhead'),
        # rsync only: without the copy of B0, the lower bound for patching B0 in place.
        'inplace_total_s': in_place.get('wall_seconds'),
        'inplace_total_no_ssh_s': in_place.get('wall_seconds_less_remote_overhead'),
        # rsync and pgdelta: the two parts of the concurrent phase and the transfer one
        # after the other, the upper bound.
        'serial_total_s': serial.get('wall_seconds'),
        'serial_total_no_ssh_s': serial.get('wall_seconds_less_remote_overhead'),
        'prep_s': phase_seconds(record, 'prep'),
        'capture_s': phase_seconds(record, 'capture'),
        'prep_capture_s': phase_seconds(record, CONCURRENT_PHASE),
        # pgdelta only: from launching the receiver until it listened (a part of receiver_capture
        # for the staged methods, the first step of pgdelta_direct*'s one phase), and the pair's wall.
        'receiver_start_s': (phase_seconds(record, 'receiver') if phase_seconds(record, 'receiver')
                             is not None else phase_seconds(record, DIRECT_RECEIVER_STEP)),
        'receiver_capture_s': phase_seconds(record, RECEIVER_PHASE),
        'transfer_s': phase_seconds(record, 'transfer'),
        'combine_s': phase_seconds(record, 'combine'),
        # pgbackrest_* only: the restore on dest, the method's one timed phase.
        'restore_s': phase_seconds(record, RESTORE_PHASE),
        'ssh_overhead_s': totals.get('remote_overhead_seconds'),
        # Untimed, before the cache drop: the rsync daemon's start, the copy of B0 to B that the
        # in-place methods and pgBackRest patch, the clean stop of the source for pgdelta_direct*.
        'untimed_setup_s': _get(record, 'preparation', 'wall_seconds'),
        # pgdelta only: what the receiver read of B0 into the page cache while it waited
        # for the sender (pgdelta's statistics, receiver.prefetch).
        'prefetch_bytes': prefetch.get('bytes'),
        'prefetch_basis_bytes': prefetch.get('basis_bytes'),
        'prefetch_s': prefetch.get('seconds'),
        'prefetch_wait_s': prefetch.get('waited_seconds'),
        'prefetch_stopped': prefetch.get('stopped'),
        # pgdelta only: the untimed gap between receiver_capture and the transfer (the counters
        # are read there), and how much of the prefetch fell in it: B0 read off the clock
        # (prefetch_untimed). The bytes are an estimate at the prefetch's average rate.
        'untimed_gap_s': untimed.get('gap_seconds'),
        'prefetch_untimed_s': untimed.get('untimed_seconds'),
        'prefetch_untimed_bytes_est': untimed.get('untimed_bytes_estimate'),
        # pgdelta only: the receiver hashing B0 for the sender (its own timer, inside the transfer).
        'hash_basis_s': _get(record, 'tool_reported', 'timings_seconds', 'hash_basis'),
        # pgdelta only, its own timers inside the transfer: the write phase, of which the fsyncs,
        # the main loop's wait for fsyncs done elsewhere (T3 (b)), and its wait for verification.
        'rebuild_s': _get(record, 'tool_reported', 'timings_seconds', 'rebuild'),
        'fsync_s': _get(record, 'tool_reported', 'timings_seconds', 'fsync'),
        'fsync_wait_s': _get(record, 'tool_reported', 'fsync_wait_seconds'),
        'verify_wait_s': _get(record, 'tool_reported', 'timings_seconds', 'verify_wait'),
        # pgdelta only: the modes its statistics report (empty before pgdelta reported them); the
        # fsync mode in effect, which for writeback can fall back to main (tool_reported.fsync_requested).
        'in_place': _get(record, 'tool_reported', 'modes', 'in_place'),
        'fsync_mode': _get(record, 'tool_reported', 'modes', 'fsync_mode'),
        'source_kind': _get(record, 'tool_reported', 'modes', 'source_kind'),
        'wire_s2d_bytes': totals.get('wire_bytes_source_to_dest'),
        'wire_d2s_bytes': totals.get('wire_bytes_dest_to_source'),
        'wire_s2d_bytes_dest_rx': totals.get('wire_bytes_source_to_dest_dest_rx'),
        'wire_d2s_bytes_source_rx': totals.get('wire_bytes_dest_to_source_source_rx'),
        # pgbackrest_* only: of the wire bytes above, those its restored copy's recovery fetched
        # (archive-get, untimed), and how many WAL files that was.
        'recovery_wire_s2d_bytes': totals.get('recovery_wire_bytes_source_to_dest'),
        'recovery_wire_d2s_bytes': totals.get('recovery_wire_bytes_dest_to_source'),
        'recovery_wal_files': _get(verification, 'recovery', 'wal_files_restored'),
        'tool_s2d_bytes': _get(record, 'tool_reported', 'bytes_source_to_dest'),
        'tool_d2s_bytes': _get(record, 'tool_reported', 'bytes_dest_to_source'),
        'output_bytes': record.get('output_bytes'),
        'output_minus_wal_padding_bytes': record.get('output_minus_wal_padding_bytes'),
        # The output's bytes in extents it shares with another file (FIEMAP), after the run: a clone
        # of B0 on XFS (rsync's cp -a, pgdelta's copy_file_range); 0 on ext4; empty without FIEMAP.
        'output_shared_bytes': _get(verification, 'shared_extents', 'shared_bytes'),
        'capture_bytes': record.get('capture_bytes'),
        'wal_bytes': wal.get('wal_bytes'),
        'wal_stream_bytes': wal.get('wal_stream_bytes'),
        'wal_padding_bytes': wal.get('wal_padding_bytes'),
        'cpu_source_s': totals.get('cpu_seconds_source'),
        'cpu_dest_s': totals.get('cpu_seconds_dest'),
        # rsync only: dest CPU over the prep+capture window. The prep runs only on
        # dest; the capture's share there is its SSH client.
        'prep_capture_cpu_dest_s': _get(record, 'phases', CONCURRENT_PHASE, 'cpu', 'dest',
                                        'busy_seconds'),
        # pgdelta only: dest CPU over the receiver+capture window: the receiver's start-up
        # and prefetch, plus the capture's SSH client.
        'receiver_capture_cpu_dest_s': _get(record, 'phases', RECEIVER_PHASE, 'cpu', 'dest',
                                            'busy_seconds'),
        # Timed phases summed into the totals, each with its own counter window
        # (and its snapshot floor, run.json phase_floor).
        'counter_windows': len(record['phases']) if record.get('phases') else None,
        'proc_cpu_source_s': totals.get('process_cpu_seconds_source'),
        'proc_cpu_dest_s': totals.get('process_cpu_seconds_dest'),
        'peak_rss_source_kb': totals.get('peak_rss_kb_source'),
        'peak_rss_dest_kb': totals.get('peak_rss_kb_dest'),
        'proc_scope': PROC_SCOPE.get(record.get('method')),
        # T2 (EXP-11). The arm's tier (TIERS), which block of the trial ran it (main, extension,
        # warm_source) and whether the source's cache was dropped (cold) or read first (warm).
        'tier': TIERS.get(method),
        'block': record.get('block'),
        'source_cache': record.get('source_cache'),
        # Durability (durability_check): whether the arm is meant to be durable, whether checks (a)
        # and (b) passed, dest's Dirty + Writeback increase over the run (MB, 10^6 bytes), and dest's
        # md array's bytes written and read in the timed phases.
        'durable': durability.get('durable'),
        'durability_ok': durability.get('ok'),
        'dirty_increase_mb': None if increase is None else round(increase / 1e6, 3),
        'disk_write_dest_bytes': phase_disk_bytes(record, 'dest', 'write_bytes'),
        'disk_read_dest_bytes': phase_disk_bytes(record, 'dest', 'read_bytes'),
        # pg_basebackup arms: their --verbose lines' phases (basebackup_phases), and the backup-mode
        # adder, checkpoint + wal_end (R_adj).
        'pbb_checkpoint_s': basebackup.get('checkpoint_s'),
        'pbb_stream_s': basebackup.get('stream_s'),
        'pbb_wal_end_s': basebackup.get('wal_end_s'),
        'pbb_sync_s': basebackup.get('sync_s'),
        'pbb_manifest_s': basebackup.get('manifest_s'),
        'backup_mode_adder_s': basebackup.get('backup_mode_adder_s'),
        # probackup*: the timed sync -f of the output (its phase's step), and its own lines' phases.
        'probackup_sync_s': phase_seconds(record, PROBACKUP_STEPS[1]) if method in PROBACKUP_METHODS else None,
        'probackup_copy_s': probackup.get('copy_s'),
        'probackup_stop_s': probackup.get('stop_s'),
        'probackup_files_sync_s': probackup.get('files_sync_s'),
        # parcopy*: its receiver's own timers (parcopy_summary), in seconds since it started; the
        # transfer step starts once it listens (parcopy_listening_s).
        'parcopy_streams': parcopy.get('streams'),
        'parcopy_max_stream_bytes': parcopy.get('max_stream_bytes'),
        'parcopy_balance': parcopy.get('balance'),
        'parcopy_listening_s': parcopy.get('listening_s'),
        'parcopy_first_byte_s': parcopy.get('first_byte_s'),
        'parcopy_last_byte_s': parcopy.get('last_byte_s'),
        'parcopy_write_s': parcopy.get('write_s'),
        'parcopy_fsync_s': parcopy.get('fsync_s'),
        'parcopy_fsync_calls': parcopy.get('fsync_calls'),
        'parcopy_fsync_expected': parcopy.get('fsync_expected'),
        # Time until usable: the copy and then its start's time to consistency (started outputs).
        'usable_s': (None if totals.get('wall_seconds') is None or ready is None
                     else round(totals['wall_seconds'] + ready, 3)),
        # probackup*: void (the source changed during the trial) and the byte diagnostic.
        'void': verification.get('void'),
        'bytes_identical': verification.get('bytes_identical'),
        # The 8-stream link test at this speed (each trial's first visit to it); the link time of the
        # run's source-to-dest wire bytes at the measured rate (link_seconds), and total_s above it.
        'measured_s2d_gbit_8streams': link.get('source_to_dest_8streams_gbit'),
        'link_time_s': link_time,
        'above_link_s': (None if link_time is None or totals.get('wall_seconds') is None
                         else round(totals['wall_seconds'] - link_time, 3)),
        # dest's write-back settings in effect during the run (full_wb's while it was set).
        'sysctl_dirty_background_bytes': in_effect.get(DIRTY_BACKGROUND_BYTES),
        'sysctl_dirty_background_ratio': in_effect.get(DIRTY_BACKGROUND_RATIO),
        'verified': verification.get('verified'),
        # Staged methods: O (or B) against the capture S; pgdelta_direct*: against the files
        # pgdelta's datadir mode selects in the stopped source.
        'identical_to_s': (verification['identical_to_capture'] if 'identical_to_capture' in verification
                           else verification.get('identical_to_source')),
        # pgbackrest_* only: the restored copy's per-table digest against the source's (None
        # until the trial's last method has run).
        'logical_match': verification.get('logical_match'),
        # pgdelta_direct* and pgbackrest_*: starting the output on dest (untimed), recovery included.
        'output_start_s': _get(verification, 'output_start', 'ready_seconds'),
        'caches_dropped': record.get('caches_dropped'),
        'error': record.get('error'),
        # An error's kind: 'tool' (a command of the method's own timed phases failed) or 'harness'.
        'failure_kind': record.get('failure_kind'),
        **load_columns(record),
        **t12_columns(record),
    }


def write_csv(path: Path, records) -> None:
    """Rewrite the summary CSV from all records so far (atomically)."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with open(temporary, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction='raise')
        writer.writeheader()
        for record in records:
            writer.writerow(summary_row(record))
    temporary.replace(path)


def command_overhead(commands) -> float:
    """Outer minus inner wall of the remote commands: SSH and the agent's start-up."""
    return sum(command['wall_seconds'] - command['inner_wall_seconds'] for command in commands
               if command.get('inner_wall_seconds') is not None)


def part_commands(phase: dict, name: str) -> list:
    return [command for command in phase.get('commands', ()) if command.get('part') == name]


def phase_overhead(phase: dict) -> float:
    """The remote-launch time on a phase's critical path.

    For a sequential phase that is every remote command's overhead. A
    concurrent phase lasts as long as its longest part, so only what the
    overhead adds beyond the other parts counts: max(part walls) minus
    max(part walls without their overhead).
    """
    parts = phase.get('parts')
    if not parts:
        return command_overhead(phase.get('commands', ()))
    walls = [part['wall_seconds'] for part in parts.values()]
    lean = [part['wall_seconds'] - command_overhead(part_commands(phase, name))
            for name, part in parts.items()]
    return max(0.0, max(walls) - max(lean))


def phase_totals(phases: dict) -> dict:
    """Sum the timed phases: wall, wire bytes per direction and CPU per VM.

    Source-to-destination bytes are the source's transmit counter; the
    destination's receive counter is kept as a cross-check (and the other way
    round for the return direction). A concurrent phase counts once, with the
    wall of its parts together.
    """
    def total(*keys):
        values = [_get(phase, *keys) for phase in phases.values()]
        if not values or any(value is None for value in values):
            return None
        return round(sum(values), 3) if any(isinstance(v, float) for v in values) \
            else sum(values)

    def commands(host):
        return [command for phase in phases.values() for command in phase.get('commands', ())
                if command.get('host') == host]

    def process_cpu(host):
        values = [command['rusage']['cpu_seconds'] for command in commands(host)
                  if command.get('rusage')]
        return round(sum(values), 3) if values else None

    def peak_rss(host):
        # GNU time's figure for each tool; unknown if any command lacks one.
        values = [command.get('peak_rss_kb') for command in commands(host)]
        if not values or any(value is None for value in values):
            return None
        return max(values)

    remote = any(command.get('inner_wall_seconds') is not None
                 for phase in phases.values() for command in phase.get('commands', ()))
    wall = total('wall_seconds')
    remote_overhead = (round(sum(phase_overhead(phase) for phase in phases.values()), 3)
                       if remote else None)
    return {
        'wall_seconds': wall,
        # Time in SSH and the agent's start-up around remote commands, where it
        # is on the critical path; it is inside wall_seconds (the destination's view).
        'remote_overhead_seconds': remote_overhead,
        # wall_seconds with that remote-launch overhead (outer minus inner wall
        # of the remote commands) taken out; equal for local-only methods.
        'wall_seconds_less_remote_overhead': None if wall is None else
        round(wall - (remote_overhead or 0), 3),
        'wire_bytes_source_to_dest': total('net', 'source', 'tx_bytes'),
        'wire_bytes_dest_to_source': total('net', 'dest', 'tx_bytes'),
        'wire_bytes_source_to_dest_dest_rx': total('net', 'dest', 'rx_bytes'),
        'wire_bytes_dest_to_source_source_rx': total('net', 'source', 'rx_bytes'),
        'cpu_seconds_source': total('cpu', 'source', 'busy_seconds'),
        'cpu_seconds_dest': total('cpu', 'dest', 'busy_seconds'),
        'process_cpu_seconds_source': process_cpu('source'),
        'process_cpu_seconds_dest': process_cpu('dest'),
        'peak_rss_kb_source': peak_rss('source'),
        'peak_rss_kb_dest': peak_rss('dest'),
    }


def without_part(phases: dict, leave_out: str) -> dict:
    """phases with one part of each concurrent phase left out (rsync in place: no copy of B0).

    The other parts become a phase of their own, with the longest one's wall
    and their commands. A window's counters are per VM, not per part: its
    network counters are kept (the left-out part must not use the link, and
    rsync's local copy of B0 does not), and so is the CPU of every VM the
    left-out part did not run on; the CPU of the VM it ran on is unknown (None).
    """
    result = {}
    for name, phase in phases.items():
        parts = phase.get('parts') or {}
        if leave_out not in parts:
            result[name] = phase
            continue
        kept = {part: info for part, info in parts.items() if part != leave_out}
        if not kept:
            continue
        busy_host = parts[leave_out]['host']
        label = '+'.join(kept)
        result[label] = {
            'name': label,
            'wall_seconds': max(info['wall_seconds'] for info in kept.values()),
            'commands': [command for part in kept for command in part_commands(phase, part)],
            'net': phase.get('net'),
            'cpu': {host: (None if host == busy_host else counters)
                    for host, counters in (phase.get('cpu') or {}).items()},
            **({'parts': kept} if len(kept) > 1 else {})}
    return result


def serial_totals(phases: dict) -> dict:
    """Wall time as if every concurrent phase's parts ran one after the other: an upper bound."""
    split = {}
    for name, phase in phases.items():
        parts = phase.get('parts')
        if not parts:
            split[name] = phase
            continue
        for part, info in parts.items():
            split[f'{name}.{part}'] = {'wall_seconds': info['wall_seconds'],
                                       'commands': part_commands(phase, part)}
    totals = phase_totals(split)
    return {key: totals[key] for key in ('wall_seconds', 'remote_overhead_seconds',
                                         'wall_seconds_less_remote_overhead')}


def prefetch_untimed(phases: dict, prefetch) -> dict | None:
    """The part of pgdelta's prefetch of B0 that fell between its two timed phases.

    The receiver reads B0 from when it listens until the sender connects.
    receiver_capture ends once it listens and the capture has ended; the
    transfer phase starts the sender. Between the two, outside any timer, the
    orchestrator reads both VMs' counters (an SSH call to the source on each
    side of the gap), and the prefetch keeps reading if it hasn't finished.
    B0 read then is read off the clock; the transfer would otherwise have read
    it, so total_s can understate by about `untimed_seconds`. No other method
    runs anything in that gap: rsync's copy of B0 ends inside prep_capture.

    pgdelta_direct* has no gap: its receiver start and transfer are one timed
    phase, the sender starting as soon as the receiver listens, so this is
    None for it.

    Both clocks are pgd-dest's: the phases' started_utc/ended_utc are the
    orchestrator's time.time(), and the prefetch's started_utc/ended_utc the
    receiver's (pgdelta's statistics). `untimed_bytes_estimate` assumes the
    prefetch's average read rate. None if the run lacks either phase or the
    prefetch has no wall-clock times (it did not run, or an older pgdelta).
    """
    first = phases.get(RECEIVER_PHASE)
    second = phases.get('transfer')
    if not first or not second or not isinstance(prefetch, dict):
        return None
    start, end = prefetch.get('started_utc'), prefetch.get('ended_utc')
    if start is None or end is None or first.get('started_utc') is None \
            or second.get('started_utc') is None:
        return None
    gap_start = first.get('ended_utc')
    if gap_start is None:  # records from before ended_utc was kept
        gap_start = first['started_utc'] + first['wall_seconds']
    gap_end = second['started_utc']
    untimed = round(max(0.0, min(end, gap_end) - max(start, gap_start)), 3)
    seconds, read = prefetch.get('seconds') or 0, prefetch.get('bytes') or 0
    estimate = min(read, round(read * untimed / seconds)) if seconds > 0 else 0
    return {'gap_seconds': round(gap_end - gap_start, 3),
            # Negative when the prefetch ended inside receiver_capture.
            'end_after_phase_seconds': round(end - gap_start, 3),
            'untimed_seconds': untimed,
            'untimed_bytes_estimate': estimate,
            'spilled': untimed > 0}


def rusage_record(usage) -> dict:
    """CPU seconds from a resource.struct_rusage (the process and its reaped children).

    ru_maxrss is kept, as wait4_maxrss_kb, only for the record: it includes the
    high-water mark inherited from the launching process, so it is not the
    command's own peak memory (see GNU_TIME_FORMAT).
    """
    return {'user_seconds': round(usage.ru_utime, 3), 'system_seconds': round(usage.ru_stime, 3),
            'cpu_seconds': round(usage.ru_utime + usage.ru_stime, 3),
            'wait4_maxrss_kb': usage.ru_maxrss}
