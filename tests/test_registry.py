from __future__ import annotations

import pytest

from relaykit.core.registry import PluginNotFound, Registry


class Dummy:
    pass


def test_local_registration_and_lookup():
    registry: Registry = Registry("relaykit.test")
    registry.register("dummy", Dummy)
    assert registry.get("dummy") is Dummy
    assert "dummy" in registry
    assert "dummy" in registry.names()


def test_missing_plugin_names_what_is_available():
    """The error is the only documentation someone gets at that moment."""
    registry: Registry = Registry("relaykit.test")
    registry.register("dummy", Dummy)
    with pytest.raises(PluginNotFound) as excinfo:
        registry.get("nope")
    assert "dummy" in str(excinfo.value)


def test_unregister_removes_it():
    registry: Registry = Registry("relaykit.test")
    registry.register("dummy", Dummy)
    registry.unregister("dummy")
    assert "dummy" not in registry


def test_shipped_engines_are_discoverable():
    from relaykit import available_engines

    names = available_engines()
    assert {"chrome", "safari", "playwright"} <= set(names)
