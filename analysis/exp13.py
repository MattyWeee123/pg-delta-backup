"""EXP-13 (T12: what changes at DR round-trip times, latency and transport), as pre-registered.

    .venv/bin/python analysis/exp13.py [--sweeps results/exp04/exp13-uniform1 results/exp04/exp13-hot
        results/exp04/exp13-uniform10] [--model-sweeps DIR ...] [--exp11-cells FILE] [--smoke] [--tag ...]
        [--datadir DIR] [--figdir DIR]

Reads each sweep as `run_exp04 --conditions` lays it out and `remote.py fetch` brings it back: summary.csv (its
`speed` column holds each condition's label, RATE:RTT_MS:HOST), the run records runs/<trial>/r<repeat>-<slug>-<method>
.json, the trial records (each visit, each condition's first-visit probes, the reference digests by file class) and
run.json; and EXP-11's curated docs/experiments/data/exp11-cells.csv (A10's drift gates). Writes the curated data
docs/experiments/data/exp13-runs.csv and exp13-cells.csv, the figures docs/figures/exp13-rtt.svg and exp13-ratios.svg,
and prints every prediction and rule of docs/experiments/EXP-13-latency-transport.md with its verdict, in its order of
judgment: the runs and their standing (A11, A13-A15), the baselines, the medians, the added time and effective round
trips, the ratios; then the ledger's L1, L2 and R1 (R1 first among the rules, since its consequence comes before the
verdict's rows), the DR verdict's rows at 20, 60 and 120 ms in order with their flags and qualifier, 1 Gbit/s beside
them, the crossovers, A2-A17 (A3's split of v1's added time into its four parts, A6's two readings), E1-E17 with the
model's recomputed range beside each written one (analysis/exp13_model.py, on --model-sweeps' inputs: the smoke run's),
the network table (link tests against the model, ss, netem and tbf), the reference digests by file class, and with
--smoke the smoke table's Record paths, each checked in the records.

A workload is a trial's pattern and fraction: uniform 1% (uniform1), hot (hot 1%) and uniform 10% (uniform10). v1's
arms are judged per workload (its 5 runs in its trial); the baselines (`full`, `parcopy8`, `parcopy16`,
`probackup8`) are pooled over the trials at each condition. Every statistic is a median, with the maximum beside it.

How this script reads what the text leaves open (each a reading of the text; where a reading needed a choice, the one
that doesn't favour v1):
- A run counts if it passed its verification (v1_tls: stunnel's log shows TLS 1.3 and its suite), every durability
  check that applies (check (a) unless A14 voids it; (b) for full copies; parcopy's fsync count), isn't void, and
  passed the network checks (A11 (ii)). A run whose network checks couldn't all be made (`network_ok` empty) is left
  out as "network unchecked", as EXP-11 left out a run whose durability went unchecked, and reported apart from A11's
  exclusions.
- A cell is "not measured" (A11 (ii)) when more than one of its runs was left out for a network reason, an A11
  exclusion or a run whose checks couldn't be made: v1's arms in their trial, a baseline over its pooled runs; a cell
  with no counting run is not measured either.
- B_fast is "not measured" at a condition when its chosen arm's cell is, or when a candidate that ran there is not
  measured and either has no counting run or its counting median isn't more than 0.05 s slower than the chosen arm's:
  "the faster of" can't be known without it.
- A13 counts an arm's failing runs at a condition over the whole session (every trial's runs there): its cell for a
  baseline, three trials' for v1. A15's tool failures (a parcopy run that failed verify_direct or didn't start, a
  probackup8 run that didn't start or differed logically while the user tables matched, either's copy its tool aborted
  or stopped at A15's bound, failure_reason 'timeout') exclude the arm from every baseline, as EXP-11's A4; they don't
  apply to `full` or to v1's arms, whose failed runs (a timeout among them) are only left out.
- `full` at 1 Gbit/s, where it didn't run (A16), stands in with its pooled runs at 2 Gbit/s, the same r and host
  setting, labelled inferred, in B_fast, B_rel and F0 alike.
- Δ at 1 Gbit/s isn't reported: no anchor runs at 1 Gbit/s. `parcopy8`'s tuned Δ uses the default anchor, labelled,
  in the table of Δs. R1, L1, L2, A3 and E6 read each Δ from the anchor of its own host setting or not at all: a tuned
  cell whose tuned anchor has no counting run has no Δ there.
- R1 is judged on every cell with counting runs, a not-measured cell included (flagged): a rule must be able to fire
  (a rule 8 change, recorded in the pre-registration's analysis readings). L1, L2, A8 and the qualifier read no
  cell A11 leaves "not measured", at r or at the anchor: such a cell is reported as not measured, a workload whose cell
  isn't measured leaves L1 or L2 not judged unless another workload already fails it, and A8 states no claim there.
  A4, A5 and A6 read no such cell either: it leaves A4 or A5 not judged, and A6 judged on its other cells.
- A8's claim at r (uniform 10% under 0.90) is made only with v1 durable (A13), and is provisional where R1 fired or
  the bound there is suspect, window-limited, not durable or excluded (A9, A13, A15), as rows 1 and 2 are.
- The default host setting must be Debian's: every invocation's t12_preflight.tcp_defaults and every default run's
  tcp_rmem and tcp_wmem maxima against measure.DEBIAN_TCP_MEM, and each invocation's teardown; any difference flags
  the session.
- A9's "2.12 GB" is the parcopy arm's own source-to-dest wire bytes there (what "a full copy moves"; 2.12 GB without
  them), at its stream count's steady rate; any parcopy arm run at a 2 Gbit/s condition that fails the gate makes rows
  1 and 2 at that r provisional ("bound suspect"), as A9 says "a parcopy arm"; the window-limited flag reads B_fast's
  arm only. A parcopy arm excluded by A15 at r reads "bound excluded", provisional,
  beside A13's "bound not durable".
- A6's priced reading adds v1_tls's extra prefetched bytes over v1's (never fewer than none) at v1's own prefetch rate
  in that cell (the median of its runs' bytes over seconds). The slower of the two readings is R_tls's.
- A3's drain is the receiver's exit less its publication: the record's receiver_wall_s less publish_s (drain_s), or,
  where the record carries the receiver's publication time, its exit (launch plus wall, dest's clock) less that.
- A2 reads the rows as computed (beside "v1 not durable" when A13 has v1); "D36 stands" also needs v1 durable.
- E-ranges are judged as written (exp13_model.WRITTEN for E3-E14). Ranges built on EXP-11's absolute times (E1, E4, E5,
  E7, E8, E13) are flagged when A10 marks the session shifted, and those built on a one-stream rate that the smoke run
  measured more than 25% from 1.26, 0.42 or 0.21 Gbit/s are flagged too (smoke item 7).
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import exp04
import exp13_model as model
import plotting
from exp11 import choose, every, flag, fmt, median, mx, number, ratio, word
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from experiments.network import measure  # noqa: E402

DATA = ROOT / 'docs' / 'experiments' / 'data'
SWEEPS = [ROOT / 'results' / 'exp04' / name for name in ('exp13-uniform1', 'exp13-hot', 'exp13-uniform10')]
WORKLOADS = ['uniform1', 'hot', 'uniform10']  # the session's order
WORKLOAD_LABEL = {'uniform1': 'uniform 1%', 'hot': 'hot', 'uniform10': 'uniform 10%'}
EXP11_PATTERN = {'uniform1': 'uniform', 'hot': 'hot'}  # A10: the workloads EXP-11 ran
DECIDING = ('hot', 'uniform1')
V1 = 'pgdelta_direct_inplace'
W1, W16, HC, TLS = measure.T12_V1_ARMS
V1_ARMS = (V1, W1, W16, HC, TLS)
BASELINE_ARMS = ('full', 'parcopy8', 'parcopy16', 'probackup8')
CONTROL = 'full_nosync'
ARMS = [(V1, 'v1: pgdelta direct, in place, window 4', COLORS['green'], '-', 'o'),
        (W1, 'v1_w1: window 1', COLORS['sky'], '--', 'v'),
        (W16, 'v1_w16: window 16', COLORS['blue'], '--', '^'),
        (HC, 'v1_hc: high-churn fallback at 0.5', COLORS['yellow'], '--', 's'),
        (TLS, 'v1_tls: through stunnel (TLS 1.3)', COLORS['red'], '--', 'D'),
        ('full', "full: pg_basebackup, one stream (Omni today)", COLORS['grey'], '-', 'o'),
        ('parcopy8', 'parcopy8: the bound, 8 streams', COLORS['purple'], '-', 'D'),
        ('parcopy16', 'parcopy16: the bound, 16 streams', COLORS['purple'], ':', 'D'),
        ('probackup8', 'probackup8: pg_probackup -j 8 over SSH', COLORS['orange'], '-', '^'),
        (CONTROL, 'full_nosync: --no-sync (negative control)', COLORS['black'], ':', 'x')]
ARM_NAMES = [arm for arm, *_ in ARMS]
ENVELOPES = {'B_fast': ('full', 'parcopy8', 'parcopy16', 'probackup8'), 'B_rel': ('full', 'probackup8'), 'F0': ('full',)}
RATIO_NAME = {'B_fast': 'R_fast', 'B_rel': 'R_rel', 'F0': 'R_F0'}
# The conditions, RATE:RTT_MS:HOST, in the order the tables show them.
CONDITIONS = ['2:0:default', '2:20:default', '2:60:default', '2:120:default', '2:0:tuned', '2:60:tuned', '2:120:tuned',
              '1:60:default', '1:120:default']
ANCHOR = {'default': '2:0:default', 'tuned': '2:0:tuned'}
RTTS = (20, 60, 120)
DESIGN = {20: ('default',), 60: ('default', 'tuned'), 120: ('default', 'tuned')}  # the host settings run at r
SIDE_CELL = '2:120:tuned'
TLS_CELLS = ('2:0:default', SIDE_CELL)
TIE_S = 0.05
HOLD, OUT = 0.90, 1.00
NEAR = 0.03
L1_LIMIT_S = 2.0
A4_GAIN_S, A4_COST_S, A4_SAME_S = 0.3, 0.1, 0.1
A5_SAVE_S, A5_COST_S = 0.3, 0.1
A6_FLOOR_S, A6_SHARE = 0.3, 0.05
A7_SHARE = 0.5
A9_SLACK_S, A9_RTTS, A9_BALANCE, A9_WINDOW = 1.0, 6, 1.1, 0.9
A10_GATES = {'full': 0.05, 'parcopy8': 0.05, V1: 0.10, 'link': 0.02, 'wire': 0.01}
A12_SHARE = 0.9
A16_LIMIT_GBIT = 0.8
A17_SPREAD, A17_ANCHOR_S = 0.03, 0.1
INPUT_DRIFT = 0.25  # smoke item 7: a measured one-stream rate more than 25% from the written one
WRITTEN_RATES = {20: 1.26, 60: 0.42, 120: 0.21}
EXP11_SHIFT_PREDICTIONS = ('E1', 'E4', 'E5', 'E7', 'E8', 'E13')
ROWS = ('v1 not durable', 'Not judged', '1. Holds as shipped', '2. Holds with tuned buffers', '3. Local changes only',
        '4. Out at DR latency', '5. Undecided')
ROW_TEXT = {
    ROWS[0]: 'no claim for v1 at any r: it failed durability (A13), which is a reading, not a missing cell',
    ROWS[1]: 'a deciding cell on default hosts is not measured (A11); nothing is rerun',
    ROWS[2]: "v1's DR claim at r and 2 Gbit/s holds with one default TCP stream, on these VMs. D36 stands, now "
             'measured at latency',
    ROWS[3]: "raised TCP buffers bring v1 under 0.90 at r; A2 and E15 say whether one stream's window is why. v1 needs "
             'several streams for page data, or raised TCP buffers on both hosts: an owner decision, after the '
             'integrator question (can Omni\'s nodes or pods set tcp_rmem and tcp_wmem?)',
    ROWS[4]: "v1's DR claim at r covers local changes only. Scattered changes need more streams and fewer bytes (T4)",
    ROWS[5]: 'Out at DR latency for local changes. A3 gives the cause',
    ROWS[6]: 'decided after T4 and T8. A4 alone decides D29',
}
CLAIM_ROWS = ROWS[2:5]  # rows 1, 2 and 3: R1 makes them provisional
BOUND_ROWS = ROWS[2:4]  # rows 1 and 2: they need a bound that passes A9

# The summary's columns this script reads (measure.CSV_FIELDS), by type.
TEXT = ('trial_id', 'pattern', 'speed', 'method', 'status', 'tier', 'error', 'failure_kind', 'failure_reason',
        'host_tcp', 'excluded_reason', 'tls_version', 'tls_suite')
FLAGS = ('verified', 'durable', 'durability_ok', 'void', 'network_ok', 'sysctl_ok', 'busy_split_valid',
         'window_limited_bound')
NUMERIC = ('fraction', 'order_index', 'target_gbit', 'measured_s2d_gbit', 'measured_d2s_gbit',
           'measured_s2d_gbit_8streams', 'total_s', 'total_no_ssh_s', 'receiver_start_s', 'transfer_s',
           'wire_s2d_bytes', 'wire_d2s_bytes', 'tool_s2d_bytes', 'output_bytes', 'cpu_source_s', 'cpu_dest_s',
           'dirty_increase_mb', 'disk_write_dest_bytes', 'pbb_checkpoint_s', 'pbb_stream_s', 'pbb_wal_end_s',
           'pbb_sync_s', 'pbb_manifest_s', 'backup_mode_adder_s', 'probackup_sync_s', 'probackup_copy_s',
           'parcopy_streams', 'parcopy_balance', 'parcopy_last_byte_s', 'parcopy_fsync_s', 'parcopy_fsync_calls',
           'parcopy_fsync_expected', 'prefetch_bytes', 'prefetch_s', 'hash_basis_s', 'fsync_s', 'verify_wait_s',
           *(field for field in measure.T12_FIELDS if field not in TEXT + FLAGS + ('inferred',)))
WAITS = measure.PGDELTA_SENDER_WAITS  # accept_wait, basis_wait, leaf_wait, complete_wait
KEEP = ['sweep', 'trial_id', 'workload', 'speed', 'method', 'repeat', 'tier', 'status', 'verified', 'void', 'usable',
        'left_out', 'network_ok', 'excluded_reason', 'total_s', 'trig_s', 'rate_gbit', 'rtt_ms', 'host_tcp',
        'ping_min_ms', 'visit_one_stream_gbit', 'zero_delay_gbit', 'busy_inflight_bytes', 'sysctl_ok',
        'tcp_rmem_max_source', 'tcp_rmem_max_dest', 'tcp_wmem_max_source', 'tcp_wmem_max_dest', 'wire_s2d_bytes',
        'wire_d2s_bytes', 'page_bytes', 'cpu_source_s', 'cpu_dest_s', 'a_ok', 'b_ok', 'fsync_ok',
        'dirty_increase_bytes', 'dirty_limit_bytes', 'noise_window_s', 'netem_bytes_source', 'netem_own_drops',
        'netem_backlog_max', 'tbf_drops_source', 'tbf_drops_dest', 'retrans_segs_source', 'retrans_segs_dest',
        'minrtt_ms', 'minrtt_ms_max', 'cwnd', 'busy_ms', 'rwnd_limited_ms', 'sndbuf_limited_ms', 'limited_fraction',
        'notsent_max', 'unacked_max', 'pbb_stream_s', 'pbb_sync_s', 'backup_mode_adder_s', 'parcopy_balance',
        'window', 'high_churn_files', 'leaf_requests', 'leaf_waits_over_1ms', 'connect_s',
        *(f'{wait}{part}_s' for wait in WAITS for part in ('', '_busy', '_idle')), 'page_send_s', 'hello_wait_s',
        'recv_wait_s', 'publish_s', 'receiver_wall_s', 'drain_s', 'idle_waits_s', 'fixed_rtts_s', 'sending_s',
        'busy_split_valid', 'prefetch_bytes', 'prefetch_s', 'tls_version', 'tls_suite', 'tls_connections',
        'window_limited_bound', 'parcopy_connect_s', 'run_limit_s', 'error', 'failure_kind', 'failure_reason']
CELL_COLUMNS = ('total_s', 'trig_s', 'wire_s2d_bytes', 'page_bytes', 'cpu_source_s', 'cpu_dest_s', 'connect_s',
                *(f'{wait}{part}_s' for wait in WAITS for part in ('', '_busy', '_idle')), 'page_send_s',
                'publish_s', 'drain_s', 'idle_waits_s', 'fixed_rtts_s', 'sending_s', 'leaf_requests', 'minrtt_ms',
                'limited_fraction', 'netem_own_drops', 'tbf_drops_source', 'retrans_segs_source', 'prefetch_bytes',
                'prefetch_s', 'pbb_sync_s', 'dirty_increase_bytes')


# --- small helpers ------------------------------------------------------------

def condition(label: str) -> dict:
    """A condition label's parts: rate (Gbit/s), r (ms, and s), host setting."""
    rate, rtt, host = label.split(':')
    return {'label': label, 'gbit': float(rate), 'rtt_ms': float(rtt), 'rtt_s': float(rtt) / 1000, 'host': host}


def label_of(gbit, rtt_ms, host) -> str:
    return f'{gbit:g}:{rtt_ms:g}:{host}'


