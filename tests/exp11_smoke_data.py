"""A synthetic EXP-11 session, for dry runs of analysis/exp11.py: what `remote.py fetch` brings back.

write_session() writes the two sweeps of EXP-11's run lines (hot and append with the extension and
the warm-source block; uniform's main block), or the smoke run's shape, as run_exp04.py lays them
out: runs/<trial>/r<repeat>-<speed>-<method>.json, summary.csv (measure.write_csv over those
records, so every column is the core harness's), trials/<trial>.json and run.json.

The times follow the pre-registration's derivations (one stream: start, a cold read or the link,
an unoverlapped flush; parallel copies on the 8-stream rate; v1 flat for local changes) with seeded
noise. Nothing here is a measurement.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from experiments.network import measure

U = measure.UNTHROTTLED
V1, V1_J8 = 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_j8'
SPEEDS = ('2g', '5g', '10g', U)
# Measured single-stream and 8-stream rates, Gbit/s (EXP-09's link; the 16 Gbps egress cap).
RATES = {'2g': 1.917, '5g': 4.792, '10g': 9.58, U: 15.05}
RATES_8 = {'2g': 1.92, '5g': 4.80, '10g': 9.61, U: 15.4}
COPY_BYTES = 2_110_000_000  # a full copy's source-to-dest bytes at scale 70
V1_WIRE = {'hot': 29_200_000, 'append': 27_570_000, 'uniform': 610_400_000}
# EXP-11's run lines: the arms' speeds (every arm not named runs at every speed), the extension and
# the warm-source block.
LOCAL_METHODS = ('full', 'full_nosync', 'full_syncfs', 'full_wb', 'probackup4', 'probackup8', 'parcopy4', 'parcopy8',
                 V1, V1_J8)
METHOD_SPEEDS = {'full_nosync': ('2g', U), 'full_syncfs': ('10g', U), 'full_wb': ('10g', U), 'probackup4': (U,),
                 'parcopy4': (U,), V1_J8: (U,)}
EXTENSION = ('full', 'full_syncfs', 'probackup8', 'probackup4', 'parcopy8', 'parcopy4', V1, V1_J8)
WARM = ('full', 'probackup8', V1)
IDLE_NOISE = 4 << 20
REMOTE_LAUNCH = 0.06  # the harness's SSH launch of the sender, outside total_no_ssh_s


def basebackup_lines(method, parts):
    """pg_basebackup --verbose's lines, stamped as Child(stamp_lines=True) logs them."""
    at, lines = 0.0, []

    def line(text, after=0.0):
        nonlocal at
        at += after
        lines.append((round(at, 6), f'pg_basebackup: {text}'))
    line('initiating base backup, waiting for checkpoint to complete', parts['start'])
    line('checkpoint completed', parts['checkpoint'])
    line('write-ahead log start point: 2/9A000028 on timeline 1')
    line('starting background WAL receiver')
    line('write-ahead log end point: 2/9A000158', parts['stream'])
    line('waiting for background process to finish streaming ...')
    if method != 'full_nosync':
        line('syncing data to disk ...', parts['wal_end'])
        line('renaming backup_manifest.tmp to backup_manifest', parts['sync'])
    else:
        line('renaming backup_manifest.tmp to backup_manifest', parts['wal_end'])
    line('base backup completed', parts['manifest'])
    return lines


def probackup_lines(parts):
    at, lines = 0.0, []
    for text, after in (('INFO: Database catchup start, pg_probackup version: 2.5.16', 0.01),
                        ('INFO: Start transferring data files', parts['start']),
                        ('INFO: Data files are transferred, time elapsed: 2s', parts['copy']),
                        ('INFO: wait for pg_stop_backup()', 0.0),
                        ('INFO: Syncing copied files to disk', parts['stop']),
                        ('INFO: Files are synced, time elapsed: 0', parts['files_sync'])):
        at += after
        lines.append((round(at, 6), text))
    return lines


