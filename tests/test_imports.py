import importlib

import pytest

MODULES = [
    "minix",
    "minix.protocol",
    "minix.config",
    "minix.discovery",
    "minix.transport",
    "minix.device",
    "minix.sim",
    "minix.controller",
    "minix.telemetry",
    "minix.policy.limits",
    "minix.policy.sequencing",
    "minix.policy.ranging",
    "minix.policy.averaging",
    "minix.policy.banding",
    "minix.ui.app",
    "minix.ui.main_window",
    "minix.ui.widgets",
    "minix.ui.dialogs",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)
