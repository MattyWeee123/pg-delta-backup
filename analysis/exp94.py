"""EXP-94: does the sender keeping its stream full remove v1's need for BBR at DR round trips?

    .venv/bin/python analysis/exp94.py [--sweep results/exp04/exp94] [--figdir DIR]

The write-up is docs/experiments/EXP-94-dr-hash-ahead.md. This scores the predictions EXP-20's scorer has no
notion of: EXP-94's are arm-against-arm in the same cell - v1 with `--hash-ahead` against the same engine
without it - where EXP-20's judge an arm against the full copies. Run `analysis/exp20.py --v1 ARM --tag ...`
for the verdict rows; run this for the deciding reading.

What it prints, each beside the bar the registration states and the noise band rule 17 gives:

  1  uniform: `_ha` / `pgdelta_direct_inplace`, bar 0.80 (the deciding prediction)
  2  rule 17's cross-session gate: this session's `full` against EXP-24's committed times, bar 1%
  3  hot, the negative control: `_ha` / `pgdelta_direct_inplace` within 1.03, ties inside the noise band
  4  uniform: `_bbr_ha` / `_bbr` within 1.05 - whether the two fixes address the same stall
  5  the source's disk reads within 1.05, so EXP-91's second reads have not reappeared
  6  the mechanism: `leaf_wait_idle_s`, the sender blocked with an idle socket, at or below 0.25
  7  the slow mode, counted by EXP-23's own rule and its exact interval

EXP-23's rule and interval are imported, not restated, so the counts stay comparable with EXP-23's and
EXP-24's. Writes docs/experiments/data/exp94-cells.csv and docs/figures/exp94-hash-ahead.svg.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import statistics
from pathlib import Path

import exp23
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data'

V1, BBR = 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_bbr'
HA, BBR_HA = 'pgdelta_direct_inplace_ha', 'pgdelta_direct_inplace_bbr_ha'
FULL = 'full'

# The registration's bars. Each one is checked against the write-up's text by tests/test_exp94_analysis.py, so
# a bar cannot be loosened here without the document disagreeing.
P1_BAR = 0.80          # uniform: _ha / v1, the deciding prediction (EXP-91 measured 0.785 and 0.777 at no delay)
P3_BAR = 1.03          # hot: the negative control, where the mechanism predicts no gain
P4_BAR = 1.05          # uniform: _bbr_ha / _bbr, whether hashing ahead adds anything once BBR is in play
P5_BAR = 1.05          # the source's disk reads, so EXP-91's second reads have not reappeared
P6_BAR = 0.25          # the sender's idle leaf wait: the stream-full claim, measured
COMPARABILITY_BAR = 0.01   # rule 17: a cross-session comparison needs its control arm within 1%
# EXP-23's own definition, in its words: "the time send() blocked over page data (pgdelta's page_send_s)
# against the time the condition's measured one-stream link rate needs for those bytes; above 2x is the slow
# mode". Named here rather than extracted into exp23.py, because exp23.py's hash is registered for EXP-23 and
# this needs no change there; tests/test_exp94_analysis.py ties this constant to that docstring.
SLOW_MODE_FACTOR = 2.0

# Rule 17's within-session spread as of EXP-92: two runs of a cell differ by up to this much at the 95th
# percentile. A difference inside the band is a tie, never a gain (research/noise-floor.csv).
NOISE_LONG, NOISE_SHORT, NOISE_SHORT_SECONDS = 0.0192, 0.0711, 10.0

# EXP-24's committed cells, the control this session's `full` is compared with. Both of EXP-24's arms ran in one
# session with one set of baselines, so either file carries the same `full`; the cubic one is read.
EARLIER_FULL_CSV = 'exp20-cells-exp24-inplace.csv'

# The one cell prediction 7 names: where EXP-23 found cubic in the slow mode 12 of 15 times.
SLOW_MODE_CELL = '2:20:default'

CONDITIONS = ('2:0:default', '2:20:default', '2:60:default', '2:120:default', '2:60:tuned', '2:120:tuned')


# --- reading a sweep ---------------------------------------------------------------------------------------

def cells(sweep) -> dict:
    """{(pattern, condition, method): (median total seconds, runs)} over the passing runs of summary.csv."""
    out: dict = {}
    with open(Path(sweep) / 'summary.csv', newline='', encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            if row['status'] != 'pass':
                continue
            out.setdefault((row['pattern'], row['speed'], row['method']), []).append(float(row['total_s']))
    return {key: (statistics.median(values), len(values)) for key, values in out.items()}


def runs(sweep) -> list:
    """Every passing run's record, from runs/*.json - what summary.csv does not carry as a column.

    The source's disk counters and the sender's protocol timers are in each record; only dest's disk bytes
    reach the CSV (measure.CSV_FIELDS), so predictions 5, 6 and 7 read the records.
    """
    out = []
    for path in sorted(glob.glob(str(Path(sweep) / 'runs' / '*.json'))):
        with open(path, encoding='utf-8') as stream:
            record = json.load(stream)
        if record.get('status') != 'pass':
            continue
        out.append(record)
    return out


def condition_of(record):
    speed = record.get('speed')
    return speed.get('label') if isinstance(speed, dict) else speed


def reported(record, name):
    """One of pgdelta's reported timers, or None where this run did not report it."""
    return (record.get('tool_reported') or {}).get(name)