def workload_name(pattern, fraction) -> str:
    """hot (1%), uniform1, uniform10: a trial's pattern and its fraction."""
    if pattern is None or fraction is None:
        return str(pattern)
    percent = f'{fraction * 100:g}'
    return 'hot' if pattern == 'hot' and percent == '1' else f'{pattern}{percent}'


def label_key(label: str):
    return (CONDITIONS.index(label), label) if label in CONDITIONS else (len(CONDITIONS), label)


def judge(value, rng):
    """Whether a value lies in a written range (either end may be infinite); None without a value."""
    return None if value is None else rng[0] <= value <= rng[1]


def show(rng, digits=2) -> str:
    return model.fmt_range(rng, digits)


def get_path(value, path):
    """Whether a dotted path exists in a record: 'a.b' keys, 'a[]' every item of a list, '*' every value of a dict."""
    if not path:
        return True
    head, _, rest = path.partition('.')
    many = head.endswith('[]')
    key = head[:-2] if many else head
    if key == '*':
        branches = list(value.values()) if isinstance(value, dict) else []
    elif isinstance(value, dict) and key in value:
        branches = [value[key]]
    else:
        return False
    if many:
        branches = [item for branch in branches if isinstance(branch, list) for item in branch]
    return any(get_path(branch, rest) for branch in branches)


def utc(text):
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


# --- the smoke table's records (smoke item 22's check: every Record path exists) ---------------

# (item, record kind, path, which runs): 'run' records of the arms named (None: any), at the condition named (None:
# any); 'trial' records; 'run.json'. run_exp04 keeps total_s as totals.wall_seconds.
PGDELTA_TIMERS = ('connect_s', *(f'{wait}{part}_s' for wait in WAITS for part in ('', '_busy', '_idle')),
                  'page_send_s', 'leaf_requests')
SMOKE_RECORDS = (
    [(1, 'trial', f'visits[].{path}', None) for path in ('tree', 'bands.before_pings', 'bands.after_pings',
                                                         'bands.after_noops')]
    + [(2, 'trial', 'visits[].link_tests[].steady_gbit', None), (2, 'run', 'totals.wall_seconds', None),
       (3, 'trial', 'visits[].pings', None), (3, 'trial', 'visits[].link_tests[].connect_s', None),
       (4, 'run.json', 'control.port', None), (4, 'run.json', 'control.multiplexed', None),
       (4, 'trial', 'visits[].control_noops', None),
       (5, 'trial', 'first_visit.*.link_tests[].qdisc', None),
       (6, 'trial', 'first_visit.*.link_tests[].steady_gbit', None),
       (7, 'run.json', 'environment.*.tcp_sysctls', None), (7, 'run.json', 't12_preflight.tcp_defaults', None),
       (8, 'trial', 'visits[].link_tests[].per_second_gbit', None),
       (9, 'run', 'tcp_metrics', None), (10, 'run', 'sysctl.tcp', None),
       (11, 'run', 'sysctl.tcp', ((), '2:0:tuned')), (11, 'run', 'condition', ((), '2:0:tuned')),
       (12, 'run', 'verification.tls', ((TLS,), None)), (12, 'run', 'stunnel.logs', ((TLS,), None)),
       (12, 'run', 'tool_reported.receiver.publish_s', ((V1, TLS), None)), (12, 'run', 'phases', ((V1, TLS), None)),
       (12, 'run', 'tool_reported.prefetch.bytes', ((V1, TLS), None)),
       (12, 'trial', 'first_visit.*.stunnel_link_test', None)]
    + [(13, 'run', f'tool_reported.{name}', ((V1, W1, W16, HC), None))
       for name in ('window', 'high_churn_node_fraction', 'high_churn_files', 'pages', *PGDELTA_TIMERS)]
    + [(14, 'run', 'ss', None), (14, 'run', 'qdisc.samples', None),
       (15, 'run', 'tool_reported.files.changed', ((V1,), None)),
       (15, 'run', 'tool_reported.leaf_requests', ((V1,), None)),
       (16, 'run', 'processes', (('probackup8',), None)), (16, 'run', 'phases', (('probackup8',), None)),
       (16, 'run', 'totals.wall_seconds', (('probackup8',), None)),
       (17, 'trial', 'first_visit.*.ssh_probe', None),
       (18, 'run', 'durability.limit_bytes', None), (18, 'run', 'durability.noise_window_s', None),
       (18, 'run.json', 'dirty_idle_noise', None), (19, 'run.json', 'syscall_audit', None),
       (20, 'trial', 'reference_digests', None), (20, 'trial', 'reference_classes', None),
       (21, 'run', 'started_utc', None), (21, 'run', 'finished_utc', None), (21, 'run', 'verification', None),
       (21, 'run', 'durability', None), (23, 'run.json', 'teardown', None)])


def smoke_applies(which, record) -> bool:
    if which is None:
        return True
    methods, label = which
    return ((not methods or record.get('method') in methods)
            and (label is None or (record.get('speed') or {}).get('label') == label))


# --- loading -----------------------------------------------------------------

def b_check(row):
    """Check (b) from the summary alone: a full copy's disk writes at least 0.98 x its output's bytes."""
    if row['method'] not in measure.FULL_COPY_METHODS:
        return None
    written, output = row['disk_write_dest_bytes'], row['output_bytes']
    if written is None or output is None:
        return None
    return written >= math.ceil(measure.DISK_WRITE_FRACTION * output)


def from_record(record: dict, row: dict) -> dict:
    """What a run's record adds to its summary row: checks (a) and (b) apart and the run's own limit, parcopy's fsync
    count, an error's kind, the link tests the run's visit carries (the N-stream steady rates, the zero-delay rates),
    pgdelta's page bytes and changed files, the drain on dest's clock where the record has the publication time, and
    the run's start and end."""
    durability = record.get('durability') or {}
    reported = record.get('tool_reported') or {}
    link = record.get('link') or {}
    increase = durability.get('dirty_writeback_increase_bytes')
    if increase is None and row['dirty_increase_mb'] is not None:
        increase = row['dirty_increase_mb'] * 1e6
    limit = durability.get('limit_bytes', row.get('dirty_limit_bytes'))
    fsync_count = durability.get('fsync_count')
    if 'a_ok' in durability and fsync_count is None:
        a_ok = durability['a_ok']
    else:  # durability_check folds parcopy's fsync count into a_ok; (a) here is the increase alone
        a_ok = None if increase is None or limit is None else increase <= limit
    kind = record.get('failure_kind') or row.get('failure_kind')
    if kind is None and record and row.get('status') == 'error':
        kind = 'tool' if any((phase or {}).get('failures') for phase in (record.get('phases') or {}).values()) \
            else 'harness'
    drain = row.get('drain_s')
    published = ((reported.get('receiver') or {}) if isinstance(reported.get('receiver'), dict) else {}).get(
        'published_utc')
    exit_utc = measure.receiver_exit_utc(record)
    if published is not None and exit_utc is not None:
        drain = round(exit_utc - published, 6)
    pages = reported.get('pages') if isinstance(reported.get('pages'), dict) else {}
    files = reported.get('files') if isinstance(reported.get('files'), dict) else {}
    started, finished = utc(record.get('started_utc')), utc(record.get('finished_utc'))
    return {'a_ok': a_ok, 'b_ok': durability['b_ok'] if 'b_ok' in durability else b_check(row),
            'fsync_ok': (fsync_count or {}).get('ok'), 'dirty_increase_bytes': increase, 'dirty_limit_bytes': limit,
            'noise_beyond_table': durability.get('noise_beyond_table'), 'failure_kind': kind,
            'failure_reason': record.get('failure_reason') or row.get('failure_reason'),
            's2d_8_steady_gbit': link.get('source_to_dest_8streams_steady_gbit'),
            's2d_16_steady_gbit': link.get('source_to_dest_16streams_steady_gbit'),
            'zero_delay_bound_gbit': link.get('zero_delay_bound_gbit'),
            'zero_delay_one_gbit': link.get('zero_delay_gbit', row.get('zero_delay_gbit')),
            'page_bytes': pages.get('sent_bytes'), 'files_changed': files.get('changed'), 'drain_s': drain,
            'network_problems': (record.get('network_checks') or {}).get('problems') or [],
            'network_unknown': (record.get('network_checks') or {}).get('unknown') or [],
            'run_wall_s': None if started is None or finished is None else finished - started}


def protocol_parts(row: dict) -> dict:
    """A3's parts of one v1 run, from its timers: idle waits (the idle parts of basis_wait and leaf_wait), fixed round
    trips (connect_s, the idle parts of accept_wait and complete_wait, the drain), sending (page_send_s and the busy
    part of every wait). Each None where a timer is missing (macOS, pipes, a pgdelta without them)."""
    def total(*names):
        values = [row.get(name) for name in names]
        return None if any(value is None for value in values) else sum(values)
    return {'idle_waits_s': total('basis_wait_idle_s', 'leaf_wait_idle_s'),
            'fixed_rtts_s': total('connect_s', 'accept_wait_idle_s', 'complete_wait_idle_s', 'drain_s'),
            'sending_s': total('page_send_s', *(f'{wait}_busy_s' for wait in WAITS))}


def load(sweep):
    """One sweep: (rows, trials, run, the smoke table's paths found). A row per summary line, typed, with what its
    record adds (from_record) and A3's parts (protocol_parts)."""
    sweep = Path(sweep)
    run = json.loads((sweep / 'run.json').read_text()) if (sweep / 'run.json').exists() else {}
    found = {(item, kind, path) for item, kind, path, _ in SMOKE_RECORDS if kind == 'run.json' and get_path(run, path)}
    trials = {}
    for path in sorted((sweep / 'trials').glob('*.json')):
        trial = json.loads(path.read_text())
        trials[(sweep.name, trial['trial_id'])] = trial
        found |= {(item, kind, p) for item, kind, p, _ in SMOKE_RECORDS if kind == 'trial' and get_path(trial, p)}
    rows = []
    with (sweep / 'summary.csv').open(newline='') as f:
        for index, line in enumerate(csv.DictReader(f)):
            row = {'sweep': sweep.name, 'index': index, **{k: line.get(k) or None for k in TEXT},
                   **{k: flag(line.get(k)) for k in FLAGS}, **{k: number(line.get(k)) for k in NUMERIC},
                   'repeat': int(line['repeat'])}
            row['workload'] = workload_name(row['pattern'], row['fraction'])
            slug = measure.parse_condition(row['speed'])['slug'] if row['speed'] and row['speed'].count(':') == 2 \
                else row['speed']
            path = sweep / 'runs' / line['trial_id'] / f"r{line['repeat']}-{slug}-{line['method']}.json"
            record = json.loads(path.read_text()) if path.exists() else {}
            row.update(from_record(record, row))
            row.update(protocol_parts(row))
            c = condition(row['speed']) if row['speed'] and row['speed'].count(':') == 2 else None
            control = row['method'] in measure.PARCOPY_METHODS or row['method'] in V1_ARMS
            # R_trig: an arm launched over the undelayed control path pays one r more as a real trigger.
            row['trig_s'] = (None if row['total_s'] is None or c is None
                             else row['total_s'] + (c['rtt_s'] if control else 0.0))
            found |= {(item, kind, p) for item, kind, p, which in SMOKE_RECORDS
                      if kind == 'run' and smoke_applies(which, record) and get_path(record, p)}
            rows.append(row)
    return rows, trials, run, found


def load_exp11(cells_path):
    """EXP-11's main-block medians at 2 Gbit/s: (pattern, method) -> column -> median."""
    cells = {}
    path = Path(cells_path)
    if not path.exists():
        return cells
    with path.open(newline='') as f:
        for row in csv.DictReader(f):
            if row.get('view') == 'main' and row.get('speed') == '2g':
                cells[(row['pattern'], row['method'])] = {key[:-len('_median')]: number(value)
                                                          for key, value in row.items() if key.endswith('_median')}
    return cells


# --- the session -------------------------------------------------------------

