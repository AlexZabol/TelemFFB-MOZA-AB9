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
    assert vals[7] == 0       # PID "from" 180 -> MOZA "toward" 0


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
    assert vals[6] == 27000   # PID 90 -> MOZA 270


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


class _ParamSdk:
    """SDK stand-in with a parameter store: writes land unless frozen/failing."""

    def __init__(self, frozen=(), write_err=0):
        self.is_open = True
        self.params = {ffb_moza.PARAM_STEER_FORCE_MODE: 0, ffb_moza.PARAM_CTRL_MODE: 1}
        self.writes = []
        self.frozen = set(frozen)
        self.write_err = write_err
        self.sent = []

    def send(self, cmd, specs, sync=False, timeout_ms=1000):
        self.sent.append((cmd, specs))
        return 0, 4

    def set_param_uint(self, pid, v, signed=False):
        self.writes.append((pid, v))
        if self.write_err in (0, ffb_moza._ERR_PENDING, ffb_moza._ERR_BUSY) and pid not in self.frozen:
            self.params[pid] = v
        return self.write_err

    def get_param_int(self, pid, timeout_ms=2000):
        return self.params.get(pid)


def _moza_device(sdk, base_mode="FFB"):
    import threading
    dev = ffb_moza.MozaFFBDevice.__new__(ffb_moza.MozaFFBDevice)
    dev.sdk = sdk
    dev._base_mode = base_mode
    dev._free_slots = set(range(1, 81))
    dev._moza_handles = []
    dev._slot_lock = threading.Lock()
    dev.fx_lock = threading.RLock()
    dev.reset_effects = MagicMock()
    return dev


@pytest.mark.unit
def test_set_base_mode_writes_steer_force_mode():
    sdk = _ParamSdk()
    dev = _moza_device(sdk)
    assert dev.set_base_mode("FS")
    assert dev._base_mode == "FS"
    assert dev.set_base_mode("FFB")
    assert not dev.set_base_mode("KEEP")
    ctrl = (ffb_moza.PARAM_CTRL_MODE, ffb_moza.CTRL_MODE_TELEMETRY_AND_DINPUT)
    assert sdk.writes == [(ffb_moza.PARAM_STEER_FORCE_MODE, 1), ctrl, (ffb_moza.PARAM_STEER_FORCE_MODE, 0), ctrl]


@pytest.mark.unit
def test_unconfirmed_base_mode_keeps_channel(monkeypatch):
    monkeypatch.setattr(ffb_moza.time, "sleep", lambda s: None)
    # base accepts the write (BUSY) but keeps reporting Force Feedback
    sdk = _ParamSdk(frozen={ffb_moza.PARAM_STEER_FORCE_MODE}, write_err=ffb_moza._ERR_BUSY)
    dev = _moza_device(sdk)
    assert not dev.set_base_mode("FS")
    assert dev._base_mode == "FFB"
    dev.reset_effects.assert_not_called()
    # mode unreadable: no switch either
    sdk = _ParamSdk()
    sdk.get_param_int = lambda pid, timeout_ms=2000: None
    dev = _moza_device(sdk)
    assert not dev.set_base_mode("FS")
    assert dev._base_mode == "FFB"


@pytest.mark.unit
def test_base_mode_switch_fails_when_ffb_source_not_confirmed(monkeypatch):
    monkeypatch.setattr(ffb_moza.time, "sleep", lambda s: None)
    sdk = _ParamSdk(frozen={ffb_moza.PARAM_CTRL_MODE})
    dev = _moza_device(sdk)
    assert not dev.set_base_mode("FS")      # reported as failure (retried by the mixin) ...
    assert dev._base_mode == "FS"           # ... but the channel follows the confirmed base mode