def source_read_bytes(record):
    """Bytes the source's data array read over the timed phases, from the record's own counters.

    measure.phase_disk_bytes(record, 'source', 'read_bytes') in the harness; repeated here because the
    analysis must not import the harness (it runs on the VMs, and pulls in fcntl at import).
    """
    phases = list((record.get('phases') or {}).values())
    if not phases:
        return None
    total = 0
    for phase in phases:
        disks = ((phase.get('disk') or {}).get('source'))
        if not isinstance(disks, dict):
            return None
        for name, counters in disks.items():
            if not name.startswith('md'):
                continue
            value = (counters or {}).get('read_bytes')
            if value is None:
                return None
            total += value
    return total


def medians_by_cell(records, pattern, value):
    """{(condition, method): median of `value(record)`} over records of `pattern`, skipping Nones."""
    gathered: dict = {}
    for record in records:
        if record.get('pattern') != pattern:
            continue
        got = value(record)
        if got is None:
            continue
        gathered.setdefault((condition_of(record), record['method']), []).append(got)
    return {key: statistics.median(values) for key, values in gathered.items()}


# --- the bars ----------------------------------------------------------------------------------------------

def noise_band(seconds) -> float:
    """Rule 17's within-session spread for a cell whose runs take `seconds`: the short-run band under 10 s."""
    return NOISE_SHORT if seconds < NOISE_SHORT_SECONDS else NOISE_LONG


def ratio(cell, pattern, condition, numerator, denominator):
    """median(numerator) / median(denominator) in one cell, or None where either arm has no passing run."""
    top, bottom = cell.get((pattern, condition, numerator)), cell.get((pattern, condition, denominator))
    if not top or not bottom or not bottom[0]:
        return None
    return top[0] / bottom[0]


def _rows(cell, pattern, numerator, denominator, judge):
    out = []
    for condition in CONDITIONS:
        value = ratio(cell, pattern, condition, numerator, denominator)
        if value is None:
            continue
        base = cell[(pattern, condition, denominator)]
        out.append({'condition': condition, 'pattern': pattern, 'numerator': numerator,
                    'denominator': denominator, 'ratio': round(value, 4), 'base_s': round(base[0], 3),
                    'runs': base[1], **judge(value, base[0])})
    return out


def slow_mode_cells(sweep) -> set:
    """The conditions where cubic v1 is in the slow mode in THIS session: a majority of its uniform runs slow.

    Prediction 1 is judged here and reported elsewhere, and the set is taken from this session rather than
    named in the registration, because the two prior sessions disagree about which cells they are: EXP-20
    measured uniform R_fast 2.578 at 2:60:default and EXP-24 measured 1.645 in the same cell with the same arm.
    The selector is prediction 7's mechanical rule (page_send_s above 2.0x the measured link floor), registered
    before the data - never the time ratio, which would make the choice post hoc.
    """
    found = set()
    for row in prediction7(sweep):
        if row['method'] != V1 or not row['runs']:
            continue
        if row['slow'] * 2 >= row['runs']:
            found.add(row['condition'])
    return found


