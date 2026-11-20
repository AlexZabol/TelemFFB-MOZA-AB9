#
# This file is part of the TelemFFB distribution (https://github.com/walmis/TelemFFB).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#

"""MOZA flight base backend (AB6/AB9) through the official MOZA SDK.

Why this exists
---------------
In the base's *Force Sensing* mode the DirectInput (USB-PID HID) effects TelemFFB normally
sends are ignored.  MOZA Cockpit drives its telemetry effects over the base's serial
protocol instead (the ``MainCtrl_Ffb*`` commands of the MOZA SDK), and the base only
renders those effects while the host keeps reading the motor output torque
(``MainGet_Steer_TorqueOutX``, SDK parameter id 95) - Cockpit polls it ~10x/s.
Without the poll the base accepts every command and produces no force, in either mode.

Design
------
``MozaFFBDevice`` *is* a ``DInputFFBDevice``: device selection, axes, buttons, hats and
condition effects (spring/damper/inertia/friction) keep going through DirectInput exactly
as before.  Constant, ramp and periodic effects - the telemetry cues: engine rumble, guns,
gear, buffeting, ... - are created through the MOZA SDK instead, and a keepalive thread
polls the motor torque and refreshes the playing SDK effects.

Requirements: MOZA DeviceService (installed with MOZA Cockpit), ``MOZA_SDK.dll`` (x64,
from the MOZA SDK) next to the executable / in ``dll/`` / in ``%TELEMFFB_MOZA_SDK%``.
The MOZA Cockpit *window* must be closed while TelemFFB runs (the SDK and Cockpit cannot
share the base); the DeviceService itself is started automatically by the SDK.
"""

import ctypes
import logging
import os
import sys
import threading
import time
import weakref
from ctypes import (CFUNCTYPE, POINTER, Structure, Union, byref, c_char, c_double, c_int32,
                    c_int64, c_uint8, c_uint16, c_uint32, c_uint64, c_void_p)
from typing import Dict, Optional

from telemffb.hw import ffb_backend
from telemffb.hw.ffb_dinput import DInputFFBDevice
from telemffb.hw.ffb_rhino import (EFFECT_CONSTANT, EFFECT_RAMP, PERIODIC_EFFECTS,
                                   FFBReport_SetCondition, FFBReport_SetEnvelope, effect_names)

MOZA_VENDOR_ID = 0x346E

# --- MOZA SDK constants (moza.h / device_as.generated.hpp) ---------------------------
_ERR_OK, _ERR_ALREADY_INITIALIZED, _ERR_BUSY, _ERR_PENDING = 0, 5, 10, 16
_VT_INT, _VT_UINT, _VT_LIST = 2, 3, 8
_DC_AS, _DT_AS_FLIGHTBASE = 2, 1

CMD_NEW_EFFECT = 603        # type(u8), effectBlockIndex(u8) -> index (0 = failure)
CMD_CONSTANT = 604          # idx, magnitude(i16), duration(u16), enableAxis{X,Y,Dir} (3 items), dirX, dirY
CMD_RAMP = 605              # idx, startLevel, endLevel, duration, enableAxis(u8 mask), dirX, dirY
CMD_PERIODIC = 606          # idx, magnitude(i16), offset(i16), phase(u16), period(u32), duration(u16), enableAxis(u8 mask), dirX, dirY
CMD_ENVELOPE = 608          # idx, attackLevel(u16), fadeLevel(u16), attackTime(u32), fadeTime(u32)
CMD_OPERATION = 609         # idx, op (0 stop, 1 start, 2 free)
CMD_CONTROL = 610           # 1 start calculation, 2 reset/free all, 3 (sent by Cockpit before playing)
PARAM_TORQUE_OUT_X = 95     # Main_Steer_TorqueOutX - polling it is what keeps FFB alive

OP_STOP, OP_START, OP_FREE = 0, 1, 2
AXIS_DIRECTION_ENABLE = 4   # enableAxis: bit2 = use direction (what Cockpit sends)
FULL_SCALE = 32767          # MOZA magnitude: int16 full scale (Cockpit: 33 % gun -> 10813)
DURATION_INFINITE = 65535
SLOT_MIN, SLOT_MAX = 1, 80

SDK_EFFECTS = set(PERIODIC_EFFECTS) | {EFFECT_CONSTANT, EFFECT_RAMP}
KEEPALIVE_INTERVAL_S = 0.1


