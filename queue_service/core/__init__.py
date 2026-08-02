"""Pure logic: admission controller, token bucket, backpressure.

Zero imports from adapters/, and zero imports of redis, django.db or anything else that does IO.
`tests/test_invariants.py::test_core_imports_nothing_from_the_io_edge` enforces this, so the
boundary cannot rot quietly.
"""