def timings(method, pattern, speed, scale, rng):
    """(total seconds, the parts each tool reports, wire bytes) for one run, from the derivations."""
    def n(value, spread=0.012):
        return max(0.0, value * (1 + rng.gauss(0, spread)))
    size = COPY_BYTES * scale / 70
    link, link8 = size * 8 / (RATES[speed] * 1e9), size * 8 / (RATES_8[speed] * 1e9)
    if method in measure.FULL_VARIANTS:
        flush = {'full': 1.3, 'full_syncfs': 1.25, 'full_wb': 0.4, 'full_nosync': 0.0}[method] * scale / 70
        parts = {'start': n(0.005), 'checkpoint': n(0.09), 'stream': n(max(link, 1.6 * scale / 70)),
                 'wal_end': n(0.05), 'sync': n(flush), 'manifest': n(0.002), 'exit': n(0.01)}
        return sum(parts.values()), parts, size
    if method in measure.PROBACKUP_METHODS:
        slower = 0.1 if method == 'probackup4' else 0.0
        parts = {'start': n(0.5), 'copy': n(max(link8, 1.25 * scale / 70) + slower), 'stop': n(0.1),
                 'files_sync': n(0.9 * scale / 70), 'exit': n(0.01), 'sync': n(0.08)}
        return sum(parts.values()), parts, size
    if method in measure.PARCOPY_METHODS:
        slower = 0.05 if method == 'parcopy4' else 0.0
        parts = {'receiver_start': n(0.06 + REMOTE_LAUNCH), 'copy': n(max(link8, 1.35 * scale / 70) + slower),
                 'fsync': n(0.25 * scale / 70), 'other': n(0.15)}
        return sum(parts.values()), parts, size
    # v1: flat for local changes; uniform's sends more.
    wire = V1_WIRE.get(pattern, V1_WIRE['hot']) * scale / 70
    transfer = 2.55 if pattern != 'uniform' else 5.0 / RATES[speed] + 2.45
    if method == V1_J8:
        transfer -= 0.2
    parts = {'receiver_start': n(0.155), 'transfer': n(transfer)}
    return sum(parts.values()), parts, wire


def record_for(method, pattern, speed, *, trial_id, run_name, repeat, block, order_index, scale, rng):
    """One run's record, in run_method's shape, as far as summary_row and the analysis read it."""
    total, parts, wire = timings(method, pattern, speed, scale, rng)
    if block == 'warm_source' and method != V1:
        total -= 0.3  # the walsender or pg_probackup's readers no longer read cold
    elif block == 'warm_source':
        total -= 0.05
    output = int(COPY_BYTES * scale / 70) if method in measure.FULL_COPY_METHODS else None
    dirty_before = 1_000_000
    dirty_after = dirty_before + (int(output * 0.85) if method in measure.NON_DURABLE_METHODS
                                  else int(rng.uniform(0, 900_000)))
    written = (int(output * (0.2 if method in measure.NON_DURABLE_METHODS else 1.012)) if output
               else int(30_000_000 * scale / 70))
    window = {'memory': {'dest': {'dirty_writeback_bytes_before': dirty_before,
                                  'dirty_writeback_bytes_after': dirty_after}},
              'disk': {'dest': {'md127': {'write_bytes': written, 'read_bytes': 0}}}, 'failures': []}
    reported, verification = {}, {'verified': True, 'basis_changes': [],
                                  'output_start': {'ok': True, 'ready_seconds': round(rng.uniform(0.3, 0.5), 3)}}
    remote = 0.0
    if method in measure.FULL_VARIANTS:
        phases = {measure.FULL_PHASE: {'name': measure.FULL_PHASE, 'wall_seconds': round(total, 3), **window}}
        reported['basebackup_phases'] = measure.basebackup_phases(basebackup_lines(method, parts), total)
        if method in measure.NON_DURABLE_METHODS:  # its first start syncs what --no-sync left
            verification['output_start']['ready_seconds'] += 1.2
    elif method in measure.PROBACKUP_METHODS:
        steps = {'catchup': {'wall_seconds': round(total - parts['sync'], 3)},
                 'sync': {'wall_seconds': round(parts['sync'], 3)}}
        phases = {measure.PROBACKUP_PHASE: {'name': measure.PROBACKUP_PHASE, 'wall_seconds': round(total, 3),
                                            'steps': steps, **window}}
        reported['probackup_phases'] = measure.probackup_phases(probackup_lines(parts), total - parts['sync'])
        verification.update(logical=None, logical_match=True, void=False, bytes_identical=True)
    else:
        start = parts['receiver_start']
        steps = {measure.DIRECT_RECEIVER_STEP: {'wall_seconds': round(start, 3)},
                 'transfer': {'wall_seconds': round(total - start, 3)}}
        phases = {measure.DIRECT_PHASE: {'name': measure.DIRECT_PHASE, 'wall_seconds': round(total, 3),
                                         'steps': steps, **window}}
        remote = REMOTE_LAUNCH
        if method in measure.PARCOPY_METHODS:
            count = measure.PARCOPY_VARIANTS[method]['streams']
            share = wire / count
            reported['parcopy_summary'] = measure.parcopy_summary({
                'streams': [{'bytes': int(share * (1.04 if i == 0 else 1 - 0.04 / (count - 1))),
                             'first_byte_s': 0.01, 'last_byte_s': round(parts['copy'], 3),
                             'write_s': round(parts['copy'] * 0.4, 3)} for i in range(count)],
                'fsync_s': round(parts['fsync'], 3), 'fsync_calls': 1013, 'fsync_expected': 1013})
        else:
            reported['timings_seconds'] = {'rebuild': 0.14, 'fsync': 0.07, 'verify_wait': 0.62, 'hash_basis': 1.6}
    cpu = {'full_nosync': (1.7, 1.6), V1: (5.6, 10.0), V1_J8: (6.4, 12.5)}.get(
        method, (1.8, 1.9) if method in measure.FULL_VARIANTS else (5.0, 5.0)
        if method in measure.PROBACKUP_METHODS else (1.5, 3.5))
    record = {
        'schema': 'exp04-run/v1', 'run_name': run_name, 'trial_id': trial_id, 'pattern': pattern, 'fraction': 0.01,
        'scale': scale, 'rows_changed': 70_000, 'repeat': repeat, 'speed': measure.parse_speed(speed),
        'speed_index': 0, 'method': method, 'order_index': order_index,
        'link': {'source_to_dest_gbit': round(RATES[speed] * (1 + rng.gauss(0, 0.002)), 4),
                 'dest_to_source_gbit': RATES[speed], 'source_to_dest_8streams_gbit': RATES_8[speed]},
        'block': block, 'source_cache': 'warm' if block == 'warm_source' else 'cold', 'status': 'pass', 'error': None,
        'phases': phases, 'tool_reported': reported, 'verification': verification, 'output_bytes': output,
        'totals': {'wall_seconds': round(total, 3), 'wall_seconds_less_remote_overhead': round(total - remote, 3),
                   'wire_bytes_source_to_dest': int(wire * 1.002), 'wire_bytes_dest_to_source': 4_200_000,
                   'cpu_seconds_source': round(cpu[0] * (1 + rng.gauss(0, 0.03)), 2),
                   'cpu_seconds_dest': round(cpu[1] * (1 + rng.gauss(0, 0.03)), 2)},
        'sysctl': {'before': {measure.DIRTY_BACKGROUND_RATIO: 10, measure.DIRTY_BACKGROUND_BYTES: 0}}}
    if method in measure.HOST_TUNED_METHODS:
        record['sysctl']['during'] = {measure.DIRTY_BACKGROUND_RATIO: 0, measure.DIRTY_BACKGROUND_BYTES: 64 << 20}
    return record


