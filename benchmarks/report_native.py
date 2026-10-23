"""Render native-run evidence without converting missing metrics into zeros."""
import argparse
import json
from pathlib import Path
import statistics

# Bundles before schema v3 labelled the two native methods with short names.
LEGACY_METHODS = {'full': 'pg_basebackup_full', 'incremental': 'pg_basebackup_incremental'}


def method_name(name):
    return LEGACY_METHODS.get(name, name)


def preparation_inclusive_seconds(row):
    """Method time plus untimed preparation; trials without preparation add nothing."""
    if row.get('method_seconds') is None:
        return None
    return row['method_seconds'] + (row.get('preparation_seconds') or 0)


def render(data):
    trials = [{**row, 'method': method_name(row['method'])} for row in data['trials']]
    for row in trials:
        row['preparation_inclusive_seconds'] = preparation_inclusive_seconds(row)
    planned = [method_name(entry[1]) for entry in data.get('trial_order', [])]
    names = sorted({name for name in planned + [r['method'] for r in trials]} - {'no_backup'})
    lines = ['# Native PostgreSQL backup smoke measurements', '',
             '**Scope: real local backups and restores; not a controlled network performance baseline.**', '',
             f"Run status: **{data['status']}**. PostgreSQL: {data['postgres_version']}.", '',
             f"Source commit: `{data.get('commit')}`. Base image: `{data.get('image_digest')}`.", '',
             '| Method | Source workload | Accepted trials | Median capture seconds | Median verified backup seconds | Median captured MiB | Median preparation + method seconds |',
             '|---|---|---:|---:|---:|---:|---:|']
    for mode in ('quiescent', 'active'):
        for method in names:
            rows = [r for r in trials if r['method'] == method and r['mode'] == mode and r['status'] == 'pass']
            def median(field, factor=1):
                values = [r[field] / factor for r in rows if r.get(field) is not None]
                return f'{statistics.median(values):.3f}' if values else 'unavailable'
            lines.append(f"| {method} | {mode} | {len(rows)} | {median('capture_seconds')} | {median('verified_backup_seconds')} | {median('capture_bytes', 1024**2)} | {median('preparation_inclusive_seconds')} |")
    lines += ['', 'Capture size includes bundled WAL and metadata. It is **not network traffic**.', '',
              'Verified time includes capture, incremental-input verification and combination or the staged transfer where applicable, normal synchronization, and output verification. Common B0 setup and method preparation (such as copying B0 for rsync) are excluded. Restore testing is additional.', '',
              'The staged methods are not timed on the same terms. rsync_staged copies B0 into the output as untimed preparation and its timer covers only the in-place update, while pg_delta_staged builds the output from B0 and the new capture inside its timed transfer. The last column adds recorded preparation time to method time (capture through transfer, before verification) and is the like-for-like comparison of the two. It is unavailable for trials that did not record method time.', '',
              '## Correctness', '',
              'Quiescent trials compare full ordered data exports and supported schema against the source after writes are fenced. Active trials test package-only startup and a conservation invariant, then exact logical equivalence at a named recovery target using additional archived WAL. The latter is explicitly PITR-assisted.', '',
              'The exact logical oracle at the active package’s own capture cutoff is not implemented. Do not read an active PASS as proof of that stronger guarantee.', '',
              '| Trial | Status | Package logical comparison | PITR logical comparison |',
              '|---|---|---|---|']
    for row in trials:
        lines.append(f"| {row['run_id']} | {row['status']} | {row.get('self_contained_restore', {}).get('logical_match', 'not applicable')} | {row.get('pitr_restore', {}).get('logical_match', 'not applicable')} |")
    lines += ['', '## Limits and reproduction', '',
              '- Tiny synthetic database, fresh cluster per trial, uncontrolled host cache and shared CI hardware when run in Actions.',
              '- Fixed low offered transaction rate checks active capture; it is not a calibrated application-capacity experiment.',
              '- Per-transaction logs preserve scheduled latency, failures and lag. Very short backup windows do not support strong p99 conclusions.',
              '- No source/destination CPU, peak-memory, device-I/O or wire-byte measurements yet.',
              ('- Staged methods ran on one host: their transfer bytes are the tools\' own stream counters, not wire bytes.'
               if any(not name.startswith('pg_basebackup_') for name in names)
               else '- No rsync/custom-engine comparison yet.'),
              '- No large-data matrix or AlloyDB Omni validation yet.',
              '- Failed trials are retained and are not included as fast successful backups.', '',
              'Use the immutable image digest and configuration in results.json to repeat the run. See benchmarks/NATIVE.md for commands and exact scope.', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(json.loads(args.results.read_text())))
