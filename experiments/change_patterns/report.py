"""One-row-per-trial CSV summary of change-pattern results."""
from __future__ import annotations

import csv
from pathlib import Path

from experiments.change_patterns.pagediff import CHUNK_SIZES, chunk_label

CHUNK_LABELS = tuple(chunk_label(size) for size in CHUNK_SIZES)  # '8K' .. '1M'
AGREEMENT_KEYS = ('changed_and_tracked', 'changed_not_tracked', 'tracked_unchanged',
                  'tracked_beyond_new_end')
# (column prefix, agreement view, scope within it)
AGREEMENT_VIEWS = (('', 'incremental_backup', 'all'),
                   ('incremental_', 'incremental_backup', 'incremental'),
                   ('walsummary_', 'wal_summaries', 'all'))
# Target-table columns taken as they are from ground_truth.pages_per_row.
TARGET_COLUMNS = {
    'target_table': 'table',
    'rows_per_heap_page': 'rows_per_heap_page_before',
    'heap_pages_existing': 'heap_pages_existing',
    'heap_existing_changed_pct': 'heap_existing_changed_pct',
    'uniform_model_heap_existing_pct': 'uniform_model_heap_existing_pct',
    'heap_appended_pages': 'heap_appended_pages',
    'heap_overall_changed_pct': 'heap_overall_changed_pct',
    'index_changed_pct': 'index_changed_pct',
}

CSV_FIELDS = (
    'trial_id', 'status', 'pattern', 'fraction', 'variant', 'scale', 'data_checksums',
    'load_vacuum', 'checkpoint', 'vacuum', 'read_scan', 'seed', 'rows_changed',
    'tpcb_transactions',
    *TARGET_COLUMNS,
    'pct_8k_changed_all', 'pct_8k_changed_heap', 'pct_8k_changed_index',
    *(f'pct_{label.lower()}_changed_data' for label in CHUNK_LABELS),
    *(f'pct_bytes_sent_{label.lower()}_data' for label in CHUNK_LABELS),
    'send_8k_bytes_data',
    'b1_bytes', 'i1_bytes', 'i1_over_b1', 'i1_incremental_files', 'i1_full_files',
    *(prefix + key for prefix, _, _ in AGREEMENT_VIEWS for key in AGREEMENT_KEYS),
    *(f'{method}_{metric}' for method in ('pgdelta', 'rsync_default', 'rsync_8k')
      for metric in ('bytes_s2d', 'bytes_d2s', 'wall_s', 'cpu_s', 'identical')),
    'pgdelta_pages_sent', 'pgdelta_pages_total',
    'error',
)


def _get(value, *keys):
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _pct(totals: dict | None, label: str, key: str = 'changed_fraction'):
    fraction = _get(totals, 'chunks', label, key)
    return None if fraction is None else round(100 * fraction, 4)


def _round(value, digits: int = 4):
    return round(value, digits) if isinstance(value, float) else value


def summary_row(result: dict) -> dict:
    """Headline numbers from one trial result; missing parts stay blank."""
    params = result.get('params', {})
    content = result.get('content_changes', {})
    data = _get(content, 'totals', 'data')
    kinds = content.get('by_kind', {})
    tracker = result.get('tracker', {})
    target = _get(result, 'ground_truth', 'pages_per_row') or {}
    b1_bytes = _get(content, 'totals', 'all', 'new_bytes')
    i1_bytes = _get(tracker, 'total_bytes')
    row = {
        'trial_id': result.get('trial_id'),
        'status': result.get('status'),
        **{key: params.get(key) for key in ('pattern', 'fraction', 'variant', 'scale',
                                            'data_checksums', 'load_vacuum', 'checkpoint',
                                            'vacuum', 'read_scan', 'seed')},
        'rows_changed': _get(result, 'ground_truth', 'rows_changed'),
        'tpcb_transactions': _get(result, 'ground_truth', 'tpcb_transactions'),
        **{column: _round(target.get(key)) for column, key in TARGET_COLUMNS.items()},
        'pct_8k_changed_all': _pct(_get(content, 'totals', 'all'), '8K'),
        'pct_8k_changed_heap': _pct(kinds.get('heap'), '8K'),
        'pct_8k_changed_index': _pct(kinds.get('index'), '8K'),
        **{f'pct_{label.lower()}_changed_data': _pct(data, label) for label in CHUNK_LABELS},
        **{f'pct_bytes_sent_{label.lower()}_data': _pct(data, label, 'send_fraction')
           for label in CHUNK_LABELS},
        'send_8k_bytes_data': _get(data, 'chunks', '8K', 'send_bytes'),
        'b1_bytes': b1_bytes,
        'i1_bytes': i1_bytes,
        'i1_over_b1': round(i1_bytes / b1_bytes, 6) if b1_bytes and i1_bytes is not None else None,
        'i1_incremental_files': _get(tracker, 'incremental_files', 'files'),
        'i1_full_files': _get(tracker, 'full_files', 'files'),
        'error': result.get('error'),
    }
    methods = result.get('methods') or {}
    for method in ('pgdelta', 'rsync_default', 'rsync_8k'):
        entry = methods.get(method) or {}
        row[f'{method}_bytes_s2d'] = entry.get('bytes_source_to_dest')
        row[f'{method}_bytes_d2s'] = entry.get('bytes_dest_to_source')
        row[f'{method}_wall_s'] = entry.get('wall_seconds')
        row[f'{method}_cpu_s'] = entry.get('cpu_seconds')
        row[f'{method}_identical'] = entry.get('identical')
    row['pgdelta_pages_sent'] = _get(methods, 'pgdelta', 'pages', 'sent')
    row['pgdelta_pages_total'] = _get(methods, 'pgdelta', 'pages', 'total')
    for prefix, view, scope in AGREEMENT_VIEWS:
        counts = _get(result, 'agreement', view, 'scopes', scope) or {}
        for key in AGREEMENT_KEYS:
            row[prefix + key] = counts.get(key)
    return row


def write_summary_csv(path: Path, results: list[dict]) -> None:
    temporary = Path(path).with_suffix('.csv.tmp')
    with open(temporary, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator='\n')
        writer.writeheader()
        for result in results:
            writer.writerow(summary_row(result))
    temporary.replace(path)