def trial_record(trial_id, pattern, scale, rng):
    """A trial's record as run_trial leaves it: its probes and reference digests."""
    def write_floor():
        seconds = round(rng.uniform(1.35, 1.5), 3)
        return {'bytes': int(COPY_BYTES * scale / 70), 'writers': 4, 'seconds': seconds,
                'mb_per_second': round(COPY_BYTES * scale / 70 / seconds / 1e6, 1)}
    pairs = [{'start_s': 0.04, 'stop_s': 0.03, 'pair_s': 0.07, 'wall_s': 0.12} for _ in range(10)]
    pairs[0].update(start_s=0.2, pair_s=0.23)
    summary = {'files': 1010, 'bytes': COPY_BYTES, 'entries': 1010, 'listing_sha256': 'a' * 64}
    return {'schema': 'exp04-trial/v1', 'trial_id': trial_id, 'pattern': pattern, 'fraction': 0.01, 'scale': scale,
            'status': 'pass', 'links': [], 'runs': [],
            'probe_relation': {'relation': 'pgbench_accounts', 'path': 'base/5/16397'},
            'probes': {'durable_write_floor': [write_floor()],
                       'read_floor': {'bytes': COPY_BYTES, 'files': 1000, 'readers': 4, 'seconds': 0.74,
                                      'mb_per_second': 2850.0, 'largest_file': {'path': 'base/5/16397', 'bytes': 1 << 30}},
                       'single_read': {'bytes': 940_000_000, 'files': 1, 'readers': 1, 'seconds': 0.62,
                                       'mb_per_second': 1516.1, 'largest_file': {'path': 'base/5/16397',
                                                                                 'bytes': 940_000_000}},
                       'bracket': {'pairs': pairs, 'first_pair_s': 0.23, 'median_pair_s': 0.07,
                                   'median_after_first_s': 0.07}},
            'reference_digests': {'start': {'summary': summary, 'excluded': ['global/pg_control'], 'missing': [],
                                            'wall_seconds': 9.0}},
            'write_floor': write_floor}