class Session:
    """Every run of the session with its standing under EXP-13's checks, the trial records and run.json."""

    def __init__(self, rows, trials=None, meta=None, found=None):
        self.rows, self.trials, self.meta = rows, trials or {}, meta or []
        self.found = found if found is not None else set()  # the smoke table's paths found in the records
        self._baselines, self._cells = {}, {}
        self.assess()
        self.tcp_default_problems = tcp_default_problems(rows, self.meta)
        self.workloads = [w for w in WORKLOADS if any(r['workload'] == w for r in rows)] + sorted(
            {r['workload'] for r in rows} - set(WORKLOADS))
        self.labels = sorted({r['speed'] for r in rows if r['speed'] and r['speed'].count(':') == 2}, key=label_key)

    def assess(self):
        """A14, then each run's durability and standing; A13's reclassified arms and A15's failed ones."""
        rows = self.rows
        # A14: once full_nosync, the negative control, passes check (a), (a) is void for the session.
        self.a_void = any(r['a_ok'] for r in rows if r['method'] in measure.NON_DURABLE_METHODS)
        for r in rows:
            durable = r['method'] not in measure.NON_DURABLE_METHODS
            checks = {} if self.a_void else {'a': r['a_ok']}
            if r['method'] in measure.FULL_COPY_METHODS:
                checks['b'] = r['b_ok']
            if r['method'] in measure.PARCOPY_METHODS:
                calls, expected = r['parcopy_fsync_calls'], r['parcopy_fsync_expected']
                checks['fsync count'] = (r['fsync_ok'] if r.get('fsync_ok') is not None
                                         else None if calls is None or expected is None else calls == expected)
            r['durability_failures'] = [name for name, ok in checks.items() if durable and ok is False]
            r['durability_unchecked'] = [name for name, ok in checks.items() if durable and ok is None]
            r['void'] = bool(r['void']) or r['status'] == 'void'
            r['a11'] = r['network_ok'] is False
        # A13: more than one failing run of an arm at a condition, over the session: not durable for the session.
        failing = Counter((r['method'], r['speed']) for r in rows if r['durability_failures'])
        self.not_durable = {}
        for (method, label), count in sorted(failing.items(), key=lambda kv: (kv[0][0], label_key(kv[0][1]))):
            if count > 1:
                self.not_durable.setdefault(method, []).append(f'{count} runs at {label}')
        # A15: a parcopy run that failed verify_direct or didn't start; a probackup8 run that didn't start or differed
        # logically while the user tables matched (not void); either's copy its tool aborted.
        self.a15 = {}
        for r in rows:
            if r['method'] not in measure.PARCOPY_METHODS and r['method'] not in measure.PROBACKUP_METHODS:
                continue
            if (r['status'] == 'fail' and not r['void']) or (r['status'] == 'error' and r['failure_kind'] == 'tool'):
                self.a15.setdefault(r['method'], []).append(r)
        self.excluded = {method: 'A13: not durable for the session (' + '; '.join(why) + ')'
                         for method, why in self.not_durable.items()}
        for method, failed in self.a15.items():
            aborted = sum(r['status'] == 'error' for r in failed)
            timeouts = sum(r.get('failure_reason') == 'timeout' for r in failed)
            self.excluded.setdefault(method, f'A15: {len(failed)} run(s) failed '
                                     + ('verify_direct or did not start' if method in measure.PARCOPY_METHODS
                                        else 'to start or the logical digest')
                                     + (f', {aborted} of them a copy its tool aborted' if aborted else '')
                                     + (f' ({timeouts} timed out at 4 times the expected upper bound, at least 60 s; '
                                        'probackup8 240 s)' if timeouts else ''))
        for r in rows:
            r['usable'] = self.counts(r)
            r['left_out'] = '' if r['usable'] else self.why(r)
            # Left out only because a network reading is missing: it counts toward A11 (ii)'s "more than 1".
            r['network_unchecked'] = r['left_out'] == 'network unchecked'

    def counts(self, r) -> bool:
        if r['void'] or r['status'] != 'pass' or not r['verified'] or r['total_s'] is None:
            return False
        if r['durability_failures'] or r['durability_unchecked']:
            return False
        return r['network_ok'] is True

    @staticmethod
    def why(r) -> str:
        if r['void']:
            return 'void'
        if r['status'] != 'pass':
            return f'status {r["status"]}' + (f' ({r["failure_kind"]}' + (f': {r["failure_reason"]}'
                                                                           if r.get('failure_reason') else '') + ')'
                                              if r['status'] == 'error' else '')
        if not r['verified']:
            return 'not verified'
        if r['durability_failures']:
            return 'A13: durability failed: ' + ', '.join(r['durability_failures'])
        if r['durability_unchecked']:
            return 'durability unchecked: ' + ', '.join(r['durability_unchecked'])
        if r['network_ok'] is False:
            return 'A11: ' + '; '.join(r['network_problems'] or ['network checks failed'])
        if r['network_ok'] is None:
            return 'network unchecked'
        return 'no time'

    # Cells.

    def runs(self, method, workload=None, label=None, counting=True):
        return [r for r in self.rows if r['method'] == method and (not counting or r['usable'])
                and (workload is None or r['workload'] == workload) and (label is None or r['speed'] == label)]

    def cell(self, method, label, workload=None) -> dict:
        """One arm's cell at a condition: in its workload's trial (v1's arms), pooled over the trials (a baseline:
        workload None). Its counting runs, median and maximum, how many of its runs A11 (ii) excluded and how many
        were left out with a network reading missing (unchecked), and whether it is measured (at most one run left
        out for either reason, and at least one counting run)."""
        key = (method, label, workload)
        if key not in self._cells:
            every_run = self.runs(method, workload, label, counting=False)
            counting = [r for r in every_run if r['usable']]
            a11 = sum(r['a11'] for r in every_run)
            unchecked = sum(bool(r.get('network_unchecked')) for r in every_run)
            times = [r['total_s'] for r in counting]
            self._cells[key] = {'runs': counting, 'all': every_run, 'n': len(counting), 'a11': a11,
                                'unchecked': unchecked, 'ran': bool(every_run),
                                'measured': bool(counting) and a11 + unchecked <= 1,
                                'median': median(times), 'max': mx(times)}
        return self._cells[key]

    def med(self, method, label, workload=None, col='total_s'):
        return median(r[col] for r in self.cell(method, label, workload)['runs'])

    def measured_med(self, method, label, workload=None):
        """An arm's median at a condition, None where A11 leaves its cell "not measured" (A4-A6 read no such cell)."""
        cell = self.cell(method, label, workload)
        return cell['median'] if cell['measured'] else None

    def top(self, method, label, workload=None, col='total_s'):
        return mx(r[col] for r in self.cell(method, label, workload)['runs'])

    @staticmethod
    def pooled(method) -> bool:
        return method in BASELINE_ARMS or method in measure.NON_DURABLE_METHODS

    def arm_cell(self, method, label, workload=None) -> dict:
        """cell(), pooled for a baseline whatever the workload."""
        return self.cell(method, label, None if self.pooled(method) else workload)

    def trial_of(self, workload):
        """The (sweep, trial id)s of a workload."""
        return sorted({(r['sweep'], r['trial_id']) for r in self.rows if r['workload'] == workload})

    # Baselines.

    def baseline(self, envelope, label) -> dict | None:
        """An envelope's arm at a condition: the lowest pooled median of its candidates' counting runs (ties within
        0.05 s to the lower pooled maximum), the arms A13 or A15 exclude left out. At 1 Gbit/s `full`, where it
        didn't run, stands in with its 2 Gbit/s runs (inferred). measured: see the module's readings."""
        key = (envelope, label)
        if key in self._baselines:
            return self._baselines[key]
        c = condition(label)
        pooled, stand_in = {}, {}
        for arm in ENVELOPES[envelope]:
            if arm in self.excluded:
                continue
            where = label
            if arm == 'full' and c['gbit'] == 1 and not self.cell(arm, label)['ran']:
                where = stand_in[arm] = label_of(2, c['rtt_ms'], c['host'])
            if self.cell(arm, where)['runs']:
                pooled[arm] = where
        chosen = choose({arm: [r['total_s'] for r in self.cell(arm, where)['runs']] for arm, where in pooled.items()})
        if chosen is None:
            self._baselines[key] = None
            return None
        arm = chosen['arm']
        where = pooled[arm]
        measured, why = self.cell(arm, where)['measured'], []
        if not measured:
            why.append(f'{arm} not measured')
        for other in ENVELOPES[envelope]:
            if other == arm or other in self.excluded or other in stand_in:
                continue
            cell = self.cell(other, label)
            if cell['ran'] and not cell['measured'] and (cell['median'] is None
                                                         or cell['median'] <= chosen['median'] + TIE_S + 1e-9):
                measured = False
                why.append(f'{other} not measured and not clearly slower')
        result = {**chosen, 'where': where, 'inferred': arm in stand_in, 'measured': measured, 'why_not': why,
                  'runs': self.cell(arm, where)['runs']}
        self._baselines[key] = result
        return result

    def adder(self):
        """full's backup-mode adder (checkpoint wait plus WAL end to sync), pooled at the default anchor (R_adj)."""
        return self.med('full', ANCHOR['default'], col='backup_mode_adder_s')

    def ratios(self, workload, label, v1=V1) -> dict:
        """v1's median at a condition over each envelope's pooled median: R (median), max (maxima), trial (over the
        arm's runs in v1's own trial), trig (one r added to each arm launched over the control path), adj (full's
        backup-mode adder at the default anchor added to v1, over a live-source arm)."""
        c = condition(label)
        cell = self.cell(v1, label, workload)
        trials = set(self.trial_of(workload))
        out = {'v1': cell['median'], 'v1_max': cell['max'], 'n_v1': cell['n'], 'v1_measured': cell['measured'],
               'v1_a11': cell['a11']}
        adder = self.adder()
        for envelope in ENVELOPES:
            base = self.baseline(envelope, label)
            if base is None:
                out[envelope] = None
                continue
            arm = base['arm']
            same = [r['total_s'] for r in base['runs'] if (r['sweep'], r['trial_id']) in trials]
            trig = c['rtt_s'] if arm in measure.PARCOPY_METHODS else 0.0
            live = arm not in measure.SOURCE_STOP_METHODS
            v1_med, v1_max = cell['median'], cell['max']
            out[envelope] = {
                'arm': arm, 'n': base['n'], 'base': base['median'], 'base_max': base['max'], 'inferred': base['inferred'],
                'base_measured': base['measured'], 'measured': base['measured'] and cell['measured'],
                'median': ratio(v1_med, base['median']), 'max': ratio(v1_max, base['max']),
                'trial': ratio(v1_med, median(same)), 'n_trial': len(same),
                'trig': (None if v1_med is None or base['median'] is None
                         else ratio(v1_med + c['rtt_s'], base['median'] + trig)),
                'adj': (None if v1_med is None or (live and adder is None)
                        else ratio(v1_med + (adder if live else 0.0), base['median']))}
        return out

    # Added by latency.

    def delta(self, method, label, workload=None, anchor=None, strict=False, measured=False):
        """(Δ, the anchor it is from): an arm's median at a condition less its median at the anchor of the same host
        setting (pooled for a baseline). Where the arm has no counting run at that anchor, the default one (the table
        of Δs, labelled), or with strict none: R1, L1, L2, A3 and E6 read the anchor of the same host setting or
        nothing. With measured, None where A11 leaves the cell at r or at the anchor "not measured"."""
        c = condition(label)
        anchor = anchor or ANCHOR.get(c['host'], ANCHOR['default'])
        if not self.arm_cell(method, anchor, workload)['runs'] and not strict:
            anchor = ANCHOR['default']
        here, base = self.arm_cell(method, label, workload), self.arm_cell(method, anchor, workload)
        if measured and not (here['measured'] and base['measured']):
            return None, anchor
        here, base = here['median'], base['median']
        return (None if here is None or base is None else here - base), anchor

    def delta_measured(self, method, label, workload=None) -> bool:
        """Whether an arm's cells at a condition and at its host setting's anchor are both measured (A11)."""
        anchor = ANCHOR.get(condition(label)['host'], ANCHOR['default'])
        return self.arm_cell(method, label, workload)['measured'] and self.arm_cell(method, anchor, workload)['measured']

    # The v1_tls reading (A6).

    def a6_cell(self, workload, label) -> dict | None:
        """v1_tls against v1 in one cell: measured, and priced (v1_tls's extra prefetched bytes over v1's, never
        negative, at v1's own prefetch rate there, added to v1_tls); the line max(0.3 s, 5% of v1)."""
        v1, tls = self.cell(V1, label, workload), self.cell(TLS, label, workload)
        if v1['median'] is None or tls['median'] is None or not (v1['measured'] and tls['measured']):
            return None
        extra = max(0.0, (median(r['prefetch_bytes'] for r in tls['runs']) or 0.0)
                    - (median(r['prefetch_bytes'] for r in v1['runs']) or 0.0))
        rate = median(r['prefetch_bytes'] / r['prefetch_s'] for r in v1['runs']
                      if r['prefetch_bytes'] is not None and r['prefetch_s'])
        priced = tls['median'] + (extra / rate if rate else 0.0)
        line = max(A6_FLOOR_S, A6_SHARE * v1['median'])
        measured, priced_d = tls['median'] - v1['median'], priced - v1['median']
        return {'v1': v1['median'], 'tls': tls['median'], 'priced': priced, 'extra_bytes': extra, 'rate': rate,
                'line': line, 'measured_d': measured, 'priced_d': priced_d, 'measured_fires': measured > line,
                'priced_fires': priced_d > line, 'reading': max(tls['median'], priced), 'n': tls['n'],
                'measured_cell': tls['measured'] and v1['measured']}

    # Link tests and probes, from the trial records.

    def visits(self, label=None):
        """(trial key, visit) for every visit (to a condition)."""
        return [(key, visit) for key, trial in sorted(self.trials.items()) for visit in trial.get('visits') or ()
                if label is None or visit.get('condition') == label]

    def one_stream(self, label):
        """The visits' one-stream steady rates at a condition (each visit's first link test)."""
        rates = []
        for _, visit in self.visits(label):
            test = next(iter(visit.get('link_tests') or ()), None) or {}
            if test.get('steady_gbit') is not None:
                rates.append(test['steady_gbit'])
        return rates

    def first_visit(self, label):
        """Each trial's first-visit probes at a condition."""
        return [(key, (trial.get('first_visit') or {}).get(label)) for key, trial in sorted(self.trials.items())
                if (trial.get('first_visit') or {}).get(label)]

    def first_one_stream(self, key, label):
        """A trial's first visit to a condition: its one-stream steady rate."""
        visit = next((v for k, v in self.visits(label) if k == key), None) or {}
        return (next(iter(visit.get('link_tests') or ()), None) or {}).get('steady_gbit')

    def first_link(self, label, streams, direction='source_to_dest'):
        return [test.get('steady_gbit') for _, first in self.first_visit(label) for test in first.get('link_tests') or ()
                if test.get('streams') == streams and test.get('direction') == direction
                and test.get('steady_gbit') is not None]

    def anchor_one_stream(self, key):
        """A trial's default anchor one-stream rate (A12's reference): the median of its visits there."""
        rates = [(next(iter(visit.get('link_tests') or ()), None) or {}).get('steady_gbit')
                 for visit in (self.trials.get(key) or {}).get('visits') or ()
                 if visit.get('condition') == ANCHOR['default']]
        return median(rates)


# --- the bound (A9) ---------------------------------------------------------------

def bound_gate(S, label) -> dict:
    """A9 at a condition: each parcopy arm run there (pooled), against (its output bytes at its stream count's
    steady rate) + 1.0 s + 6 r, and its busiest stream against 1.1 x the mean; whether B_fast's arm there is a parcopy
    arm whose stream count's link test is under 0.9 x the zero-delay rate at the same tbf rate (window-limited); and
    whether a durable parcopy arm is left."""
    c = condition(label)
    arms = {}
    for arm in measure.PARCOPY_METHODS:
        cell = S.cell(arm, label)
        if not cell['ran']:
            continue
        streams = measure.PARCOPY_VARIANTS[arm]['streams']
        key = 's2d_8_steady_gbit' if streams == 8 else 's2d_16_steady_gbit' if streams == 16 else None
        runs = cell['runs'] or cell['all']
        rate = median(r[key] for r in runs) if key else None
        if streams == 8 and rate is None:
            rate = median(r['measured_s2d_gbit_8streams'] for r in runs)
        nbytes = median(r['wire_s2d_bytes'] for r in runs) or model.COPY_BYTES
        limit = None if not rate else nbytes * 8 / (rate * 1e9) + A9_SLACK_S + A9_RTTS * c['rtt_s']
        balance = mx(r['parcopy_balance'] for r in runs)
        slow = None if limit is None or cell['median'] is None else cell['median'] > limit
        unbalanced = None if balance is None else balance > A9_BALANCE
        zero = median(r['zero_delay_bound_gbit'] for r in runs)
        arms[arm] = {'median': cell['median'], 'n': cell['n'], 'rate': rate, 'limit': limit, 'balance': balance,
                     'connect_s': median(r.get('parcopy_connect_s') for r in runs),
                     'slow': slow, 'unbalanced': unbalanced, 'suspect': bool(slow or unbalanced),
                     'excluded': S.excluded.get(arm), 'zero_delay': zero,
                     'window_limited': None if not rate or not zero else rate < A9_WINDOW * zero}
    fast = S.baseline('B_fast', label)
    chosen = (fast or {}).get('arm')
    reasons = []
    if any(a['suspect'] for arm, a in arms.items() if arm not in S.excluded):
        reasons.append('bound suspect')
    if chosen in arms and arms[chosen]['window_limited']:
        reasons.append('window-limited bound')
    if arms and not any(arm not in S.excluded for arm in arms):
        reasons.append('bound not durable' if any(arm in S.not_durable for arm in arms) else 'bound excluded (A15)')
    return {'label': label, 'arms': arms, 'b_fast': chosen, 'reasons': reasons}


# --- the DR verdict -----------------------------------------------------------

def dr_row(values, design):
    """The DR verdict's row at one r from its deciding R_fast values, checked in order (Not judged, rows 1-5).

    values: host -> workload -> R_fast, None where the cell is "not measured" (v1's or B_fast's); design: the host
    settings the design runs at r. Returns (row, notes): 'tuned not measured' where a tuned deciding cell is missing
    (row 2 isn't judged; rows 3 and 4 read on)."""
    notes = []

    def get(host, workload):
        return (values.get(host) or {}).get(workload)

    def measured(host, workloads=DECIDING):
        return all(get(host, w) is not None for w in workloads)

    def le(host, workload):
        return get(host, workload) is not None and get(host, workload) <= HOLD

    def ge(host, workload):
        return get(host, workload) is not None and get(host, workload) >= OUT
    if not measured('default'):
        return ROWS[1], notes
    if all(le('default', w) for w in DECIDING):
        return ROWS[2], notes
    tuned = 'tuned' in design
    if tuned and not measured('tuned'):
        notes.append('tuned not measured')
    elif tuned and all(le('tuned', w) for w in DECIDING):
        return ROWS[3], notes
    if (all(measured(h) for h in design) and any(le(h, 'hot') for h in design)
            and all(ge(h, 'uniform1') for h in design)):
        return ROWS[4], notes
    if all(ge(h, 'hot') for h in design if get(h, 'hot') is not None):
        return ROWS[5], notes
    return ROWS[6], notes


def flip(value, line):
    """The value just across a line from where it is (rows read ≤ 0.90 and ≥ 1.00)."""
    if line == HOLD:
        return line + 1e-6 if value <= line else line
    return line - 1e-6 if value >= line else line


def near_line(values, design, margin=NEAR):
    """The deciding ratios within ±0.03 of 0.90 or 1.00 whose crossing, alone or with other such ratios, would change
    the row (each one needed in some such set of crossings), as EXP-11 flagged them."""
    base = dr_row(values, design)[0]
    near = [(h, w, v, line) for h in design for w in DECIDING for line in (HOLD, OUT)
            if (v := (values.get(h) or {}).get(w)) is not None and abs(v - line) <= margin + 1e-9]

    def changes(crossed):
        moved = {h: dict(values.get(h) or {}) for h in values}
        for h, w, v, line in crossed:
            moved.setdefault(h, {})[w] = flip(v, line)
        return dr_row(moved, design)[0] != base
    needed = set()
    for size in range(1, len(near) + 1):
        for crossed in itertools.combinations(range(len(near)), size):
            if changes([near[i] for i in crossed]):
                needed.update(i for i in crossed if not changes([near[j] for j in crossed if j != i]))
    return [near[i] for i in sorted(needed)]


def r1_reading(S) -> dict:
    """R1 (the ledger's rule, change 4): Δ_v1 > Δ_full at any r of 20, 60 and 120 ms at 2 Gbit/s, on either host
    setting, in any workload, each Δ from its own host setting's anchor (none where that anchor has no counting run).
    Every cell with counting runs is judged; one whose cells at r or at the anchor A11 leaves "not measured" is flagged
    (a rule 8 change: a rule must be able to fire)."""
    cells = []
    for host in ('default', 'tuned'):
        for rtt in RTTS:
            label = label_of(2, rtt, host)
            full, full_anchor = S.delta('full', label, strict=True)
            for workload in S.workloads:
                v1, v1_anchor = S.delta(V1, label, workload, strict=True)
                if v1 is None or full is None:
                    continue
                measured = S.delta_measured(V1, label, workload) and S.delta_measured('full', label)
                cells.append({'label': label, 'workload': workload, 'v1': v1, 'full': full, 'fires': v1 > full,
                              'measured': measured, 'anchors': (v1_anchor, full_anchor)})
    return {'fires': any(c['fires'] for c in cells), 'cells': cells,
            'judged': bool(cells)}


def dr_values(S, rtt, view='median', tls=False):
    """host -> workload -> the deciding ratio at r (2 Gbit/s), None where v1's or B_fast's cell is not measured; with
    tls, R_tls (A6's reading) in place of v1's tuned ratio where v1_tls ran."""
    values = {}
    for host in DESIGN[rtt]:
        label = label_of(2, rtt, host)
        values[host] = {}
        for workload in DECIDING:
            r = S.ratios(workload, label).get('B_fast') or {}
            value = r.get(view) if r.get('measured') else None
            if tls and host == 'tuned':
                cell = S.a6_cell(workload, label)
                if cell is not None and value is not None and r.get('base'):
                    value = cell['reading'] / r['base']
            values[host][workload] = value
    return values


