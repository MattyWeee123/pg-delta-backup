"""Summarize an EXP-04 summary.csv: median time per method and link speed, and the crossover.

    python experiments/network/report.py results/exp04/NAME/summary.csv [--out report.md]

For every trial it prints the median time of each method at each link speed
(passing runs only), the time relative to the full copy, and the first speed,
going up, at which each delta method is no longer faster than the full copy.
Speeds are ordered by target rate; 'unthrottled' sorts last at its measured
rate.

The headline, and the crossover, use `total_s`: end to end, as measured,
every timed phase including rsync's copy of B0 and the SSH launch of remote
commands. rsync's copy of B0 (on dest) runs at the same time as its capture of
S (on source), so its `total_s` is max(prep, capture) + transfer. pgdelta's
receiver starts (on dest) at the same time as its capture, so its `total_s`
is max(receiver start, capture) + transfer. More views are printed and
labelled as such:
- 'rsync_* (serial)' and 'pgdelta* (serial)' rows: the two parts of that
  concurrent phase one after the other (`serial_total_s`), the upper bound.
  The two differ: rsync's copy of B0, which reads B0 cold, comes before the
  capture, while pgdelta's receiver starts first and still prefetches B0
  during the capture, so pgdelta's serial rows hold no cold read of B0;
- 'rsync_* (in place)' rows: rsync without its copy of B0 (`inplace_total_s`),
  the lower bound for patching B0 directly;
- the same tables on `total_no_ssh_s`, with the remote-launch overhead (outer
  minus inner wall of each remote command, about 60 ms each, where it is on
  the critical path) taken out.

The optional methods of T3, T8a and T11 are in the same tables. What their
`total_s` holds:
- T3's staged methods (pgdelta_gapfill_seq, pgdelta_seq, pgdelta_fsync_wb,
  pgdelta_fsync_worker, pgdelta_inplace): as pgdelta, with serial rows too.
  pgdelta_inplace patches B, a copy of B0 made before the timers (untimed);
- pgdelta_direct*: receiver start + transfer, one timed phase with no
  capture; the source was stopped before the timers (untimed);
- pgbackrest_*: the restore alone; the backup is counted as already paid.

After the totals, a table of median timed phases per method and speed shows
where each method's time goes, with pgdelta's own timers for its write phase
(`rebuild_s`, `fsync_s`, `fsync_wait_s`), which lie inside its transfer.

pgdelta runs whose prefetch read B0 between its two timed phases, off the
clock (`prefetch_untimed_s` > 0), are listed with that time and an estimate
of the bytes; their `total_s` can understate by about that time.
pgdelta_direct* has no such gap: its receiver start and transfer are one
timed phase.

T2's arms (EXP-11) are in the same tables: the pg_basebackup arms (full_*),
probackup* (catchup, then a timed sync -f of the output) and parcopy*, each one
timed phase. The extension's runs count with the main block's at the
unthrottled link, as EXP-11 compares them; the warm-source block's runs, whose
source cache was not dropped, are rows of their own, 'METHOD (warm source)'.
The phase table adds their own timers (pg_basebackup's --verbose lines,
pg_probackup's, parcopy's) and the time above link time. Runs that failed
EXP-11's durability checks are listed; their status is their verification's.
analysis/exp11.py judges EXP-11's predictions and rules.

Ratios are computed from the medians and rounded only for display; "faster"
compares the medians themselves.
"""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
import statistics

