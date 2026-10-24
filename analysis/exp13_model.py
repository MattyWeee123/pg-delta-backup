"""EXP-13's expected ranges, E3-E14, derived as code ("How E3-E14 were derived" in the pre-registration).

    .venv/bin/python analysis/exp13_model.py [--sweeps DIR ...] [--tcp-rmem-max BYTES] [--tcp-adv-win-scale N]
        [--tuned-tcp-max-bytes BYTES] [--rate RTT_MS:GBIT ...] [--json]

Its only inputs are W, what one default stream holds in flight (from `net.ipv4.tcp_rmem`'s maximum and
`tcp_adv_win_scale`), and one default stream's steady rate at 20, 60 and 120 ms. Everything else is a constant the
pre-registration states: the link's payload, a full copy's bytes, slow start's rule, each arm's fixed work and round
trips, OpenSSH's channel window, EXP-11's anchor. With no input it uses the ones written there (Debian's 6 MiB maximum
and scale 1, so W is 2.67-3.15 MB, and each rate is W / r); with --sweeps (the smoke run's results), dest's sysctls in
run.json and the median of the visits' one-stream steady rates at 2 Gbit/s on default hosts; an option overrides
either. It prints each range beside the written one, and whether it falls inside it: smoke item 7 records them, dated,
before the sweep. Every E-range is judged as written (analysis/exp13.py); this one is reported beside it.

A range is (low, high), each end one consistent scenario (Model): the low end takes the faster rate with the window it
implies, the fewer round trips and the shorter slow start, the high end the other ends. So it is wider than an outcome
is likely to be, as the written ones are. A ratio divides one range by another end to end.

The written ranges were rounded, some to the nearest digit rather than outward, so "inside" is read at the written
precision: a model's end is inside once rounded to the digits the written end shows. Where even that fails
(KNOWN_GAPS), the derivation as the text gives it yields a wider range than the one written; the gap is named, and the
written range is still the one judged.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

# The link and the copy (Inputs).
LINK_PAYLOAD = 0.96  # payload over the tbf rate: EXP-11 measured 1.92 Gbit/s at 2
COPY_BYTES = 2.12e9  # a full copy, every trial
INITIAL_WINDOW = 14.5e3  # 10 segments
SLOW_START = (0.6, 1.2)  # x log2(cap / initial window), less one: the round trips lost per bulk transfer
WINDOW_SHORTFALL = 0.85  # one default stream holds 0.85-1.0 of tcp_rmem's window (autotuning, the send buffer)
DEFAULT_RMEM_MAX, DEFAULT_ADV_WIN_SCALE = 6291456, 1  # Debian 12's, in EXP-11's run.json
TUNED_RMEM_MAX = 128 << 20
RTTS = (20, 60, 120)
QUEUE_FLOOR_S = 0.05  # the tbf queue: max(50 ms, r)
# full: pg_basebackup's start, its flush (1.33 s; 0.5-1.33 s once the stream outlasts 30 s, when the kernel's
# 30 s expiry has written back early files), 6-10 round trips (two connections and the replication commands).
FULL_START, FULL_FLUSH, FULL_FLUSH_LONG, FULL_EXPIRY_S, FULL_RTTS = 0.12, 1.33, (0.5, 1.33), 30.0, (6, 10)
# parcopy: its start and fsyncs above the link (EXP-11's 0.47 s, rounded up), 3-6 round trips.
PARCOPY_START, PARCOPY_RTTS = 0.55, (3, 6)
# v1, uniform: fixed work (hashing, patching, flushing), the bytes on the wire, 8-18 round trips (the connect, HELLO
# and ACCEPT, the basis lag, exposed leaf waits, COMPLETE and the receiver's drain). On tuned hosts a full bottleneck
# queue stretches the exposed leaf waits during bulk sends by up to half again (the 18 - 8 of them).
UNIFORM = {'uniform1': {'fixed': (2.6, 3.0), 'bytes': 609e6}, 'uniform10': {'fixed': (2.6, 4.0), 'bytes': 1.13e9}}
V1_RTTS, V1_STRETCH = (8, 18), 0.5
# v1, hot: 3 fixed round trips; each of the 19 changed files up to one round trip (3 at the least), of which the
# verifier's backlog absorbs up to 0.6 s; the burst of 25 MB when pgbench_accounts finishes, which slow-starts.
HOT_FIXED_RTTS, HOT_FILES, HOT_EXPOSED_LOW, HOT_ABSORBED_S, HOT_BURST_BYTES = 3, 19, 3, 0.6, 25e6
# EXP-11's v1 and copies at the anchor (E1, as written: EXP-11's medians within A10's gates).
ANCHOR = {'hot': (2.8, 3.4), 'uniform1': (4.8, 5.8), 'full': (9.8, 10.8), 'parcopy8': (8.9, 9.8)}
# The window (E9): window 1 exposes every leaf wait and window 4 at least the lower bound's 3; window 4 exposes at
# most one wait per 4 files hashed ahead; a window of 16 costs at most 0.1 s (more requests in flight).
WINDOW_COST_S = 0.1
# The fallback (E10): EXP-01's 45.9% of pgbench_accounts' heap pages changed at uniform 1%, 99.8% at 10%; its heap
# 0.94 GB, within 5%; the leaf round trips saved, up to the lower bound's 3.
ACCOUNTS_BYTES, CHANGED, FALLBACK_BYTES_SPREAD = 0.94e9, {'uniform1': 0.459, 'uniform10': 0.998}, 0.05
# TLS (E11): one more round trip; AES-GCM with AES-NI at 2 GB/s or more per core, at worst not overlapped; the
# proxy's longer prefetch saves up to 0.05 s; TLS records add 22 bytes per 16 KiB (0.13%), up to 0.5% with the
# handshake and small records under TCP_NODELAY. Its CPU, 0.2-0.8 s per VM in uniform, is carried as written.
TLS_RTTS, AES_BYTES_PER_S, TLS_PREFETCH_GAIN_S, TLS_WIRE, TLS_CPU = 1, 2e9, 0.05, (0.0013, 0.005), (0.2, 0.8)
HOT_WIRE_BYTES = 29e6
# SSH (E3, E12): OpenSSH's channel window, 64 packets of 32 KiB, of which one channel carries 0.8-1.0 per round
# trip (framing, MACs, the window adjust's own round trip). pg_probackup's largest file (0.94 GB) on one channel,
# then its 1.6 s flush.
SSH_WINDOW, SSH_EFFICIENCY = 2 << 20, (0.8, 1.0)
PROBACKUP_LARGEST, PROBACKUP_FLUSH = 0.94e9, 1.6

# The ranges as written (Added predictions, E3-E14), each (low, high), keyed by (what, condition RATE:RTT_MS:HOST).
INF = math.inf
WRITTEN = {
    'E3': {('one stream', '2:20:default'): (1.04, 1.26), ('one stream', '2:60:default'): (0.35, 0.42),
           ('one stream', '2:120:default'): (0.17, 0.21), ('one stream', '1:60:default'): (0.35, 0.42),
           ('one stream', '1:120:default'): (0.17, 0.21),
           ('8 streams', '2:20:default'): (1.80, INF), ('8 streams', '2:60:default'): (1.80, INF),
           ('8 streams', '2:120:default'): (1.39, 1.68), ('8 streams', '1:60:default'): (0.90, INF),
           ('8 streams', '1:120:default'): (0.90, INF), ('16 streams', '2:120:default'): (1.80, INF),
           ('one stream', '2:60:tuned'): (1.80, INF), ('one stream', '2:120:tuned'): (1.80, INF),
           ('stunnel / direct', '2:0:default'): (0.90, 1.10), ('stunnel / direct', '2:60:default'): (0.90, 1.10),
           ('stunnel / direct', '2:120:tuned'): (0.90, 1.10),
           ('SSH probe', '2:20:default'): (0.6, 0.84), ('SSH probe', '2:60:default'): (0.22, 0.28),
           ('SSH probe', '2:120:default'): (0.11, 0.14), ('SSH probe', '2:60:tuned'): (0.22, 0.28),
           ('SSH probe', '2:120:tuned'): (0.11, 0.14)},
    'E4': {('full', '2:20:default'): (15, 18), ('full', '2:60:default'): (41, 52), ('full', '2:120:default'): (82, 102),
           ('full', '2:60:tuned'): (11.0, 12.2), ('full', '2:120:tuned'): (11.7, 13.8),
           ('sync, stream under 30 s', '2:0:default'): (1.2, 1.45),
           ('sync, stream under 30 s', '2:20:default'): (1.2, 1.45),
           ('sync, stream over 30 s', '2:60:default'): (0.5, 1.4),
           ('sync, stream over 30 s', '2:120:default'): (0.5, 1.4)},
    'E5': {('parcopy8', '2:20:default'): (9.4, 9.9), ('parcopy8', '2:60:default'): (9.6, 10.5),
           ('parcopy8', '2:60:tuned'): (9.6, 10.5), ('parcopy8', '2:120:default'): (11.5, 14.5),
           ('parcopy8', '2:120:tuned'): (10.0, 11.2), ('parcopy8', '1:60:default'): (18.3, 19.3),
           ('parcopy8', '1:120:default'): (18.8, 20.0), ('parcopy16', '2:120:default'): (10.1, 11.0)},
    'E6': {('hot, added', '2:20:default'): (0.1, 0.7), ('hot, added', '2:60:default'): (0.5, 2.1),
           ('hot, added', '2:120:default'): (1.1, 4.4), ('hot, added', '2:60:tuned'): (0.4, 1.8),
           ('hot, added', '2:120:tuned'): (0.9, 3.8)},
    'E7': {('uniform1', '2:20:default'): (6.7, 8.2), ('uniform1', '2:60:default'): (15, 18.6),
           ('uniform1', '2:120:default'): (27, 34), ('uniform1', '1:60:default'): (15, 18.6),
           ('uniform1', '1:120:default'): (27, 34), ('uniform1', '2:60:tuned'): (6.0, 7.7),
           ('uniform1', '2:120:tuned'): (6.8, 10.0)},
    'E8': {('uniform10', '2:0:default'): (7.3, 8.7), ('uniform10', '2:20:default'): (10.3, 12.5),
           ('uniform10', '2:60:default'): (25, 31), ('uniform10', '2:60:tuned'): (8.4, 10.2),
           ('uniform10', '2:120:tuned'): (9.3, 12.5)},
    'E9': {('hot: w1 - v1', '2:120:tuned'): (0, 2.0), ('hot: w16 - v1', '2:120:tuned'): (-0.5, 0.1),
           ('uniform1: w1 - v1', '2:120:tuned'): (0, 1.0), ('uniform1: w16 - v1', '2:120:tuned'): (-0.3, 0.1),
           ('leaf_wait idle at window 4', '2:120:tuned'): (0.2, 2.3)},
    'E10': {('uniform1: hc - v1', '2:120:tuned'): (1.5, 2.8), ('uniform1: more bytes, GB', '2:120:tuned'): (0.45, 0.55),
            ('uniform10: hc - v1', '2:120:tuned'): (-0.4, 0.3),
            ('uniform10: wire bytes', '2:120:tuned'): (-0.02, 0.02)},
    'E11': {('hot: tls - v1', '2:0:default'): (-0.05, 0.15), ('uniform1: tls - v1', '2:0:default'): (-0.05, 0.30),
            ('hot: tls - v1', '2:120:tuned'): (0.05, 0.40), ('uniform1: tls - v1', '2:120:tuned'): (0.05, 0.50),
            ('uniform1: CPU-s per VM', '2:0:default'): (0.2, 0.8),
            ('uniform1: CPU-s per VM', '2:120:tuned'): (0.2, 0.8),
            ('wire bytes', '2:0:default'): (0.001, 0.005), ('wire bytes', '2:120:tuned'): (0.001, 0.005)},
    'E12': {('probackup8', '2:60:default'): (28, 60)},
    'E13': {('hot', '2:0:default'): (0.29, 0.38), ('uniform1', '2:0:default'): (0.49, 0.65),
            ('uniform10', '2:0:default'): (0.74, 0.98), ('hot', '2:20:default'): (0.31, 0.43),
            ('uniform1', '2:20:default'): (0.68, 0.87), ('uniform10', '2:20:default'): (1.04, 1.33),
            ('hot', '2:60:default'): (0.33, 0.57), ('uniform1', '2:60:default'): (1.43, 1.94),
            ('uniform10', '2:60:default'): (2.4, 3.2), ('hot', '2:120:default'): (0.37, 0.76),
            ('uniform1', '2:120:default'): (2.4, 3.4), ('hot', '2:60:tuned'): (0.32, 0.54),
            ('uniform1', '2:60:tuned'): (0.57, 0.80), ('uniform10', '2:60:tuned'): (0.80, 1.06),
            ('hot', '2:120:tuned'): (0.35, 0.72), ('uniform1', '2:120:tuned'): (0.61, 1.00),
            ('uniform10', '2:120:tuned'): (0.83, 1.25), ('uniform1', '1:60:default'): (0.78, 1.02),
            ('uniform1', '1:120:default'): (1.35, 1.81)},
    # Crossovers, Gbit/s, by (workload, RTT_MS:HOST): 0 for "below 1", inf for "above 2" (E14).
    'E14': {('uniform1', '60:default'): (0, 1.3), ('uniform1', '120:default'): (0, 1.0),
            ('hot', '20:default'): (2.0, INF), ('hot', '60:default'): (2.0, INF), ('hot', '120:default'): (2.0, INF),
            ('hot', '60:tuned'): (2.0, INF), ('hot', '120:tuned'): (2.0, INF), ('uniform1', '60:tuned'): (2.0, INF),
            ('uniform1', '120:tuned'): (2.0, INF)},
}
# Where the derivation as the text gives it yields a range the written one doesn't hold, even at its precision:
# {(prediction, key): why}. Each is reported beside the written range, which is the one judged.
KNOWN_GAPS = {
    ('E8', ('uniform10', '2:20:default')): ('2.6 s of fixed work, 1.13 GB at one stream\'s 134-157 MB/s, 8 round '
                                            'trips and slow start give 10.0 s at the least'),
    ('E8', ('uniform10', '2:60:tuned')): ('2.6-4.0 s of fixed work, 1.13 GB at the link, 8-18 round trips (stretched) '
                                          'and slow start give 8.1-10.8 s'),
    ('E8', ('uniform10', '2:120:tuned')): 'the same give 9.0-13.0 s',
    ('E13', ('uniform10', '2:20:default')): "E8's range over E5's, end to end",
    ('E13', ('uniform10', '2:60:tuned')): "E8's range over E4's and E5's, end to end",
    ('E13', ('uniform10', '2:120:tuned')): "E8's range over E5's, end to end",
    ('E13', ('hot', '2:120:default')): ("E1's 2.8 s plus E6's low end, over parcopy16's high end (E5), gives 0.36; "
                                        'the written 0.37 is not E1 + E6 over E5'),
    ('E13', ('hot', '2:120:tuned')): ("E1's 2.8 s plus E6's low end, over parcopy8's high end (E5), gives 0.34; the "
                                      'written 0.35 is not E1 + E6 over E5'),
}


def window(rmem_max, adv_win_scale) -> float:
    """What one stream can hold in flight from a tcp_rmem maximum (kernel 6.1's tcp_win_from_space)."""
    rmem_max, scale = int(rmem_max), int(adv_win_scale)
    return rmem_max - (rmem_max >> scale) if scale > 0 else rmem_max >> -scale


def rates_from_window(w) -> dict:
    return {rtt: (w[0] / (rtt / 1000) * 8 / 1e9, w[1] / (rtt / 1000) * 8 / 1e9) for rtt in RTTS}


def written_inputs() -> dict:
    """The inputs written in the pre-registration: Debian's tcp_rmem maximum and scale (W less up to 15%), each rate
    W / r, and the 128 MiB tuned maximum."""
    high = window(DEFAULT_RMEM_MAX, DEFAULT_ADV_WIN_SCALE)
    w = (WINDOW_SHORTFALL * high, float(high))
    return {'window_bytes': w, 'tuned_window_bytes': float(window(TUNED_RMEM_MAX, DEFAULT_ADV_WIN_SCALE)),
            'rates_gbit': rates_from_window(w), 'source': 'written', 'rates_measured': {}}


def measured_inputs(rmem_max, adv_win_scale, rates_gbit: dict, tuned_rmem_max=TUNED_RMEM_MAX) -> dict:
    """The smoke run's inputs: W from the recorded sysctls, and each measured rate (a point; a missing one W / r)."""
    high = window(rmem_max, adv_win_scale)
    w = (WINDOW_SHORTFALL * high, float(high))
    rates = rates_from_window(w)
    for rtt in RTTS:
        if rates_gbit.get(rtt) is not None:
            rates[rtt] = (rates_gbit[rtt], rates_gbit[rtt])
    return {'window_bytes': w, 'tuned_window_bytes': float(window(tuned_rmem_max, adv_win_scale)),
            'rates_gbit': rates, 'source': 'measured', 'rmem_max': int(rmem_max), 'adv_win_scale': int(adv_win_scale),
            'rates_measured': {rtt: rates_gbit.get(rtt) for rtt in RTTS}}


# --- ranges ----------------------------------------------------------------------

def add(*ranges):
    return (sum(r[0] for r in ranges), sum(r[1] for r in ranges))


def divide(a, b):
    """a / b end to end: the low end over b's high end, the high end over b's low end."""
    return (a[0] / b[1], a[1] / b[0])


def point(value):
    return (value, value)


def parse_condition(label: str) -> dict:
    rate, rtt, host = label.split(':')
    return {'gbit': float(rate), 'rtt_s': float(rtt) / 1000, 'rtt_ms': float(rtt), 'host': host}


LOW, HIGH = 0, 1  # a time's low end (every term at its fast end) and its high end


class Model:
    """The derivation on one set of inputs (written_inputs() or measured_inputs()). Each time is computed at both
    ends, LOW and HIGH, each one consistent scenario: LOW takes the faster rate with the window it implies, the fewer
    round trips and the shorter slow start; HIGH the slower rate, the more round trips, the longer slow start."""

    def __init__(self, inputs: dict):
        self.inputs = inputs

    @staticmethod
    def link(gbit) -> float:
        """The link's payload rate, bytes per second."""
        return gbit * 1e9 * LINK_PAYLOAD / 8

    def stream(self, label: str, streams: int = 1, end: int = LOW) -> float:
        """N streams' rate in bytes per second at a condition: on default hosts N windows (each the one-stream rate
        at r, the faster at LOW), up to the link; on tuned hosts the tuned window, up to the link; the link at r = 0."""
        c = parse_condition(label)
        link = self.link(c['gbit'])
        if c['rtt_s'] == 0:
            return link
        if c['host'] == 'tuned':
            return min(link, streams * self.inputs['tuned_window_bytes'] / c['rtt_s'])
        one = self.inputs['rates_gbit'][int(c['rtt_ms'])]
        return min(link, streams * (one[1] if end == LOW else one[0]) * 1e9 / 8)

    def rate_range(self, label: str, streams: int = 1) -> tuple:
        """N streams' rate in Gbit/s, (slower, faster)."""
        return (self.stream(label, streams, HIGH) * 8 / 1e9, self.stream(label, streams, LOW) * 8 / 1e9)

    def cap(self, label: str, streams: int, end: int) -> float:
        """What each of N streams carries per round trip once out of slow start: its share of the rate times r; one
        stream on a tuned host, where no window binds, fills the bottleneck queue (max(50 ms, r)) too."""
        c = parse_condition(label)
        span = c['rtt_s'] + (max(QUEUE_FLOOR_S, c['rtt_s']) if c['host'] == 'tuned' and streams == 1 else 0.0)
        return self.stream(label, streams, end) * span / streams

    def slow_start(self, label: str, streams: int, end: int) -> float:
        """Seconds lost to slow start in one bulk transfer: 0.6 (LOW) or 1.2 (HIGH) x log2(cap / 10 segments), less
        one, round trips; none at r = 0."""
        r = parse_condition(label)['rtt_s']
        if r == 0:
            return 0.0
        cap = max(self.cap(label, streams, end), INITIAL_WINDOW)
        return max(0.0, SLOW_START[end] * math.log2(cap / INITIAL_WINDOW) - 1) * r

    @staticmethod
    def rtts(label: str, counts, end: int) -> float:
        return counts[end] * parse_condition(label)['rtt_s']

    def transfer(self, label: str, nbytes: float, streams: int, end: int) -> float:
        return nbytes / self.stream(label, streams, end)

    @staticmethod
    def both(compute) -> tuple:
        return (compute(LOW), compute(HIGH))

    # The arms.

    def full_flush(self, label: str, end: int) -> float:
        long = self.transfer(label, COPY_BYTES, 1, end) > FULL_EXPIRY_S
        return FULL_FLUSH_LONG[end] if long else FULL_FLUSH

    def full(self, label: str) -> tuple:
        """`full`: its start, 2.12 GB at one stream's rate, its flush, 6-10 round trips, slow start."""
        return self.both(lambda end: FULL_START + self.transfer(label, COPY_BYTES, 1, end) + self.full_flush(label, end)
                         + self.rtts(label, FULL_RTTS, end) + self.slow_start(label, 1, end))

    def full_sync(self, label: str) -> tuple:
        return self.both(lambda end: self.full_flush(label, end))

    def parcopy(self, label: str, streams: int = 8) -> tuple:
        """`parcopy8`, `parcopy16`: its start, 2.12 GB at N streams' rate, 3-6 round trips, each stream's slow start."""
        return self.both(lambda end: PARCOPY_START + self.transfer(label, COPY_BYTES, streams, end)
                         + self.rtts(label, PARCOPY_RTTS, end) + self.slow_start(label, streams, end))

    def bound(self, label: str) -> tuple:
        """B_fast's copy: the faster of `full` and the parcopy arms run there, end by end; at r = 0, E1's."""
        if parse_condition(label)['rtt_s'] == 0:
            copies = [ANCHOR['full'], ANCHOR['parcopy8']]
        else:
            copies = [self.full(label), self.parcopy(label, 8)]
            if label == '2:120:default':
                copies.append(self.parcopy(label, 16))
        return (min(r[0] for r in copies), min(r[1] for r in copies))

    def uniform(self, workload: str, label: str) -> tuple:
        """v1, uniform: fixed work, its bytes at one stream's rate, 8-18 round trips (on tuned hosts, at the high
        end, the exposed leaf waits among them stretched by half again), slow start."""
        c = parse_condition(label)
        spec = UNIFORM[workload]

        def at(end):
            rtts = self.rtts(label, V1_RTTS, end)
            if c['host'] == 'tuned' and end == HIGH:
                rtts += V1_STRETCH * (V1_RTTS[1] - V1_RTTS[0]) * c['rtt_s']
            return (spec['fixed'][end] + self.transfer(label, spec['bytes'], 1, end) + rtts
                    + self.slow_start(label, 1, end))
        return self.both(at)

    def burst(self, label: str, end: int) -> float:
        """Seconds the 25 MB burst takes beyond its time at the link: from 10 segments, doubling each round trip up
        to the stream's cap, the rounds it takes times r, less its link time."""
        c = parse_condition(label)
        if c['rtt_s'] == 0:
            return 0.0
        cap, rounds, sent, cwnd = self.cap(label, 1, end), 0.0, 0.0, INITIAL_WINDOW
        while True:
            step = min(cwnd, cap)
            if sent + step >= HOT_BURST_BYTES:
                rounds += (HOT_BURST_BYTES - sent) / step
                break
            sent += step
            rounds += 1
            cwnd *= 2
        return max(0.0, rounds * c['rtt_s'] - HOT_BURST_BYTES / self.link(c['gbit']))

    def hot_added(self, label: str) -> tuple:
        """v1, hot, added by latency. LOW: 3 fixed round trips, 3 exposed leaf waits all absorbed by the verifier's
        backlog, half the burst overlapped. HIGH: all 19 leaf waits exposed, nothing absorbed, the whole burst on the
        critical path, its first round being pgbench_accounts' own leaf wait (counted once)."""
        r = parse_condition(label)['rtt_s']
        return (HOT_FIXED_RTTS * r + max(0.0, HOT_EXPOSED_LOW * r - HOT_ABSORBED_S) + self.burst(label, LOW) / 2,
                HOT_FIXED_RTTS * r + (HOT_FILES - 1) * r + self.burst(label, HIGH))

    def v1(self, workload: str, label: str) -> tuple:
        """v1's time: at the anchor E1's (uniform 10% from its fixed work and the link), plus what latency adds
        (hot), or uniform's derivation."""
        if parse_condition(label)['rtt_s'] == 0:
            return ANCHOR[workload] if workload in ANCHOR else self.uniform(workload, label)
        if workload == 'hot':
            return add(ANCHOR['hot'], self.hot_added(label))
        return self.uniform(workload, label)

    def ssh(self, label: str) -> tuple:
        """One SSH channel, Gbit/s: 0.8-1.0 of its window per round trip, up to one TCP stream's rate."""
        r = parse_condition(label)['rtt_s']
        return (min(SSH_WINDOW * SSH_EFFICIENCY[0] / r, self.stream(label, 1, HIGH)) * 8 / 1e9,
                min(SSH_WINDOW * SSH_EFFICIENCY[1] / r, self.stream(label, 1, LOW)) * 8 / 1e9)

    def probackup(self, label: str) -> tuple:
        """pg_probackup's largest file on one SSH channel, then its flush."""
        rate = self.ssh(label)
        return (PROBACKUP_LARGEST / (rate[1] * 1e9 / 8) + PROBACKUP_FLUSH,
                PROBACKUP_LARGEST / (rate[0] * 1e9 / 8) + PROBACKUP_FLUSH)

    def crossover(self, workload: str, rtt_ms: int, host: str) -> tuple:
        """Where v1 / B_fast crosses 1 between 1 and 2 Gbit/s (D78: log-log between the zero-delay rates at the two
        tbf rates), over the ratios' ends: 0 for "below 1" (1 or more at both), inf for "above 2" (under 1 at 2 Gbit/s
        where 1 Gbit/s didn't run). Where only 2 Gbit/s ran and it is 1 or more there: "below 2", (0, 2)."""
        two = divide(self.v1(workload, f'2:{rtt_ms}:{host}'), self.bound(f'2:{rtt_ms}:{host}'))
        if host != 'default' or workload != 'uniform1' or rtt_ms not in (60, 120):
            return (2.0, INF) if two[1] < 1 else (0.0, 2.0) if two[0] >= 1 else (0.0, INF)
        one_label = f'1:{rtt_ms}:{host}'
        one = divide(self.v1(workload, one_label), self.bound(one_label))
        x1, x2 = LINK_PAYLOAD, 2 * LINK_PAYLOAD
        found = []
        for r1 in one:
            for r2 in two:
                if r1 >= 1:
                    found.append(0.0 if r2 >= 1 else math.nan)  # slower at 1 Gbit/s, faster at 2: not a crossover
                elif r2 < 1:
                    found.append(INF)
                else:
                    t = -math.log(r1) / (math.log(r2) - math.log(r1))
                    found.append(math.exp(math.log(x1) + t * (math.log(x2) - math.log(x1))))
        found = [value for value in found if not math.isnan(value)]
        return (min(found), max(found)) if found else (0.0, INF)

    # Every prediction.

    def ranges(self) -> dict:
        out: dict = {name: {} for name in WRITTEN}
        for key in WRITTEN['E3']:
            what, label = key
            if what == 'stunnel / direct':
                out['E3'][key] = point(1.0)  # stunnel sets no buffers: its WAN socket autotunes as the direct one
            elif what == 'SSH probe':
                out['E3'][key] = self.ssh(label)
            else:
                out['E3'][key] = self.rate_range(label, {'one stream': 1, '8 streams': 8, '16 streams': 16}[what])
        for key in WRITTEN['E4']:
            out['E4'][key] = self.full(key[1]) if key[0] == 'full' else self.full_sync(key[1])
        for key in WRITTEN['E5']:
            out['E5'][key] = self.parcopy(key[1], 16 if key[0] == 'parcopy16' else 8)
        for key in WRITTEN['E6']:
            out['E6'][key] = self.hot_added(key[1])
        for key in WRITTEN['E7']:
            out['E7'][key] = self.uniform('uniform1', key[1])
        for key in WRITTEN['E8']:
            out['E8'][key] = self.v1('uniform10', key[1])
        out['E9'] = self.window_ranges()
        out['E10'] = self.fallback_ranges()
        out['E11'] = self.tls_ranges()
        out['E12'] = {('probackup8', '2:60:default'): self.probackup('2:60:default')}
        for key in WRITTEN['E13']:
            out['E13'][key] = divide(self.v1(*key), self.bound(key[1]))
        for key in WRITTEN['E14']:
            rtt, host = key[1].split(':')
            out['E14'][key] = self.crossover(key[0], int(rtt), host)
        return out

    @staticmethod
    def window_ranges() -> dict:
        """E9, at 120 ms tuned: window 1 exposes every leaf wait and window 4 at least the lower bound's 3; window 4
        exposes at most one wait per 4 files hashed ahead, so a window of 16 saves at most those beyond the verifier's
        backlog, and costs at most 0.1 s. In uniform, hashing the large files hides more: window 1 exposes at most
        half the waits, and window 16 saves at most half as much. The idle part of leaf_wait_s at window 4: the lower
        bound's 3 waits to all 19, each a round trip (the backlog absorbs time on the critical path, not the wait)."""
        label = '2:120:tuned'
        r = parse_condition(label)['rtt_s']
        saved16 = max(0.0, math.ceil(HOT_FILES / 4) * r - HOT_ABSORBED_S)
        return {('hot: w1 - v1', label): (0.0, (HOT_FILES - HOT_EXPOSED_LOW) * r),
                ('hot: w16 - v1', label): (0.0 - saved16 if saved16 else 0.0, WINDOW_COST_S),
                ('uniform1: w1 - v1', label): (0.0, (HOT_FILES // 2 - HOT_EXPOSED_LOW) * r),
                ('uniform1: w16 - v1', label): (0.0 - saved16 / 2 if saved16 else 0.0, WINDOW_COST_S),
                ('leaf_wait idle at window 4', label): (HOT_EXPOSED_LOW * r, HOT_FILES * r)}

    def fallback_ranges(self) -> dict:
        """E10, at 120 ms tuned: whole nodes send pgbench_accounts' unchanged heap pages too (EXP-01's changed
        share; within 5%), at the link, less up to the 3 leaf round trips saved."""
        label = '2:120:tuned'
        r = parse_condition(label)['rtt_s']
        link = self.link(2)
        out = {}
        for workload in ('uniform1', 'uniform10'):
            extra = (1 - CHANGED[workload]) * ACCOUNTS_BYTES
            extra = (extra * (1 - FALLBACK_BYTES_SPREAD), extra * (1 + FALLBACK_BYTES_SPREAD))
            out[(f'{workload}: hc - v1', label)] = (extra[0] / link - HOT_EXPOSED_LOW * r, extra[1] / link)
            if workload == 'uniform1':
                out[('uniform1: more bytes, GB', label)] = (extra[0] / 1e9, extra[1] / 1e9)
            else:
                wire = UNIFORM['uniform10']['bytes']
                out[('uniform10: wire bytes', label)] = (extra[0] / wire, extra[1] / wire)
        return out

    @staticmethod
    def tls_ranges() -> dict:
        """E11: one more round trip, less what the proxy's longer prefetch saves, plus the encryption where it isn't
        overlapped; the TLS records on the wire; the CPU as written."""
        out = {}
        for label in ('2:0:default', '2:120:tuned'):
            r = parse_condition(label)['rtt_s']
            for workload, nbytes in (('hot', HOT_WIRE_BYTES), ('uniform1', UNIFORM['uniform1']['bytes'])):
                out[(f'{workload}: tls - v1', label)] = (TLS_RTTS * r - TLS_PREFETCH_GAIN_S,
                                                          TLS_RTTS * r + nbytes / AES_BYTES_PER_S)
            out[('uniform1: CPU-s per VM', label)] = TLS_CPU
            out[('wire bytes', label)] = TLS_WIRE
        return out


# --- comparison --------------------------------------------------------------

def slack(value) -> float:
    """Half a unit of the last digit a written value shows (0 for an infinite one)."""
    if math.isinf(value):
        return 0.0
    text = repr(value)
    return 0.5 * 10 ** -(len(text.split('.')[1]) if '.' in text else 0)


def inside(model, written) -> bool:
    """Whether a model range lies inside a written one, read at the written precision."""
    return (model[0] >= written[0] - slack(written[0]) - 1e-9
            and model[1] <= written[1] + slack(written[1]) + 1e-9)


def compare(ranges: dict) -> list[dict]:
    """Each written range beside the model's: inside or not, and any known gap."""
    rows = []
    for name, written in WRITTEN.items():
        for key, value in written.items():
            model = ranges.get(name, {}).get(key)
            rows.append({'prediction': name, 'key': key, 'written': value, 'model': model,
                         'inside': None if model is None else inside(model, value),
                         'known_gap': KNOWN_GAPS.get((name, key))})
    return rows


def fmt_range(rng, digits=2) -> str:
    if rng is None:
        return '–'
    low, high = rng
    if math.isinf(high):
        return f'at least {low:.{digits}f}' if low > 0 else 'any'
    return f'{low:.{digits}f}–{high:.{digits}f}'


def lines(ranges: dict, inputs: dict) -> list[str]:
    w = inputs['window_bytes']
    out = [f'## The model\'s ranges ({inputs["source"]} inputs; analysis/exp13_model.py)',
           f'- W {w[0] / 1e6:.3f}–{w[1] / 1e6:.3f} MB (tuned {inputs["tuned_window_bytes"] / 1e6:.1f} MB); one default '
           'stream, Gbit/s: ' + ', '.join(f'{rtt} ms {fmt_range(rate, 3)}' for rtt, rate in inputs['rates_gbit'].items()),
           '| Prediction | what | condition | written | model | inside |', '|---|---|---|---|---|---|']
    for row in compare(ranges):
        what, where = row['key']
        state = ('–' if row['inside'] is None else 'yes' if row['inside'] else
                 'no' + (f' (known: {row["known_gap"]})' if row['known_gap'] else ''))
        out.append(f'| {row["prediction"]} | {what} | {where} | {fmt_range(row["written"])} | '
                   f'{fmt_range(row["model"])} | {state} |')
    return out


def sweep_inputs(sweeps) -> dict | None:
    """The smoke run's inputs from its results: dest's tcp_rmem and tcp_adv_win_scale as run.json recorded them
    (t12_preflight.tcp_defaults, else the environment), and the median of the visits' one-stream steady rates at
    2 Gbit/s on default hosts at each r (trials/*.json). None without the sysctls."""
    rmem = win_scale = None
    rates: dict = {}
    for sweep in map(Path, sweeps):
        run_path = sweep / 'run.json'
        if run_path.exists():
            run = json.loads(run_path.read_text())
            defaults = ((run.get('t12_preflight') or {}).get('tcp_defaults') or {}).get('dest') or {}
            environment = (run.get('environment') or {}).get('dest') or {}
            sysctls = environment.get('tcp_sysctls') or {}
            rmem = rmem or (defaults.get('net.ipv4.tcp_rmem') or sysctls.get('net.ipv4.tcp_rmem')
                            or environment.get('tcp_rmem'))
            if win_scale is None:
                for found in (defaults.get('net.ipv4.tcp_adv_win_scale'), sysctls.get('net.ipv4.tcp_adv_win_scale'),
                              environment.get('tcp_adv_win_scale')):
                    if found is not None:
                        win_scale = found
                        break
        for path in sorted((sweep / 'trials').glob('*.json')):
            trial = json.loads(path.read_text())
            for visit in trial.get('visits') or ():
                parts = (visit.get('condition') or '').split(':')
                if len(parts) != 3 or parts[0] != '2' or parts[2] != 'default' or float(parts[1]) not in RTTS:
                    continue
                test = next(iter(visit.get('link_tests') or ()), None) or {}
                if test.get('steady_gbit') is not None:
                    rates.setdefault(int(float(parts[1])), []).append(test['steady_gbit'])
    if rmem is None:
        return None
    return measured_inputs(int(str(rmem).split()[-1]), int(win_scale if win_scale is not None
                                                            else DEFAULT_ADV_WIN_SCALE),
                           {rtt: statistics.median(values) for rtt, values in rates.items()})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sweeps', nargs='*', default=[], help="the smoke run's results directories")
    parser.add_argument('--tcp-rmem-max', type=int, help="tcp_rmem's maximum (default: the sweeps', else Debian's)")
    parser.add_argument('--tcp-adv-win-scale', type=int)
    parser.add_argument('--tuned-tcp-max-bytes', type=int, default=TUNED_RMEM_MAX)
    parser.add_argument('--rate', action='append', default=[], metavar='RTT_MS:GBIT',
                        help="one default stream's steady rate at 20, 60 or 120 ms (repeatable)")
    parser.add_argument('--json', action='store_true', help='print the ranges as JSON')
    args = parser.parse_args(argv)
    rates = {}
    for item in args.rate:
        rtt, colon, gbit = item.partition(':')
        if not colon or not rtt.isdigit() or int(rtt) not in RTTS:
            parser.error(f'--rate wants RTT_MS:GBIT with RTT_MS one of {RTTS}, got {item!r}')
        rates[int(rtt)] = float(gbit)
    found = sweep_inputs(args.sweeps) if args.sweeps else None
    if found is None and args.tcp_rmem_max is None and args.tcp_adv_win_scale is None and not rates:
        inputs = written_inputs()
    else:
        found = found or {}
        measured = {**(found.get('rates_measured') or {}), **rates}
        inputs = measured_inputs(args.tcp_rmem_max or found.get('rmem_max') or DEFAULT_RMEM_MAX,
                                 args.tcp_adv_win_scale if args.tcp_adv_win_scale is not None
                                 else found.get('adv_win_scale', DEFAULT_ADV_WIN_SCALE),
                                 measured, args.tuned_tcp_max_bytes)
    ranges = Model(inputs).ranges()
    if args.json:
        print(json.dumps({'inputs': {key: value for key, value in inputs.items()},
                          'ranges': {name: {' @ '.join(key): list(value) for key, value in group.items()}
                                     for name, group in ranges.items()}}, indent=1, default=str))
    else:
        print('\n'.join(lines(ranges, inputs)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