def write_sweep(root, name, patterns, *, methods=LOCAL_METHODS, speeds=SPEEDS, repeats=5, extension_repeats=5,
                warm_repeats=3, extension=EXTENSION, warm=WARM, scale=70, seed=11, first_trial=0, adjust=None):
    """One results directory as run_exp04.py writes it; adjust(record) may change a record before its
    durability checks are taken (a void run, a failure). Returns its path."""
    rng = random.Random(seed)
    out = Path(root) / name
    records, links = [], []
    for index, pattern in enumerate(patterns, start=first_trial):
        trial_id = f'{index:02d}-{pattern}-f0.01-s{scale}'
        trial = trial_record(trial_id, pattern, scale, rng)
        write_floor = trial.pop('write_floor')

        def cell(block, repeat, speed, arms):
            order = [arm for arm in arms if speed in METHOD_SPEEDS.get(arm, speeds)]
            rng.shuffle(order)
            links.append({'trial_id': trial_id, 'repeat': repeat, 'speed': speed, 'block': block,
                          'source_to_dest_gbit': RATES[speed], 'source_to_dest_8streams_gbit': RATES_8[speed]})
            for order_index, method in enumerate(order):
                record = record_for(method, pattern, speed, trial_id=trial_id, run_name=name, repeat=repeat,
                                    block=block, order_index=order_index, scale=scale, rng=rng)
                if adjust:
                    adjust(record)
                record['durability'] = measure.durability_check(record, record['output_bytes'], IDLE_NOISE)
                records.append(record)
                trial['runs'].append({'repeat': repeat, 'speed': speed, 'method': method,
                                      'status': record['status'], 'block': block})
        for repeat in range(repeats):
            order = list(speeds)
            rng.shuffle(order)
            for speed in order:
                cell('main', repeat, speed, methods)
        repeat = repeats
        for block, count, arms in (('extension', extension_repeats, extension), ('warm_source', warm_repeats, warm)):
            for _ in range(count):
                cell(block, repeat, U, [arm for arm in arms if arm in methods])
                repeat += 1
        trial['probes']['durable_write_floor'].append(write_floor())
        trial['reference_digests']['end'] = dict(trial['reference_digests']['start'])
        trial['reference_match'], trial['reference_differences'] = True, []
        (out / 'trials').mkdir(parents=True, exist_ok=True)
        (out / 'trials' / f'{trial_id}.json').write_text(json.dumps(trial, indent=1))
    for record in records:
        path = out / 'runs' / record['trial_id'] / f'r{record["repeat"]}-{record["speed"]["label"]}-{record["method"]}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    measure.write_csv(out / 'summary.csv', records)
    run = {'name': name, 't2': True, 'methods': list(methods), 'idle_noise_bytes': IDLE_NOISE,
           'dirty_idle_noise': {'windows': 20, 'window_seconds': 5.0, 'max_increase_bytes': 1_200_000},
           't2_preflight': {'writeback_defaults': {measure.DIRTY_BACKGROUND_RATIO: 10,
                                                   measure.DIRTY_BACKGROUND_BYTES: 0}},
           'blocks': {'main': {'repeats': repeats}, 'extension': {'repeats': extension_repeats},
                      'warm_source': {'repeats': warm_repeats}}, 'links': links, 'status': 'pass'}
    (out / 'run.json').write_text(json.dumps(run, indent=1))
    return out


def write_session(root, smoke=False, seed=11, adjust=None):
    """EXP-11's two sweeps (exp11-local: hot and append with v1 at 8 jobs, the extension and the
    warm-source block; exp11-uniform: uniform's main block), or with smoke=True the smoke run's shape:
    scale 5, hot and append, 2 and 10 Gbit/s and unthrottled, one repeat of each block."""
    if smoke:
        return [write_sweep(root, 'exp11-smoke', ('hot', 'append'), speeds=('2g', '10g', U), repeats=1,
                            extension_repeats=1, warm_repeats=1, scale=5, seed=seed, adjust=adjust)]
    local = write_sweep(root, 'exp11-local', ('hot', 'append'), seed=seed, adjust=adjust)
    uniform = write_sweep(root, 'exp11-uniform', ('uniform',), methods=LOCAL_METHODS[:-1], extension_repeats=0,
                          warm_repeats=0, seed=seed + 1, adjust=adjust)
    return [local, uniform]