def prediction1(cell, slow_cells=None) -> list:
    """Uniform 1%: `_ha` at or below 0.80 of v1. A ratio in 0.80-0.98 is a real but smaller gain, not a pass.

    With `slow_cells` (slow_mode_cells()), only those cells are judged; the rest carry their ratio and say so.
    A cell where cubic never enters the slow mode is a cell where the mechanism predicts nothing to remove.
    Without it every cell is judged, which is what the scorer did before the deciding set was introduced.
    """
    def judge(value, _seconds):
        if value <= P1_BAR:
            verdict = 'held'
        elif value < 0.98:
            verdict = 'gained less than the bar'
        else:
            verdict = 'failed'
        return {'bar': P1_BAR, 'verdict': verdict}
    rows = _rows(cell, 'uniform', HA, V1, judge)
    for row in rows:
        deciding = True if slow_cells is None else row['condition'] in slow_cells
        row['deciding'] = deciding
        if not deciding:
            row['verdict'] = 'reported (cubic not in the slow mode here)'
    return rows


def prediction2(cell, earlier) -> list:
    """Rule 17's gate: this session's `full` within 1% of EXP-24's in each shared cell."""
    out = []
    for (pattern, condition), before in sorted(earlier.items()):
        now = cell.get((pattern, condition, FULL))
        if not now or not before:
            continue
        value = now[0] / before
        out.append({'pattern': pattern, 'condition': condition, 'ratio': round(value, 4),
                    'now_s': round(now[0], 3), 'earlier_s': before, 'bar': COMPARABILITY_BAR,
                    'verdict': 'held' if abs(value - 1.0) <= COMPARABILITY_BAR else 'failed'})
    return out


def prediction3(cell) -> list:
    """Hot, the negative control: `_ha` within 1.03 of v1, and a difference inside the noise band is a tie."""
    def judge(value, seconds):
        band = noise_band(seconds)
        if abs(value - 1.0) <= band:
            verdict = 'tie (inside the noise band)'
        elif value < 1.0:
            verdict = 'gained in hot, which the mechanism does not predict'
        elif value <= P3_BAR:
            verdict = 'held'
        else:
            verdict = 'failed (slower in hot)'
        return {'bar': P3_BAR, 'band': round(band, 4), 'verdict': verdict}
    return _rows(cell, 'hot', HA, V1, judge)


def prediction4(cell, gate=None) -> list:
    """Uniform 1%: `_bbr_ha` within 1.05 of `_bbr`. Clearly faster means the fixes address different stalls.

    `gate` is congestion_gate()'s result. A failing gate makes every row unreadable rather than wrong: both
    arms would have run cubic, so the comparison is prediction 1 again under another name.
    """
    if gate is not None and not gate['ok']:
        def refuse(_value, _seconds):
            return {'bar': P4_BAR, 'verdict': 'not readable: a BBR arm did not get BBR'}
        return _rows(cell, 'uniform', BBR_HA, BBR, refuse)

    def judge(value, _seconds):
        # Clearly faster with both than with BBR alone: 0.95 is the mirror of the 1.05 bar, so a gain has to
        # be as large as the tolerance before it counts as one.
        if value <= 2.0 - P4_BAR:
            verdict = 'a further gain: the two fixes address different stalls'
        elif value <= P4_BAR:
            verdict = 'held (the two fixes address the same stall)'
        else:
            verdict = 'failed (slower with both)'
        return {'bar': P4_BAR, 'verdict': verdict}
    return _rows(cell, 'uniform', BBR_HA, BBR, judge)


def prediction5(sweep) -> list:
    """The source's disk reads within 1.05 of v1's, so EXP-91's second reads have not reappeared at 2.1 GB."""
    by_cell = medians_by_cell(runs(sweep), 'uniform', source_read_bytes)
    out = []
    for condition in CONDITIONS:
        base, arm = by_cell.get((condition, V1)), by_cell.get((condition, HA))
        if not base or arm is None:
            continue
        value = arm / base
        out.append({'condition': condition, 'ratio': round(value, 4), 'base_bytes': int(base),
                    'arm_bytes': int(arm), 'bar': P5_BAR,
                    'verdict': 'held' if value <= P5_BAR else 'failed'})
    return out


