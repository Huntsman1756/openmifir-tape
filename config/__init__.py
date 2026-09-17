"""Packaged runtime configuration resources for OpenMiFIR Tape.

This package exists so that ``config/sources/*.yaml`` ship inside the wheel
and resolve via ``importlib.resources`` in both editable checkouts and
installed distributions. It intentionally contains no code.
"""