def dr_verdict(S, rtt, r1=None, gates=None, a12=None, a17=None) -> dict:
    """The DR verdict at r: its row (v1 not durable first, then dr_row), the computed row, the flags (near the line,
    fragile, TLS-sensitive at 120 ms, pooled baseline unstable, tuned not measured, emulator-limited, provisional) and
    the qualifier."""
    r1 = r1 if r1 is not None else r1_reading(S)
    design = DESIGN[rtt]
    values = dr_values(S, rtt)
    computed, notes = dr_row(values, design)
    row = ROWS[0] if V1 in S.not_durable else computed
    provisional = []
    if r1['fires'] and computed in CLAIM_ROWS:
        provisional.append('R1 fired')
    labels = [label_of(2, rtt, host) for host in design]
    gates = gates or {label: bound_gate(S, label) for label in labels}
    bound = sorted({reason for label in labels for reason in (gates.get(label) or {}).get('reasons', ())})
    if computed in BOUND_ROWS:
        provisional += bound
    flags = {'near': near_line(values, design), 'fragile': [], 'unread': [], 'tls_sensitive': None, 'notes': notes,
             'unstable': [], 'emulator_limited': []}
    for view, name in (('max', 'R_max'), ('trial', 'R_trial'), ('trig', 'R_trig')):
        seen = dr_values(S, rtt, view)
        if any((seen[h][w] is None) != (values[h][w] is None) for h in design for w in DECIDING):
            flags['unread'].append(name)  # e.g. R_trial without the baseline's runs in v1's trial: no row
            continue
        other = dr_row(seen, design)[0]
        if other != computed:
            flags['fragile'].append((name, other))
    if rtt == 120:
        flags['tls_sensitive'] = dr_row(dr_values(S, rtt, tls=True), design)[0]
    a17 = a17 if a17 is not None else a17_spread(S)
    flags['unstable'] = [item for item in a17 if item['label'] in labels and item['unstable']]
    if a12 is not None and computed not in (ROWS[1], ROWS[2], ROWS[3]):
        flags['emulator_limited'] = [item for item in a12 if item['label'] in labels and item['limited']]
    # The qualifier reads only measured cells (v1's and F0's, A11): a ratio from a cell not measured is None.
    f0 = {}
    for w in DECIDING:
        r = S.ratios(w, label_of(2, rtt, 'default')).get('F0') or {}
        f0[w] = r.get('median') if r.get('measured') else None
    qualifier = all(v is not None and v <= HOLD for v in f0.values())
    probackup = None
    if rtt == 60:
        here = label_of(2, rtt, 'default')
        base = S.cell('probackup8', here)
        values_p = {w: ratio(S.cell(V1, here, w)['median'], base['median'])
                    if S.cell(V1, here, w)['measured'] and base['measured'] else None for w in DECIDING}
        probackup = all(v is not None and v <= HOLD for v in values_p.values()) and 'probackup8' not in S.excluded
    return {'rtt': rtt, 'row': row, 'computed': computed, 'values': values, 'provisional': provisional,
            'flags': flags, 'qualifier': qualifier, 'f0': f0, 'probackup': probackup, 'bound': bound}


# --- the added rules -------------------------------------------------------------

def a3_parts(S, workload, label) -> dict | None:
    """A3 in one cell: v1's Δ split into idle waits, fixed round trips and sending (each its median at r less its
    median at the anchor of the same host setting) and the residual; the largest part names the fix."""
    delta, anchor = S.delta(V1, label, workload, strict=True)
    if delta is None:
        return None
    parts = {}
    for name in ('idle_waits_s', 'fixed_rtts_s', 'sending_s'):
        here, base = S.med(V1, label, workload, name), S.med(V1, anchor, workload, name)
        parts[name] = None if here is None or base is None else here - base
    known = [value for value in parts.values() if value is not None]
    residual = None if len(known) < len(parts) else delta - sum(known)
    largest = None
    if residual is not None:
        candidates = {**parts, 'residual': residual}
        largest = max(candidates, key=lambda k: candidates[k])
    return {'delta': delta, 'anchor': anchor, **parts, 'residual': residual, 'largest': largest}


A3_FIX = {'idle_waits_s': 'idle waits: deeper pipelining (A4 says whether a window of 16 is enough; if not, leaf '
                          'requests batched across files, or leaf digests sent unasked, go on T8\'s protocol list)',
          'fixed_rtts_s': 'fixed round trips: nothing to pipeline; the DR claim is narrowed by that much',
          'sending_s': "sending: the transport (A2's fix), not pipelining",
          'residual': 'the residual: the timers don\'t show where the time went, and no fix is named'}


def a4_reading(S) -> dict:
    """A4 at 120 ms tuned, hot and uniform 1%: (i) v1_w16 at least 0.3 s faster than v1 in either workload and at most
    0.1 s slower in the other; (ii) v1_w1 within 0.1 s of v1 in both; (iii) otherwise."""
    d1 = {w: None for w in DECIDING}
    d16 = dict(d1)
    for w in DECIDING:
        v1 = S.measured_med(V1, SIDE_CELL, w)
        for arm, store in ((W1, d1), (W16, d16)):
            value = S.measured_med(arm, SIDE_CELL, w)
            store[w] = None if value is None or v1 is None else value - v1
    if any(v is None for v in (*d1.values(), *d16.values())):
        part = None
    elif any(d16[w] <= -A4_GAIN_S + 1e-9 and d16[o] <= A4_COST_S + 1e-9 for w, o in (DECIDING, DECIDING[::-1])):
        part = '(i)'
    elif all(abs(d1[w]) <= A4_SAME_S + 1e-9 for w in DECIDING):
        part = '(ii)'
    else:
        part = '(iii)'
    return {'part': part, 'w1': d1, 'w16': d16}


def a5_reading(S) -> dict:
    """A5 at 120 ms tuned: v1_hc's saving in uniform 10% (v1 − v1_hc) and its cost in uniform 1% (v1_hc − v1)."""
    def diff(w):
        v1, hc = S.measured_med(V1, SIDE_CELL, w), S.measured_med(HC, SIDE_CELL, w)
        return None if v1 is None or hc is None else hc - v1
    cost, saving = diff('uniform1'), diff('uniform10')
    saving = None if saving is None else -saving
    if cost is None or saving is None:
        part = None
    elif saving >= A5_SAVE_S - 1e-9 and cost <= A5_COST_S + 1e-9:
        part = '(i)'
    elif saving >= A5_SAVE_S - 1e-9:
        part = '(ii)'
    else:
        part = '(iii)'
    return {'part': part, 'cost_uniform1': cost, 'saving_uniform10': saving}


def a6_reading(S) -> dict:
    cells = {(w, label): S.a6_cell(w, label) for label in TLS_CELLS for w in DECIDING}
    judged = {key: c for key, c in cells.items() if c is not None}
    measured = any(c['measured_fires'] for c in judged.values())
    priced = any(c['priced_fires'] for c in judged.values())
    return {'cells': cells, 'fires': (measured or priced) if judged else None, 'measured': measured, 'priced': priced,
            'differ': measured != priced, 'at_anchor': any(c['measured_fires'] or c['priced_fires']
                                                           for (w, label), c in judged.items() if label == TLS_CELLS[0])}


def ssh_probes(S, label):
    """(probe Gbit/s, the same trial's first visit one-stream rate) for each trial's SSH probe at a condition."""
    out = []
    for key, first in S.first_visit(label):
        probe = first.get('ssh_probe') or {}
        if probe.get('last_gbit') is not None:
            out.append((probe['last_gbit'], S.first_one_stream(key, label)))
    return out


def a7_reading(S) -> dict:
    shares = {}
    for label in ('2:20:default', '2:60:default', '2:120:default', '2:60:tuned', '2:120:tuned'):
        probes = ssh_probes(S, label)
        probe, one = median(p for p, _ in probes), median(o for _, o in probes)
        shares[label] = {'probe': probe, 'one': one, 'share': ratio(probe, one)}
    tuned = [shares[label]['share'] for label in ('2:60:tuned', '2:120:tuned')]
    fires = None if any(s is None for s in tuned) else all(s < A7_SHARE for s in tuned)
    return {'shares': shares, 'fires': fires}


def a10_gates(S, exp11) -> list[dict]:
    """A10: this session's default anchor against EXP-11's 2 Gbit/s cells (exp11-cells.csv), per workload EXP-11 ran:
    `full` and `parcopy8` (pooled here) within ±5%, v1 within ±10%, the one-stream rate within ±2%, `full`'s and
    v1's wire bytes within ±1%."""
    anchor = ANCHOR['default']
    gates = []
    for workload in S.workloads:
        pattern = EXP11_PATTERN.get(workload)
        if pattern is None:
            continue
        rows = [r for r in S.rows if r['speed'] == anchor and r['workload'] == workload]
        link_here = median(r['measured_s2d_gbit'] for r in rows)
        link_there = median((cells.get('measured_s2d_gbit')) for (p, _), cells in exp11.items() if p == pattern)
        items = (('full total_s', S.med('full', anchor), (exp11.get((pattern, 'full')) or {}).get('total_s'),
                  A10_GATES['full']),
                 ('parcopy8 total_s', S.med('parcopy8', anchor), (exp11.get((pattern, 'parcopy8')) or {}).get('total_s'),
                  A10_GATES['parcopy8']),
                 ('v1 total_s', S.med(V1, anchor, workload), (exp11.get((pattern, V1)) or {}).get('total_s'),
                  A10_GATES[V1]),
                 ('one stream, Gbit/s', link_here, link_there, A10_GATES['link']),
                 ('full wire bytes', S.med('full', anchor, col='wire_s2d_bytes'),
                  (exp11.get((pattern, 'full')) or {}).get('wire_s2d_bytes'), A10_GATES['wire']),
                 ('v1 wire bytes', S.med(V1, anchor, workload, 'wire_s2d_bytes'),
                  (exp11.get((pattern, V1)) or {}).get('wire_s2d_bytes'), A10_GATES['wire']))
        for name, here, there, tolerance in items:
            change = None if here is None or not there else here / there - 1
            gates.append({'workload': workload, 'gate': name, 'here': here, 'exp11': there, 'change': change,
                          'tolerance': tolerance, 'ok': None if change is None else abs(change) <= tolerance + 1e-12})
    return gates


def shifted(gates) -> bool:
    oks = [g['ok'] for g in gates if g['ok'] is not None]
    return not oks or not all(oks)


def a12_reading(S) -> list[dict]:
    """A12: at each trial's first visit to a tuned condition, its one-stream steady rate against 0.9 x that trial's
    default anchor rate."""
    out = []
    for key, trial in sorted(S.trials.items()):
        reference = S.anchor_one_stream(key)
        seen = set()
        for visit in trial.get('visits') or ():
            label = visit.get('condition') or ''
            if label in seen or not label.endswith(':tuned') or condition(label)['rtt_ms'] == 0:
                continue
            seen.add(label)
            rate = (next(iter(visit.get('link_tests') or ()), None) or {}).get('steady_gbit')
            out.append({'trial': key[1], 'label': label, 'rate': rate, 'reference': reference,
                        'limited': None if rate is None or reference is None else rate < A12_SHARE * reference})
    return out


def a16_reading(S) -> dict:
    rates = {label: median(S.one_stream(label)) for label in ('1:60:default', '1:120:default')}
    measured = [v for v in rates.values() if v is not None]
    skip = None if len(measured) < len(rates) else all(v <= A16_LIMIT_GBIT for v in measured)
    ran = {label: S.cell('full', label)['ran'] for label in rates}
    return {'rates': rates, 'skip': skip, 'ran': ran,
            'consistent': None if skip is None else (not any(ran.values())) == skip}


def a17_spread(S) -> list[dict]:
    """A17 (i): a baseline's per-trial medians at a condition more than 3% apart."""
    out = []
    for arm in BASELINE_ARMS:
        for label in S.labels:
            per_trial = {}
            for r in S.cell(arm, label)['runs']:
                per_trial.setdefault((r['sweep'], r['trial_id']), []).append(r['total_s'])
            medians = [statistics.median(v) for v in per_trial.values()]
            if len(medians) < 2:
                continue
            spread = max(medians) / min(medians) - 1
            out.append({'arm': arm, 'label': label, 'medians': medians, 'spread': spread,
                        'unstable': spread > A17_SPREAD + 1e-12})
    return out


def a17_anchors(S) -> list[dict]:
    """A17 (ii): the tuned anchor against the default anchor, v1 per workload and `full`."""
    out = []
    for arm, workload in [(V1, w) for w in S.workloads] + [('full', None)]:
        tuned, default = S.arm_cell(arm, ANCHOR['tuned'], workload)['median'], \
            S.arm_cell(arm, ANCHOR['default'], workload)['median']
        if tuned is None or default is None:
            continue
        out.append({'arm': arm, 'workload': workload, 'gap': tuned - default,
                    'reported': abs(tuned - default) > A17_ANCHOR_S + 1e-12})
    return out


# --- crossovers ---------------------------------------------------------------

def crossover(S, workload, rtt, host='default') -> dict:
    """D78's crossover where 1 and 2 Gbit/s both ran (analysis/exp04.crossover against B_fast; x the trial's zero-delay
    one-stream rate at each tbf rate): the crossing, "below 1" or "above 2"; where only 2 Gbit/s ran, "above 2" if
    R_fast < 1.00 there, "below 2" otherwise. A window-limited bound at 1 Gbit/s labels it (A9)."""
    two, one = label_of(2, rtt, host), label_of(1, rtt, host)
    r2 = (S.ratios(workload, two).get('B_fast') or {}).get('median')
    table = {}
    for label in (one, two):
        cell = S.cell(V1, label, workload)
        base = S.baseline('B_fast', label)
        x = median(r['zero_delay_one_gbit'] for r in cell['all'])
        if x and cell['median'] is not None and base is not None:
            table[(workload, label)] = {'speed_gbit': x, V1: {'total_s': cell['median']},
                                        'B_fast': {'total_s': base['median'], 'arm': base['arm']}}
    labels = []
    if (workload, one) in table and 'window-limited bound' in bound_gate(S, one)['reasons']:
        labels.append('window-limited bound')
    if (workload, one) in table and (workload, two) in table:
        value, how = exp04.crossover(table, workload, V1, baseline='B_fast')
        if how == 'crosses':
            text, rng = f'{value:.2f} Gbit/s', (value, value)
        elif how.startswith('faster'):
            text, rng = 'above 2', (2.0, math.inf)
        elif how.startswith('slower'):
            text, rng = 'below 1', (0.0, 0.0)
        else:
            text, rng = how, None
    elif r2 is not None:
        text, rng = ('above 2', (2.0, math.inf)) if r2 < OUT else ('below 2', (0.0, 2.0))
    else:
        text, rng = 'no data', None
    return {'workload': workload, 'rtt': rtt, 'host': host, 'text': text, 'range': rng, 'labels': labels, 'r2': r2,
            'table': table}


# --- the report ----------------------------------------------------------------

def counts_lines(S):
    lines = ['## Runs']
    statuses = Counter(r['status'] for r in S.rows)
    lines.append(f'- {len(S.rows)} runs; status ' + ', '.join(f'{k} {n}' for k, n in sorted(statuses.items())))
    left = Counter(r['left_out'].split(':')[0] for r in S.rows if r['left_out'])
    lines.append('- left out of every median: ' + (', '.join(f'{k} {n}' for k, n in sorted(left.items())) or 'none'))
    for r in S.rows:
        if r['status'] == 'error':
            kind = {'tool': 'the tool', 'harness': 'the harness'}.get(r['failure_kind'], 'cause not recorded')
            lines.append(f'  - error ({kind}): {r["trial_id"]} r{r["repeat"]} {r["speed"]} {r["method"]}: '
                         f'{r["error"] or ""}')
    return lines


def network_run_lines(S):
    """A11 (ii): every run's network checks, its exclusions by arm and condition, and the cells not measured."""
    lines = ['## Network validity of the runs (A11 (ii); E2)']
    a11 = [r for r in S.rows if r['a11']]
    unchecked = [r for r in S.rows if r['network_ok'] is None]
    lines.append(f'- {len(a11)} run(s) excluded by the network checks; {len(unchecked)} unchecked (a reading missing)')
    for r in a11:
        lines.append(f'  - {r["trial_id"]} r{r["repeat"]} {r["speed"]} {r["method"]}: '
                     + '; '.join(r['network_problems']))
    for r in unchecked[:20]:
        lines.append(f'  - unchecked: {r["trial_id"]} r{r["repeat"]} {r["speed"]} {r["method"]}: '
                     + ', '.join(r['network_unknown'] or ['no network checks recorded']))
    missing = []
    for label in S.labels:
        for arm in ARM_NAMES:
            if S.pooled(arm):
                cell = S.cell(arm, label)
                if cell['ran'] and not cell['measured']:
                    missing.append(f'{arm} at {label} (pooled; {cell["a11"]} excluded, {cell["unchecked"]} unchecked, '
                                   f'{cell["n"]} counting)')
            else:
                for w in S.workloads:
                    cell = S.cell(arm, label, w)
                    if cell['ran'] and not cell['measured']:
                        missing.append(f'{arm} {w} at {label} ({cell["a11"]} excluded, {cell["unchecked"]} unchecked, '
                                       f'{cell["n"]} counting)')
    lines.append('- cells "not measured" (more than one run left out for a network reason, excluded by A11 or '
                 'unchecked, or none counting): ' + ('; '.join(missing) or 'none'))
    lines.append("- the default host setting, Debian's TCP settings on both VMs (each invocation's "
                 "t12_preflight.tcp_defaults and teardown, every default run's tcp_rmem and tcp_wmem maxima): "
                 + ('**differs: the session is flagged** (' + '; '.join(S.tcp_default_problems[:10])
                    + (f'; and {len(S.tcp_default_problems) - 10} more' if len(S.tcp_default_problems) > 10 else '')
                    + ')' if S.tcp_default_problems else 'held'))
    return lines


