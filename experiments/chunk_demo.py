"""Educational byte-reuse experiment, not a database backup implementation.

All data stays in memory. Estimated framing costs are illustrative, not wire
measurements. This deliberately has no filesystem or live-database interface.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass


def digest(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


@dataclass(frozen=True)
class Copy:
    offset: int
    length: int
    expected_digest: bytes


@dataclass(frozen=True)
class Literal:
    data: bytes
    expected_digest: bytes


@dataclass(frozen=True)
class Plan:
    operations: tuple[Copy | Literal, ...]
    target_length: int
    target_digest: bytes
    basis_chunks: int


def plan_transfer(basis: bytes, target: bytes, chunk_size: int) -> Plan:
    if chunk_size <= 0:
        raise ValueError('chunk_size must be positive')
    index: dict[tuple[int, bytes], int] = {}
    for offset in range(0, len(basis), chunk_size):
        block = basis[offset:offset + chunk_size]
        index.setdefault((len(block), digest(block)), offset)
    operations: list[Copy | Literal] = []
    for offset in range(0, len(target), chunk_size):
        block = target[offset:offset + chunk_size]
        block_digest = digest(block)
        basis_offset = index.get((len(block), block_digest))
        if basis_offset is None:
            operations.append(Literal(block, block_digest))
        else:
            operations.append(Copy(basis_offset, len(block), block_digest))
    return Plan(tuple(operations), len(target), digest(target),
                (len(basis) + chunk_size - 1) // chunk_size)


def reconstruct(basis: bytes, plan: Plan) -> bytes:
    if plan.target_length < 0:
        raise ValueError('invalid target length')
    output = bytearray()
    for operation in plan.operations:
        if isinstance(operation, Copy):
            if (operation.offset < 0 or operation.length <= 0
                    or operation.offset + operation.length > len(basis)):
                raise ValueError('invalid basis range')
            block = basis[operation.offset:operation.offset + operation.length]
        elif isinstance(operation, Literal):
            block = operation.data
        else:
            raise ValueError('unknown operation')
        if digest(block) != operation.expected_digest:
            raise ValueError('chunk verification failed')
        if len(output) + len(block) > plan.target_length:
            raise ValueError('output exceeds target length')
        output.extend(block)
    result = bytes(output)
    if len(result) != plan.target_length or digest(result) != plan.target_digest:
        raise ValueError('target verification failed')
    return result


def describe(plan: Plan) -> dict[str, int | float | None]:
    copies = [op for op in plan.operations if isinstance(op, Copy)]
    literals = [op for op in plan.operations if isinstance(op, Literal)]
    literal_bytes = sum(len(op.data) for op in literals)
    # Illustrative model: 48-byte signatures; COPY 49 bytes; DATA 41 + payload.
    # Excludes session metadata, file inventories, transport, WAL and retries.
    signature_bytes = plan.basis_chunks * 48
    instruction_bytes = len(copies) * 49 + len(literals) * 41
    estimated_bytes = signature_bytes + instruction_bytes + literal_bytes
    return {
        'target_bytes': plan.target_length,
        'literal_bytes': literal_bytes,
        'reused_bytes': sum(op.length for op in copies),
        'estimated_signature_bytes': signature_bytes,
        'estimated_instruction_bytes': instruction_bytes,
        'estimated_total_bytes': estimated_bytes,
        'estimated_saving_fraction': (
            1 - estimated_bytes / plan.target_length
            if plan.target_length else None
        ),
    }


def main() -> None:
    rng = random.Random(20260930)
    page_size = 8192
    page_count = 1024
    basis = rng.randbytes(page_size * page_count)
    results = []
    for changed_pages in (0, 10, 102, 1024):
        target = bytearray(basis)
        for page in rng.sample(range(page_count), changed_pages):
            target[page * page_size] ^= 255
        target_bytes = bytes(target)
        for chunk_size in (8192, 65536):
            plan = plan_transfer(basis, target_bytes, chunk_size)
            assert reconstruct(basis, plan) == target_bytes
            results.append({
                'scenario': 'one byte changed per selected synthetic page',
                'changed_pages': changed_pages,
                'total_pages': page_count,
                'chunk_bytes': chunk_size,
                'exact_reconstruction': True,
                **describe(plan),
            })
    shifted = b'!' + basis
    shifted_plan = plan_transfer(basis, shifted, page_size)
    assert reconstruct(basis, shifted_plan) == shifted
    results.append({
        'scenario': 'one-byte prefix insertion; generic-file counterexample',
        'chunk_bytes': page_size,
        'exact_reconstruction': True,
        **describe(shifted_plan),
    })
    print(json.dumps({
        'measurement_kind': 'synthetic_payload_estimate',
        'seed': 20260930,
        'warning': 'Not PostgreSQL performance or actual network traffic.',
        'excluded_costs': [
            'capture', 'disk_io', 'cpu_time', 'session_metadata',
            'file_inventory', 'transport_overhead', 'wal', 'retransmissions',
        ],
        'results': results,
    }, indent=2))


if __name__ == '__main__':
    main()
