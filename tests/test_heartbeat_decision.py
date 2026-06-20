"""Unit tests for the pure heartbeat decision in pyatv_tvos26_patch.

The module imports `homeassistant.core` / `homeassistant.helpers.typing` at
top level, so we stub those before importing it — the decision function
itself is HA-free.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _load_patch_module():
    for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers",
                 "homeassistant.helpers.typing"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    if not hasattr(sys.modules["homeassistant.core"], "HomeAssistant"):
        sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
    if not hasattr(sys.modules["homeassistant.helpers.typing"], "ConfigType"):
        sys.modules["homeassistant.helpers.typing"].ConfigType = dict
    path = (
        Path(__file__).parent.parent
        / "custom_components" / "pyatv_tvos26_patch" / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("pyatv_tvos26_patch_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


m = _load_patch_module()
decide = m.decide_heartbeat_action
SILENCE = m.HEARTBEAT_SILENCE_S
COOLDOWN = m.HEARTBEAT_RESUB_COOLDOWN_S


def test_recent_push_is_noop():
    """Push within the silence window -> healthy -> do nothing."""
    assert decide(0.0, None) == m.ACTION_NOOP
    assert decide(SILENCE - 1, None) == m.ACTION_NOOP


def test_silent_past_threshold_acts_when_never_resubscribed():
    assert decide(SILENCE, None) == m.ACTION_ACT
    assert decide(SILENCE + 600, None) == m.ACTION_ACT


def test_silent_but_recently_resubscribed_is_cooldown():
    """Silent past threshold but we re-subscribed recently -> wait, don't
    hammer (the HA-reload backstop covers the truly-stuck case)."""
    assert decide(SILENCE + 100, 0.0) == m.ACTION_COOLDOWN
    assert decide(SILENCE + 100, COOLDOWN - 1) == m.ACTION_COOLDOWN


def test_silent_and_cooldown_elapsed_acts_again():
    assert decide(SILENCE + 100, COOLDOWN) == m.ACTION_ACT
    assert decide(SILENCE + 100, COOLDOWN + 600) == m.ACTION_ACT


def test_recent_push_wins_even_if_cooldown_elapsed():
    """A fresh push always means healthy, regardless of resub history."""
    assert decide(1.0, COOLDOWN + 9999) == m.ACTION_NOOP


def test_exact_silence_boundary_is_inclusive():
    assert decide(SILENCE, None) == m.ACTION_ACT
    assert decide(SILENCE - 0.001, None) == m.ACTION_NOOP
