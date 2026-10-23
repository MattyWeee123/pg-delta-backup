"""Error classes carrying the exit codes from docs/SPEC.md section 3."""
from __future__ import annotations

EXIT_OK = 0
EXIT_INVALID_INPUT = 2
EXIT_TRANSFER_FAILURE = 3
EXIT_VERIFICATION_FAILURE = 4
EXIT_CODES = (EXIT_INVALID_INPUT, EXIT_TRANSFER_FAILURE, EXIT_VERIFICATION_FAILURE)


class PgDeltaError(Exception):
    exit_code = EXIT_TRANSFER_FAILURE


class InvalidInput(PgDeltaError):
    """Unsupported or unsafe input: refused before or instead of transferring."""
    exit_code = EXIT_INVALID_INPUT


class UnsafePath(InvalidInput):
    pass


class TransferError(PgDeltaError):
    """I/O failure, lost connection or protocol violation."""
    exit_code = EXIT_TRANSFER_FAILURE


class ProtocolError(TransferError):
    pass


class VerificationError(PgDeltaError):
    """A digest check failed: the output does not match the source."""
    exit_code = EXIT_VERIFICATION_FAILURE


class PeerError(PgDeltaError):
    """The other end reported a failure; carry its exit code."""

    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code if exit_code in EXIT_CODES else EXIT_TRANSFER_FAILURE
