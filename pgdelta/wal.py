"""Optional WAL selection for immutable, cleanly stopped PostgreSQL 17 primaries."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


def select_shutdown_wal(source, entries, controldata=None):
    """Return the transfer inventory and selection evidence; unsupported sources keep all WAL.

    The source must remain immutable, as required by the sender itself. This deliberately
    handles only the clean primary case whose shutdown checkpoint fits in one WAL segment.
    """
    source = Path(source)

    def unchanged(reason):
        return entries, {'requested': 'shutdown', 'applied': False, 'reason': reason}

    version = source / 'PG_VERSION'
    if not version.is_file() or version.read_text().strip() != '17':
        return unchanged('requires PostgreSQL 17')
    for name in ('postmaster.pid', 'backup_label', 'recovery.signal', 'standby.signal'):
        if (source / name).exists():
            return unchanged(f'source has {name}')
    if (source / 'pg_wal').is_symlink():
        return unchanged('external WAL directory')
    if any(e.kind == 'link' for e in entries):
        return unchanged('source contains symbolic links')
    if any(e.kind == 'file' and e.path.startswith('pg_replslot/') for e in entries):
        return unchanged('replication slots can require retained WAL')
    tool = Path(controldata) if controldata else shutil.which('pg_controldata')
    if not tool:
        return unchanged('pg_controldata unavailable')
    tool = Path(tool)
    waldump = tool.with_name('pg_waldump')
    if not tool.is_file() or not waldump.is_file():
        return unchanged('PostgreSQL control/WAL tools unavailable')
    result = subprocess.run([str(tool), str(source)], env={**os.environ, 'LC_ALL': 'C'},
                            check=True, capture_output=True, text=True)
    if result.stderr.strip():
        raise ValueError(f'pg_controldata: {result.stderr.strip()}')
    fields = {k.strip(): v.strip() for line in result.stdout.splitlines() if ':' in line
              for k, v in [line.split(':', 1)]}
    if fields['Database cluster state'] != 'shut down':
        return unchanged('source is not a cleanly shut-down primary')
    checkpoint = fields['Latest checkpoint location']
    if fields["Latest checkpoint's REDO location"] != checkpoint:
        return unchanged('checkpoint and REDO location differ')
    if fields["Latest checkpoint's TimeLineID"] != '1':
        return unchanged('requires timeline 1')
    if fields['Minimum recovery ending location'] != '0/0' or fields['Backup start location'] != '0/0':
        return unchanged('source has recovery or backup dependencies')
    segment = fields["Latest checkpoint's REDO WAL file"]
    size = int(fields['Bytes per WAL segment'])
    high, low = checkpoint.split('/')
    position = (int(high, 16) << 32) + int(low, 16)
    if size != 16 * 1024 * 1024 or position % size > size - 8192:
        return unchanged('unsupported WAL size or checkpoint near segment boundary')
    if not re.fullmatch(r'00000001[0-9A-F]{16}', segment):
        raise ValueError('invalid checkpoint WAL filename')
    expected = f'{1:08X}{position // (1 << 32):08X}{(position % (1 << 32)) // size:08X}'
    if segment != expected:
        raise ValueError('checkpoint WAL filename disagrees with checkpoint location')
    path = source / 'pg_wal' / segment
    if not path.is_file() or path.stat().st_size != size:
        raise ValueError('checkpoint WAL segment missing or truncated')
    record = subprocess.run([str(waldump), '-p', str(source / 'pg_wal'), '-s', checkpoint, '-n', '1'],
                            check=True, capture_output=True, text=True)
    if 'CHECKPOINT_SHUTDOWN' not in record.stdout:
        raise ValueError('checkpoint WAL record is not a shutdown checkpoint')
    keep = f'pg_wal/{segment}'
    selected = [entry for entry in entries if not entry.path.startswith('pg_wal/')
                or entry.path in (keep, 'pg_wal/archive_status')]
    before = sum(e.size for e in entries if e.kind == 'file' and e.path.startswith('pg_wal/'))
    return selected, {'requested': 'shutdown', 'applied': True, 'checkpoint': checkpoint,
                      'kept_segment': segment, 'wal_before_bytes': before, 'wal_after_bytes': size}
