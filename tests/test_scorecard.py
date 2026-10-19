"""xrd-devtools' own take-over scorecard gate: the plugin it offers the chain, run on ``src``.

The gates every repository of the chain is held to hold here too; CI runs
this module with the plugin loaded explicitly and ``XRD_DEVTOOLS_REQUIRED``
set, as it does everywhere else.
"""

from xrddevtools import pytest_plugin

test_xrd_devtools_is_easier_to_take_over_than_the_industry_median = pytest_plugin.scorecard_gate(
    __file__
)