def prediction6(sweep) -> list:
    """The mechanism: `_ha`'s idle leaf wait at or below 0.25 of v1's.

    `leaf_wait_idle_s` is the time the sender was blocked waiting for leaf digests while its socket had nothing
    unsent and nothing in flight (pgdelta/waits.py). An idle leaf wait *is* a stream that is not full, so this
    is EXP-21's "keeping its stream full between files" in measurable form. A cell where v1 never waited idle
    had no stall to remove, and is reported as such rather than divided by.
    """
    by_cell = medians_by_cell(runs(sweep), 'uniform', lambda record: reported(record, 'leaf_wait_idle_s'))
    out = []
    for condition in CONDITIONS:
        base, arm = by_cell.get((condition, V1)), by_cell.get((condition, HA))
        if base is None or arm is None:
            continue
        if not base:
            out.append({'condition': condition, 'ratio': None, 'base_s': round(base, 3),
                        'arm_s': round(arm, 3), 'bar': P6_BAR, 'verdict': 'no idle wait to remove'})
            continue
        value = arm / base
        out.append({'condition': condition, 'ratio': round(value, 4), 'base_s': round(base, 3),
                    'arm_s': round(arm, 3), 'bar': P6_BAR,
                    'verdict': 'held' if value <= P6_BAR else 'failed'})
    return out


def link_rates(sweep) -> dict:
    """{condition label: [each visit's measured one-stream steady rate]} from the sweep's trials.

    The same reading exp23.link_rates takes, done here rather than imported: exp23.py leaves the trial files
    open (its `json.load(open(tf))`), which turns this analysis's test output from pristine into a page of
    ResourceWarnings, and exp23.py's hash is registered for EXP-23 so it is not edited for this. The slow-mode
    *rule* and its interval still come from exp23 - only the file reading is local.
    """
    out: dict = {}
    for path in sorted(glob.glob(str(Path(sweep) / 'trials' / '*.json'))):
        with open(path, encoding='utf-8') as stream:
            trial = json.load(stream)
        for visit in trial.get('visits') or []:
            condition = visit.get('condition')
            label = condition.get('label') if isinstance(condition, dict) else condition
            for test in visit.get('link_tests') or []:
                if isinstance(test, dict) and test.get('steady_gbit'):
                    out.setdefault(label, []).append(test['steady_gbit'])
    return out


def link_floor_seconds(record, link_gbit) -> float:
    """The seconds the condition's measured one-stream rate needs for the bytes this run sent (EXP-23's floor)."""
    sent = (record.get('totals') or {}).get('wire_bytes_source_to_dest')
    if not sent or not link_gbit:
        return 0.0
    return sent * 8 / (link_gbit * 1e9)


def prediction7(sweep) -> list:
    """The slow mode by EXP-23's rule: page_send_s above SLOW_MODE_FACTOR times the link floor.

    Expected, per the registration: v1 in at least 2 of its 3 uniform runs at 2:20:default (EXP-23 found 12 of
    15 there), `_ha` in 0 of 3 (BBR was in 0 of 57 across three sessions). With 3 runs a cell the exact
    interval is wide; it is reported rather than hidden.
    """
    links = link_rates(sweep)
    counted: dict = {}
    for record in runs(sweep):
        if record.get('pattern') != 'uniform' or record['method'] not in (V1, BBR, HA, BBR_HA):
            continue
        page_send = reported(record, 'page_send_s')
        rates = links.get(condition_of(record)) or []
        if page_send is None or not rates:
            continue
        floor = link_floor_seconds(record, min(rates))
        if not floor:
            continue
        key = (record['method'], condition_of(record))
        slow, total = counted.get(key, (0, 0))
        counted[key] = (slow + (1 if page_send / floor > SLOW_MODE_FACTOR else 0), total + 1)
    out = []
    for (method, condition), (slow, total) in sorted(counted.items()):
        # The registration predicts two cells and only two: cubic v1 in at least 2 of 3 at 2:20:default (where
        # EXP-23 found 12 of 15), and _ha in 0 of 3 there. Every other cell is a reading, not a prediction, and
        # says so rather than borrowing a verdict.
        if method == V1 and condition == SLOW_MODE_CELL:
            expected, held = 'at least 2 of 3', slow >= 2
        elif method == HA and condition == SLOW_MODE_CELL:
            expected, held = '0 of 3', slow == 0
        else:
            expected, held = None, None
        low, high = exp23.interval(slow, total)
        out.append({'method': method, 'condition': condition, 'slow': slow, 'runs': total,
                    'interval': (round(low, 4), round(high, 4)), 'expected': expected,
                    'verdict': 'reported, not predicted' if held is None else ('held' if held else 'failed')})
    return out


