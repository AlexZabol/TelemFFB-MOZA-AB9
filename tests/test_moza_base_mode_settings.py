"""The 'MOZA Base Mode' setting switches the base whenever the user picks a value,
also when the pick only erases an override (the value equals the inherited one)."""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault('telemffb.hw.hid', MagicMock())

import telemffb.globals as G  # noqa: E402
from telemffb.hw import ffb_moza  # noqa: E402
from telemffb.SettingsManager import SettingsManager  # noqa: E402


@pytest.fixture
def applied(monkeypatch):
    calls = []
    monkeypatch.setattr(ffb_moza, "apply_base_mode_setting", lambda v: calls.append(v) or True)
    return calls


def _manager(monkeypatch, effective):
    """A stand-in with SettingsManager's real erase / apply logic and no XML or Qt."""
    erased = []
    sm = SimpleNamespace(
        _erase_from_xml=lambda *a, **k: erased.append(a),
        read_setting_from_xml=lambda *a, **k: effective,
        apply_moza_base_mode=SettingsManager.apply_moza_base_mode,
    )
    sm.erase_from_xml = lambda *a, **k: SettingsManager.erase_from_xml(sm, *a, **k)
    return sm, erased


@pytest.mark.unit
def test_erasing_the_override_applies_the_mode_now_in_effect(monkeypatch, applied):
    sm, erased = _manager(monkeypatch, "FS")
    sm.erase_from_xml("DCS", "JetAircraft", "F-16C_50", "moza_base_mode")
    assert erased and applied == ["FS"]


@pytest.mark.unit
def test_erasing_other_settings_does_not_touch_the_base(monkeypatch, applied):
    sm, erased = _manager(monkeypatch, "FS")
    sm.erase_from_xml("DCS", "JetAircraft", "F-16C_50", "gun_vibration_intensity")
    assert erased and applied == []


@pytest.mark.unit
def test_picking_the_inherited_value_without_override_still_switches(monkeypatch, applied):
    from telemffb.ui.widgets.SettingsLayout import SettingsLayout

    sm, erased = _manager(monkeypatch, "FS")
    sm.current_sim, sm.current_class, sm.current_pattern = "DCS", "JetAircraft", "F-16C_50"
    monkeypatch.setattr(G, "settings_mgr", sm, raising=False)
    layout = SimpleNamespace(revert_targets={"moza_base_mode": ("FS", "", False, False)},
                             setting_values_equal=SettingsLayout.setting_values_equal)
    assert SettingsLayout.write_or_revert(layout, "moza_base_mode", "FS") is True
    assert erased == [] and applied == ["FS"]


@pytest.mark.unit
def test_picking_the_inherited_value_with_override_erases_and_switches(monkeypatch, applied):
    from telemffb.ui.widgets.SettingsLayout import SettingsLayout

    sm, erased = _manager(monkeypatch, "FS")
    sm.current_sim, sm.current_class, sm.current_pattern = "DCS", "JetAircraft", "F-16C_50"
    monkeypatch.setattr(G, "settings_mgr", sm, raising=False)
    layout = SimpleNamespace(revert_targets={"moza_base_mode": ("FS", "", True, False)},
                             setting_values_equal=SettingsLayout.setting_values_equal,
                             trigger_form_reload=False)
    assert SettingsLayout.write_or_revert(layout, "moza_base_mode", "FS") is True
    assert len(erased) == 1 and applied == ["FS"]