NUMERIC = ('fraction', 'target_gbit', 'measured_s2d_gbit', 'measured_d2s_gbit', 'total_s',
           'total_no_ssh_s', 'inplace_total_s', 'inplace_total_no_ssh_s', 'serial_total_s',
           'serial_total_no_ssh_s', 'prep_s', 'capture_s', 'prep_capture_s', 'receiver_start_s',
           'receiver_capture_s', 'transfer_s', 'prefetch_bytes', 'prefetch_basis_bytes', 'prefetch_s',
           'prefetch_wait_s', 'prefetch_untimed_s', 'prefetch_untimed_bytes_est', 'untimed_gap_s',
           'hash_basis_s', 'wire_s2d_bytes', 'wire_d2s_bytes', 'cpu_source_s', 'cpu_dest_s',
           'combine_s', 'restore_s', 'untimed_setup_s', 'rebuild_s', 'fsync_s', 'fsync_wait_s',
           'verify_wait_s', 'output_start_s', 'recovery_wire_s2d_bytes', 'recovery_wire_d2s_bytes',
           'recovery_wal_files', 'output_shared_bytes',
           # T2 (EXP-11)
           'measured_s2d_gbit_8streams', 'dirty_increase_mb', 'disk_write_dest_bytes', 'disk_read_dest_bytes',
           'pbb_checkpoint_s', 'pbb_stream_s', 'pbb_wal_end_s', 'pbb_sync_s', 'pbb_manifest_s',
           'backup_mode_adder_s', 'probackup_sync_s', 'probackup_copy_s', 'probackup_stop_s',
           'probackup_files_sync_s', 'parcopy_streams', 'parcopy_max_stream_bytes', 'parcopy_balance',
           'parcopy_first_byte_s', 'parcopy_last_byte_s', 'parcopy_write_s', 'parcopy_fsync_s',
           'parcopy_fsync_calls', 'parcopy_fsync_expected', 'usable_s', 'link_time_s', 'above_link_s',
           'sysctl_dirty_background_bytes', 'sysctl_dirty_background_ratio')
DELTA_METHODS = ('incremental', 'pgdelta', 'pgdelta_1thread', 'rsync_8k', 'rsync_default',
                 'pgdelta_gapfill_seq', 'pgdelta_seq', 'pgdelta_fsync_wb', 'pgdelta_fsync_worker',
                 'pgdelta_inplace', 'incremental_cfr', 'pgdelta_direct', 'pgdelta_direct_inplace',
                 'pgdelta_direct_inplace_j8', 'pgbackrest_zst', 'pgbackrest_none', 'pgbackrest_block')
# The median phase table: timed phases (and parts) in the order they run, then pgdelta's own
# timers for its write phase, which lie inside its transfer, then T2's tools' own timers inside
# their one phase (pg_basebackup's --verbose lines, pg_probackup's lines and its timed sync -f,
# parcopy's receiver), then the total and, for T2, its part above link time.
PHASE_COLUMNS = ('receiver_start_s', 'capture_s', 'prep_s', 'transfer_s', 'combine_s', 'restore_s',
                 'rebuild_s', 'fsync_s', 'fsync_wait_s', 'verify_wait_s',
                 'pbb_checkpoint_s', 'pbb_stream_s', 'pbb_wal_end_s', 'pbb_sync_s', 'pbb_manifest_s',
                 'probackup_copy_s', 'probackup_stop_s', 'probackup_files_sync_s', 'probackup_sync_s',
                 'parcopy_last_byte_s', 'parcopy_fsync_s', 'total_s', 'above_link_s')
IN_PLACE = ' (in place)'
SERIAL = ' (serial)'
WARM_SOURCE = ' (warm source)'  # T2's warm-source block: rows of their own
# (column, heading)
VIEWS = (
    ('total_s',
     "end to end, as measured: every timed phase, rsync's copy of B0 included (run at the "
     "same time as its capture, so max(prep, capture) + transfer), pgdelta's receiver start-up "
     'included (run at the same time as its capture, so max(receiver start, capture) + '
     'transfer), and the SSH launch of remote commands'),
    ('total_no_ssh_s',
     'end to end with the remote-launch overhead taken out (outer minus inner wall of each '
     'remote command, where it is on the critical path); not the headline'),
)
# Extra rows per rsync or pgdelta run (wherever the column is set): suffix, then the
# columns standing in for total_s and total_no_ssh_s.
EXTRA_ROWS = (
    (SERIAL, 'serial_total_s', 'serial_total_no_ssh_s'),
    (IN_PLACE, 'inplace_total_s', 'inplace_total_no_ssh_s'),
)