def congestion_gate(sweep) -> dict:
    """Whether every run that asked for BBR actually got it.

    pgdelta asks with setsockopt(TCP_CONGESTION) and **does not fail if the host refuses**: an unprivileged
    process may choose only what net.ipv4.tcp_allowed_congestion_control lists, and
    transport.set_congestion_control then records {'effective': 'cubic', 'error': ...} while the transfer goes
    on. run_exp04.py checks nothing about it. So if EXP-94's one host-setting step is forgotten or does not
    stick, both BBR arms run cubic, every run passes, and prediction 4 compares cubic-with-hashing-ahead
    against cubic - prediction 1 again under another name. EXP-24 caught this by reading the statistics after
    the fact; here the scorer refuses to read prediction 4 without them.

    Returns {'requested', 'effective', 'bad': [{'run', 'effective', 'error'}], 'ok', 'reason'}.
    """
    requested, effective, bad = 0, 0, []
    for path in sorted(glob.glob(str(Path(sweep) / 'logs' / '*' / 'source' / '*-stats.json.log'))):
        text = Path(path).read_text(encoding='utf-8', errors='replace')
        brace = text.find('{')
        if brace < 0:
            continue
        try:
            stats = json.loads(text[brace:])
        except ValueError:
            continue
        block = stats.get('congestion_control') or {}
        if block.get('requested') != 'bbr':
            continue
        requested += 1
        if block.get('effective') == 'bbr' and not block.get('error'):
            effective += 1
        else:
            bad.append({'run': Path(path).name.replace('-stats.json.log', ''),
                        'effective': block.get('effective'), 'error': block.get('error') or ''})
    if not requested:
        reason = ('no run asked for BBR in its statistics: either the BBR arms did not run, or their logs were '
                  'not fetched. Absent statistics are not evidence that BBR worked')
    elif bad:
        reason = (f'{len(bad)} of {requested} runs asked for BBR and did not get it. The source needs BBR in '
                  "net.ipv4.tcp_allowed_congestion_control (EXP-24's one host setting)")
    else:
        reason = f'all {requested} runs that asked for BBR got it'
    return {'requested': requested, 'effective': effective, 'bad': bad,
            'ok': bool(requested) and not bad, 'reason': reason}


