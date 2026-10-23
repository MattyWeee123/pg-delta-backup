"""Aggregate EXP-03 scenario directories into summary.csv and summary.md."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

CSV_COLUMNS = [
    'scenario', 'name', 'built_ok', 'pg_old_copy', 'pg_source', 'threshold_lsn',
    'threshold_source', 'same_system_identifier', 'timeline_old_copy', 'timeline_source',
    'checksums_old_copy', 'checksums_source', 'pages_compared', 'pages_without_counterpart',
    'pages_changed', 'pages_skipped',
    'pages_wrongly_skipped', 'wrongly_skipped_by_kind', 'wrongly_skipped_by_category',
    'lsn_prediction', 'lsn_prediction_held',
    'tool_status', 'tool_mode', 'tool_pages_total', 'tool_pages_sent', 'bytes_source_to_dest',
    'bytes_dest_to_source', 'byte_identical', 'verifybackup', 'restore_ok', 'logical_match',
    'tool_correct', 'selfcheck_ok', 'notes',
]

MARKDOWN_COLUMNS = [
    ('scenario', 'Scenario'), ('built_ok', 'Built'), ('threshold_source', 'Threshold source'),
    ('pages_compared', 'Compared'), ('pages_without_counterpart', 'S-only pages'),
    ('pages_changed', 'Changed'), ('pages_skipped', 'Skipped'),
    ('pages_wrongly_skipped', 'Wrongly skipped'), ('wrongly_skipped_by_kind', 'Wrong by fork'),
    ('wrongly_skipped_by_category', 'Wrong by cause'),
    ('lsn_prediction', 'LSN prediction'), ('lsn_prediction_held', 'Held'),
    ('tool_status', 'Tool'), ('byte_identical', 'O = S'), ('verifybackup', 'verifybackup'),
    ('restore_ok', 'Restores'), ('logical_match', 'Logical'), ('tool_correct', 'Tool correct'),
    ('notes', 'Notes'),
]

SCENARIO_ORDER = re.compile(r'^S(\d+)')


def prediction_held(prediction, wrongly_skipped):
    """EXP-03 pass rule: 'wrong' needs > 0 wrongly-skipped pages, 'correct' needs 0."""
    if wrongly_skipped is None or prediction not in ('wrong', 'correct'):
        return None
    return wrongly_skipped > 0 if prediction == 'wrong' else wrongly_skipped == 0


def short_threshold_source(source):
    if not source:
        return ''
    if source.startswith('backup_label'):
        return 'backup_label START WAL LOCATION'
    if source.startswith('pg_controldata'):
        return 'pg_controldata REDO location'
    return source


def scenario_notes(meta, evaluation):
    notes = []
    if meta.get('error'):
        notes.append(meta['error'])
    for name in meta.get('failed_checks', []):
        notes.append(f'check failed: {name}')
    checks = meta.get('checks', {})
    if 'diverged_pages_differ' in checks:
        c = checks['diverged_pages_differ']
        notes.append(f"{c.get('pages_differing')}/{c.get('pages_written_after_split')} pages "
                     f"written after the split differ in B")
    if 'heap_file_truncated' in checks:
        c = checks['heap_file_truncated']
        notes.append(f"accounts heap {c.get('old_copy_bytes')} -> {c.get('source_bytes')} bytes")
    if meta.get('same_system_identifier') is False:
        notes.append('different system identifiers')
    timelines = meta.get('timelines') or {}
    if timelines.get('old_copy') != timelines.get('source_backup'):
        notes.append(f"timelines {timelines.get('old_copy')} -> {timelines.get('source_backup')}")
    sums = meta.get('data_checksum_versions') or {}
    if sums.get('old_copy') != sums.get('source_backup'):
        notes.append(f"checksum version {sums.get('old_copy')} -> {sums.get('source_backup')}")
    threshold_note = (meta.get('observations') or {}).get('threshold_note')
    if threshold_note:
        notes.append(threshold_note)
    notes.extend(meta.get('notes', []))
    if evaluation and evaluation.get('tool', {}).get('status') not in (None, 'ok'):
        notes.append(f"tool: {evaluation['tool']['status']}")
    return '; '.join(notes)


def scenario_row(scenario_dir):
    scenario_dir = Path(scenario_dir)
    meta = json.loads((scenario_dir / 'scenario.json').read_text())
    scan_path, eval_path = scenario_dir / 'lsnscan.json', scenario_dir / 'evaluation.json'
    scan = json.loads(scan_path.read_text()) if scan_path.is_file() else {}
    evaluation = json.loads(eval_path.read_text()) if eval_path.is_file() else None
    totals = scan.get('totals', {})
    by_kind = scan.get('by_kind', {})
    wrong_kinds = ', '.join(f'{kind} {v["pages_wrongly_skipped"]}'
                            for kind, v in sorted(by_kind.items()) if v['pages_wrongly_skipped'])
    categories = (meta.get('observations') or {}).get('wrong_skip_categories') or {}
    wrong_causes = ', '.join(f'{name} {count}' for name, count in categories.items())
    threshold = meta.get('threshold') or {}
    old, src = meta.get('old_copy') or {}, meta.get('source_backup') or {}
    prediction = (meta.get('predictions') or {}).get('lsn_skip')
    held = prediction_held(prediction, totals.get('pages_wrongly_skipped'))
    tool = (evaluation or {}).get('tool') or {}
    stats = tool.get('stats_summary') or {}
    selfcheck = (evaluation or {}).get('source_selfcheck')
    return {
        'scenario': meta.get('id'), 'name': meta.get('name'), 'built_ok': meta.get('built_ok'),
        'pg_old_copy': old.get('pg_version'), 'pg_source': src.get('pg_version'),
        'threshold_lsn': threshold.get('lsn'),
        'threshold_source': short_threshold_source(threshold.get('source')),
        'same_system_identifier': meta.get('same_system_identifier'),
        'timeline_old_copy': old.get('timeline'), 'timeline_source': src.get('timeline'),
        'checksums_old_copy': old.get('data_checksum_version'),
        'checksums_source': src.get('data_checksum_version'),
        'pages_compared': totals.get('pages_compared'),
        'pages_without_counterpart': totals.get('pages_without_counterpart'),
        'pages_changed': totals.get('pages_changed'),
        'pages_skipped': totals.get('pages_skipped'),
        'pages_wrongly_skipped': totals.get('pages_wrongly_skipped'),
        'wrongly_skipped_by_kind': wrong_kinds,
        'wrongly_skipped_by_category': wrong_causes,
        'lsn_prediction': prediction,
        'lsn_prediction_held': 'n/a' if held is None else held,
        'tool_status': tool.get('status', 'not evaluated'),
        'tool_mode': stats.get('mode'), 'tool_pages_total': stats.get('pages_total'),
        'tool_pages_sent': stats.get('pages_sent'),
        'bytes_source_to_dest': stats.get('bytes_source_to_dest'),
        'bytes_dest_to_source': stats.get('bytes_dest_to_source'),
        'byte_identical': (evaluation or {}).get('byte_identical'),
        'verifybackup': (evaluation or {}).get('verifybackup'),
        'restore_ok': (evaluation or {}).get('restore_ok'),
        'logical_match': (evaluation or {}).get('logical_match'),
        'tool_correct': (evaluation or {}).get('tool_correct'),
        'selfcheck_ok': None if not selfcheck else all(
            selfcheck.get(k) for k in ('verifybackup', 'restore_ok', 'logical_match')),
        'notes': scenario_notes(meta, evaluation),
    }


def scenario_dirs(results_dir):
    dirs = [p for p in Path(results_dir).iterdir() if (p / 'scenario.json').is_file()]

    def key(path):
        match = SCENARIO_ORDER.match(path.name)
        return (int(match.group(1)) if match else 10**6, path.name)
    return sorted(dirs, key=key)


def collect_rows(results_dir):
    return [scenario_row(d) for d in scenario_dirs(results_dir)]


def cell(value):
    if value is None:
        return ''
    if value is True:
        return 'yes'
    if value is False:
        return 'no'
    return str(value).replace('|', '\\|').replace('\n', ' ')


def markdown_table(rows):
    header = '| ' + ' | '.join(title for _, title in MARKDOWN_COLUMNS) + ' |'
    rule = '|' + '|'.join('---' for _ in MARKDOWN_COLUMNS) + '|'
    body = ['| ' + ' | '.join(cell(row.get(key)) for key, _ in MARKDOWN_COLUMNS) + ' |'
            for row in rows]
    return '\n'.join([header, rule, *body]) + '\n'


def write_summary(results_dir, csv_path=None, markdown_path=None):
    results_dir = Path(results_dir)
    rows = collect_rows(results_dir)
    csv_path = Path(csv_path or results_dir / 'summary.csv')
    markdown_path = Path(markdown_path or results_dir / 'summary.md')
    with csv_path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: '' if row.get(key) is None else row.get(key)
                             for key in CSV_COLUMNS})
    markdown_path.write_text('# EXP-03 scenario summary\n\n' + markdown_table(rows))
    return rows, csv_path, markdown_path
