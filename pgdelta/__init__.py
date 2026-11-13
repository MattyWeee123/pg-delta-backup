"""pgdelta: refresh an old copy of a PostgreSQL data directory by sending only the chunks that differ.

This is the naive starting version: one thread, one TCP connection, and one round trip per chunk
("stop and wait"). See pgdelta/README.md for how it works and the roadmap issues for what comes next.
"""

__version__ = '0.1.0'