def earlier_full() -> dict:
    """{(pattern, condition): EXP-24's `full` seconds} from the committed CSV, never retyped here."""
    out = {}
    with open(DATA / EARLIER_FULL_CSV, newline='', encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            if row.get('full'):
                out[(row['pattern'], row['condition'])] = float(row['full'])
    return out


# --- output ------------------------------------------------------------------------------------------------

def report(sweep) -> dict:
    cell = cells(sweep)
    gate = congestion_gate(sweep)
    slow = slow_mode_cells(sweep)
    return {'gate': gate, 'slow_cells': slow,
            '1': prediction1(cell, slow_cells=slow), '2': prediction2(cell, earlier_full()),
            '3': prediction3(cell),
            '4': prediction4(cell, gate=gate), '5': prediction5(sweep), '6': prediction6(sweep),
            '7': prediction7(sweep)}


TITLES = {
    '1': 'P1 (deciding) uniform 1%: --hash-ahead / v1, bar 0.80',
    '2': "P2 rule 17's gate: this session's `full` / EXP-24's, bar 1%",
    '3': 'P3 negative control, hot: --hash-ahead / v1, bar 1.03 (ties inside the noise band)',
    '4': 'P4 uniform 1%: --hash-ahead with BBR / BBR alone, bar 1.05',
    '5': "P5 the source's disk reads: --hash-ahead / v1, bar 1.05",
    '6': 'P6 the mechanism: idle leaf wait, --hash-ahead / v1, bar 0.25',
    '7': "P7 the slow mode, by EXP-23's rule (page_send_s > 2.0x the link floor)",
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or '').splitlines()[0])
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'exp94'))
    ap.add_argument('--figdir')
    ap.add_argument('--out', default=str(DATA / 'exp94-cells.csv'),
                    help='where the scored cells go. Point it away from docs/ when scoring anything but the '
                         'session: a smoke run over synthetic records wrote fake numbers into the committed '
                         'data directory before this option existed')
    args = ap.parse_args(argv)
    if args.figdir:
        plotting.FIGURES = Path(args.figdir)
    plotting.style()
    sweep = Path(args.sweep)
    got = report(sweep)

    gate = got['gate']
    print()
    print(f"BBR gate: {gate['effective']} of {gate['requested']} runs that asked for BBR got it - "
          f"{gate['reason']}")
    for run in gate['bad']:
        print(f"  did not get BBR: {run['run']} (effective {run['effective']!r}) {run['error']}")

    if got['slow_cells']:
        print(f"deciding cells for P1 (cubic in the slow mode here): {', '.join(sorted(got['slow_cells']))}")
    else:
        print('deciding cells for P1: none - cubic did not enter the slow mode in any cell of this session, '
              'so P1 has nothing to decide and every cell is reported')

    flat = []
    for key in ('1', '2', '3', '4', '5', '6', '7'):
        print(f'\n{TITLES[key]}')
        if not got[key]:
            print('  (no cell has both arms passing)')
            continue
        for row in got[key]:
            flat.append({'prediction': key, **row})
            if key == '7':
                low, high = row['interval']
                print(f"  {row['method']:34s} {row['condition']:14s} {row['slow']} of {row['runs']} slow "
                      f"({100 * low:.0f}-{100 * high:.0f}%); {row['verdict']}")
            else:
                value = row.get('ratio')
                shown = '-' if value is None else f'{value:.3f}'
                where = f"{row.get('pattern', 'uniform'):8s} {row['condition']:14s}"
                print(f"  {where} {shown:>7s}  {row['verdict']}")

    out = Path(args.out)
    if flat:
        names = sorted({name for row in flat for name in row})
        with open(out, 'w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=['prediction'] + [n for n in names if n != 'prediction'])
            writer.writeheader()
            writer.writerows(flat)
        print(f'\n{out}')

    # One figure: the deciding reading and the mechanism beside it, so a reader can see whether the stall that
    # was removed is the one that governed the time.
    p1 = {row['condition']: row['ratio'] for row in got['1']}
    p6 = {row['condition']: row['ratio'] for row in got['6'] if row['ratio'] is not None}
    if p1:
        fig, ax = plt.subplots(figsize=(8.8, 4.4))
        order = [c for c in CONDITIONS if c in p1]
        xs = range(len(order))
        ax.plot(list(xs), [p1[c] for c in order], '-o', color=COLORS['blue'],
                label='time: --hash-ahead ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· v1 (uniform 1%)')
        if p6:
            ax.plot([i for i, c in enumerate(order) if c in p6], [p6[c] for c in order if c in p6],
                    '--s', color=COLORS['orange'], label='idle leaf wait: --hash-ahead ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· v1')
        for bar, style, text in ((P1_BAR, ':', f'P1 bar {P1_BAR:g}'), (1.0, '-', 'no change')):
            ax.axhline(bar, color=COLORS['black'], lw=0.8, ls=style)
            ax.text(len(order) - 0.98, bar, f' {text}', fontsize=7.5, va='center')
        ax.set_xticks(list(xs), order, rotation=20, ha='right')
        ax.set_ylabel('ratio (below 1: hashing ahead is better)')
        ax.set_title("EXP-94: what hashing ahead changed, and whether it changed the stall it targets",
                     fontsize=11)
        ax.legend(fontsize=8)
        fig.tight_layout()
        print(plotting.save(fig, 'exp94-hash-ahead'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