class _PtrSize(Structure):
    _pack_ = 1
    _fields_ = [("data", c_void_p), ("size", c_uint32)]


class _ValueData(Union):
    _pack_ = 1
    _fields_ = [("intValue", c_int64), ("uintValue", c_uint64), ("doubleValue", c_double),
                ("boolValue", c_uint8), ("stringValue", _PtrSize), ("binaryValue", _PtrSize),
                ("listValue", _PtrSize)]


class _Value(Structure):
    _pack_ = 1
    _fields_ = [("type", c_int32), ("data", _ValueData)]


_VALUE_SIZE = 16


class _DevInfo(Structure):
    _pack_ = 1
    _fields_ = [("id", c_uint32), ("name", c_char * 64), ("portName", c_char * 256),
                ("vendorId", c_uint16), ("productId", c_uint16), ("category", c_int32),
                ("type", c_int32), ("state", c_int32), ("hidDeviceCount", c_uint8),
                ("reserved", c_void_p)]


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def sdk_dll_path() -> Optional[str]:
    """Where MOZA_SDK.dll is: %TELEMFFB_MOZA_SDK%, next to the exe/main.py, or in dll/."""
    cands = []
    env = os.environ.get("TELEMFFB_MOZA_SDK")
    if env:
        cands.append(env if env.lower().endswith(".dll") else os.path.join(env, "MOZA_SDK.dll"))
    bases = [os.path.dirname(os.path.abspath(sys.executable)),
             os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else ".")),
             getattr(sys, "_MEIPASS", "")]
    for b in bases:
        if b:
            cands += [os.path.join(b, "MOZA_SDK.dll"), os.path.join(b, "dll", "MOZA_SDK.dll")]
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def is_moza_device(info) -> bool:
    return getattr(info, "vendor_id", None) == MOZA_VENDOR_ID