def tcp_default_problems(rows, meta) -> list[str]:
    """Where the session's default host setting wasn't Debian's (measure.DEBIAN_TCP_MEM): each invocation's
    t12_preflight.tcp_defaults on both VMs, its teardown (measure.teardown_ok), and every default run's recorded
    tcp_rmem and tcp_wmem maxima on both VMs."""
    problems = []
    for run in meta:
        name = run.get('name') or 'an invocation'
        defaults = (run.get('t12_preflight') or {}).get('tcp_defaults') or {}
        for vm, values in sorted(defaults.items()):
            problems += [f'{name} {vm}: {problem}' for problem in measure.tcp_defaults_problems(values)]
        if run.get('t12_preflight') and 'teardown' in run and not measure.teardown_ok(run['teardown']):
            problems.append(f'{name}: its teardown did not leave both VMs reset ({run["teardown"]})')
    wanted = {'rmem': measure.DEBIAN_TCP_MEM['net.ipv4.tcp_rmem'][2],
              'wmem': measure.DEBIAN_TCP_MEM['net.ipv4.tcp_wmem'][2]}
    for r in rows:
        if r.get('host_tcp') != 'default':
            continue
        found = [f'tcp_{kind}_max_{vm} {r.get(f"tcp_{kind}_max_{vm}"):.0f}' for kind, value in wanted.items()
                 for vm in ('source', 'dest')
                 if r.get(f'tcp_{kind}_max_{vm}') is not None and r[f'tcp_{kind}_max_{vm}'] != value]
        if found:
            problems.append(f'{r["trial_id"]} r{r["repeat"]} {r["speed"]} {r["method"]}: ' + ', '.join(found))
    return problems


def visit_lines(S):
    """A11 (i): each visit's checks, the retries and any infrastructure fault."""
    lines = ['## Visits (A11 (i))']
    visits = S.visits()
    retried = [(key, v) for key, v in visits if len(v.get('attempts') or ()) > 1]
    failed = [(key, v) for key, v in visits if v.get('ok') is False]
    lines.append(f'- {len(visits)} visits; {len(retried)} needed the reset and a second check; {len(failed)} failed '
                 'twice')
    for key, v in retried:
        first = ((v.get('attempts') or [{}])[0].get('checks') or {}).get('problems') or {}
        lines.append(f'  - {key[1]} r{v.get("repeat")} {v.get("condition")}: '
                     + '; '.join(f'{k}: {", ".join(map(str, p))}' for k, p in first.items() if p))
    for key, trial in sorted(S.trials.items()):
        if trial.get('infrastructure_fault'):
            lines.append(f'- **infrastructure fault** in {key[1]}: {trial["infrastructure_fault"]} (the trial stopped; '
                         'nothing rerun)')
    return lines


def durability_lines(S):
    lines = ['## Durability (checks (a) and (b) in every run; A13, A14; the negative control)']
    limits = sorted({r['dirty_limit_bytes'] for r in S.rows if r['dirty_limit_bytes'] is not None})
    tables = list(dict.fromkeys(json.dumps(run['noise_table_bytes'], sort_keys=True) for run in S.meta
                                if run.get('noise_table_bytes')))
    lines.append(f'- check (a) limits in use: {", ".join(f"{v / (1 << 20):.2f} MiB" for v in limits) or "–"}; the '
                 f'noise tables (window s: bytes): {"; ".join(tables) or "–"}; check (a) '
                 f'{"VOID for the session (A14)" if S.a_void else "in force"}')
    beyond = [r for r in S.rows if r.get('noise_beyond_table')]
    if beyond:
        lines.append(f'- {len(beyond)} run(s) outlasted every noise window and took the longest one\'s, flagged: '
                     + ', '.join(f'{r["speed"]} {r["method"]} r{r["repeat"]}' for r in beyond[:10]))
    lines += ['| Arm | runs | (a) pass / fail / unchecked | (b) pass / fail / unchecked | largest increase, MB | '
              'fsync count wrong | runs failing | excluded |', '|---|' + '---|' * 7]
    for arm in [a for a in ARM_NAMES if any(r['method'] == a for r in S.rows)]:
        runs = [r for r in S.rows if r['method'] == arm]
        a = Counter(r['a_ok'] for r in runs)
        b = Counter(r['b_ok'] for r in runs) if arm in measure.FULL_COPY_METHODS else None
        largest = mx(r['dirty_increase_bytes'] for r in runs)
        wrong = (sum('fsync count' in r['durability_failures'] for r in runs) if arm in measure.PARCOPY_METHODS
                 else 'n/a')
        lines.append(f'| {arm} | {len(runs)} | {a[True]} / {a[False]} / {a[None]} | '
                     + (f'{b[True]} / {b[False]} / {b[None]}' if b else 'n/a') + ' | '
                     f'{fmt(None if largest is None else largest / 1e6, 1)} | {wrong} | '
                     f'{sum(bool(r["durability_failures"]) for r in runs)} | {S.excluded.get(arm, "")} |')
    for r in S.rows:
        if r['durability_failures']:
            lines.append(f'  - {r["trial_id"]} r{r["repeat"]} {r["speed"]} {r["method"]}: '
                         f'{", ".join(r["durability_failures"])} failed; left out')
    return lines


def baseline_lines(S):
    lines = [f'## Baselines: each envelope\'s arm per condition (pooled over the trials; ties within {TIE_S} s go to '
             'the lower pooled maximum)',
             '| Condition | Envelope | arm | pooled median (max), runs | measured | every candidate: median (max, n) |',
             '|---|---|---|---|---|---|']
    for label in S.labels:
        for envelope in ENVELOPES:
            b = S.baseline(envelope, label)
            if b is None:
                continue
            inferred = f' (inferred from {b["where"]})' if b['inferred'] else ''
            lines.append(f'| {label} | {envelope} | {b["arm"]}{inferred} | {fmt(b["median"])} ({fmt(b["max"])}), {b["n"]} | '
                         f'{"yes" if b["measured"] else "no: " + "; ".join(b["why_not"])} | '
                         + ', '.join(f'{arm} {fmt(m)} ({fmt(t)}, {n})'
                                     for arm, (m, t, n) in sorted(b['stats'].items(), key=lambda kv: kv[1])) + ' |')
    for arm, why in sorted(S.excluded.items()):
        lines.append(f'- left out of every baseline: {arm}: {why}')
    voids = [r for r in S.rows if r['void']]
    if voids:
        lines.append(f'- {len(voids)} void run(s) (A15: a user-table file differs between the reference digests): '
                     + ', '.join(f'{r["trial_id"]} r{r["repeat"]} {r["speed"]} {r["method"]}' for r in voids))
    return lines


def median_lines(S):
    lines = ['## Medians, seconds: median (max) and counting runs']
    labels = S.labels
    lines += ['### Baselines (pooled over the trials)', '| Arm | ' + ' | '.join(labels) + ' |',
              '|---|' + '---:|' * len(labels)]
    for arm in [a for a in ARM_NAMES if S.pooled(a)]:
        if any(S.cell(arm, label)['ran'] for label in labels):
            lines.append(f'| {arm} | ' + ' | '.join(
                f'{fmt(c["median"])} ({fmt(c["max"])}), {c["n"]}' if (c := S.cell(arm, label))['ran'] else ''
                for label in labels) + ' |')
    for w in S.workloads:
        lines += [f'### {WORKLOAD_LABEL.get(w, w)}', '| Arm | ' + ' | '.join(labels) + ' |',
                  '|---|' + '---:|' * len(labels)]
        for arm in V1_ARMS:
            if any(S.cell(arm, label, w)['ran'] for label in labels):
                lines.append(f'| {arm} | ' + ' | '.join(
                    f'{fmt(c["median"])} ({fmt(c["max"])}), {c["n"]}' if (c := S.cell(arm, label, w))['ran'] else ''
                    for label in labels) + ' |')
    return lines


def delta_lines(S):
    """Added by latency: each arm's Δ from the anchor of its host setting (tuned from the tuned anchor for v1 and
    `full`, the default anchor's beside), and the effective round trips Δ / r."""
    lines = ['## Added by latency (Δ, s) and effective round trips (Δ / r), 2 Gbit/s',
             '| Arm | workload | condition | Δ (from) | n = Δ / r | Δ from the default anchor (beside) | max at r |',
             '|---|---|---|---:|---:|---:|---:|']
    for label in [label for label in S.labels if condition(label)['gbit'] == 2 and condition(label)['rtt_ms'] > 0]:
        c = condition(label)
        for arm in ARM_NAMES:
            groups = [None] if S.pooled(arm) else S.workloads
            for w in groups:
                if not S.arm_cell(arm, label, w)['runs']:
                    continue
                delta, anchor = S.delta(arm, label, w)
                if delta is None:
                    continue
                beside = S.delta(arm, label, w, ANCHOR['default'])[0] if c['host'] == 'tuned' else None
                labelled = anchor + (' (default: no tuned anchor)' if c['host'] == 'tuned' and anchor == ANCHOR[
                    'default'] else '')
                top = S.arm_cell(arm, label, w)['max']
                lines.append(f'| {arm} | {w or "pooled"} | {label} | {fmt(delta)} ({labelled}) | '
                             f'{fmt(delta / c["rtt_s"], 1)} | {fmt(beside)} | {fmt(top)} |')
    return lines


def ratio_lines(S):
    lines = ["## Ratios: v1's median (5 runs, its trial) over the baseline's pooled median",
             '| Workload | condition | envelope | arm (runs) | R | R_max | R_trial (runs) | R_trig | R_adj | measured |',
             '|---|---|---|---|---:|---:|---:|---:|---:|---|']
    for w in S.workloads:
        for label in S.labels:
            r = S.ratios(w, label)
            if r['v1'] is None:
                continue
            for envelope in ENVELOPES:
                e = r.get(envelope)
                if not e:
                    continue
                lines.append(f'| {w} | {label} | {RATIO_NAME[envelope]} | {e["arm"]} ({e["n"]})'
                             f'{" inferred" if e["inferred"] else ""}{" 3 runs" if e["arm"] == "probackup8" else ""} | '
                             f'{fmt(e["median"])} | {fmt(e["max"])} | {fmt(e["trial"])} ({e["n_trial"]}) | '
                             f'{fmt(e["trig"])} | {fmt(e["adj"])} | {"yes" if e["measured"] else "no"} |')
    tls = [(w, label, c, (S.ratios(w, label).get('B_fast') or {}).get('base')) for (w, label), c in
           a6_reading(S)['cells'].items() if c is not None]
    if tls:
        lines.append('- R_tls (v1_tls by A6\'s reading, the slower of measured and priced, over B_fast; "v1 pays TLS '
                     'through a proxy; the baselines run in clear"): '
                     + ', '.join(f'{w} {label} {fmt(ratio(c["reading"], base))}' for w, label, c, base in tls))
    lines.append(f'- the backup-mode adder (`full` at the default anchor, pooled): {fmt(S.adder(), 3)} s; R_adj adds it '
                 'to v1 over a live-source arm only')
    return lines


def ledger_lines(S, r1):
    """L1, L2 and R1 (R1 first among the rules: its consequence comes before the verdict's rows)."""
    lines = ["## The ledger's prediction and rule (registered; medians, with the maximum beside)"]
    say = lines.append
    label, tuned = '2:60:default', '2:60:tuned'
    # Every workload v1 ran at 60 ms default; a cell (at r or at the anchor) A11 leaves not measured gives None.
    ran = [w for w in S.workloads if S.cell(V1, label, w)['ran']]
    ran_tuned = [w for w in S.workloads if S.cell(V1, tuned, w)['ran']]
    l1 = {w: S.delta(V1, label, w, strict=True, measured=True)[0] for w in ran}
    l1_tuned = {w: S.delta(V1, tuned, w, strict=True, measured=True)[0] for w in ran_tuned}
    l1_max = {w: S.top(V1, label, w) for w in ran}

    def shown(values, unit=''):
        return ', '.join(f'{w} {fmt(v)}{unit}' if v is not None else f'{w} not measured' for w, v in values.items())
    say(f'- **L1** ("with a window of 4, pgdelta adds 2 s or less per 2.1 GB at 60 ms": Δ_v1 at 60 ms, 2 Gbit/s, '
        f'default hosts, at most {L1_LIMIT_S} s in every workload run there; a workload not measured leaves it not '
        f'judged unless another fails it): '
        f'{word(all_judged({w: None if v is None else v <= L1_LIMIT_S + 1e-9 for w, v in l1.items()}))} ('
        + ', '.join(f'{w} {fmt(v)} s (max {fmt(l1_max[w])} s at r)' if v is not None else f'{w} not measured'
                    for w, v in l1.items())
        + '); tuned, from the tuned anchor, not judged: ' + (shown(l1_tuned, ' s') or 'no data'))
    full = S.delta('full', label, strict=True, measured=True)[0]
    full_tuned = S.delta('full', tuned, strict=True, measured=True)[0]
    l2 = {w: None if v is None or full is None else full > v for w, v in l1.items()}
    say(f'- **L2** ("A single-stream full copy slows down more": Δ_full > Δ_v1 at 60 ms, 2 Gbit/s, default hosts, '
        f'every workload): {word(all_judged(l2))} (Δ_full '
        + (f'{fmt(full)} s pooled' if full is not None else 'not measured') + '; Δ_v1 ' + (shown(l1) or '–')
        + '); tuned beside: Δ_full ' + (f'{fmt(full_tuned)} s' if full_tuned is not None else 'not measured')
        + ', Δ_v1 ' + (shown(l1_tuned) or '–'))
    fired = [c for c in r1['cells'] if c['fires']]
    say(f'- **R1** ("if pgdelta slows down more than the full copy, the protocol needs deeper pipelining before any WAN '
        f'claim": Δ_v1 > Δ_full at any r of 20, 60 and 120 ms, 2 Gbit/s, either host setting, any workload, each from '
        f'its own anchor): ' + ('**fires**' if r1['fires'] else 'does not fire' if r1['judged'] else 'not judged (no data)')
        + (' at ' + ', '.join(f'{c["workload"]} {c["label"]} (Δ_v1 {fmt(c["v1"])} > Δ_full {fmt(c["full"])}'
                              + ('' if c['measured'] else '; a cell not measured, flagged') + ')' for c in fired)
           if fired else ''))
    lines.append('| workload | condition | Δ_v1 | Δ_full | fires |')
    lines.append('|---|---|---:|---:|---|')
    for c in r1['cells']:
        lines.append(f'| {c["workload"]} | {c["label"]} | {fmt(c["v1"])} | {fmt(c["full"])} | '
                     f'{"yes" if c["fires"] else "no"}{"" if c["measured"] else " (not measured)"} |')
    if r1['fires']:
        say("  - consequence, as registered: deeper pipelining before any WAN claim. Rows 1, 2 and 3 of the DR verdict "
            'carry "provisional: R1 fired" at every r, and this session makes no DR claim for v1. It is lifted only by '
            'a later test that measures deeper pipelining at every cell where R1 fired, with R1 no longer firing there; '
            "A3's split below lifts nothing")
        w16 = {w: (S.med(W16, SIDE_CELL, w), S.med(V1, SIDE_CELL, w)) for w in DECIDING}
        say("  - beside it (lifts nothing): v1_w16 at 120 ms tuned "
            + ', '.join(f'{w} {fmt(a)} s against v1\'s {fmt(b)} s' for w, (a, b) in w16.items()))
    return lines, {'l1': all_judged({w: None if v is None else v <= L1_LIMIT_S + 1e-9 for w, v in l1.items()}),
                   'l2': all_judged(l2)}


def all_judged(oks: dict):
    """A prediction over every workload run (workload -> True, False, or None where its cell isn't measured): False
    once one fails; else None if one isn't measured, or none ran; else True."""
    if any(ok is False for ok in oks.values()):
        return False
    if not oks or any(ok is None for ok in oks.values()):
        return None
    return True


def verdict_text(v) -> str:
    text = f'**{v["rtt"]} ms: {v["row"]}.**'
    if v['row'] == ROWS[0]:
        text += f' {ROW_TEXT[ROWS[0]]}; as computed: {v["computed"]}.'
    else:
        text += f' {ROW_TEXT[v["row"]]}.'
    if v['provisional']:
        text += ' **Provisional: ' + '; '.join(v['provisional']) + '.**'
        if v['computed'] in BOUND_ROWS and any(p.startswith('bound') or p.startswith('window') for p in v['provisional']):
            text += ' Against the copies measured.'
    return text


