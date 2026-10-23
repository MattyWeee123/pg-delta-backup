"""Cluster identity from pg_controldata, and the gate that decides whether to compare against B.

The gate is an efficiency policy, not a correctness requirement: whatever it
decides, every output file is verified against the source digest before O
is published. By default it skips the comparison (full mode) when B is not
known to be a copy of the same cluster and build, because such a basis
seldom has equal pages at the same offsets. --reuse-any-basis compares
against any basis.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from pgdelta.errors import InvalidInput

FIELDS = {
    'Database system identifier': ('system_identifier', str),
    'pg_control version number': ('pg_control_version', int),
    'Catalog version number': ('catalog_version', int),
    'Database block size': ('block_size', int),
    'Blocks per segment of large relation': ('blocks_per_segment', int),
    'Data page checksum version': ('data_checksum_version', int),
    "Latest checkpoint's REDO location": ('redo_lsn', str),
    "Latest checkpoint's TimeLineID": ('timeline', int),
    'Database cluster state': ('cluster_state', str),
    'Bytes per WAL segment': ('wal_segment_size', int),
}
REQUIRED = ('system_identifier', 'pg_control_version', 'catalog_version', 'block_size',
            'blocks_per_segment', 'data_checksum_version', 'redo_lsn', 'timeline')
# pg_controldata prints this (to stderr, exit status 0) when the control file's layout differs from
# its own, typically because it belongs to another major version; it says its values are untrustworthy.
UNTRUSTWORTHY_MARK = 'crc checksum does not match'
SAME_CLUSTER, ANY_BASIS = 'same-cluster', 'any-basis'
GATE_FIELDS = (
    ('system_identifier', 'system identifier'),
    ('catalog_version', 'catalog version'),
    ('pg_control_version', 'pg_control version'),
    ('block_size', 'block size'),
    ('blocks_per_segment', 'blocks per segment'),
    ('data_checksum_version', 'data checksum version'),
)


def parse_controldata(text):
    identity = {}
    for line in text.splitlines():
        label, separator, value = line.partition(':')
        if not separator or label.strip() not in FIELDS:
            continue
        key, convert = FIELDS[label.strip()]
        try:
            identity[key] = convert(value.strip())
        except ValueError as exc:
            raise InvalidInput(f'unexpected pg_controldata value for {label.strip()!r}: {value.strip()!r}') from exc
    missing = [key for key in REQUIRED if key not in identity]
    if missing:
        raise InvalidInput(f'pg_controldata output lacks {", ".join(missing)}')
    return identity


def read_identity(datadir, pg_controldata, timeout=60):
    env = dict(os.environ, LC_ALL='C', LANG='C', LC_MESSAGES='C')
    command = [str(pg_controldata), '-D', str(Path(datadir))]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise InvalidInput(f'pg_controldata not found: {pg_controldata}') from exc
    except PermissionError as exc:
        raise InvalidInput(f'pg_controldata is not executable: {pg_controldata}') from exc
    except subprocess.TimeoutExpired as exc:
        raise InvalidInput(f'pg_controldata timed out on {datadir}') from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise InvalidInput(f'pg_controldata failed on {datadir}: {detail[-1] if detail else result.returncode}')
    identity = parse_controldata(result.stdout)
    warning = next((line.strip() for line in (result.stderr or '').splitlines()
                    if UNTRUSTWORTHY_MARK in line.lower()), None)
    if warning:
        # Keep the values for the record, but the gate treats the identity as unknown.
        identity['untrustworthy'] = (f'{pg_controldata} says: {warning}; read this cluster with the '
                                     f'pg_controldata of its own major version')
    return identity


def reuse_policy(reuse_any_basis):
    """The policy name recorded in ACCEPT and in the statistics."""
    return ANY_BASIS if reuse_any_basis else SAME_CLUSTER


def _compare(source_identity, basis_identity):
    """(identities known, trustworthy and matching on GATE_FIELDS, what the comparison found)."""
    if source_identity is None or basis_identity is None:
        unknown = ' and '.join(name for name, value in
                               (('source', source_identity), ('basis', basis_identity)) if value is None)
        return False, f'{unknown} identity unknown'
    for side, identity, option in (('source', source_identity, '--pg-controldata'),
                                   ('basis', basis_identity, '--basis-pg-controldata')):
        if identity.get('untrustworthy'):
            return False, f'{side} identity untrustworthy ({identity["untrustworthy"]}; see {option})'
    for key, label in GATE_FIELDS:
        source_value, basis_value = source_identity.get(key), basis_identity.get(key)
        if source_value is None or basis_value is None:
            return False, f'{label} unknown'
        if source_value != basis_value:
            return False, f'{label} differs (source {source_value}, basis {basis_value})'
    return True, 'source and basis identities match'


def gate(source_identity, basis_identity, reuse_any_basis=False):
    """Return (reuse, reason): whether to compare S with B page by page and copy equal pages from B.

    This is an efficiency policy, not a correctness requirement. Correctness
    comes from verification: every output file, including each page copied
    from B, is re-hashed and checked against the source's keyed digest before
    O is published. Reusing an unrelated basis therefore costs at most the
    digest traffic of the comparison; it can never produce a wrong output.

    Policy "same-cluster" (the default): reuse only when both identities are
    known, trustworthy and equal on GATE_FIELDS; otherwise use full mode,
    which sends every page and exchanges no digests. A different cluster or
    build rarely has equal pages at the same offsets, so the comparison
    would usually be wasted.

    Policy "any-basis" (reuse_any_basis, CLI --reuse-any-basis or its older
    alias --assume-same-cluster): always reuse, for example from a basis
    taken before pg_upgrade, or when no identity is available. The reason
    still says what the comparison found.
    """
    match, finding = _compare(source_identity, basis_identity)
    if match or not reuse_any_basis:
        return match, finding
    return True, f'{finding}; reuse allowed by --reuse-any-basis (every reused page is still verified)'