class MozaSdk:
    """Minimal thread-safe ctypes wrapper around MOZA_SDK.dll for one flight base."""

    def __init__(self, dll_path: str):
        try:
            os.add_dll_directory(os.path.dirname(dll_path))
        except (AttributeError, OSError):
            pass
        L = self._L = ctypes.CDLL(dll_path)
        pV = POINTER(_Value)
        for name, res, args in (
                ("Moza_Initialize", c_int32, []),
                ("Moza_Shutdown", None, []),
                ("Moza_ValueCreate", c_uint8, [c_int32, pV, c_uint32]),
                ("Moza_ValueDestroy", None, [pV]),
                ("Moza_DeviceGetList", c_int32, [c_void_p, POINTER(c_uint32)]),
                ("Moza_DeviceOpen", c_int32, [c_uint32, POINTER(c_void_p)]),
                ("Moza_DeviceClose", c_int32, [c_void_p]),
                ("Moza_DeviceParameterFetch", c_int32, [c_void_p, c_int32]),
                ("Moza_DeviceCommandSend", c_int32, [c_void_p, c_int32, pV]),
                ("Moza_DeviceCommandSendSync", c_int32, [c_void_p, c_int32, pV, c_int32, pV])):
            fn = getattr(L, name)
            fn.restype = res
            fn.argtypes = args
        self._lock = threading.RLock()
        self._h = None
        self.device_name = ""

    # -- connection -----------------------------------------------------------------
    def open(self, timeout_s: float = 10.0) -> bool:
        with self._lock:
            e = self._L.Moza_Initialize()
            if e not in (_ERR_OK, _ERR_ALREADY_INITIALIZED):
                logging.warning(f"MOZA SDK: Moza_Initialize failed ({e}); is MOZA DeviceService installed "
                                "and the MOZA Cockpit window closed?")
                return False
            t0 = time.monotonic()
            n = c_uint32(0)
            while time.monotonic() - t0 < timeout_s:   # enumeration is asynchronous
                n = c_uint32(0)
                self._L.Moza_DeviceGetList(None, byref(n))
                if n.value:
                    break
                time.sleep(0.25)
            arr = (_DevInfo * max(n.value, 1))()
            cap = c_uint32(n.value)
            self._L.Moza_DeviceGetList(arr, byref(cap))
            dev = next((arr[i] for i in range(cap.value)
                        if arr[i].category == _DC_AS and arr[i].type & _DT_AS_FLIGHTBASE), None)
            if dev is None:
                logging.warning("MOZA SDK: no flight base found")
                self._L.Moza_Shutdown()
                return False
            h = c_void_p()
            e = self._L.Moza_DeviceOpen(dev.id, byref(h))
            if e != _ERR_OK or not h.value:
                logging.warning(f"MOZA SDK: Moza_DeviceOpen failed ({e})")
                self._L.Moza_Shutdown()
                return False
            self._h = h.value
            self.device_name = dev.name.decode("utf-8", "replace")
            return True

    def close(self):
        with self._lock:
            if self._h:
                try:
                    self._L.Moza_DeviceClose(self._h)
                finally:
                    self._h = None
                    self._L.Moza_Shutdown()

    @property
    def is_open(self) -> bool:
        return self._h is not None

    # -- primitives -----------------------------------------------------------------
    def _fill(self, v, spec):
        kind, val = spec
        if kind == "i":
            v.type = _VT_INT
            v.data.intValue = int(val)
        elif kind == "u":
            v.type = _VT_UINT
            v.data.uintValue = int(val)
        else:
            if not self._L.Moza_ValueCreate(_VT_LIST, byref(v), len(val)):
                raise RuntimeError("Moza_ValueCreate failed")
            base = v.data.listValue.data
            for i, sub in enumerate(val):
                self._fill(_Value.from_address(base + _VALUE_SIZE * i), sub)

    def send(self, cmd: int, specs, sync: bool = False, timeout_ms: int = 1000):
        """Send a command; returns (err, reply-scalar-or-None)."""
        with self._lock:
            if not self._h:
                return -1, None
            args, res = _Value(), _Value()
            try:
                self._fill(args, ("list", specs))
                if not sync:
                    return self._L.Moza_DeviceCommandSend(self._h, cmd, byref(args)), None
                e = self._L.Moza_DeviceCommandSendSync(self._h, cmd, byref(args), timeout_ms, byref(res))
                reply = None
                if e == _ERR_OK:
                    v = res
                    while v.type == _VT_LIST and v.data.listValue.size:
                        v = _Value.from_address(v.data.listValue.data)
                    if v.type in (_VT_INT, _VT_UINT):
                        reply = v.data.uintValue if v.type == _VT_UINT else v.data.intValue
                return e, reply
            finally:
                self._L.Moza_ValueDestroy(byref(res))
                self._L.Moza_ValueDestroy(byref(args))

    def poll_torque(self) -> int:
        with self._lock:
            if not self._h:
                return -1
            return self._L.Moza_DeviceParameterFetch(self._h, PARAM_TORQUE_OUT_X)


