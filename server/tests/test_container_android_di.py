"""Container initializes and resolves workflow_service with the android plugin absent.

Regression test for the DI crash: ``server/nodes/android/`` was
decommissioned, but ``core/container.py`` wired ``android_service`` as a
required (eagerly-resolved) dependency of ``workflow_service`` /
``NodeExecutor``. Every ``workflow_service`` resolution — which happens on
every ``execute_node`` call over ``/ws/internal`` — raised before reaching
the manifest gate.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# conftest.py stubs ``core.container`` (MagicMock) for the rest of the
# suite so node tests don't pay the real-DI cost. This test needs the
# genuine module, so load it directly from file — same pattern conftest
# already uses for core.ansi / core.auth_cookies.
_SERVER_DIR = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "core.container_real", _SERVER_DIR / "core" / "container.py"
)
_container_mod = importlib.util.module_from_spec(_spec)
sys.modules["core.container_real"] = _container_mod
_spec.loader.exec_module(_container_mod)

Container = _container_mod.Container
_NullAndroidService = _container_mod._NullAndroidService


def test_container_resolves_workflow_service_with_android_plugin_absent():
    """workflow_service must resolve even though nodes/android/ doesn't exist."""
    container = Container()

    workflow_service = container.workflow_service()

    assert workflow_service is not None
    assert isinstance(workflow_service._node_executor.android_service, _NullAndroidService)


def test_null_android_service_raises_only_when_actually_used():
    """The fallback defers failure to call time, not container construction."""
    stub = _NullAndroidService()

    with pytest.raises(RuntimeError, match="Android service is unavailable"):
        stub.send_sms("+15555555555", "hi")