def verdict_lines(S, r1, gates, a12, a17):
    lines = ['## The DR verdict (EXP-09\'s bands against rule 3\'s baseline, at 2 Gbit/s; rows checked in order)']
    if r1['fires']:
        lines.append('- **R1 fired**: rows 1, 2 and 3 at every r are provisional, and no DR claim for v1 is made from '
                     'them')
    if V1 in S.not_durable:
        lines.append(f'- **v1 is not durable for the session** (A13: {"; ".join(S.not_durable[V1])}): every r reads '
                     '"v1 not durable"; the rows below are still computed and reported')
    if S.tcp_default_problems:
        lines.append("- **The default host setting wasn't Debian's everywhere** (see the network validity of the runs): "
                     'every reading on default hosts is flagged')
    verdicts = {}
    for rtt in RTTS:
        v = dr_verdict(S, rtt, r1, {label: gates[label] for label in gates}, a12, a17)
        verdicts[rtt] = v
        lines.append(f'### {rtt} ms')
        lines.append('| Host | ' + ' | '.join(f'R_fast {w}' for w in DECIDING) + ' |')
        lines.append('|---|' + '---:|' * len(DECIDING))
        for host in DESIGN[rtt]:
            lines.append(f'| {host} | ' + ' | '.join(fmt(v['values'][host][w]) if v['values'][host][w] is not None
                                                      else 'not measured' for w in DECIDING) + ' |')
        lines.append(verdict_text(v))
        flags = v['flags']
        lines.append('- *Near the line*: ' + ('; '.join(f'{h} {w} {fmt(value, 3)} near {line:.2f}'
                                                        for h, w, value, line in flags['near'])
                                              if flags['near'] else 'no'))
        read = [name for name in ('R_max', 'R_trial', 'R_trig') if name not in flags['unread']]
        lines.append('- *Fragile*: ' + ('; '.join(f'{name} would give {row}' for name, row in flags['fragile'])
                                        if flags['fragile'] else f'no ({", ".join(read) or "no view"} give the same row)')
                     + (f'; {", ".join(flags["unread"])} could not be read (a deciding cell without it)'
                        if flags['unread'] else ''))
        if rtt == 120:
            sensitive = flags['tls_sensitive'] is not None and flags['tls_sensitive'] != v['computed']
            lines.append('- *TLS-sensitive*: ' + (f'yes: with R_tls in place of v1\'s tuned ratio the row is '
                                                  f'{flags["tls_sensitive"]}' if sensitive else 'no'))
        lines.append('- *Pooled baseline unstable* (A17 (i)): ' + (
            '; '.join(f'{u["arm"]} at {u["label"]} ({u["spread"]:.1%}; R_trial stated beside every ratio there)'
                      for u in flags['unstable']) if flags['unstable'] else 'no'))
        if 'tuned' in DESIGN[rtt]:
            lines.append('- *Tuned not measured*: ' + ('yes: row 2 not judged; rows 3 and 4 read on'
                                                       if 'tuned not measured' in flags['notes'] else 'no'))
        if flags['emulator_limited']:
            lines.append('- *Emulator-limited* (A12): row 2 failed where one tuned stream reached under 0.9 of the '
                         'anchor: ' + ', '.join(f'{e["trial"]} {e["label"]}' for e in flags['emulator_limited']))
        qualifier = ('v1 is faster than one `pg_basebackup` stream, the copy Omni documents for a DR re-seed, at '
                     f'{rtt} ms and 2 Gbit/s on these VMs' + ('; and than `probackup8`' if v['probackup'] else ''))
        lines.append('- *Qualifier* (R_F0 ≤ 0.90 on default hosts for hot and uniform 1%: '
                     + ', '.join(f'{w} {fmt(x) if x is not None else "not measured"}' for w, x in v['f0'].items())
                     + '): '
                     + (f'"{qualifier}"' + (' (provisional: R1 fired)' if r1['fires'] else '') if v['qualifier']
                        else 'does not apply'))
    lines.append('### At 1 Gbit/s (uniform 1%, default hosts; reported, not a row)')
    for label in ('1:60:default', '1:120:default'):
        r = S.ratios('uniform1', label).get('B_fast') or {}
        gate = gates.get(label) or bound_gate(S, label)
        lines.append(f'- {label}: R_fast {fmt(r.get("median"))} against {r.get("arm") or "–"}'
                     + (' (full inferred)' if r.get('inferred') else '')
                     + ('; **window-limited bound**' if 'window-limited bound' in gate['reasons'] else ''))
    return lines, verdicts


def crossover_lines(S):
    lines = ['## Crossovers (D78\'s method: exp04.crossover against B_fast; x the trial\'s zero-delay one-stream rate)',
             '| Workload | r | host | crossover | R_fast at 2 Gbit/s | labels |', '|---|---|---|---|---:|---|']
    found = {}
    for w in [w for w in DECIDING if w in S.workloads]:
        for host in ('default', 'tuned'):
            for rtt in RTTS:
                if host == 'tuned' and rtt == 20:
                    continue
                x = crossover(S, w, rtt, host)
                found[(w, rtt, host)] = x
                if x['text'] == 'no data':
                    continue
                lines.append(f'| {w} | {rtt} ms | {host} | {x["text"]} | {fmt(x["r2"])} | '
                             f'{", ".join(x["labels"]) or ""} |')
    return lines, found


def gate_lines(gates):
    lines = ['- **A9** (the bound\'s gate at each condition: pooled median > its bytes at its stream count\'s steady '
             'rate + 1.0 s + 6 r, or its busiest stream > 1.1 × the mean; window-limited if B_fast\'s parcopy arm\'s '
             'link test is under 0.9 × the zero-delay rate at the same tbf rate):']
    for label, gate in sorted(gates.items(), key=lambda kv: label_key(kv[0])):
        for arm, a in gate['arms'].items():
            lines.append(f'  - {label} {arm}: {fmt(a["median"])} s over {a["n"]} runs against {fmt(a["limit"])} s '
                         f'({fmt(a["rate"])} Gbit/s); balance {fmt(a["balance"], 3)}; zero-delay {fmt(a["zero_delay"])} '
                         'Gbit/s'
                         + (f'; its connection set-up {fmt(a["connect_s"], 3)} s (parcopy_connect_s)'
                            if condition(label)['rtt_ms'] > 0 else '')
                         + f' → {"suspect" if a["suspect"] else "passes" if a["slow"] is not None else "no data"}'
                         + ('; window-limited' if a['window_limited'] else '')
                         + (f'; {a["excluded"]}' if a['excluded'] else ''))
        if gate['reasons']:
            lines.append(f'    → at {label}: {", ".join(gate["reasons"])}'
                         + (' (rows 1 and 2 at that r provisional)' if condition(label)['gbit'] == 2 else
                            ' (labels R_fast and the crossover)'))
    return lines


def a_lines(S, r1, ledger, verdicts, gates, a10, a12, a17, a16):
    """A1-A17."""
    lines = ['## Added rules']
    say = lines.append
    say('- **A1** (the DR verdict at each r, after R1): ' + '; '.join(f'{rtt} ms {v["row"]}' for rtt, v in verdicts.items()))
    # A2.
    rows = {rtt: verdicts[rtt] for rtt in (60, 120) if rtt in verdicts}
    computed = {rtt: v['computed'] for rtt, v in rows.items()}
    if any(row == ROWS[3] for row in computed.values()):
        a2 = 'D36 is **contradicted** for DR on default buffers (X): v1 needs parallel page streams, or Omni\'s nodes ' \
             'need a TCP buffer setting (an owner decision, with the integrator question); parallel streams go on ' \
             "T8's build list, sized from the 1- and 8-stream link tests, unless the owner accepts a host requirement"
        causes = []
        for rtt in (60, 120):
            if computed.get(rtt) != ROWS[3]:
                continue
            fraction = S.med(V1, label_of(2, rtt, 'default'), 'uniform1', 'limited_fraction')
            causes.append(f'{rtt} ms: v1 uniform 1% on default hosts limited {fmt(fraction)} of its busy time'
                          + ('' if fraction is None or fraction >= 0.5 else
                             ' (under half: the tuned setting removes the loss, and the timers don\'t show why)'))
        a2 += '. Its cause (E15): ' + ('; '.join(causes) or 'no ss reading')
    elif (all(row == ROWS[2] for row in computed.values()) and len(computed) == 2
          and not any(v['provisional'] for v in rows.values()) and V1 not in S.not_durable):
        a2 = 'D36 stands, now measured at DR latency, graded B'
    else:
        a2 = 'D36 stays at C (any other mix); the readings recorded'
    say(f'- **A2** (one stream, D36; rows at 60 and 120 ms as computed: '
        + ', '.join(f'{rtt} ms {row}' + (' (provisional)' if rows[rtt]['provisional'] else '')
                    for rtt, row in computed.items()) + f'): {a2}')
    # A3.
    applies = r1['fires'] or ledger.get('l1') is False or any(v['computed'] in ROWS[4:] for v in verdicts.values())
    say('- **A3** (where v1\'s added time goes; applied when R1 fires, L1 fails, or a row is 3, 4 or 5): '
        + ('applies' if applies else 'does not apply; reported') + '. Each part is its median at r less its median '
        'at the anchor of the same host setting; the residual is Δ_v1 less the three, with its sign:')
    lines += ['| Workload | condition | Δ_v1 | idle waits | fixed round trips | sending | residual | largest: the fix |',
              '|---|---|---:|---:|---:|---:|---:|---|']
    for w in S.workloads:
        for label in S.labels:
            c = condition(label)
            if c['gbit'] != 2 or c['rtt_ms'] == 0:
                continue
            parts = a3_parts(S, w, label)
            if parts is None:
                continue
            lines.append(f'| {w} | {label} | {fmt(parts["delta"])} | {fmt(parts["idle_waits_s"])} | '
                         f'{fmt(parts["fixed_rtts_s"])} | {fmt(parts["sending_s"])} | '
                         f'{"–" if parts["residual"] is None else format(parts["residual"], "+.2f")} | '
                         f'{A3_FIX[parts["largest"]] if parts["largest"] else "the timers are missing: not split"} |')
    if r1['fires']:
        say('  - R1 fired: the split is reported and lifts nothing')
    # A4.
    a4 = a4_reading(S)
    idle = {(arm, w): S.med(arm, SIDE_CELL, w, 'leaf_wait_idle_s') for arm in (W1, V1, W16) for w in DECIDING}
    requests = {w: S.med(V1, SIDE_CELL, w, 'leaf_requests') for w in DECIDING}
    text = {'(i)': 'the default window becomes 16; D29 changes, graded B',
            '(ii)': "the window doesn't matter on this path at DR latency (the verifier's backlog hides the round "
                    'trips); D29 stays 4, graded B',
            '(iii)': 'the window matters, and 4 is enough; D29 stays 4, graded B', None: 'not judged (no data)'}
    say(f'- **A4** (the window, D29, at 120 ms tuned): {a4["part"] or "–"}: {text[a4["part"]]} (v1_w16 − v1: '
        + ', '.join(f'{w} {fmt(v)}' for w, v in a4['w16'].items()) + '; v1_w1 − v1: '
        + ', '.join(f'{w} {fmt(v)}' for w, v in a4['w1'].items()) + '). D30 isn\'t varied: v1\'s idle leaf waits '
        + ', '.join(f'{w} {fmt(idle[(V1, w)])} s over {fmt(requests[w], 0)} requests' for w in DECIDING)
        + ' bound what changing it could save; its grade stays D')
    # A5.
    a5 = a5_reading(S)
    text = {'(i)': 'the fallback is turned on at 0.5', '(ii)': "the node fraction can't tell 1% from 10% at this data "
            'shape; a page-level trigger goes to T14, and the fallback stays off',
            '(iii)': 'it stays off, now measured at latency', None: 'not judged (no data)'}
    say(f'- **A5** (the fallback, D22, at 120 ms tuned): {a5["part"] or "–"}: {text[a5["part"]]} (saves '
        f'{fmt(a5["saving_uniform10"])} s in uniform 10%, costs {fmt(a5["cost_uniform1"])} s in uniform 1%); D22\'s '
        'grade is set from the ledger\'s table')
    # A6.
    a6 = a6_reading(S)
    say(f'- **A6** (TLS through the proxy: v1_tls − v1 more than max(0.3 s, 5%) at the anchor or at 120 ms tuned, in '
        f'hot or uniform 1%; judged as measured and priced): '
        + ('not judged (no v1_tls)' if a6['fires'] is None else '**fires**' if a6['fires'] else 'does not fire')
        + (' (the two readings differ: the one that fires governs, flagged)' if a6['fires'] is not None and a6['differ']
           else ''))
    for (w, label), c in a6['cells'].items():
        if c is None:
            continue
        tls_runs = S.cell(TLS, label, w)['runs']
        v1_runs = S.cell(V1, label, w)['runs']
        waits = {name: (median(sum(r[f'{wait}_s'] or 0 for wait in WAITS) for r in runs))
                 for name, runs in (('tls', tls_runs), ('v1', v1_runs))}
        say(f'  - {w} {label}: measured {fmt(c["measured_d"], 3)} s, priced {fmt(c["priced_d"], 3)} s (extra prefetch '
            f'{fmt(c["extra_bytes"] / 1e6, 1)} MB at {fmt((c["rate"] or 0) / 1e6, 0)} MB/s) against {fmt(c["line"], 3)} '
            f's; waits (busy plus idle) {fmt(waits["tls"])} against {fmt(waits["v1"])} s; CPU source '
            f'{fmt(diff_med(S, label, w, "cpu_source_s"))} s, dest {fmt(diff_med(S, label, w, "cpu_dest_s"))} s; '
            f"stunnel's WAN socket notsent max {fmt(S.med(TLS, label, w, 'notsent_max'), 0)}, unacked max "
            f"{fmt(S.med(TLS, label, w, 'unacked_max'), 0)} bytes; TLS "
            + ', '.join(sorted({f"{r['tls_version']} {r['tls_suite']}" for r in tls_runs})))
    if a6['fires']:
        say('  - TLS through the proxy is on v1\'s critical path: native TLS gets its own timed arm in T8 before any '
            'WAN claim with TLS' + ('; the excess shows at the anchor: it is CPU, and the HA case pays it too'
                                    if a6['at_anchor'] else ''))
    elif a6['fires'] is False:
        say('  - TLS is priced at the measured cost; T8 builds native TLS against that number; O6 adds no material '
            'cost on these VMs')
    # A7.
    a7 = a7_reading(S)
    say('- **A7** (SSH, D37: on tuned hosts the SSH probe carries less than half of one TCP stream at 60 and at 120 ms): '
        + ('**fires**: SSH is dropped as a WAN transport for pgdelta; D37\'s "SSH later" closes' if a7['fires'] else
           'does not fire: SSH stays an option, with its measured rate' if a7['fires'] is False else 'not judged (no data)')
        + ' (' + ', '.join(f'{label} {fmt(s["probe"], 3)} of {fmt(s["one"], 3)} Gbit/s = {fmt(s["share"])}'
                           for label, s in a7['shares'].items() if s['probe'] is not None) + ')')
    # A8.
    items = []
    for label in S.labels:
        c = condition(label)
        fast = (S.ratios('uniform10', label).get('B_fast') or {}) if 'uniform10' in S.workloads else {}
        r = fast.get('median')
        if r is None or c['rtt_ms'] == 0:
            continue
        if not fast.get('measured'):
            # A11: v1's cell or B_fast's isn't measured there: no reading, no claim.
            items.append(f'{label} R_fast {fmt(r)}: not measured (A11), not read')
            continue
        if r <= HOLD:
            if V1 in S.not_durable:
                reading = 'v1 not durable (A13): no claim for v1'
            else:
                provisional = (['R1 fired'] if r1['fires'] else []) + list(
                    (gates.get(label) or bound_gate(S, label))['reasons'])
                reading = ("v1's DR claim covers 10% scattered changes there"
                           + (f' (provisional: {"; ".join(provisional)})' if provisional else ''))
        elif r >= OUT:
            reading = ('a case for a full copy at that r, rate and host setting; pgdelta\'s documented fallback '
                       'behaviour (O2) says so')
        else:
            reading = 'reported'
        items.append(f'{label} R_fast {fmt(r)}: {reading}')
    say('- **A8** (uniform 10% at each r and host setting run): ' + ('; '.join(items) or 'no data'))
    lines += gate_lines(gates)
    failed = [g for g in a10 if g['ok'] is False]
    say('- **A10** (drift gates against EXP-11 at the default anchor): '
        + ('the session is **shifted** (' + (', '.join(f'{g["workload"]} {g["gate"]} {g["change"]:+.1%}' for g in failed)
                                             or 'no gate could be checked')
           + '); every verdict here is within the session and stands; E-ranges built on EXP-11\'s absolute times are '
             'judged as written and flagged' if shifted(a10) else 'every gate holds'))
    lines += ['| Workload | gate | here | EXP-11 | change | tolerance | |', '|---|---|---:|---:|---:|---:|---|']
    for g in a10:
        scale, digits = (1e6, 1) if g['gate'].endswith('bytes') else (1, 3)
        change = '–' if g['change'] is None else format(g['change'], '+.1%')
        lines.append(f'| {g["workload"]} | {g["gate"]}{" (MB)" if scale > 1 else ""} | '
                     f'{fmt(None if g["here"] is None else g["here"] / scale, digits)} | '
                     f'{fmt(None if g["exp11"] is None else g["exp11"] / scale, digits)} | '
                     f'{change} | ±{g["tolerance"]:.0%} | {word(g["ok"])} |')
    faults = [key[1] for key, trial in S.trials.items() if trial.get('infrastructure_fault')]
    retried = [v for _, v in S.visits() if len(v.get('attempts') or ()) > 1]
    a11 = [r for r in S.rows if r['a11']]
    say(f'- **A11** (network validity): (i) {len(retried)} visit(s) reset and checked again; '
        + (f'infrastructure fault in {", ".join(faults)}' if faults else 'no infrastructure fault')
        + f'; (ii) {len(a11)} run(s) excluded (see Network validity); cells not measured are listed there')
    limited = [e for e in a12 if e['limited']]
    say('- **A12** (a tuned stream through the emulator: at a trial\'s first visit to a tuned condition, under 0.9 × the '
        'default anchor\'s one-stream rate): ' + ('; '.join(f'{e["trial"]} {e["label"]} {fmt(e["rate"], 3)} against '
                                                         f'{fmt(e["reference"], 3)}: the tuned cells there are '
                                                         '"emulator-limited"' for e in limited) if limited else
                                                'does not fire' if a12 else 'no data')
        + (' (' + ', '.join(f'{e["trial"]} {e["label"]} {fmt(e["rate"], 3)}' for e in a12 if not e['limited']) + ')'
           if a12 and len(limited) < len(a12) else ''))
    failing = [r for r in S.rows if r['durability_failures']]
    say(f'- **A13** (durability, as EXP-11\'s A5): {len(failing)} run(s) excluded'
        + ('; reclassified as not durable for the session: ' + '; '.join(f'{arm} ({", ".join(why)})' for arm, why in
                                                                          S.not_durable.items())
           if S.not_durable else '; no arm reclassified'))
    control = [r for r in S.rows if r['method'] in measure.NON_DURABLE_METHODS]
    say('- **A14** (the negative control, full_nosync, passes (a) in any trial): '
        + ('**fires**: check (a) is void for the session; durability rests on (b)' if S.a_void else
           'does not fire' if control else 'no data'))
    voids = [r for r in S.rows if r['void']]
    say('- **A15** (verification, as EXP-11\'s A4, void only where a user-table file differs between the reference '
        'digests): ' + ('; '.join(f'fires for {arm}: ' + ', '.join(f'{r["trial_id"]} r{r["repeat"]} {r["speed"]}'
                                                                  + (' (its tool aborted the copy)'
                                                                     if r['status'] == 'error' else '')
                                                                  for r in runs) for arm, runs in S.a15.items())
                         or 'no arm excluded') + f'; void runs (not failures): {len(voids)}')
    for key, trial in sorted(S.trials.items()):
        classes = trial.get('reference_classes')
        if classes:
            counts = classes.get('counts') or {}
            say(f'  - {key[1]}: reference digests differ in ' + ', '.join(f'{name} {counts.get(name, 0)}' for name in
                                                                           measure.FILE_CLASSES)
                + f' files; user tables match: {word(classes.get("user_tables_match"))}'
                + (f' (user-table files: {", ".join((classes.get("paths") or {}).get("user_table") or [])})'
                   if counts.get('user_table') else ''))
    say(f'- **A16** (`full` at 1 Gbit/s: one default stream at most {A16_LIMIT_GBIT} Gbit/s at 60 and 120 ms there): '
        + ', '.join(f'{label} {fmt(rate, 3)}' for label, rate in a16['rates'].items()) + ' → '
        + ({True: "`full` doesn't run at 1 Gbit/s; its 2 Gbit/s median stands in, labelled inferred",
            False: '`full` runs at 1 Gbit/s', None: 'no data'}[a16['skip']])
        + ('' if a16['consistent'] is not False else '; **the sweep didn\'t follow it** (full ran: '
           + ', '.join(f'{label} {ran}' for label, ran in a16['ran'].items()) + ')'))
    unstable = [u for u in a17 if u['unstable']]
    say('- **A17** (i) (a baseline\'s per-trial medians at a condition more than 3% apart): '
        + ('; '.join(f'{u["arm"]} at {u["label"]} {u["spread"]:.1%}: flagged, R_trial stated beside every ratio there'
                     for u in unstable) or 'none'))
    anchors = a17_anchors(S)
    say('- **A17** (ii) (the tuned anchor more than 0.1 s from the default anchor, v1 per workload and `full`): '
        + ('; '.join(f'{a["arm"]}{" " + a["workload"] if a["workload"] else ""} {a["gap"]:+.2f} s'
                     for a in anchors if a['reported']) or 'none')
        + ' (' + ', '.join(f'{a["arm"]}{" " + a["workload"] if a["workload"] else ""} {a["gap"]:+.2f}' for a in anchors)
        + '); the tuned Δs use the tuned anchor either way')
    return lines


