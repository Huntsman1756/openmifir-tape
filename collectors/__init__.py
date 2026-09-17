"""OpenMiFIR Tape: G0 acquisition collectors.

G0-A1 scope only. These modules acquire raw source objects immutably and
record provenance metadata. They do NOT normalize, build a universe, or
provide a query layer. Provider payloads are written ONLY to the gitignored
local data directory; the repository holds code, schemas, tests, and metadata.
"""

__all__ = ["config", "harness", "sources", "storage"]
