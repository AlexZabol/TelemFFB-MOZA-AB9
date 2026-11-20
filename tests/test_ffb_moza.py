"""Unit tests for the MOZA SDK backend's parameter conversion (no hardware, no SDK DLL)."""
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault('telemffb.hw.hid', MagicMock())

from telemffb.hw import ffb_moza  # noqa: E402
from telemffb.hw.ffb_rhino import EFFECT_CONSTANT, EFFECT_SINE  # noqa: E402


class _FakeSdk:
    def __init__(self):
        self.sent = []
        self.is_open = True

    def send(self, cmd, specs, sync=False, timeout_ms=1000):
        self.sent.append((cmd, specs))
        return 0, None


class _FakeDevice:
    def __init__(self):
        self.sdk = _FakeSdk()
        self.released = []

    def release_slot(self, slot):
        self.released.append(slot)


@pytest.mark.unit
def test_periodic_maps_to_moza_units():
    dev = _FakeDevice()
    h = ffb_moza.MozaEffectHandle(dev, 3, EFFECT_SINE)
    h.setPeriodic(20, 0.33, 180)
    cmd, specs = dev.sdk.sent[-1]
    assert cmd == ffb_moza.CMD_PERIODIC
    vals = [v for _, v in specs]
    # idx, magnitude, offset, phase, period(ms), duration, enableAxis, dirX, dirY
    assert vals[0] == 3
    assert vals[1] == round(0.33 * ffb_moza.FULL_SCALE)
    assert vals[4] == 50
    assert vals[5] == ffb_moza.DURATION_INFINITE
    assert vals[6] == ffb_moza.AXIS_DIRECTION_ENABLE
    assert vals[7] == 18000


@pytest.mark.unit
def test_constant_uses_flat_enable_axis_and_signed_magnitude():
    dev = _FakeDevice()
    h = ffb_moza.MozaEffectHandle(dev, 5, EFFECT_CONSTANT)
    h.setConstantForce(-0.5, 90)
    cmd, specs = dev.sdk.sent[-1]
    assert cmd == ffb_moza.CMD_CONSTANT
    vals = [v for _, v in specs]
    assert len(vals) == 8
    assert vals[1] == -round(0.5 * ffb_moza.FULL_SCALE)
    assert vals[3:6] == [0, 0, 1]
    assert vals[6] == 9000


@pytest.mark.unit
def test_start_stop_destroy_send_operations_and_free_slot():
    dev = _FakeDevice()
    h = ffb_moza.MozaEffectHandle(dev, 7, EFFECT_SINE)
    h.setPeriodic(10, 0.1, 0)
    h.start()
    assert h.started
    assert (ffb_moza.CMD_OPERATION, [("u", 7), ("u", ffb_moza.OP_START)]) in dev.sdk.sent
    h.stop()
    assert not h.started
    h.destroy()
    assert dev.released == [7]
    assert (ffb_moza.CMD_OPERATION, [("u", 7), ("u", ffb_moza.OP_FREE)]) in dev.sdk.sent


@pytest.mark.unit
def test_moza_build_disables_official_updates(monkeypatch):
    import types
    from telemffb.ui import updates
    monkeypatch.setitem(sys.modules, "version", types.SimpleNamespace(VERSION="MOZA 0.1.3 (TelemFFB 3.0.261019)"))
    assert updates.is_moza_build()
    monkeypatch.setitem(sys.modules, "version", types.SimpleNamespace(VERSION="3.0.261019"))
    assert not updates.is_moza_build()


@pytest.mark.unit
def test_is_moza_device():
    info = MagicMock(vendor_id=0x346E)
    assert ffb_moza.is_moza_device(info)
    assert not ffb_moza.is_moza_device(MagicMock(vendor_id=0xFFFF))