def _mixin(monkeypatch, dev):
    from telemffb.hw.ffb_rhino import HapticEffect
    from telemffb.sim.base.MozaBaseModeMixIn import MozaBaseModeMixIn
    monkeypatch.setattr(HapticEffect, "device", dev)
    m = MozaBaseModeMixIn.__new__(MozaBaseModeMixIn)
    m._moza_mode_applied = "KEEP"
    m._moza_device = dev
    m._moza_retry_at = 0.0
    return m


def _ab9(**kw):
    dev = MagicMock(**kw)
    dev.info = MagicMock(product_id=0x1000, product_string="MOZA AB9 FFB Base")
    return dev


@pytest.mark.unit
def test_moza_base_mode_mixin_applies_once(monkeypatch):
    dev = _ab9(connected=True)
    dev.set_base_mode.return_value = True
    m = _mixin(monkeypatch, dev)
    m.moza_base_mode = "FS"
    m._apply_moza_base_mode()
    m._apply_moza_base_mode()
    dev.set_base_mode.assert_called_once_with("FS")


@pytest.mark.unit
def test_moza_base_mode_mixin_retries(monkeypatch):
    from telemffb.sim.base import MozaBaseModeMixIn as mod
    clock = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    dev = _ab9(connected=False)
    dev.set_base_mode.return_value = False
    m = _mixin(monkeypatch, dev)
    m.moza_base_mode = "FS"
    m._apply_moza_base_mode()                 # disconnected: nothing sent, not marked applied
    dev.set_base_mode.assert_not_called()
    dev.connected = True
    m._apply_moza_base_mode()                 # base did not confirm
    m._apply_moza_base_mode()                 # within the back-off: no new attempt
    assert dev.set_base_mode.call_count == 1
    clock[0] += mod.RETRY_INTERVAL_S
    dev.set_base_mode.return_value = True
    m._apply_moza_base_mode()
    m._apply_moza_base_mode()
    assert dev.set_base_mode.call_count == 2
    # a re-opened device gets the mode again
    dev2 = _ab9(connected=True)
    dev2.set_base_mode.return_value = True
    from telemffb.hw.ffb_rhino import HapticEffect
    monkeypatch.setattr(HapticEffect, "device", dev2)
    m._apply_moza_base_mode()
    dev2.set_base_mode.assert_called_once_with("FS")