def diff_med(S, label, workload, col, arm=TLS, base=V1):
    a, b = S.med(arm, label, workload, col), S.med(base, label, workload, col)
    return None if a is None or b is None else a - b


# --- predictions ----------------------------------------------------------------

def input_drift(S) -> dict:
    """Smoke item 7: each r whose measured one-stream rate (2 Gbit/s, default) is more than 25% from the written one."""
    out = {}
    for rtt, written in WRITTEN_RATES.items():
        rate = median(S.one_stream(label_of(2, rtt, 'default')))
        if rate is not None:
            out[rtt] = {'rate': rate, 'off': abs(rate / written - 1) > INPUT_DRIFT}
    return out


class Predictions:
    """E1-E17, each judged as written, with the model's recomputed range beside (E3-E14) and the flags."""

    def __init__(self, S, ranges=None, shifted_session=False, drift=None):
        self.S, self.ranges, self.shifted, self.drift = S, ranges or {}, shifted_session, drift or {}
        self.lines = ['## Added predictions (judged as written; the model\'s recomputed range beside, "model")']

    def flags(self, name, label=None) -> str:
        notes = []
        if self.shifted and name in EXP11_SHIFT_PREDICTIONS:
            notes.append('shifted session (A10)')
        if label and label.count(':') == 2:
            c = condition(label)
            if c['host'] == 'default' and (self.drift.get(int(c['rtt_ms'])) or {}).get('off'):
                notes.append('input off by more than 25%')
        return f' [{"; ".join(notes)}]' if notes else ''

    def item(self, name, key, value, digits=2, written=None):
        """'what at label: value (written lo–hi; model lo–hi) held' for one written range."""
        written = written or model.WRITTEN[name][key]
        modelled = (self.ranges.get(name) or {}).get(key)
        what, where = key
        return (f'{what} at {where} {fmt(value, digits)} (written {show(written)}'
                + (f'; model {show(modelled)}' if modelled else '') + f') {word(judge(value, written))}'
                + self.flags(name, where))

    def say(self, name, title, items, oks):
        self.lines.append(f'- **{name}** ({title}): {word(every(oks))}' + (': ' + '; '.join(items) if items else ''))

    def judge_written(self, name, values: dict, title, digits=2):
        """Every written range of a prediction with its value (keys missing from values: no data)."""
        items, oks = [], []
        for key in model.WRITTEN[name]:
            value = values.get(key)
            if value is None:
                continue
            items.append(self.item(name, key, value, digits))
            oks.append(judge(value, model.WRITTEN[name][key]))
        self.say(name, title, items, oks)

    def all(self, r1, verdicts, crossings):
        S = self.S
        self.e1()
        self.e2()
        self.e3()
        self.judge_written('E4', {key: (S.med('full', key[1]) if key[0] == 'full' else
                                        S.med('full', key[1], col='pbb_sync_s')) for key in model.WRITTEN['E4']},
                           '`full`, pooled; its sync phase up to 20 ms and once its stream outlasts 30 s')
        values = {key: S.med(key[0], key[1]) for key in model.WRITTEN['E5']}
        self.judge_written('E5', values, '`parcopy8` and `parcopy16`, pooled')
        p8, p16 = S.med('parcopy8', '2:120:default'), S.med('parcopy16', '2:120:default')
        self.lines.append(f'  - parcopy16 faster than parcopy8 at 120 ms default: '
                          f'{word(None if p8 is None or p16 is None else p16 < p8)} ({fmt(p16)} against {fmt(p8)} s)')
        self.judge_written('E6', {key: S.delta(V1, key[1], 'hot', strict=True)[0] for key in model.WRITTEN['E6']},
                           'v1 hot, added by latency (tuned from the tuned anchor)')
        self.judge_written('E7', {key: S.med(V1, key[1], 'uniform1') for key in model.WRITTEN['E7']}, 'v1 uniform 1%')
        self.judge_written('E8', {key: S.med(V1, key[1], 'uniform10') for key in model.WRITTEN['E8']},
                           'v1 uniform 10%')
        self.e9()
        self.e10()
        self.e11()
        self.e12()
        self.e13(r1, verdicts)
        self.e14(crossings)
        self.e15()
        self.e16()
        self.e17()
        return self.lines

    def e1(self):
        S = self.S
        anchor = ANCHOR['default']
        items, oks = [], []
        for arm, rng in (('full', (9.8, 10.8)), ('parcopy8', (8.9, 9.8))):
            value = S.med(arm, anchor)
            if value is not None:
                items.append(f'{arm} {fmt(value)} (written {show(rng)}) {word(judge(value, rng))}')
                oks.append(judge(value, rng))
        for w, rng in (('hot', (2.8, 3.4)), ('uniform1', (4.8, 5.8))):
            value = S.med(V1, anchor, w)
            if value is not None:
                items.append(f'v1 {w} {fmt(value)} (written {show(rng)}) {word(judge(value, rng))}')
                oks.append(judge(value, rng))
        for key, trial in sorted(S.trials.items()):
            rate = S.anchor_one_stream(key)
            if rate is not None:
                items.append(f'one stream in {key[1]} {fmt(rate, 3)} (written 1.88–1.96) {word(judge(rate, (1.88, 1.96)))}')
                oks.append(judge(rate, (1.88, 1.96)))
        for a in a17_anchors(S):
            ok = abs(a['gap']) <= A17_ANCHOR_S + 1e-12
            items.append(f'tuned anchor − default anchor, {a["arm"]}{" " + a["workload"] if a["workload"] else ""} '
                         f'{a["gap"]:+.2f} s (within 0.1 s) {word(ok)}')
            oks.append(ok)
        items.append('the tree at r = 0 against EXP-11\'s plain tbf: smoke item 2, not in the sweep\'s records')
        self.say('E1', 'the anchor reproduces EXP-11\'s 2 Gbit/s cell; the tuned anchor within 0.1 s'
                 + self.flags('E1'), items, oks)

    def e2(self):
        S = self.S
        visits = S.visits()
        first = [len(v.get('attempts') or ()) <= 1 and v.get('ok') is not False for _, v in visits]
        runs = [r['network_ok'] for r in S.rows if r['status'] != 'error']
        def drops(r):
            return (r['tbf_drops_source'] or 0) + (r['tbf_drops_dest'] or 0)
        anchor_drops = [r for r in S.rows if r['rtt_ms'] == 0 and drops(r) > 0]
        later = [r for r in S.rows if (r['rtt_ms'] or 0) > 0 and drops(r) > 0]
        self.say('E2', 'the emulation: every visit passes its checks; every run\'s netem own drops 0, backlog below its '
                       'limit, netem bytes ≥ 0.98 × wire, minrtt in [r, r + 2 ms]; tbf drops only at r > 0',
                 [f'visits passing on the first check {sum(first)} of {len(first)}',
                  f'runs passing the network checks {sum(ok is True for ok in runs)} of {len(runs)} (failing '
                  f'{sum(ok is False for ok in runs)}, unchecked {sum(ok is None for ok in runs)})',
                  f'runs with tbf drops at r = 0: {len(anchor_drops)}; at r > 0: {len(later)}'],
                 [every(first), every(runs), not anchor_drops])

    def e3(self):
        S = self.S
        values = {}
        for key in model.WRITTEN['E3']:
            what, label = key
            if what == 'one stream':
                values[key] = median(S.one_stream(label))
            elif what in ('8 streams', '16 streams'):
                values[key] = median(S.first_link(label, int(what.split()[0])))
            elif what == 'stunnel / direct':
                shares = []
                for trial_key, first in S.first_visit(label):
                    test = first.get('stunnel_link_test') or {}
                    direct = S.first_one_stream(trial_key, label)  # the same visit's direct one-stream test
                    if test.get('steady_gbit') is not None and direct:
                        shares.append(test['steady_gbit'] / direct)
                values[key] = median(shares)
            elif what == 'SSH probe':
                values[key] = median(p for p, _ in ssh_probes(S, label))
        self.judge_written('E3', values, 'link tests, steady Gbit/s', 3)

    def e9(self):
        S = self.S
        values = {('hot: w1 - v1', SIDE_CELL): diff_med(S, SIDE_CELL, 'hot', 'total_s', W1),
                  ('hot: w16 - v1', SIDE_CELL): diff_med(S, SIDE_CELL, 'hot', 'total_s', W16),
                  ('uniform1: w1 - v1', SIDE_CELL): diff_med(S, SIDE_CELL, 'uniform1', 'total_s', W1),
                  ('uniform1: w16 - v1', SIDE_CELL): diff_med(S, SIDE_CELL, 'uniform1', 'total_s', W16)}
        items, oks = [], []
        for key, value in values.items():
            if value is not None:
                items.append(self.item('E9', key, value))
                oks.append(judge(value, model.WRITTEN['E9'][key]))
        for w in DECIDING:
            idle = [S.med(arm, SIDE_CELL, w, 'leaf_wait_idle_s') for arm in (W1, V1, W16)]
            totals = [S.med(arm, SIDE_CELL, w, 'leaf_wait_s') for arm in (W1, V1, W16)]
            if all(v is not None for v in idle):
                ordered = idle[0] >= idle[1] >= idle[2]
                items.append(f'{w}: leaf_wait idle w1 ≥ w4 ≥ w16 {fmt(idle[0], 3)} ≥ {fmt(idle[1], 3)} ≥ '
                             f'{fmt(idle[2], 3)} {word(ordered)} (totals {", ".join(fmt(t, 3) for t in totals)})')
                oks.append(ordered)
            if idle[1] is not None:
                key = ('leaf_wait idle at window 4', SIDE_CELL)
                items.append(f'{w} ' + self.item('E9', key, idle[1], 3))
                oks.append(judge(idle[1], model.WRITTEN['E9'][key]))
        self.say('E9', 'the window at 120 ms tuned', items, oks)

    def e10(self):
        S = self.S
        items, oks = [], []
        cost = diff_med(S, SIDE_CELL, 'uniform1', 'total_s', HC)
        more = diff_med(S, SIDE_CELL, 'uniform1', 'wire_s2d_bytes', HC)
        save = diff_med(S, SIDE_CELL, 'uniform10', 'total_s', HC)
        wire10 = diff_med(S, SIDE_CELL, 'uniform10', 'wire_s2d_bytes', HC)
        base10 = S.med(V1, SIDE_CELL, 'uniform10', 'wire_s2d_bytes')
        for key, value in ((('uniform1: hc - v1', SIDE_CELL), cost),
                           (('uniform1: more bytes, GB', SIDE_CELL), None if more is None else more / 1e9),
                           (('uniform10: hc - v1', SIDE_CELL), save),
                           (('uniform10: wire bytes', SIDE_CELL), ratio(wire10, base10))):
            if value is not None:
                items.append(self.item('E10', key, value, 3))
                oks.append(judge(value, model.WRITTEN['E10'][key]))
        for w in ('uniform1', 'uniform10'):
            files = [r['high_churn_files'] for r in S.cell(HC, SIDE_CELL, w)['runs']]
            if files:
                ok = all(f is not None and f >= 1 for f in files)
                items.append(f'{w}: high-churn files at least 1 in every v1_hc run {word(ok)} ({min(f or 0 for f in files)} '
                             'at the least)')
                oks.append(ok)
        self.say('E10', 'the fallback at 120 ms tuned', items, oks)

    def e11(self):
        S = self.S
        items, oks = [], []
        for label in TLS_CELLS:
            for w in DECIDING:
                key = (f'{w}: tls - v1', label)
                value = diff_med(S, label, w, 'total_s')
                if value is not None:
                    items.append(self.item('E11', key, value, 3))
                    oks.append(judge(value, model.WRITTEN['E11'][key]))
            for host_col in ('cpu_source_s', 'cpu_dest_s'):
                value = diff_med(S, label, 'uniform1', host_col)
                if value is not None:
                    key = ('uniform1: CPU-s per VM', label)
                    items.append(f'{host_col} ' + self.item('E11', key, value))
                    oks.append(judge(value, model.WRITTEN['E11'][key]))
            for w in DECIDING:
                wire = ratio(diff_med(S, label, w, 'wire_s2d_bytes'), S.med(V1, label, w, 'wire_s2d_bytes'))
                if wire is not None:
                    key = ('wire bytes', label)
                    items.append(f'{w} ' + self.item('E11', key, wire, 4))
                    oks.append(judge(wire, model.WRITTEN['E11'][key]))
        suites = [(r['tls_version'], r['tls_suite']) for r in S.rows if r['method'] == TLS and r['usable']]
        if suites:
            ok = all(v == 'TLSv1.3' and s == 'TLS_AES_256_GCM_SHA384' for v, s in suites)
            items.append(f'TLS 1.3 with TLS_AES_256_GCM_SHA384 in every run {word(ok)} '
                         f'({", ".join(sorted({f"{v} {s}" for v, s in suites}))})')
            oks.append(ok)
        self.say('E11', 'TLS through the proxy', items, oks)

    def e12(self):
        S = self.S
        label = '2:60:default'
        value = S.med('probackup8', label)
        items, oks = [], []
        if value is not None:
            key = ('probackup8', label)
            items.append(self.item('E12', key, value))
            oks.append(judge(value, model.WRITTEN['E12'][key]))
            p8 = S.med('parcopy8', label)
            items.append(f'slower than parcopy8 ({fmt(p8)} s) {word(None if p8 is None else value > p8)}')
            oks.append(None if p8 is None else value > p8)
            shares = [ratio(r['rwnd_limited_ms'], r['busy_ms']) for r in S.cell('probackup8', label)['runs']]
            share = median(shares)
            items.append(f'its SSH sockets rwnd_limited / busy {fmt(share)}, mostly not (under 0.5) '
                         f'{word(None if share is None else share < 0.5)}')
            oks.append(None if share is None else share < 0.5)
        self.say('E12', '`probackup8` at 60 ms default', items, oks)

    def e13(self, r1, verdicts):
        S = self.S
        values = {}
        for key in model.WRITTEN['E13']:
            w, label = key
            if w in S.workloads:
                values[key] = (S.ratios(w, label).get('B_fast') or {}).get('median')
        self.judge_written('E13', values, '`R_fast`')
        expected = {20: (ROWS[2],), 60: (ROWS[3],), 120: (ROWS[3], ROWS[6])}
        rows = [(rtt, v['computed'], v['computed'] in expected[rtt]) for rtt, v in verdicts.items()]
        f0 = [(w, rtt, (S.ratios(w, label_of(2, rtt, 'default')).get('F0') or {}).get('median'))
              for rtt in RTTS for w in DECIDING]
        f0_ok = [None if v is None else v <= {'hot': 0.27, 'uniform1': 0.55}[w] for w, _, v in f0]
        qualifier = [v['qualifier'] for v in verdicts.values()]
        tuned_fires = any(c['fires'] and c['label'].endswith('tuned') for c in r1['cells']) if r1['cells'] else None
        self.lines.append('  - the expected rows (20 ms row 1; 60 ms row 2; 120 ms row 2 or Undecided): '
                          + ', '.join(f'{rtt} ms {row} {word(ok)}' for rtt, row, ok in rows)
                          + f'; the qualifier at every r {word(every(qualifier))}; R_F0 on default hosts at most 0.27 '
                          f'(hot) and 0.55 (uniform 1%) {word(every(f0_ok))} ('
                          + ', '.join(f'{w} {rtt} ms {fmt(v)}' for w, rtt, v in f0 if v is not None)
                          + f'); R1 fires on tuned hosts, so every row is provisional: {word(tuned_fires)}')

    def e14(self, crossings):
        items, oks = [], []
        for key, rng in model.WRITTEN['E14'].items():
            w, where = key
            rtt, host = where.split(':')
            found = crossings.get((w, int(rtt), host))
            if not found or found['range'] is None:
                continue
            low, high = found['range']
            ok = rng[0] <= low and high <= rng[1]
            modelled = (self.ranges.get('E14') or {}).get(key)
            items.append(f'{w} {rtt} ms {host}: {found["text"]} (written {show(rng)}'
                         + (f'; model {show(modelled)}' if modelled else '') + f') {word(ok)}')
            oks.append(ok)
        self.say('E14', 'crossovers, Gbit/s: 0 is "below 1", "at least 2.00" is "above 2"', items, oks)

    def e15(self):
        S = self.S
        items, oks = [], []
        for host, test, text in (('default', lambda f: f >= 0.5, 'at least half'), ('tuned', lambda f: f <= 0.1,
                                                                                    'at most a tenth')):
            for rtt in (60, 120):
                label = label_of(2, rtt, host)
                for arm, w in ((V1, 'uniform1'), (V1, 'uniform10'), ('full', None)):
                    value = S.med(arm, label, w, 'limited_fraction') if arm == V1 else S.med(arm, label,
                                                                                             col='limited_fraction')
                    if value is None:
                        continue
                    ok = test(value)
                    items.append(f'{arm}{" " + w if w else ""} {label} {fmt(value)} ({text}) {word(ok)}')
                    oks.append(ok)
        self.say('E15', 'TCP state: rwnd_limited + sndbuf_limited over the sending socket\'s busy time', items, oks)

    def e16(self):
        S = self.S
        items, oks = [], []
        for w in S.workloads:
            values = [r['page_bytes'] for arm in (V1, W1, W16, TLS) for r in S.runs(arm, w) if r['page_bytes']]
            if values:
                spread = max(values) / min(values) - 1
                ok = spread <= 0.001 + 1e-12
                items.append(f'{w}: v1\'s page bytes across r, host setting and window within 0.1% {word(ok)} '
                             f'({spread:.3%})')
                oks.append(ok)
        self.say('E16', 'bytes: v1\'s page bytes don\'t depend on r, host setting or window; v1_hc adds bytes as in E10',
                 items, oks)

    def e17(self):
        S = self.S
        durable = [r for r in S.rows if r['method'] not in measure.NON_DURABLE_METHODS
                   and r['status'] in ('pass', 'fail', 'void')]
        per_trial = {}
        for r in S.rows:
            if r['method'] in measure.NON_DURABLE_METHODS:
                per_trial.setdefault(r['trial_id'], []).append(r['dirty_increase_bytes'])
        control = [None if not v or any(x is None for x in v) else all(x >= 1.5e9 for x in v)
                   for v in per_trial.values()]
        self.say('E17', 'every durable run passes (a) and (b) at every condition; full_nosync fails (a) by at least '
                        '1.5 GB in each trial',
                 [f'{sum(bool(r["durability_failures"]) for r in durable)} of {len(durable)} durable runs fail',
                  f'full_nosync in {len(control)} trial(s): {word(every(control))}'],
                 [every(not r['durability_failures'] for r in durable), every(control) if control else None])


