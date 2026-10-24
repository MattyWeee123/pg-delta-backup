"""A synthetic EXP-12 session, for dry runs of analysis/exp12.py: what `remote.py fetch` brings back from
`run_exp04 --load bank`.

write_session() writes one sweep of EXP-12's run line (the busy block at 0.5 and 2 Gbit/s and unthrottled, the
controls, the warm block, the moderate block and the calibration block), or the smoke run's shape, as run_exp04.py
lays it out: runs/<trial>/r<repeat>-<speed>-<method>.json, summary.csv (measure.write_csv over those records, so every
column is the core harness's), trials/<trial>.json, run.json, and the load's logs under logs/<trial>/load/: each
logged pgbench run's per-thread logs as `pgbench -l` writes them under -R, and the sampler's ticks and SQL lines.

The load is a model. Each logged run schedules transactions on a Poisson clock at its rate, served one at a time in
the order scheduled by a server of capacity CAPACITY per second; an arm's EFFECTS lower that capacity for a while
inside its window (0: the server stops, as pgbench does under SIGSTOP). Every transaction's completion and latency
follow, with LATENCY_FLOOR on top. The sampler's pgbench threads wait more in the run queue during the arms in
WAIT_EFFECTS. Nothing here is a measurement.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from experiments.network import measure

U = measure.UNTHROTTLED
V1 = 'pgdelta_direct_inplace'
TRIAL = '00-hot-f0.01-s70'
EPOCH = 1_792_000_000.0  # dest's clock at the trial's start
OFFSET = 0.002  # the source's clock minus dest's
RATES = {'busy': 200, 'moderate': 100}  # -R per block (the warm block runs at the busy rate)
CAPACITY = 2000  # the server's transactions per second; C, the trial's capacity
CLIENTS, THREADS = 8, 2
LATENCY_FLOOR = 0.0008  # s, on top of the queue
ABORT_SHARE = 0.01
LINKS = {'0.5g': 0.49, '2g': 1.917, U: 14.8}
SPEED = {'0.5g': {'label': '0.5g', 'gbit': 0.5, 'tc_rate': '500mbit'},
         '2g': {'label': '2g', 'gbit': 2.0, 'tc_rate': '2000mbit'},
         U: {'label': U, 'gbit': None, 'tc_rate': 'off'}}
# The registered plan: each block's speeds and each speed's arms; the link-free arms in the 2 Gbit/s visit.
BUSY = {'0.5g': ('full', 'pgdelta', 'v1proxy', 'v1proxy_s1'),
        '2g': ('none', 'capture', 'bracket', 'full', 'pgdelta', 'v1proxy', 'v1proxy_s1'),
        U: ('full', 'pgdelta', 'v1proxy')}
WARM = {'2g': ('full', 'v1proxy')}
MODERATE = {'2g': ('none', 'full', 'v1proxy', 'v1proxy_s1'), U: ('full', 'v1proxy')}
CALIBRATION = ('full', 'capture', 'pgdelta', 'v1proxy', 'v1proxy_s1', V1, 'burn4')
# Each arm's timed phases (seconds), by speed where the link matters.
DURATIONS = {'none': 60.0, 'capture': 4.8, 'bracket': 0.3, 'v1proxy': 3.8, 'v1proxy_s1': 5.5, V1: 3.4, 'pause2': 2.1,
             'burn4': 2.05, ('full', '0.5g'): 40.0, ('full', '2g'): 10.8, ('full', U): 4.3}
PGDELTA_PHASES = (5.0, 4.5)  # receiver_capture, then transfer
# What each arm does to the server, from t0 (source clock): (from, seconds, capacity factor). The capture flushes
# 2.1 GB at its end; v1proxy hashes on 4 workers for a second; burn4 halves it; pause2 stops pgbench for 2.0 s.
EFFECTS = {'capture': [(3.4, 1.3, 0.02)], 'pgdelta': [(3.6, 1.3, 0.02)], 'v1proxy': [(1.0, 1.0, 0.06)],
           'burn4': [(0.0, 2.0, 0.095)], 'pause2': [(0.05, 2.0, 0.0)]}
WAIT_EFFECTS = {'v1proxy': 0.2, 'burn4': 0.3}  # extra run-queue wait per pgbench thread, s per s, in those windows
# Calibrated CPU-s without load, and wire bytes source to dest, per arm.
CPU = {'full': 2.6, 'capture': 4.5, 'pgdelta': 9.8, 'v1proxy': 6.5, 'v1proxy_s1': 6.2, V1: 6.4, 'burn4': 8.1,
       'none': 0.0, 'bracket': 0.05, 'pause2': 0.0}
WIRE = {'full': 2_200_000_000, 'pgdelta': 95_000_000, 'v1proxy': 28_400_000, 'v1proxy_s1': 28_400_000,
        V1: 28_400_000}
IDLE_RATE = 0.15  # the source's idle busy CPU-s per second
BUSY_RATE = {'busy': 4.8, 'moderate': 2.4}  # the source's busy CPU-s per second in a settle at each rate
SETTLE_S, TAIL_S, STEP1_S = 15.0, 15.0, 2.0
SAMPLER_TICK = 0.5
LOGICAL = {'pgbench_accounts': {'rows': 7_000_000, 'md5': 'a'}, 'events': {'rows': 1_000_000, 'md5': 'b'}}


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def cpu_dict(busy, steal=0.0):
    """A snapshot_delta cpu dict whose busy seconds without steal are `busy`."""
    out = {f'{field}_seconds': 0.0 for field in measure.CPU_FIELDS}
    out['user_seconds'] = round(busy, 3)
    out['steal_seconds'] = round(steal, 3)
    out['busy_seconds'] = round(busy + steal, 3)
    return out


def phase(name, started, wall, *, cpu_source, wire=0, dirty_increase=1 << 20, md_writes=0):
    """One timed phase as Orchestrator.timed_phase records it (dest's clock)."""
    return {'name': name, 'wall_seconds': round(wall, 3), 'started_utc': started, 'ended_utc': started + wall,
            'commands': [], 'failures': [],
            'net': {'source': {'tx_bytes': wire, 'rx_bytes': wire // 100, 'tx_packets': 1, 'rx_packets': 1},
                    'dest': {'tx_bytes': wire // 100, 'rx_bytes': wire, 'tx_packets': 1, 'rx_packets': 1}},
            'cpu': {'source': cpu_dict(cpu_source), 'dest': cpu_dict(1.0)},
            'counter_window_seconds': {'source': wall, 'dest': wall},
            'memory': {'source': None, 'dest': {'dirty_writeback_bytes_before': 4 << 20,
                                                'dirty_writeback_bytes_after': (4 << 20) + dirty_increase,
                                                'dirty_writeback_increase_bytes': dirty_increase}},
            'disk': {'source': None, 'dest': {'md127': {'read_bytes': 0, 'write_bytes': md_writes, 'reads': 0,
                                                        'writes': 1}}}}


class Timeline:
    """dest's clock, moving on as the trial runs; every window and effect on the source's clock."""

    def __init__(self):
        self.now = EPOCH + 600.0  # after set-up, B0, the references, the snapshot and the probes
        self.effects: list = []  # (source start, source end, factor)
        self.waits: list = []  # (source start, source end, extra wait per thread, s/s)
        self.requested: list = []  # source times of the checkpoints the arms ask for

    def advance(self, seconds):
        start = self.now
        self.now += seconds
        return start


class Session:
    """The trial's runs in their order, each record as run_exp04.run_load_method writes it, and the load's runs."""

    def __init__(self, smoke=False, seed=7, unsettled=()):
        self.smoke = smoke
        self.rng = random.Random(seed)
        self.clock = Timeline()
        self.records: list = []
        self.repeat = 0
        self.load_runs: list = []
        self.warmups: list = []
        self.controls: list = []
        self.speed_orders: list = []
        self.unsettled = set(unsettled)  # (block, block_repeat, method) whose settle doesn't pass its gate

    def next_repeat(self):
        repeat, self.repeat = self.repeat, self.repeat + 1
        return repeat

    def load_start(self, run_id, rate, kind):
        pid = 4000 + len(self.load_runs) + 1
        run = {'run_id': run_id, 'kind': kind, 'rate': rate, 'clients': CLIENTS, 'threads': THREADS,
               'seed': 121 + len(self.load_runs), 'argv': ['pgbench', '-R', str(rate)], 'pid': pid,
               'started_utc': self.clock.now + OFFSET, 'stop': None,
               'log_files': [f'{run_id}.{pid}'] + [f'{run_id}.{pid}.{t}' for t in range(1, THREADS)]}
        self.load_runs.append(run)
        self.current = run
        return run

    def load_stop(self):
        self.current['stopped_utc'] = self.clock.now + OFFSET
        self.current = None

    def warmup(self, seconds=120.0):
        started = self.clock.advance(seconds)
        self.warmups.append({'run_id': self.current['run_id'], 'started_utc': started, 'ended_utc': self.clock.now,
                             'seconds': seconds, 'pool_utc': [self.clock.now - 60, self.clock.now]})

    def duration(self, method, speed):
        return DURATIONS.get((method, speed), DURATIONS.get(method))

    def run(self, block, repeat, block_repeat, speed, method, *, slot=None, order_index=0, order=()):
        """One run's timeline and record: step 1, the settle, the timed phases, the quiet tail (no tail and no gate
        without the load)."""
        clock = self.clock
        loaded = block != 'calibration'
        rate = RATES['moderate' if block == 'moderate' else 'busy'] if loaded else None
        record = {'schema': 'exp04-method-run/1', 'run_name': 'exp12-hot', 'trial_id': TRIAL, 'pattern': 'hot',
                  'fraction': 0.01, 'scale': 70, 'rows_changed': 70_000, 'repeat': repeat, 'speed': SPEED[speed],
                  'speed_index': 0, 'speed_order': None, 'method': method, 'order_index': order_index,
                  'method_order': list(order), 'link': {'source_to_dest_gbit': LINKS[speed]},
                  'paths': {}, 'block': block, 'block_repeat': block_repeat, 'control_slot': slot,
                  'source_cache': 'warm' if block == 'warm' else 'cold', 'load_rate': rate,
                  'load_run': self.current['run_id'] if loaded else None, 'clock_offset_s': OFFSET,
                  'idle_rate_no_steal': IDLE_RATE, 'started_utc': iso(clock.now), 'status': 'running', 'error': None,
                  'failure_kind': None, 'void': False}
        clock.advance(STEP1_S)
        warm = block == 'warm'
        fincore = {'benchmark': {'files': 40, 'bytes': 2_100_000_000, 'resident_bytes': 0 if not warm else 2_100_000_000,
                                 'resident_share': 1.0 if warm else 0.0005},
                   'snapshot': {'files': 900, 'bytes': 2_200_000_000, 'resident_bytes': 0,
                                'resident_share': 1.0 if warm else 0.0004},
                   'loaddb': {'files': 12, 'bytes': 31_000_000, 'resident_bytes': 31_000_000, 'resident_share': 1.0}}
        record['source_untimed'] = {'pending': [], 'load': {'running': True} if loaded else None,
                                    'cache': {'policy': 'warm' if warm else 'evict', 'fincore': fincore,
                                              'cache_flag': measure.cache_flag(fincore, warm),
                                              ('read' if warm else 'evicted'): {}}, 'seconds': STEP1_S}
        record['caches_dropped'] = 'dest'
        settle_start = clock.advance(SETTLE_S)
        unsettled = (block, block_repeat, method) in self.unsettled
        busy_rate = BUSY_RATE['moderate' if block == 'moderate' else 'busy'] if loaded else IDLE_RATE
        record['settle'] = {'started_utc': settle_start, 'ended_utc': clock.now, 'seconds': SETTLE_S,
                            'dest': {'steps': [], 'seconds': 1.0}, 'dest_wait_s': 1.0,
                            'gate': ({'passed': not unsettled, 'unsettled': unsettled, 'waited_s': SETTLE_S,
                                      'lines': 10, 'mean_lag_ms': 0.3, 'recent': []} if loaded else None),
                            'unsettled': unsettled, 'cpu_source': cpu_dict(busy_rate * SETTLE_S),
                            'cpu_window_seconds': SETTLE_S, 'busy_rate_no_steal': busy_rate}
        phases = {}
        t0 = clock.now
        if method == 'pgdelta':
            first, second = PGDELTA_PHASES
            phases[measure.RECEIVER_PHASE] = phase(measure.RECEIVER_PHASE, clock.advance(first), first,
                                                   cpu_source=busy_rate * first + CPU[method] * 0.45, wire=WIRE[method] // 10)
            phases[measure.RECEIVER_PHASE]['parts'] = {'capture': {'started_utc': t0 + 0.05, 'wall_seconds': first - 0.1}}
            clock.advance(0.12)
            phases['transfer'] = phase('transfer', clock.advance(second), second,
                                       cpu_source=busy_rate * second + CPU[method] * 0.55, wire=WIRE[method] * 9 // 10,
                                       md_writes=2_300_000_000)
        else:
            wall = self.duration(method, speed)
            name = {'none': measure.NONE_PHASE, 'capture': measure.CAPTURE_PHASE, 'bracket': measure.BRACKET_PHASE,
                    'full': measure.FULL_PHASE, 'v1proxy': measure.V1PROXY_PHASE, 'v1proxy_s1': measure.V1PROXY_PHASE,
                    V1: measure.DIRECT_PHASE}.get(method, measure.CONTROL_PHASE)
            writes = 2_250_000_000 if method == 'full' else 30_000_000 if method in (*measure.V1PROXY_METHODS, V1) else 0
            phases[name] = phase(name, clock.advance(wall), wall, cpu_source=busy_rate * wall + CPU.get(method, 0.0),
                                 wire=WIRE.get(method, 1000), md_writes=writes)
        t1 = clock.now
        record['phases'] = phases
        record['window'] = {'t0_utc': t0, 't1_utc': t1, 'clock_offset_s': OFFSET, 't0_source_utc': t0 + OFFSET,
                            't1_source_utc': t1 + OFFSET, 'tail_s': 10}
        record['totals'] = measure.phase_totals(phases)
        source_t0 = t0 + OFFSET
        if loaded:
            for start, seconds, factor in EFFECTS.get(method, ()):
                clock.effects.append((source_t0 + start, source_t0 + start + seconds, factor))
            if method in WAIT_EFFECTS:
                clock.waits.append((source_t0, t1 + OFFSET, WAIT_EFFECTS[method]))
        if method in ('full', 'capture', 'pgdelta', 'bracket', *measure.V1PROXY_METHODS):
            clock.requested.append(source_t0 + 0.05)
        if method == 'pause2':
            stop = source_t0 + EFFECTS['pause2'][0][0]
            phases[measure.CONTROL_PHASE]['control'] = {
                'run_id': self.current['run_id'], 'pid': self.current['pid'], 'seconds': 2.0,
                'sigstop_utc': [stop - 0.0001, stop], 'sigcont_utc': [stop + 2.0, stop + 2.0001], 'paused_s': 2.0}
        if method == 'burn4':
            phases[measure.CONTROL_PHASE]['control'] = {'procs': 4, 'seconds': 2.0, 'started_utc': source_t0,
                                                        'ended_utc': source_t0 + 2.0, 'cpu_seconds': 8.0}
        self.verify(record, method, t0, t1)
        record['durability'] = measure.durability_check(record, record.get('output_bytes'), 4 << 20)
        record['status'] = 'pass'
        if loaded:
            tail_start = clock.now
            clock.advance(TAIL_S)
            record['tail'] = {'started_utc': tail_start, 'ended_utc': clock.now, 'seconds': TAIL_S,
                              'verification_s': 6.0, 'overran_s': 0.0}
        record['finished_utc'] = iso(clock.now)
        self.records.append(record)
        return record

    def verify(self, record, method, t0, t1):
        """What run_exp04's verification leaves in a record: the oracle and G4 for the live outputs, the proxies'
        backup and hashing, the others' plain verdicts."""
        if method in measure.G4_METHODS:
            record['output_bytes'] = 2_250_000_000
            record['verification'] = {
                'verified': True, 'basis_changes': [], 'output_bytes': 2_250_000_000,
                'output_start': {'ok': True, 'ready_seconds': 1.4},
                'oracle': {**dict.fromkeys(('bytes', *measure.ORACLE_CHECKS), 'ok'), 'progress': {'0': 1},
                           'static_digest': record['block_repeat'] == 0, 'amcheck_relations': 9}}
            if method == 'full':
                end = t1 - 0.4
                cut = end + OFFSET - 0.1
                window = [t0, t1]
            else:
                end = t0 + PGDELTA_PHASES[0] - 0.3
                cut = end + OFFSET - 0.1
                window = [t0 + 0.05, t0 + PGDELTA_PHASES[0] - 0.05]
            record['g4'] = {'capture_end': {'lsn': '2/9A000158', 'seconds': end - t0, 'utc': end},
                            'publication_utc': t1, 'usable_utc': t1 + 1.4, 'coverage_window_utc': window,
                            'cut_utc': [cut - 0.0005, cut + 0.0005], 'staleness_s': round(t1 + OFFSET - cut, 6)}
            record['verification']['oracle']['coverage_window_utc'] = window
        else:
            record['verification'] = {'verified': True, 'basis_changes': []}
            if method in (*measure.V1PROXY_METHODS, V1):
                record['output_bytes'] = 2_250_000_000
                record['verification']['output_start'] = {'ok': True, 'ready_seconds': 1.1}
            if method in ('full', 'pgdelta'):
                record['output_bytes'] = 2_250_000_000
        if method in measure.V1PROXY_METHODS:
            jobs = 1 if method == 'v1proxy_s1' else 4
            record['tool_reported'] = {'switches': {'sender': {'jobs': jobs, 'hash_backend': 'process' if jobs > 1
                                                               else 'serial'}, 'receiver': {'jobs': 4}},
                                       'files': {'total': 900}, 'pages': {'total': 270_000}}
            record['proxy'] = {'backup': {'start_lsn': '2/A0000028', 'start_utc': t0 + 0.1, 'stop_lsn': '2/A0000158',
                                          'stop_utc': t1, 'one_session': True, 'started_before_sender': True,
                                          'stopped_after_transfer': True, 'held': True},
                               'hashed': {'files_selected': 900, 'files_hashed': 900, 'pages_selected': 270_000,
                                          'pages_hashed': 270_000, 'all_hashed': True}}
        if method in ('full', 'capture'):
            record.setdefault('tool_reported', {})['basebackup_phases'] = {'checkpoint_s': 0.05, 'stream_s': 1.0}

    # The trial.

    def plan(self):
        repeats = {'busy': 1, 'warm': 1, 'moderate': 1, 'calibration': 1} if self.smoke else \
            {'busy': 5, 'warm': 5, 'moderate': 3, 'calibration': 2}
        self.load_start('busy', RATES['busy'], 'busy')
        self.warmup()
        self.control_slot('busy_start', ('pause2', 'burn4'))
        self.block('busy', repeats['busy'], BUSY)
        self.control_slot('busy_end', ('pause2', 'burn4'))
        self.block('warm', repeats['warm'], WARM)
        self.load_stop()
        self.load_start('moderate', RATES['moderate'], 'moderate')
        self.warmup()
        self.control_slot('moderate_start', ('pause2',))
        self.block('moderate', repeats['moderate'], MODERATE)
        self.load_stop()
        self.clock.advance(20 + 30)  # load-check, then idle before the calibration block
        repeat_base = self.repeat
        for block_repeat in range(repeats['calibration']):
            repeat = self.next_repeat()
            order = list(CALIBRATION)
            self.rng.shuffle(order)
            for index, method in enumerate(order):
                self.run('calibration', repeat, block_repeat, '2g', method, order_index=index, order=order)
        return repeat_base

    def control_slot(self, slot, methods):
        repeat = self.next_repeat()
        order = list(methods)
        self.rng.shuffle(order)
        block = 'moderate' if slot == 'moderate_start' else 'busy'
        self.controls.append({'slot': slot, 'block': block, 'repeat': repeat, 'order': order})
        for index, method in enumerate(order):
            self.run(block, repeat, None, U, method, slot=slot, order_index=index, order=order)

    def block(self, name, repeats, plan):
        for block_repeat in range(repeats):
            repeat = self.next_repeat()
            speeds = list(plan)
            self.rng.shuffle(speeds)
            self.speed_orders.append({'block': name, 'repeat': repeat, 'block_repeat': block_repeat, 'speeds': speeds})
            for speed in speeds:
                order = list(plan[speed])
                self.rng.shuffle(order)
                for index, method in enumerate(order):
                    self.run(name, repeat, block_repeat, speed, method, order_index=index, order=order)


def simulate(run, effects, seed, capacity=CAPACITY):
    """One logged run's transactions: Poisson arrivals at its rate on the source's clock from its start to its stop,
    served in order by a server whose capacity `effects` lowers for a while (work time W(t), its inverse for the
    completions). Returns arrays: scheduled, started, ended (s), client, script."""
    rng = np.random.default_rng(seed)
    start, stop, rate = run['started_utc'], run['stopped_utc'], run['rate']
    count = int((stop - start) * rate * 1.1) + 100
    arrivals = start + np.cumsum(rng.exponential(1.0 / rate, count))
    arrivals = arrivals[arrivals < stop]
    # Work time: CAPACITY per second, the effects' factors inside them.
    edges = sorted({start, stop, *[t for a, b, _ in effects for t in (a, b) if start < t < stop]})
    work = [0.0]
    for a, b in zip(edges, edges[1:]):
        middle = (a + b) / 2
        factor = min([f for lo, hi, f in effects if lo <= middle < hi], default=1.0)
        work.append(work[-1] + capacity * factor * (b - a))
    edges, work = np.array(edges), np.array(work)
    # The last edge extended, so every completion maps back to a time.
    edges = np.append(edges, stop + 3600)
    work = np.append(work, work[-1] + capacity * 3600)
    arrived = np.interp(arrivals, edges, work)
    index = np.arange(len(arrivals))
    done = index + 1 + np.maximum.accumulate(arrived - index)
    began = np.maximum(arrived, np.concatenate(([-np.inf], done[:-1])))

    def back(values):  # the earliest time at which work reaches each value
        return np.interp(values, work, edges)
    ended = back(done) + LATENCY_FLOOR
    started = np.maximum(arrivals, back(began))
    client = rng.integers(0, CLIENTS, len(arrivals))
    script = (rng.random(len(arrivals)) < ABORT_SHARE).astype(int)
    return arrivals, started, ended, client, script


def write_logs(directory, run, effects, seed, capacity=CAPACITY):
    """The run's per-thread logs (client % THREADS), each in completion order, as pgbench -l writes them under -R.
    Returns each client's commits, the transactions, and their completion times (us, sorted)."""
    scheduled, started, ended, client, script = simulate(run, effects, seed, capacity)
    us = lambda values: np.round(values * 1e6).astype(np.int64)  # noqa: E731
    sched_us, start_us, end_us = us(scheduled), us(started), us(ended)
    latency, lag = end_us - sched_us, start_us - sched_us
    order = np.argsort(end_us, kind='stable')
    numbers = {}
    files = {thread: [] for thread in range(THREADS)}
    commits = {}
    for i in order:
        c = int(client[i])
        numbers[c] = numbers.get(c, 0) + 1
        if script[i] == measure.COMMIT_SCRIPT:
            commits[c] = commits.get(c, 0) + 1
        files[c % THREADS].append(f'{c} {numbers[c]} {latency[i]} {script[i]} {end_us[i] // 1_000_000} '
                                  f'{end_us[i] % 1_000_000} {lag[i]}\n')
    directory.mkdir(parents=True, exist_ok=True)
    for thread, lines in files.items():
        (directory / run['log_files'][thread]).write_text(''.join(lines))
    return commits, len(order), end_us[order]


def stamp_pauses(session, run, ends):
    """pause2's SIGSTOP stamps where the model's pgbench stopped: its last completion before the pause (at the
    model's rates the stop falls between two completions a few ms apart; at the sweep's, well under one)."""
    for record in session.records:
        if record['method'] != 'pause2' or record['load_run'] != run['run_id']:
            continue
        control = record['phases'][measure.CONTROL_PHASE]['control']
        stop = control['sigstop_utc'][1]
        before = ends[ends < int(round(stop * 1e6))]
        if len(before):
            last = before[-1] / 1e6
            control['sigstop_utc'] = [last, last + 0.0001]


def write_sampler(directory, session, start, stop):
    """The sampler's ticks (every SAMPLER_TICK s) and SQL lines (every 1 s) on the source's clock: the VM's CPU, the
    pgbench threads' run and run-queue wait (more of it in WAIT_EFFECTS' windows), the arms' checkpoints."""
    directory.mkdir(parents=True, exist_ok=True)
    pids = [(run['started_utc'], run['stopped_utc'], run['pid']) for run in session.load_runs]
    ticks, cumulative, threads = [], [0] * 8, {}
    t = start
    while t <= stop:
        pid = next((p for a, b, p in pids if a <= t <= b), None)
        cumulative[0] += int(480 * SAMPLER_TICK)
        cumulative[3] += int(320 * SAMPLER_TICK)
        extra = sum(w for a, b, w in session.clock.waits if a <= t < b)
        line = {'t': round(t, 6), 'cpu': list(cumulative), 'run': 3, 'g': {'pgbench': 6, 'backend': 20}, 'pgbench': pid,
                'th': {}, 'd': {}}
        if pid is not None:
            for tid in (str(pid), str(pid + 1)):
                run_ns, wait_ns = threads.get(tid, (0, 0))
                run_ns += int(0.3e9 * SAMPLER_TICK)
                wait_ns += int((0.002 + extra) * 1e9 * SAMPLER_TICK)
                threads[tid] = (run_ns, wait_ns)
                line['th'][tid] = [run_ns, wait_ns]
        ticks.append(json.dumps(line, separators=(',', ':')))
        t += SAMPLER_TICK
    sql, t = [], start
    while t <= stop:
        requested = sum(1 for moment in session.clock.requested if moment <= t)
        sql.append(json.dumps({'t': round(t, 6), 'wal': {'wal_sync': int(t - start), 'wal_sync_time': 0.2 * (t - start)},
                               'checkpointer': {'num_timed': 0, 'num_requested': requested}, 'autovacuum': []}))
        t += 1.0
    (directory / 'sampler.jsonl').write_text('\n'.join(ticks) + '\n')
    (directory / 'sampler-sql.jsonl').write_text('\n'.join(sql) + '\n')
    final = {'ticks': len(ticks), 'late_ticks': 0, 'interval_s': SAMPLER_TICK, 'seconds': round(stop - start, 3),
             'cpu_seconds': round(0.004 * (stop - start), 3), 'ended_utc': stop}
    (directory / 'sampler-final.json').write_text(json.dumps(final))
    return final


def write_session(root, *, smoke=False, adjust=None, unsettled=(), name=None, seed=7):
    """One sweep under root (results/exp04 laid out); `adjust(record)` may change each run record before it is written,
    and `unsettled` names (block, block_repeat, method) runs whose settle's gate didn't pass. Returns its directory."""
    name = name or ('exp12-smoke' if smoke else 'exp12-hot')
    sweep = Path(root) / name
    session = Session(smoke=smoke, seed=seed, unsettled=unsettled)
    capacity_start = session.clock.now + OFFSET - 40
    capacity = {'run_id': 'capacity-1', 'kind': 'capacity', 'rate': None, 'clients': CLIENTS, 'threads': THREADS,
                'seed': 121, 'argv': ['pgbench'], 'pid': 3999, 'started_utc': capacity_start,
                'stopped_utc': capacity_start + 10, 'log_files': ['capacity-1.3999', 'capacity-1.3999.1'],
                'tps': float(CAPACITY), 'complete': True, 'failed': 0}
    session.plan()
    logs = sweep / 'logs' / TRIAL / 'load'
    # The capacity run: closed loop, CAPACITY per second, no lag column.
    lines = {thread: [] for thread in range(THREADS)}
    counts = {}
    for index in range(int(10 * CAPACITY)):
        end = int(round((capacity_start + (index + 1) / CAPACITY) * 1e6))
        c = index % CLIENTS
        counts[c] = counts.get(c, 0) + 1
        lines[c % THREADS].append(f'{c} {counts[c]} 2000 0 {end // 1_000_000} {end % 1_000_000}\n')
    logs.mkdir(parents=True, exist_ok=True)
    for thread, text in lines.items():
        (logs / capacity['log_files'][thread]).write_text(''.join(text))
    n = {str(c): counts.get(c, 0) for c in range(CLIENTS)}
    capacity['start'] = {'utc': capacity_start, 'n': {str(c): 0 for c in range(CLIENTS)}, 'sum_n': 0, 'sum_abalance': 0}
    capacity['stop'] = {'utc': capacity_start + 10, 'n': dict(n), 'sum_n': sum(n.values()),
                        'sum_abalance': sum(n.values())}
    capacity['commits'], capacity['commits_total'] = dict(n), sum(n.values())
    runs = [capacity]
    for index, run in enumerate(session.load_runs):
        commits, processed, ends = write_logs(logs, run, session.clock.effects, seed + index)
        stamp_pauses(session, run, ends)
        start = dict(n)
        n = {key: value + commits.get(int(key), 0) for key, value in n.items()}
        run.update(start={'utc': run['started_utc'], 'n': start, 'sum_n': sum(start.values()),
                          'sum_abalance': sum(start.values())},
                   stop={'utc': run['stopped_utc'], 'n': dict(n), 'sum_n': sum(n.values()),
                         'sum_abalance': sum(n.values())},
                   commits={str(k): v for k, v in sorted(commits.items())}, commits_total=sum(commits.values()),
                   complete=True, mismatched={}, tps=float(run['rate']), processed=processed, failed=0, aborted=False,
                   how='SIGALRM', signalled_utc=run['stopped_utc'])
        runs.append(run)
    final = write_sampler(logs / 'sampler', session, session.load_runs[0]['started_utc'],
                          session.load_runs[-1]['stopped_utc'])
    records = session.records
    for record in records:
        if adjust:
            adjust(record)
        path = sweep / 'runs' / TRIAL / f'r{record["repeat"]}-{record["speed"]["label"]}-{record["method"]}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    measure.write_csv(sweep / 'summary.csv', records)
    trial = {'schema': 'exp04-trial/1', 'trial_id': TRIAL, 'pattern': 'hot', 'fraction': 0.01, 'scale': 70, 'seed': 1,
             'status': 'pass', 'runs': [{'repeat': r['repeat'], 'speed': r['speed']['label'], 'method': r['method'],
                                         'status': r['status'], 'block': r['block']} for r in records],
             'speed_orders': session.speed_orders, 'controls': session.controls,
             'load': {'runs': runs, 'warmups': session.warmups, 'restarts': [],
                      'capacity': {'capacity_tps': float(CAPACITY), 'all_complete': True, 'runs': ['capacity-1'],
                                   'rest_s': 30},
                      'sampler': {'pid': 1},
                      'end': {'stop': runs[-1], 'check': {'ok': True, 'sum_abalance': sum(n.values()),
                                                          'sum_n': sum(n.values()), 'expected_commits': sum(n.values()),
                                                          'incomplete': [], 'gaps': []},
                              'sampler': {'final': final}, 'compress': {}, 'fetched': {'dir': str(logs)}}},
             'references': {'start': {'logical': LOGICAL, 'schema': {'loaddb': 'a', 'postgres': 'b'}, 'zero': True,
                                      'loaddb_pages': {'pgbench_accounts': 3637}},
                            'end': {'logical_match': True, 'schema_match': True, 'loaddb_growth': 0.004}},
             'snapshot': {'end_match': True}, 'probes': {'idle': {'busy_rate_no_steal': IDLE_RATE},
                                                         'clock': {'offset_s': OFFSET, 'rtt_s': 0.0002}},
             'clock': {'start': {'offset_s': OFFSET, 'rtt_s': 0.0002}, 'end': {'offset_s': OFFSET, 'rtt_s': 0.0002}},
             'load_checks': {'outputs': 0}}
    if smoke:
        trial['oracle_controls'] = {'controls': [
            {'name': 'good', 'oracle': {}, 'verdict': {'control': 'good', 'status': 'pass', 'ok': True}},
            *[{'name': name, 'oracle': {}, 'verdict': {**measure.oracle_control_verdict(
                name, {check: 'wrong' if check == target else 'ok' for check in measure.ORACLE_CHECKS})}}
              for name, target in measure.ORACLE_CONTROLS.items()]]}
    (sweep / 'trials').mkdir(parents=True, exist_ok=True)
    (sweep / 'trials' / f'{TRIAL}.json').write_text(json.dumps(trial))
    chrony = 'System time     : 0.000012345 seconds fast of NTP time\n'
    run_json = {'name': name, 'idle_noise_bytes': 4 << 20, 'dirty_idle_noise': {'max_increase_bytes': 458752},
                't10a': {'load': 'bank', 'rates': {'busy': RATES['busy'], 'moderate': RATES['moderate']},
                         'window_tail_s': 10, 'tail_s': TAIL_S},
                't10a_preflight': {'versions': {host: {'postgres': 'postgres (PostgreSQL) 17.11'}
                                                for host in ('source', 'dest')}},
                'environment': {'source': {'cpu_count': 8, 'chrony': chrony}, 'dest': {'cpu_count': 8, 'chrony': chrony}}}
    (sweep / 'run.json').write_text(json.dumps(run_json))
    return sweep


def pause_log(directory, rate, capacity, pause, seconds=40.0, start=EPOCH, seed=3):
    """One logged run at `rate` against a server of `capacity`, with the server stopped for `pause` = (from, seconds)
    after `start` (source clock): the queue model's answer to E3. Returns its registry entry."""
    run = {'run_id': 'p', 'started_utc': start, 'stopped_utc': start + seconds, 'rate': rate, 'pid': 1,
           'log_files': ['p.1'] + [f'p.1.{t}' for t in range(1, THREADS)]}
    effects = [(start + pause[0], start + pause[0] + pause[1], 0.0)] if pause else []
    write_logs(Path(directory), run, effects, seed, capacity)
    return run
