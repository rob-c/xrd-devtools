"""xrd-devtools' own release metadata: the gate it offers the chain, run on this checkout."""

from xrddevtools import pytest_plugin

test_xrddevtools_release_metadata_is_consistent = pytest_plugin.release_gate(__file__)
