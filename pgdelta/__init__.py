"""pgdelta: refresh an older physical copy of a PostgreSQL cluster.

Only pages whose keyed hash differs cross the link; every output file is
re-hashed against the source before the output directory is published.
See pgdelta/README.md for the protocol and docs/SPEC.md for the design.
"""

__version__ = '0.1.0'