def _number(text):
    if text in (None, ''):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def load(path: Path) -> list[dict]:
    with open(path, newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for key in NUMERIC:
            row[key] = _number(row.get(key))
    return rows


def with_in_place(rows) -> list[dict]:
    """rows plus the bounds of each rsync and pgdelta run, as rows of their own.

    'METHOD (serial)' (rsync and pgdelta): the two parts of its concurrent phase
    one after the other (rsync's copy of B0, or pgdelta's receiver start-up, then
    the capture), the upper bound. 'METHOD (in place)' (rsync): without its copy
    of B0, the lower bound.
    """
    expanded = []
    for row in rows:
        expanded.append(row)
        for suffix, total, no_ssh in EXTRA_ROWS:
            if row.get(total) is not None:
                expanded.append({**row, 'method': row['method'] + suffix,
                                 'total_s': row[total], 'total_no_ssh_s': row.get(no_ssh)})
    return expanded


def speed_order(rows) -> list[str]:
    """Speed labels from slowest to fastest; unthrottled by its measured rate."""
    rates: dict[str, list[float]] = {}
    for row in rows:
        rate = row['target_gbit'] if row['target_gbit'] is not None else row['measured_s2d_gbit']
        rates.setdefault(row['speed'], []).append(rate if rate is not None else math.inf)
    return sorted(rates, key=lambda label: (statistics.median(rates[label]), label))


def medians(rows, value: str = 'total_s') -> dict:
    """(trial_id, speed) -> method -> median of value over passing runs."""
    groups: dict = {}
    for row in rows:
        if row.get('status') != 'pass' or row.get(value) is None:
            continue
        groups.setdefault((row['trial_id'], row['speed']), {}).setdefault(
            row['method'], []).append(row[value])
    return {key: {method: statistics.median(values) for method, values in methods.items()}
            for key, methods in groups.items()}


def crossover(table: dict, speeds: list[str], trial_id: str, method: str,
              baseline: str = 'full') -> dict:
    """Ratios method/baseline per speed and the first speed where method is not faster.

    The ratios are not rounded (round them for display only), and "not faster"
    compares the two medians themselves, so a method 1 ms faster than the
    baseline counts as faster even where its ratio would round to 1.
    """
    ratios, slower = {}, []
    for speed in speeds:
        cell = table.get((trial_id, speed), {})
        if method in cell and baseline in cell and cell[baseline] > 0:
            ratios[speed] = cell[method] / cell[baseline]
            if cell[method] >= cell[baseline]:
                slower.append(speed)
    stops = slower[0] if slower else None
    return {'ratios': ratios, 'not_faster_from': stops,
            'faster_everywhere': bool(ratios) and stops is None}


def _tables(table, speeds, trial_id, methods) -> list[str]:
    lines = ['| method | ' + ' | '.join(speeds) + ' |', '|---|' + '---|' * len(speeds)]
    for method in methods:
        cells = [table.get((trial_id, speed), {}).get(method) for speed in speeds]
        lines.append(f'| {method} | ' + ' | '.join(
            '' if cell is None else f'{cell:.2f}' for cell in cells) + ' |')
    lines += ['', 'Time relative to the full copy, and where it stops being faster:', '',
              '| method | ' + ' | '.join(speeds) + ' | not faster from |',
              '|---|' + '---|' * (len(speeds) + 1)]
    for method in methods:
        if method == 'full':
            continue
        result = crossover(table, speeds, trial_id, method)
        cells = [result['ratios'].get(speed) for speed in speeds]
        verdict = result['not_faster_from'] or ('never (faster at every speed)'
                                                if result['faster_everywhere'] else 'n/a')
        lines.append(f'| {method} | ' + ' | '.join(
            '' if cell is None else f'{cell:.2f}' for cell in cells) + f' | {verdict} |')
    return lines


def with_blocks(rows) -> list[dict]:
    """rows with T2's warm-source runs as 'METHOD (warm source)' rows, apart from the cold ones.

    The source's cache was read, not dropped, before those runs (EXP-11's warm-source block), so they
    are not pooled with the same arm's unthrottled runs. The extension's runs stay pooled with the
    main block's, as EXP-11 compares them outside the warm-source block.
    """
    return [{**row, 'method': row['method'] + WARM_SOURCE} if row.get('block') == 'warm_source' else row
            for row in rows]


def render(rows: list[dict]) -> str:
    rows = with_blocks(rows)
    expanded = with_in_place(rows)
    speeds = speed_order(rows)
    methods = sorted({row['method'] for row in expanded})
    lines = ['Headline: the crossover uses `total_s`, ' + VIEWS[0][1] + ". '(serial)' "
             "rows are rsync with its copy of B0, or pgdelta with its receiver's start-up, and "
             'the capture one after the other (`serial_total_s`), an upper bound. The two '
             "kinds differ: rsync's copy, before the capture, reads B0 cold on the critical "
             "path, while pgdelta's receiver, started first, still prefetches B0 during the "
             "capture, so pgdelta's serial rows hold no cold read of B0. "
             "'(in place)' rows are rsync without its copy of B0 (`inplace_total_s`), a lower "
             'bound for patching B0 directly. pgdelta_inplace and pgbackrest_* patch B, a copy of '
             'B0 made before the timers; pgdelta_direct* is receiver start + transfer, with no '
             'capture; pgbackrest_* is the restore alone, its backup counted as already paid.'
             + (" T2's arms are one timed phase each; '(warm source)' rows are EXP-11's warm-source "
                'block, whose source cache was read rather than dropped.'
                if any(row.get('block') for row in rows) else ''), '']
    for trial_id in sorted({row['trial_id'] for row in rows}):
        lines += [f'## {trial_id}', '']
        for index, (column, heading) in enumerate(VIEWS):
            table = medians(expanded, column)
            if not any(key[0] == trial_id for key in table):
                continue
            title = 'Median' if index == 0 else '### Secondary view: median'
            lines += [f'{title} `{column}` seconds (passing runs), {heading}. In this view the '
                      f'crossover uses `{column}`.', '']
            lines += _tables(table, speeds, trial_id, methods)
            lines.append('')
        lines += phase_table(rows, speeds, trial_id)
    lines += untimed_prefetch(rows)
    lines += durability(rows)
    failed = [row for row in rows if row.get('status') != 'pass']
    if failed:
        lines += [f'{len(failed)} run(s) did not pass and are left out:', '']
        lines += [f'- {row["trial_id"]} {row["speed"]} {row["method"]}: {row["status"]} '
                  f'{row.get("error") or ""}' for row in failed]
    return '\n'.join(lines) + '\n'


def phase_table(rows, speeds, trial_id) -> list[str]:
    """Median timed phases per method and speed (passing runs), the columns any run has."""
    passing = [row for row in rows if row['trial_id'] == trial_id and row.get('status') == 'pass']
    columns = [column for column in PHASE_COLUMNS
               if any(row.get(column) is not None for row in passing)]
    if not passing or set(columns) <= {'total_s', 'above_link_s'}:
        return []
    groups: dict = {}
    for row in passing:
        groups.setdefault((row['method'], row['speed']), []).append(row)
    t2 = any(column.startswith(('pbb_', 'probackup_', 'parcopy_')) or column == 'above_link_s'
             for column in columns)
    lines = ['Median timed phases, seconds (passing runs). `rebuild_s`, `fsync_s`, `fsync_wait_s` and '
             "`verify_wait_s` are pgdelta's own timers, inside its transfer"
             + ("; `pbb_*`, `probackup_*` and `parcopy_*` are T2's tools' own, inside their one phase "
                '(`probackup_sync_s` is its timed sync -f of the output, a step of that phase); '
                '`above_link_s` is `total_s` minus the wire bytes at the measured rate (8 streams for the '
                'parallel arms)' if t2 else '') + ':', '',
             '| method | speed | runs | ' + ' | '.join(columns) + ' |',
             '|---|---|---|' + '---|' * len(columns)]
    order = {speed: index for index, speed in enumerate(speeds)}
    for method, speed in sorted(groups, key=lambda key: (key[0], order.get(key[1], len(order)))):
        group = groups[(method, speed)]
        cells = []
        for column in columns:
            values = [row[column] for row in group if row.get(column) is not None]
            cells.append(f'{statistics.median(values):.2f}' if values else '')
        lines.append(f'| {method} | {speed} | {len(group)} | ' + ' | '.join(cells) + ' |')
    return lines + ['']


def durability(rows) -> list[str]:
    """T2 (EXP-11): runs whose durability checks (a) and (b) failed; their status is their verification's.

    The negative control (full_nosync, not durable) is meant to fail them, and is counted apart.
    """
    checked = [row for row in rows if row.get('durability_ok') not in (None, '')]
    if not checked:
        return []
    control = [row for row in checked if str(row.get('durable')) == 'False']
    failed = [row for row in checked if str(row['durability_ok']) == 'False' and str(row.get('durable')) != 'False']
    lines = []
    if control:
        lines.append(f'The negative control ({", ".join(sorted({row["method"] for row in control}))}, not '
                     "durable) failed EXP-11's durability checks in "
                     f'{sum(str(row["durability_ok"]) == "False" for row in control)} of {len(control)} runs.')
    if not failed:
        return lines + [f"Every durable arm's run passed EXP-11's durability checks "
                        f'({len(checked) - len(control)} runs checked).', '']
    lines += [f"{len(failed)} run(s) of durable arms failed EXP-11's durability checks ((a): dest's Dirty + "
              "Writeback increase; (b): dest's disk writes against the output); their status is their "
              "verification's, and analysis/exp11.py leaves them out (A5):", '']
    for row in failed:
        increase = row.get('dirty_increase_mb')
        lines.append(f'- {row["trial_id"]} r{row.get("repeat", "?")} {row["speed"]} {row["method"]}'
                     + ('' if increase is None else f': Dirty + Writeback up {increase:.1f} MB'))
    return lines + ['']


def untimed_prefetch(rows) -> list[str]:
    """Lines listing the passing pgdelta runs whose prefetch read B0 between the timed phases."""
    measured = [row for row in rows if row.get('status') == 'pass'
                and row.get('prefetch_untimed_s') is not None]
    if not measured:
        return []
    spilled = [row for row in measured if row['prefetch_untimed_s'] > 0]
    if not spilled:
        return [f'None of the {len(measured)} pgdelta runs read B0 between their timed phases '
                '(`prefetch_untimed_s` 0).', '']
    lines = [f'{len(spilled)} of {len(measured)} pgdelta runs read B0 between their timed phases, '
             'off the clock (`prefetch_untimed_s`); their `total_s` can understate by about that '
             "time. Bytes are estimated at each prefetch's average rate:", '']
    for row in spilled:
        estimate = row.get('prefetch_untimed_bytes_est')
        size = '' if estimate is None else f', about {estimate / 1e6:.1f} MB'
        lines.append(f'- {row["trial_id"]} r{row.get("repeat", "?")} {row["speed"]} {row["method"]}: '
                     f'{row["prefetch_untimed_s"]:.3f} s{size}')
    return lines + ['']


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('summary', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    text = render(load(args.summary))
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    print(text, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