# --- network, smoke and time tables ------------------------------------------------

def network_lines(S, ranges):
    """The link tests against the model, the ss readings, netem's and tbf's counters, per condition."""
    lines = ['## The network (link tests against the model; ss; netem and tbf)',
             '| Condition | one stream (visits) | model | 8 streams | 16 streams | dest to source | SSH probe | stunnel |',
             '|---|---:|---|---:|---:|---:|---:|---:|']
    e3 = ranges.get('E3') or {}
    for label in S.labels:
        one = S.one_stream(label)
        stunnel = [(first.get('stunnel_link_test') or {}).get('steady_gbit') for _, first in S.first_visit(label)]
        lines.append(f'| {label} | {fmt(median(one), 3)} ({len(one)}) | {show(e3.get(("one stream", label)), 3)} | '
                     f'{fmt(median(S.first_link(label, 8)), 3)} | {fmt(median(S.first_link(label, 16)), 3)} | '
                     f'{fmt(median(S.first_link(label, 1, "dest_to_source")), 3)} | '
                     f'{fmt(median(p for p, _ in ssh_probes(S, label)), 3)} | {fmt(median(stunnel), 3)} |')
    lines += ['', '| Arm | condition | runs | minrtt ms | limited / busy | retrans (source) | netem own drops | '
                  'tbf drops (source) | netem backlog max, packets |', '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for arm in ARM_NAMES:
        for label in S.labels:
            runs = [r for r in S.rows if r['method'] == arm and r['speed'] == label]
            if not runs:
                continue
            lines.append(f'| {arm} | {label} | {len(runs)} | {fmt(median(r["minrtt_ms"] for r in runs), 1)} | '
                         f'{fmt(median(r["limited_fraction"] for r in runs))} | '
                         f'{fmt(median(r["retrans_segs_source"] for r in runs), 0)} | '
                         f'{fmt(mx(r["netem_own_drops"] for r in runs), 0)} | '
                         f'{fmt(median(r["tbf_drops_source"] for r in runs), 0)} | '
                         f'{fmt(mx(r["netem_backlog_max"] for r in runs), 0)} |')
    return lines


def smoke_lines(S):
    """Smoke item 22's check: every Record path of the smoke table exists in the records."""
    lines = ['## The smoke table\'s records (every Record path, checked in the records)']
    missing = [(item, kind, path) for item, kind, path, _ in SMOKE_RECORDS if (item, kind, path) not in S.found]
    lines.append(f'- {len(SMOKE_RECORDS) - len(missing)} of {len(SMOKE_RECORDS)} paths found'
                 + ('; **missing**: ' + '; '.join(f'item {item}: {kind} {path}' for item, kind, path in missing)
                    if missing else ''))
    walls = {}
    for r in S.rows:
        if r['run_wall_s'] is not None:
            walls.setdefault((r['method'], r['speed']), []).append(r['run_wall_s'])
    if walls:
        lines.append('- smoke item 21, each run\'s wall (timed and untimed), median s: '
                     + ', '.join(f'{arm} {label} {fmt(statistics.median(v), 1)}'
                                 for (arm, label), v in sorted(walls.items(), key=lambda kv: (kv[0][0],
                                                                                              label_key(kv[0][1])))))
    return lines, missing


# --- every section ------------------------------------------------------------------

def verdicts(S, exp11, ranges=None, smoke=False):
    """Every section, in order; returns (markdown lines, the verdicts at each r)."""
    ranges = ranges or {}
    r1 = r1_reading(S)
    gates = {label: bound_gate(S, label) for label in S.labels}
    a10 = a10_gates(S, exp11)
    a12 = a12_reading(S)
    a17 = a17_spread(S)
    lines = counts_lines(S) + [''] + visit_lines(S) + [''] + network_run_lines(S) + [''] + durability_lines(S) + ['']
    lines += baseline_lines(S) + [''] + median_lines(S) + [''] + delta_lines(S) + [''] + ratio_lines(S) + ['']
    ledger, ledger_state = ledger_lines(S, r1)
    lines += ledger + ['']
    verdict, found = verdict_lines(S, r1, gates, a12, a17)
    lines += verdict + ['']
    crossing, crossings = crossover_lines(S)
    lines += crossing + ['']
    lines += a_lines(S, r1, ledger_state, found, gates, a10, a12, a17, a16_reading(S)) + ['']
    lines += Predictions(S, ranges, shifted(a10), input_drift(S)).all(r1, found, crossings) + ['']
    lines += network_lines(S, ranges)
    if smoke:
        lines += [''] + smoke_lines(S)[0]
    return lines, found


# --- data and figures -------------------------------------------------------------

def write_data(S, datadir, tag):
    datadir = Path(datadir)
    datadir.mkdir(parents=True, exist_ok=True)
    runs = datadir / f'exp13{tag}-runs.csv'
    with runs.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=KEEP)
        writer.writeheader()
        order = {w: i for i, w in enumerate(WORKLOADS)}
        for r in sorted(S.rows, key=lambda r: (order.get(r['workload'], 9), r['workload'], label_key(r['speed'] or ''),
                                               r['method'], r['repeat'])):
            writer.writerow({k: r.get(k) for k in KEEP})
    cells = datadir / f'exp13{tag}-cells.csv'
    with cells.open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['workload', 'condition', 'method', 'tier', 'n', 'a11_excluded', 'measured',
                         *(f'{c}_{s}' for c in CELL_COLUMNS for s in ('median', 'max'))])
        for arm in ARM_NAMES:
            for w in ([None] if S.pooled(arm) else S.workloads):
                for label in S.labels:
                    cell = S.cell(arm, label, w)
                    if not cell['ran']:
                        continue
                    values = [(median(r[c] for r in cell['runs']), mx(r[c] for r in cell['runs'])) for c in CELL_COLUMNS]
                    writer.writerow([w or 'pooled', label, arm, measure.TIERS.get(arm), cell['n'], cell['a11'],
                                     cell['measured'], *('' if v is None else round(v, 4) for pair in values for v in pair)])
    return runs, cells


def fig_rtt(S, tag):
    """Time against r, per workload (columns) and host setting (rows), each arm's median with a bar to its maximum."""
    hosts = [h for h in ('default', 'tuned') if any(condition(label)['host'] == h for label in S.labels)] or ['default']
    workloads = S.workloads or ['hot']
    fig, axes = plt.subplots(len(hosts), len(workloads), figsize=(4.6 * len(workloads), 3.8 * len(hosts)),
                             squeeze=False, sharey='row')
    for row, host in zip(axes, hosts):
        labels = [label for label in S.labels if condition(label)['host'] == host and condition(label)['gbit'] == 2]
        for ax, w in zip(row, workloads):
            for arm, name, color, ls, marker in ARMS:
                pts = []
                for label in labels:
                    cell = S.arm_cell(arm, label, w)
                    if cell['median'] is not None:
                        pts.append((condition(label)['rtt_ms'], cell['median'], cell['max']))
                if not pts:
                    continue
                pts.sort()
                ax.errorbar([p[0] for p in pts], [p[1] for p in pts], yerr=[[0] * len(pts), [p[2] - p[1] for p in pts]],
                            color=color, ls=ls, marker=marker, ms=4, capsize=2, lw=2.0 if arm == V1 else 1.1,
                            label=name)
            ax.set_yscale('log')
            ax.set_xticks([0, 20, 60, 120])
            ax.set_xlabel('Added round-trip time r (ms), 2 Gbit/s')
            ax.set_title(f'{WORKLOAD_LABEL.get(w, w)}, {host} TCP', fontsize=10.5)
            exp04.plain_log(ax)
        row[0].set_ylabel('Time, median (s; bar to maximum)')
    handles, names = [], []
    for ax in axes.flat:
        for handle, name in zip(*ax.get_legend_handles_labels()):
            if name not in names:
                handles.append(handle)
                names.append(name)
    fig.legend(handles, names, loc='lower center', ncol=4, fontsize=8, bbox_to_anchor=(0.5, -0.08))
    fig.suptitle('EXP-13: time against the added round trip, by workload and host TCP setting', fontsize=12)
    return plotting.save(fig, f'exp13-rtt{tag}')


def fig_ratios(S, tag):
    """R_fast and R_F0 against r, per workload, one line per host setting; EXP-09's bands at 0.90 and 1.00."""
    workloads = S.workloads or ['hot']
    fig, axes = plt.subplots(1, len(workloads), figsize=(4.6 * len(workloads), 3.9), squeeze=False, sharey=True)
    for ax, w in zip(axes[0], workloads):
        for host, color in (('default', COLORS['blue']), ('tuned', COLORS['orange'])):
            for envelope, ls, marker in (('B_fast', '-', 'o'), ('F0', ':', 's')):
                pts = []
                for label in S.labels:
                    c = condition(label)
                    if c['host'] != host or c['gbit'] != 2:
                        continue
                    value = (S.ratios(w, label).get(envelope) or {}).get('median')
                    if value is not None:
                        pts.append((c['rtt_ms'], value))
                if pts:
                    pts.sort()
                    ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color, ls=ls, marker=marker, ms=4,
                            label=f'{RATIO_NAME[envelope]}, {host} TCP')
        for line, style in ((HOLD, '--'), (OUT, '-')):
            ax.axhline(line, color=COLORS['black'], lw=0.8, ls=style)
        ax.set_xticks([0, 20, 60, 120])
        ax.set_xlabel('Added round-trip time r (ms), 2 Gbit/s')
        ax.set_title(WORKLOAD_LABEL.get(w, w), fontsize=10.5)
    axes[0][0].set_ylabel("v1's median over the baseline's")
    handles, names = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, names, loc='lower center', ncol=4, fontsize=8, bbox_to_anchor=(0.5, -0.1))
    fig.suptitle('EXP-13: v1 against the fastest durable copy (R_fast) and one pg_basebackup stream (R_F0)', fontsize=12)
    return plotting.save(fig, f'exp13-ratios{tag}')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sweeps', nargs='+', default=[str(p) for p in SWEEPS],
                        help='results directories from remote.py fetch (default: the three invocations)')
    parser.add_argument('--model-sweeps', nargs='*',
                        help="the smoke run's results, whose sysctls and one-stream rates the model recomputes the "
                             'E-ranges from (default: --sweeps; none given: the written inputs)')
    parser.add_argument('--exp11-cells', default=str(DATA / 'exp11-cells.csv'))
    parser.add_argument('--smoke', action='store_true',
                        help="check the smoke table's Record paths in the records, and each run's wall (smoke items "
                             '21 and 22)')
    parser.add_argument('--datadir', default=str(DATA))
    parser.add_argument('--figdir', help='write figures here instead of docs/figures')
    parser.add_argument('--tag', default='', help='suffix for data and figure names (e.g. -smoke)')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    sweeps = [Path(s) for s in args.sweeps if Path(s, 'summary.csv').exists()]
    if not sweeps:
        raise SystemExit(f'no summary.csv under {", ".join(args.sweeps)}')
    if args.figdir:
        plotting.FIGURES = Path(args.figdir)
    plotting.style()
    rows, trials, runs, found = [], {}, [], set()
    for sweep in sweeps:
        r, t, run, paths = load(sweep)
        rows += r
        trials.update(t)
        runs.append(run)
        found |= paths
    S = Session(rows, trials, runs, found)
    model_sweeps = sweeps if args.model_sweeps is None else [Path(s) for s in args.model_sweeps]
    inputs = (model.sweep_inputs(model_sweeps) if model_sweeps else None) or model.written_inputs()
    ranges = model.Model(inputs).ranges()
    lines, _ = verdicts(S, load_exp11(args.exp11_cells), ranges, args.smoke)
    print(f'{len(rows)} runs from {", ".join(str(s) for s in sweeps)}')
    print(*write_data(S, args.datadir, args.tag), sep='\n')
    print('\n'.join(lines))
    print('\n'.join([''] + model.lines(ranges, inputs)))
    print(fig_rtt(S, args.tag))
    print(fig_ratios(S, args.tag))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