class MozaEffectHandle(ffb_backend.BaseEffectHandle):
    """A constant / ramp / periodic effect living in a MOZA SDK effect slot."""

    def __init__(self, device: "MozaFFBDevice", slot: int, effect_type: int) -> None:
        super().__init__()
        self.device = device
        self.effect_id = slot
        self.type = effect_type
        self._started = False
        self._last_cmd = None          # (cmd, specs) last parameter set, re-sent by the keepalive
        self._last_env = None

    def __bool__(self):
        return bool(self.effect_id and self.type)

    def __repr__(self):
        return f"MozaEffectHandle({self.effect_id}, {effect_names.get(self.type)})"

    @property
    def name(self):
        return effect_names.get(self.type)

    @property
    def started(self):
        return self._started

    def invalidate(self):
        self.effect_id = 0
        self._started = False

    def forget_playback(self):
        self._started = False

    def _send(self, cmd, specs):
        if not self.effect_id:
            return
        self._last_cmd = (cmd, specs)
        self.device.sdk.send(cmd, specs)

    def resend(self):
        """Keepalive refresh: re-send the last parameters of a playing effect."""
        if self._started and self._last_cmd and self.effect_id:
            self.device.sdk.send(*self._last_cmd)

    def start(self, loopCount=1, override=False):
        if not self.effect_id:
            logging.warning(f"start on an invalidated effect ({self.name})")
            return self
        if self._last_cmd:
            self.device.sdk.send(*self._last_cmd)
        self.device.sdk.send(CMD_OPERATION, [("u", self.effect_id), ("u", OP_START)])
        self._started = True
        return self

    def stop(self):
        if self.effect_id and self._started:
            self.device.sdk.send(CMD_OPERATION, [("u", self.effect_id), ("u", OP_STOP)])
        self._started = False
        return self

    def destroy(self):
        if self.effect_id:
            self.device.sdk.send(CMD_OPERATION, [("u", self.effect_id), ("u", OP_STOP)])
            self.device.sdk.send(CMD_OPERATION, [("u", self.effect_id), ("u", OP_FREE)])
            self.device.release_slot(self.effect_id)
            self.effect_id = None
            self.type = 0
            self._started = False

    def __del__(self):
        try:
            self.destroy()
        except Exception:
            pass

    # -- parameters ---------------------------------------------------------------
    @staticmethod
    def _dir(direction_deg) -> int:
        return int(round(direction_deg)) % 360 * 100

    def setEffect(self, **kwargs):
        # gain/duration of the Rhino SET_EFFECT report: duration is applied with the next
        # parameter set; device gain stays at the base's own setting
        if "duration" in kwargs and self._last_cmd:
            pass

    def setCondition(self, condition: FFBReport_SetCondition):
        logging.debug("MozaEffectHandle.setCondition: conditions are handled by DirectInput")

    def setConstantForce(self, magnitude, direction, **kwargs):
        mag = int(round(_clamp(magnitude, -1.0, 1.0) * FULL_SCALE))
        self._magnitude = abs(magnitude)
        # 604 takes enableAxis as three flat items {X, Y, DirectionEnable}
        self._send(CMD_CONSTANT, [("u", self.effect_id), ("i", mag), ("u", DURATION_INFINITE),
                                  ("i", 0), ("i", 0), ("i", 1),
                                  ("u", self._dir(direction)), ("u", 0)])
        return self

    def setPeriodic(self, freq, magnitude, direction, duration=0, **kwargs):
        mag = int(round(_clamp(magnitude, 0.0, 1.0) * FULL_SCALE))
        self._magnitude = abs(magnitude)
        period = int(round(1000.0 / freq)) if freq else 0
        dur = DURATION_INFINITE if not duration else int(_clamp(duration, 1, DURATION_INFINITE - 1))
        offset = int(_clamp(int(kwargs.get("offset", 0)), -4096, 4096) * FULL_SCALE / 4096)
        phase = int(round(int(kwargs.get("phase", 0)) * 360 / 255)) % 360
        self._send(CMD_PERIODIC, [("u", self.effect_id), ("i", mag), ("i", offset), ("u", phase),
                                  ("u", max(1, period)), ("u", dur), ("u", AXIS_DIRECTION_ENABLE),
                                  ("u", self._dir(direction)), ("u", 0)])
        return self

    def setRamp(self, start, end, direction, duration=0):
        """Not part of the BaseEffectHandle contract; kept for completeness."""
        s = int(round(_clamp(start, -1.0, 1.0) * FULL_SCALE)) & 0xFFFF
        e = int(round(_clamp(end, -1.0, 1.0) * FULL_SCALE)) & 0xFFFF
        dur = DURATION_INFINITE if not duration else int(_clamp(duration, 1, DURATION_INFINITE - 1))
        self._send(CMD_RAMP, [("u", self.effect_id), ("u", s), ("u", e), ("u", dur),
                              ("u", AXIS_DIRECTION_ENABLE), ("u", self._dir(direction)), ("u", 0)])
        return self

    def setEnvelope(self, envelope: FFBReport_SetEnvelope):
        scale = FULL_SCALE / 4096
        specs = [("u", self.effect_id),
                 ("u", int(_clamp(envelope.attackFromForce, 0, 4096) * scale)),
                 ("u", int(_clamp(envelope.decayToForce, 0, 4096) * scale)),
                 ("u", int(envelope.attackTime)), ("u", int(envelope.decayTime))]
        self._last_env = specs
        if self.effect_id:
            self.device.sdk.send(CMD_ENVELOPE, specs)


