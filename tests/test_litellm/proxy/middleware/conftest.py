"""
Local conftest for middleware unit tests.

Overrides the parent proxy conftest's autouse fixture so middleware tests
can run without the full proxy server dependency stack (orjson, jwt, etc.).
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_proxy_module_globals():
    """No-op override — middleware unit tests don't touch proxy_server globals."""
    yield