@pytest.mark.unit
def test_finite_effect_can_be_restarted(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(ffb_moza.time, "monotonic", lambda: clock[0])
    dev = _FakeDevice()
    h = ffb_moza.MozaEffectHandle(dev, 2, EFFECT_SINE)
    h.setPeriodic(10, 0.5, 0, duration=40)
    h.start()
    assert h.started
    clock[0] += 0.05                           # the 40 ms clunk is over on the base
    assert not h.started
    h.start()
    starts = [s for s in dev.sdk.sent if s == (ffb_moza.CMD_OPERATION, [("u", 2), ("u", ffb_moza.OP_START)])]
    assert len(starts) == 2
    h.effect_id = None


@pytest.mark.unit
def test_failed_start_is_not_playing():
    dev = _FakeDevice()
    dev.sdk.send = lambda cmd, specs, sync=False, timeout_ms=1000: (-1, None)
    h = ffb_moza.MozaEffectHandle(dev, 2, EFFECT_SINE)
    h.start()
    assert not h.started
    h.effect_id = None


@pytest.mark.unit
def test_unchanged_parameters_are_not_resent_while_playing():
    dev = _FakeDevice()
    h = ffb_moza.MozaEffectHandle(dev, 2, EFFECT_SINE)
    h.setPeriodic(20, 0.3, 0)
    h.start()
    n = len(dev.sdk.sent)
    for _ in range(100):
        h.setPeriodic(20, 0.3, 0)
    assert len(dev.sdk.sent) == n
    h.setPeriodic(20, 0.4, 0)
    assert len(dev.sdk.sent) == n + 1
    h.resend()                                 # keepalive refresh still goes out
    assert len(dev.sdk.sent) == n + 2
    h.effect_id = None


@pytest.mark.unit
def test_no_resend_after_destroy():
    import threading
    dev = _FakeDevice()
    dev.fx_lock = threading.RLock()
    h = ffb_moza.MozaEffectHandle(dev, 2, EFFECT_SINE)
    h.setPeriodic(20, 0.3, 0)
    h.start()
    h.destroy()
    n = len(dev.sdk.sent)
    h.resend()
    assert len(dev.sdk.sent) == n


@pytest.mark.unit
def test_select_base_matches_directinput_device():
    from types import SimpleNamespace as NS
    ab9, ab6 = NS(productId=0x1000), NS(productId=0x1001)
    assert ffb_moza.select_base([ab9]) is ab9                 # one base: unambiguous
    assert ffb_moza.select_base([ab9], 0x1001) is ab9
    assert ffb_moza.select_base([ab9, ab6], 0x1001) is ab6
    assert ffb_moza.select_base([ab9, ab6]) is None           # unknown which one
    assert ffb_moza.select_base([ab9, NS(productId=0x1000)], 0x1000) is None   # two AB9
    assert ffb_moza.select_base([], 0x1000) is None


@pytest.mark.unit
def test_base_mode_only_for_ab9():
    ab9 = MagicMock(info=MagicMock(product_id=0x1000, product_string="MOZA AB9 FFB Base"))
    ab6 = MagicMock(info=MagicMock(product_id=0x1001, product_string="MOZA AB6 FFB Base"))
    assert ffb_moza.base_mode_supported(ab9)
    assert not ffb_moza.base_mode_supported(ab6)
    assert not ffb_moza.base_mode_supported(MagicMock(spec=[]))

@pytest.mark.unit
def test_direction_is_rotated_180_for_moza():
    # elevator droop: TelemFFB constant(force, 180) must push the stick forward, as with DirectInput
    assert ffb_moza.MozaEffectHandle._dir(180) == 0
    assert ffb_moza.MozaEffectHandle._dir(0) == 18000
    assert ffb_moza.MozaEffectHandle._dir(270) == 9000

@pytest.mark.unit
def test_effect_channel_follows_base_mode(monkeypatch):
    from telemffb.hw.ffb_dinput import DInputFFBDevice
    dinput_create = MagicMock(return_value="dinput-handle")
    monkeypatch.setattr(DInputFFBDevice, "create_effect", dinput_create)
    sdk = _ParamSdk()
    sdk.params[ffb_moza.PARAM_STEER_FORCE_MODE] = 1
    dev = _moza_device(sdk, base_mode="FFB")
    # Force Feedback: exactly like official TelemFFB - DirectInput
    assert dev.create_effect(EFFECT_SINE) == "dinput-handle"
    # Force Sensing: MOZA SDK
    dev._base_mode = "FS"
    h = dev.create_effect(EFFECT_SINE)
    assert isinstance(h, ffb_moza.MozaEffectHandle)
    # mode change moves effects to the other channel
    assert dev.set_base_mode("FFB")
    dev.reset_effects.assert_called_once()
    h.effect_id = None   # avoid __del__ side effects

@pytest.mark.unit
def test_moza_release_versions(monkeypatch):
    from telemffb.utils import moza_release as mr
    assert mr.is_moza_version("MOZA 0.1.8 (TelemFFB 3.0.261019)")
    assert not mr.is_moza_version("3.0.261019")
    assert mr.version_tuple("MOZA 0.1.10 (TelemFFB 3.0.261019)") == (0, 1, 10)
    assert mr.version_tuple("v0.1.9") == (0, 1, 9)
    monkeypatch.setattr(mr, "fetch_latest", lambda timeout=10: ("0.1.10", "u", "p"))
    assert mr.newer_release("MOZA 0.1.9 (TelemFFB 3.0.261019)") == ("0.1.10", "u", "p")
    assert mr.newer_release("MOZA 0.1.10 (TelemFFB 3.0.261019)") is None