class MozaFFBDevice(DInputFFBDevice):
    """DirectInput device + MOZA SDK effect channel and FFB keepalive (see module doc)."""

    def __init__(self, guid: str, sdk: MozaSdk, **kwargs) -> None:
        self.sdk = sdk
        self._free_slots = set(range(SLOT_MIN, SLOT_MAX + 1))
        self._moza_handles: list = []
        self._slot_lock = threading.Lock()
        self._keepalive_stop = threading.Event()
        super().__init__(guid, **kwargs)
        self.sdk.send(CMD_CONTROL, [("i", 1)], sync=True)   # start effect calculation
        self.sdk.send(CMD_CONTROL, [("i", 3)], sync=True)   # as Cockpit does before playing
        self._keepalive = threading.Thread(target=self._keepalive_loop, name="MozaFFBKeepalive", daemon=True)
        self._keepalive.start()
        logging.info(f"MOZA: SDK effect channel open on '{sdk.device_name}' "
                     "(periodic/constant effects via MOZA SDK, conditions via DirectInput)")

    # -- slots ----------------------------------------------------------------------
    def _take_slot(self) -> Optional[int]:
        with self._slot_lock:
            if not self._free_slots:
                return None
            s = min(self._free_slots)
            self._free_slots.discard(s)
            return s

    def release_slot(self, slot: int):
        with self._slot_lock:
            if SLOT_MIN <= slot <= SLOT_MAX:
                self._free_slots.add(slot)

    # -- effects --------------------------------------------------------------------
    def create_effect(self, type):
        if type not in SDK_EFFECTS:
            return super().create_effect(type)          # conditions -> DirectInput
        if not self.sdk.is_open:
            return super().create_effect(type)
        slot = self._take_slot()
        if slot is None:
            logging.warning(f"MOZA: effect slots exhausted, cannot create {effect_names.get(type, type)}")
            return None
        e, idx = self.sdk.send(CMD_NEW_EFFECT, [("u", type), ("u", slot)], sync=True)
        if e != _ERR_OK or not idx:
            logging.warning(f"MOZA: NewEffect({effect_names.get(type, type)}, slot {slot}) failed: err={e} reply={idx}")
            self.release_slot(slot)
            return None
        h = MozaEffectHandle(self, int(idx), type)
        self._moza_handles.append(weakref.ref(h))
        return h

    def _live_moza_handles(self):
        alive = []
        for r in list(self._moza_handles):
            h = r()
            if h is None:
                try:
                    self._moza_handles.remove(r)
                except ValueError:
                    pass
            else:
                alive.append(h)
        return alive

    def reset_effects(self):
        super().reset_effects()
        for h in self._live_moza_handles():
            h.invalidate()
        with self._slot_lock:
            self._free_slots = set(range(SLOT_MIN, SLOT_MAX + 1))
        self.sdk.send(CMD_CONTROL, [("i", 2)], sync=True)
        self.sdk.send(CMD_CONTROL, [("i", 1)], sync=True)
        self.sdk.send(CMD_CONTROL, [("i", 3)], sync=True)

    # -- keepalive -------------------------------------------------------------------
    def _keepalive_loop(self):
        bad = 0
        while not self._keepalive_stop.wait(KEEPALIVE_INTERVAL_S):
            try:
                e = self.sdk.poll_torque()
                bad = bad + 1 if e not in (_ERR_OK, _ERR_BUSY, _ERR_PENDING) else 0
                if bad == 50:
                    logging.warning(f"MOZA: torque poll failing (err {e}); FFB may stop until reconnect")
                for h in self._live_moza_handles():
                    h.resend()
            except Exception:
                logging.exception("MOZA keepalive")

    def shutdown(self):
        self._keepalive_stop.set()
        try:
            for h in self._live_moza_handles():
                h.stop()
            self.sdk.send(CMD_CONTROL, [("i", 2)], sync=True)
        except Exception:
            logging.exception("MOZA shutdown")
        super().shutdown()
        self.sdk.close()


def open_moza_or_none(guid: str, info=None) -> Optional[MozaFFBDevice]:
    """Return a MozaFFBDevice for a MOZA DirectInput device when the MOZA SDK is usable,
    else None (the caller falls back to plain DirectInput)."""
    if os.environ.get("TELEMFFB_MOZA_DISABLE"):
        return None
    if info is not None and not is_moza_device(info):
        return None
    dll = sdk_dll_path()
    if not dll:
        logging.warning("MOZA device found but MOZA_SDK.dll is missing - using plain DirectInput "
                        "(effects will not play in Force Sensing mode)")
        return None
    try:
        sdk = MozaSdk(dll)
        if not sdk.open():
            return None
    except Exception:
        logging.exception("MOZA SDK load failed - using plain DirectInput")
        return None
    try:
        return MozaFFBDevice(guid, sdk)
    except Exception:
        sdk.close()
        raise
