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

"""MOZA AB9 flight base backend through the official MOZA SDK (other MOZA bases have no
Force Sensing mode and stay on plain DirectInput).

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
PARAM_STEER_FORCE_MODE = 247  # Main_SteerForceMode: 0 = Force Feedback, 1 = Force Sensing
BASE_MODES = {"FFB": 0, "FS": 1}
# Main_CtrlModeSet: which FFB source the base renders - 0 Telemetry FFB, 1 DirectInput FFB,
# 2 both.  MOZA Cockpit writes 0 in Force Sensing and 1 (or 2) in Force Feedback; with 1 the
# base ignores our (telemetry) effects, so TelemFFB always selects 2.
PARAM_CTRL_MODE = 53
CTRL_MODE_TELEMETRY_AND_DINPUT = 2

OP_STOP, OP_START, OP_FREE = 0, 1, 2
AXIS_DIRECTION_ENABLE = 4   # enableAxis: bit2 = use direction (what Cockpit sends)
FULL_SCALE = 32767          # MOZA magnitude: int16 full scale (Cockpit: 33 % gun -> 10813)
DURATION_INFINITE = 65535
SLOT_MIN, SLOT_MAX = 1, 80

SDK_EFFECTS = set(PERIODIC_EFFECTS) | {EFFECT_CONSTANT, EFFECT_RAMP}
# Moza_Initialize/Moza_Shutdown are process-wide: count the open MozaSdk users
_SDK_INIT_LOCK = threading.Lock()
_SDK_USERS = [0]
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


class _LogContext(Structure):     # MOZA_LOG_CONTEXT (declared outside moza.h's pack(1) block)
    _fields_ = [("level", c_int32), ("message", c_void_p), ("messageSize", c_uint32),
                ("category", c_void_p), ("categorySize", c_uint32), ("reserved", c_void_p * 4)]


_LOG_CB_T = CFUNCTYPE(None, POINTER(_LogContext), c_void_p)
_SDK_LOG_WARNING = 2   # MOZA_LOG_LEVEL: 0 debug, 1 info, 2 warning, 3 critical, 4 fatal


def _sdk_log(ctx_ptr, _user):
    try:
        c = ctx_ptr.contents
        if c.level < _SDK_LOG_WARNING:
            return
        msg = ctypes.string_at(c.message, c.messageSize).decode("utf-8", "replace") if c.message else ""
        if "MsgType" in msg and "未处理" in msg:
            # "unhandled message type": periodic service status messages this SDK version
            # (1.0.0.6) does not know (DeviceService 1.1.x sends them every ~3 s) - harmless
            logging.debug(f"MOZA SDK: {msg}")
            return
        logging.warning(f"MOZA SDK: {msg}")
    except Exception:
        pass


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


AB9_PRODUCT_ID = 0x1000


def is_ab9(info) -> bool:
    """A MOZA AB9 flight base - the only MOZA base with a Force Sensing mode, and so
    the only one the MOZA SDK backend is used for."""
    if not is_moza_device(info):
        return False
    name = str(getattr(info, "product_string", "") or "").upper()
    return getattr(info, "product_id", None) == AB9_PRODUCT_ID or "AB9" in name


def base_mode_supported(device=None) -> bool:
    """Whether the connected device is a MOZA AB9 driven by the MOZA SDK backend.
    Gates the 'MOZA Base Mode' setting."""
    if device is None:
        from telemffb.hw.ffb_rhino import HapticEffect
        device = HapticEffect.device
    if device is None or not hasattr(device, "set_base_mode"):
        return False
    info = getattr(device, "info", None)
    name = str(getattr(info, "product_string", "") or "").upper()
    return getattr(info, "product_id", None) == AB9_PRODUCT_ID or "AB9" in name


def apply_base_mode_setting(value) -> bool:
    """Apply a 'moza_base_mode' setting value (KEEP / FFB / FS, str or enum) to the
    connected base right away - used when the user changes the setting, which also
    covers Offline/Preview mode where no aircraft telemetry runs."""
    mode = str(getattr(value, "name", value) or "KEEP").upper()
    if mode not in BASE_MODES or not base_mode_supported():
        return False
    from telemffb.hw.ffb_rhino import HapticEffect

    def done(m, ok):
        if ok:
            logging.info(f"MOZA: base mode set to {'Force Sensing' if m == 'FS' else 'Force Feedback'} (settings)")
        else:
            logging.warning(f"MOZA: base mode {m} from settings not confirmed; it is retried while an aircraft is loaded")
    # off the UI thread: the switch waits for the base's confirmation
    HapticEffect.device.request_base_mode(mode, done)
    return True


def select_base(bases, product_id=None):
    """The SDK flight base to drive: the single one with the DirectInput device's USB
    ids (the SDK reports the same 346E:PPPP, e.g. AB9 = 346E:1000).  Without a product
    id only a lone base is accepted.  Anything ambiguous -> None (plain DirectInput)."""
    if product_id is None:
        return bases[0] if len(bases) == 1 else None
    match = [b for b in bases if getattr(b, "productId", None) == product_id
             and getattr(b, "vendorId", MOZA_VENDOR_ID) == MOZA_VENDOR_ID]
    return match[0] if len(match) == 1 else None


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
                ("Moza_DeviceParameterSetValue", c_int32, [c_void_p, c_int32, pV]),
                ("Moza_DeviceParameterGetValueSync", c_int32, [c_void_p, c_int32, c_int32, pV]),
                ("Moza_DeviceParameterFetchSync", c_int32, [c_void_p, c_int32, c_int32, pV]),
                ("Moza_DeviceCommandSend", c_int32, [c_void_p, c_int32, pV]),
                ("Moza_DeviceCommandSendSync", c_int32, [c_void_p, c_int32, pV, c_int32, pV])):
            fn = getattr(L, name)
            fn.restype = res
            fn.argtypes = args
        # _lock serialises the short non-blocking calls (their order on the wire matters).
        # Blocking calls (…Sync) run outside it, so a slow reply never holds up effect
        # sends; close() waits until none is in flight.
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._inflight = 0
        self._h = None
        self._closing_h = None
        self.device_name = ""
        # Route the SDK's own log to TelemFFB's log (warnings and errors only) instead of
        # its default per-command DEBUG output on the console.
        try:
            L.Moza_SetLogCallback.restype = c_int32
            L.Moza_SetLogCallback.argtypes = [_LOG_CB_T, c_void_p]
            self._log_cb = _LOG_CB_T(_sdk_log)          # keep a reference for the DLL's lifetime
            L.Moza_SetLogCallback(self._log_cb, None)
        except Exception:
            logging.debug("MOZA SDK: log callback not set", exc_info=True)

    # -- connection -----------------------------------------------------------------
    def open(self, timeout_s: float = 10.0, product_id: Optional[int] = None) -> bool:
        """Open the flight base whose USB ids match the DirectInput device
        (``product_id``); fail when there is no single match, so the caller keeps plain
        DirectInput rather than drive a different base."""
        with self._lock:
            # initialise and register as a user under the lock the last Shutdown takes, so
            # an old instance closing meanwhile cannot shut the SDK down under us
            with _SDK_INIT_LOCK:
                e = self._L.Moza_Initialize()
                if e not in (_ERR_OK, _ERR_ALREADY_INITIALIZED):
                    logging.warning(f"MOZA SDK: Moza_Initialize failed ({e}); is MOZA DeviceService installed "
                                    "and the MOZA Cockpit window closed?")
                    return False
                _SDK_USERS[0] += 1
            # the registration belongs to this object only once the base is open: any
            # other way out - a refusal or an exception - gives it back
            opened = False
            try:
                opened = self._open_base(timeout_s, product_id)
            finally:
                if not opened:
                    self._release_sdk()
            return opened

    def _open_base(self, timeout_s: float, product_id: Optional[int]) -> bool:
        """Enumerate and open the matching base (with _lock held, SDK initialised)."""
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
        bases = [arr[i] for i in range(cap.value)
                 if arr[i].category == _DC_AS and arr[i].type & _DT_AS_FLIGHTBASE]
        dev = select_base(bases, product_id)
        if dev is None:
            if not bases:
                logging.warning("MOZA SDK: no flight base found")
            else:
                found = ", ".join(f"{b.name.decode('utf-8', 'replace')} ({b.vendorId:04X}:{b.productId:04X})"
                                  for b in bases)
                want = f"346E:{product_id:04X}" if isinstance(product_id, int) else "unknown"
                logging.warning(f"MOZA SDK: no single flight base matches the selected device ({want}); "
                                f"found {found} - using plain DirectInput")
            return False
        h = c_void_p()
        e = self._L.Moza_DeviceOpen(dev.id, byref(h))
        if e != _ERR_OK or not h.value:
            logging.warning(f"MOZA SDK: Moza_DeviceOpen failed ({e})")
            return False
        self._h = h.value
        self.device_name = dev.name.decode("utf-8", "replace")
        return True

    def close(self, timeout: float = 5.0):
        """Close the device and shut the SDK down - never while a native call is still
        running: after ``timeout`` the close is left to the last call to finish."""
        with self._lock:
            if not self._h:
                return
            h, self._h = self._h, None              # no new call starts from here on
            self._closing_h = h
            if not self._idle.wait_for(lambda: self._inflight == 0, timeout=timeout):
                logging.warning("MOZA SDK: a call is still running; closing when it returns")
                return
            self._finish_close()

    def _finish_close(self):                         # with _lock held, nothing in flight
        h, self._closing_h = self._closing_h, None
        if h:
            try:
                self._L.Moza_DeviceClose(h)
            finally:
                self._release_sdk()

    def _release_sdk(self):
        """Moza_Shutdown only when no other MozaSdk (e.g. after a device switch) still
        uses the process-wide SDK."""
        with _SDK_INIT_LOCK:
            _SDK_USERS[0] = max(0, _SDK_USERS[0] - 1)
            if _SDK_USERS[0] == 0:
                self._L.Moza_Shutdown()

    def _begin_blocking(self):
        """Register a blocking call; returns the device handle, or None when closed."""
        with self._lock:
            if not self._h:
                return None
            self._inflight += 1
            return self._h

    def _end_blocking(self):
        with self._lock:
            self._inflight -= 1
            self._idle.notify_all()
            if self._inflight == 0 and self._closing_h is not None and self._h is None:
                self._finish_close()                 # close() timed out waiting for us

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
        """Send a command; returns (err, reply-scalar-or-None).  An asynchronous send is
        serialised with the other short calls; a synchronous one waits outside the lock."""
        if not sync:
            with self._lock:
                if not self._h:
                    return -1, None
                args = _Value()
                try:
                    self._fill(args, ("list", specs))
                    return self._L.Moza_DeviceCommandSend(self._h, cmd, byref(args)), None
                finally:
                    self._L.Moza_ValueDestroy(byref(args))
        h = self._begin_blocking()
        if h is None:
            return -1, None
        args, res = _Value(), _Value()
        try:
            self._fill(args, ("list", specs))
            e = self._L.Moza_DeviceCommandSendSync(h, cmd, byref(args), timeout_ms, byref(res))
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
            self._end_blocking()

    def set_param_uint(self, param_id: int, value: int, signed: bool = False) -> int:
        """Asynchronously write an integer parameter; returns the SDK error code."""
        with self._lock:
            if not self._h:
                return -1
            v = _Value()
            if signed:
                v.type = _VT_INT
                v.data.intValue = int(value)
            else:
                v.type = _VT_UINT
                v.data.uintValue = int(value)
            return self._L.Moza_DeviceParameterSetValue(self._h, param_id, byref(v))

    def get_param_int(self, param_id: int, timeout_ms: int = 2000, fresh: bool = True) -> Optional[int]:
        """Read an integer parameter (blocking, outside the send lock); None on failure.
        ``fresh``: ask the base (FetchSync) instead of possibly returning the SDK's cached
        value (GetValueSync) - needed to confirm a write."""
        h = self._begin_blocking()
        if h is None:
            return None
        v = _Value()
        try:
            fn = self._L.Moza_DeviceParameterFetchSync if fresh else self._L.Moza_DeviceParameterGetValueSync
            e = fn(h, param_id, timeout_ms, byref(v))
            if e != _ERR_OK:
                return None
            if v.type == _VT_UINT:
                return int(v.data.uintValue)
            if v.type == _VT_INT:
                return int(v.data.intValue)
            return None
        finally:
            self._L.Moza_ValueDestroy(byref(v))
            self._end_blocking()

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
        self._ends_at = None           # monotonic end time of a finite-duration effect
        self._duration_ms = None       # duration of the last parameter set (None = infinite)
        self._last_cmd = None          # (cmd, specs) last parameter set, re-sent by the keepalive
        self._last_env = None
        # one lock per device: a keepalive refresh never interleaves with stop/free/reset
        self._lock = getattr(device, "fx_lock", None) or threading.RLock()

    def __bool__(self):
        return bool(self.effect_id and self.type)

    def __repr__(self):
        return f"MozaEffectHandle({self.effect_id}, {effect_names.get(self.type)})"

    @property
    def name(self):
        return effect_names.get(self.type)

    @property
    def started(self):
        # a finite effect (e.g. the 40 ms gear clunk) has stopped playing on the base by
        # itself; report it so the next HapticEffect.start() sends OP_START again
        if self._started and self._ends_at is not None and time.monotonic() >= self._ends_at:
            self._started = False
            self._ends_at = None
        return self._started

    def invalidate(self):
        with self._lock:
            self.effect_id = 0
            self._started = False
            self._ends_at = None

    def forget_playback(self):
        self._started = False
        self._ends_at = None

    def _send(self, cmd, specs, duration_ms=None):
        with self._lock:
            if not self.effect_id:
                return
            if self.started and self._last_cmd == (cmd, specs):
                return      # unchanged while playing - the keepalive refreshes it anyway
            self._last_cmd = (cmd, specs)
            self._duration_ms = duration_ms
            self.device.sdk.send(cmd, specs)

    def resend(self):
        """Keepalive refresh: re-send the last parameters of a playing effect."""
        with self._lock:
            if self.effect_id and self._last_cmd and self.started:
                self.device.sdk.send(*self._last_cmd)

    def start(self, loopCount=1, override=False):
        with self._lock:
            if not self.effect_id:
                logging.warning(f"start on an invalidated effect ({self.name})")
                return self
            if self._last_cmd:
                self.device.sdk.send(*self._last_cmd)
            e, _ = self.device.sdk.send(CMD_OPERATION, [("u", self.effect_id), ("u", OP_START)])
            # only an accepted command counts (the SDK returns OK); BUSY or an error leaves
            # the effect stopped, so the next HapticEffect.start() sends OP_START again
            if e not in (_ERR_OK, _ERR_PENDING):
                logging.warning(f"MOZA: start of {self.name} (slot {self.effect_id}) failed (err {e})")
                self._started = False
                return self
            self._started = True
            self._ends_at = (time.monotonic() + self._duration_ms / 1000.0
                             if self._duration_ms else None)
            return self

    def stop(self):
        with self._lock:
            if self.effect_id and self._started:
                self.device.sdk.send(CMD_OPERATION, [("u", self.effect_id), ("u", OP_STOP)])
            self._started = False
            self._ends_at = None
            return self

    def destroy(self):
        with self._lock:
            if self.effect_id:
                self.device.sdk.send(CMD_OPERATION, [("u", self.effect_id), ("u", OP_STOP)])
                e, _ = self.device.sdk.send(CMD_OPERATION, [("u", self.effect_id), ("u", OP_FREE)])
                # a slot the base did not free stays out of the pool until the next reset
                self.device.release_slot(self.effect_id, freed=e in (_ERR_OK, _ERR_PENDING))
                self.effect_id = None
                self.type = 0
                self._started = False
                self._ends_at = None

    def __del__(self):
        try:
            self.destroy()
        except Exception:
            pass

    # -- parameters ---------------------------------------------------------------
    @staticmethod
    def _dir(direction_deg) -> int:
        """TelemFFB / USB-PID / DirectInput direction (degrees, the direction the force
        comes FROM) to MOZA's direction (1/100 degree, the direction the force pushes
        TOWARD): rotate by 180 degrees.  Without it a constant force pushes the stick the
        opposite way (e.g. elevator droop, direction 180, pulled the stick back)."""
        return (int(round(direction_deg)) + 180) % 360 * 100

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
                                  ("u", self._dir(direction)), ("u", 0)],
                   duration_ms=None if dur == DURATION_INFINITE else dur)
        return self

    def setRamp(self, start, end, direction, duration=0):
        """Not part of the BaseEffectHandle contract; kept for completeness."""
        s = int(round(_clamp(start, -1.0, 1.0) * FULL_SCALE)) & 0xFFFF
        e = int(round(_clamp(end, -1.0, 1.0) * FULL_SCALE)) & 0xFFFF
        dur = DURATION_INFINITE if not duration else int(_clamp(duration, 1, DURATION_INFINITE - 1))
        self._send(CMD_RAMP, [("u", self.effect_id), ("u", s), ("u", e), ("u", dur),
                              ("u", AXIS_DIRECTION_ENABLE), ("u", self._dir(direction)), ("u", 0)],
                   duration_ms=None if dur == DURATION_INFINITE else dur)
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
        self.fx_lock = threading.RLock()     # serialises effect sends, stop/free and reset
        self._resetting = 0                  # > 0 while the base's effects are being reset
        self._migration_pending = False      # a channel move whose effect reset failed
        self._keepalive_stop = threading.Event()
        self.connection_generation = 0       # +1 on every DirectInput re-open
        self._mode_req_lock = threading.Lock()
        self._mode_req = None                # latest pending (mode, on_done) request
        self._refresh_req = False            # re-read the base after a reconnect
        self._refresh_retries = 0
        self._mode_worker = None
        super().__init__(guid, **kwargs)
        # Which channel plays the constant/ramp/periodic effects follows the base mode:
        # Force Feedback -> DirectInput (exactly like official TelemFFB),
        # Force Sensing  -> MOZA SDK (DirectInput effects are ignored by the base there).
        self._base_mode = "FS" if self.sdk.get_param_int(PARAM_STEER_FORCE_MODE) == 1 else "FFB"
        if self.ensure_telemetry_ffb_source():
            logging.info("MOZA: FFB source set to Telemetry + DirectInput (Main_CtrlModeSet=2)")
        self.sdk.send(CMD_CONTROL, [("i", 1)], sync=True)   # start effect calculation
        self.sdk.send(CMD_CONTROL, [("i", 3)], sync=True)   # as Cockpit does before playing
        self._keepalive = threading.Thread(target=self._keepalive_loop, name="MozaFFBKeepalive", daemon=True)
        self._keepalive.start()
        logging.info(f"MOZA: SDK effect channel open on '{sdk.device_name}'; base in "
                     f"{'Force Sensing' if self._base_mode == 'FS' else 'Force Feedback'} mode -> "
                     f"effects via {self._channel_name()}")

    def _use_sdk_effects(self) -> bool:
        return self._base_mode == "FS" and self.sdk.is_open

    def _channel_name(self) -> str:
        return "MOZA SDK (conditions via DirectInput)" if self._use_sdk_effects() else "DirectInput (as official TelemFFB)"

    # -- slots ----------------------------------------------------------------------
    def _take_slot(self) -> Optional[int]:
        with self._slot_lock:
            if not self._free_slots:
                return None
            s = min(self._free_slots)
            self._free_slots.discard(s)
            return s

    def release_slot(self, slot: int, freed: bool = True):
        with self._slot_lock:
            if SLOT_MIN <= slot <= SLOT_MAX:
                if freed:
                    self._free_slots.add(slot)
                else:
                    logging.warning(f"MOZA: freeing effect slot {slot} failed; slot kept until the next reset")

    # -- effects --------------------------------------------------------------------
    def create_effect(self, type):
        # channel choice, slot allocation, NEW_EFFECT and registration happen under the
        # same lock as reset_effects(): a reset can never hand a slot to two handles
        with self.fx_lock:
            if type not in SDK_EFFECTS or not self._use_sdk_effects():
                return super().create_effect(type)          # conditions, and everything in FFB mode
            if self._resetting:
                # the base is being reset: defer (HapticEffect keeps its pending creation
                # and retries on the next frame) instead of waiting for the reset
                return None
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

    def reset_effects(self) -> bool:
        """Drop every effect on both channels.  Returns True when the base confirmed the
        SDK reset and the restart of its effect calculation; raises what the DirectInput
        reset raised (after the SDK side was reset anyway)."""
        # Only the bookkeeping runs under fx_lock; the three synchronous control commands
        # run outside it, so telemetry frames never wait for the base.  While they run,
        # new SDK effects are deferred (create_effect returns None) and the invalidated
        # handles send nothing.
        with self.fx_lock:
            self._resetting += 1
        dinput_error = None
        dinput_ok = True
        try:
            with self.fx_lock:
                for h in self._live_moza_handles():
                    h.invalidate()
                try:
                    # destroy the DirectInput effects first, checking each result (the base
                    # class ignores it); then the base class does the rest of its reset
                    failed = self._destroy_dinput_effects()
                    super().reset_effects()
                    if failed:
                        dinput_ok = False
                        try:
                            raise OSError(f"DirectInput effect_destroy failed: {failed}")
                        except OSError:
                            # a native effect may still exist: re-open the device, which
                            # frees them all (DirectInput's own recovery)
                            self._note_native_fault("reset_effects")
                except Exception as ex:     # DirectInput failed: still reset the SDK side,
                    dinput_error = ex       # then report it to the caller as before
                    # some native effects may not have been freed: use DirectInput's own
                    # recovery - invalidate every handle (their owners re-create them on
                    # the current channel) and re-open the device, which frees them all
                    try:
                        self._note_native_fault("reset_effects")
                    except Exception:
                        logging.exception("MOZA: DirectInput recovery request failed")
            e, _ = self.sdk.send(CMD_CONTROL, [("i", 2)], sync=True)
            ok = e == _ERR_OK
            if ok:
                with self._slot_lock:       # the base freed every effect: all slots are free
                    self._free_slots = set(range(SLOT_MIN, SLOT_MAX + 1))
            else:
                # not confirmed: the invalidated handles' slots may still be in use on the
                # base, so they stay out of the pool until a reset succeeds
                logging.warning(f"MOZA: effect reset not confirmed (err {e}); "
                                f"{len(self._free_slots)} effect slots stay available")
            e1, _ = self.sdk.send(CMD_CONTROL, [("i", 1)], sync=True)   # restart calculation
            self.sdk.send(CMD_CONTROL, [("i", 3)], sync=True)
            if e1 != _ERR_OK:
                logging.warning(f"MOZA: restarting the effect calculation not confirmed (err {e1})")
                ok = False
        finally:
            with self.fx_lock:
                self._resetting -= 1
        if dinput_error is not None:
            raise dinput_error
        return ok and dinput_ok

    def _destroy_dinput_effects(self) -> list:
        """Destroy and invalidate every live DirectInput effect; returns the
        (effect id, return code) pairs the bridge did not confirm."""
        from telemffb.hw.ffb_dinput import DIB_OK
        failed = []
        for ref in list(getattr(self, "_effect_handles", ())):
            effect = ref()
            if effect and effect.effect_id:
                rc = self.bridge.effect_destroy(effect.effect_id)
                if rc != DIB_OK:
                    failed.append((effect.effect_id, rc))
                effect.invalidate()
        return failed

    # -- base mode -------------------------------------------------------------------
    def set_base_mode(self, mode: str) -> bool:
        """Switch the base to "FFB" (Force Feedback) or "FS" (Force Sensing).
        Returns True only when the base confirms the new mode (read back) and the FFB
        source was set.  The effect channel always follows the mode the base actually
        reports, never the one merely requested."""
        mode = str(mode).upper()
        value = BASE_MODES.get(mode)
        if value is None or not self.sdk.is_open:
            return False
        e = self.sdk.set_param_uint(PARAM_STEER_FORCE_MODE, value)
        if e not in (_ERR_OK, _ERR_PENDING, _ERR_BUSY):
            logging.warning(f"MOZA: setting base mode {mode} failed (err {e})")
            return False
        actual = self._read_base_mode(expect=value)
        source_ok = self.ensure_telemetry_ffb_source()
        if actual is None:
            logging.warning(f"MOZA: base did not report its mode after switching to {mode}; "
                            f"effects stay via {self._channel_name()}")
            return False
        if not self._follow_base_mode(actual):
            return False                # effects not moved yet: not done, retried later
        if actual != mode:
            logging.warning(f"MOZA: base mode {mode} requested, base reports "
                            f"{'Force Sensing' if actual == 'FS' else 'Force Feedback'}")
            return False
        return source_ok

    def request_base_mode(self, mode: str, on_done=None):
        """Switch the base mode on a worker thread (the switch waits for the base's
        confirmation and must not block the telemetry loop or the UI).  Requests are
        serialised; while one runs only the newest pending request is kept.
        ``on_done(mode, ok)`` is called on the worker thread for every request - a
        request superseded by a newer one before it ran gets ``ok=False``."""
        superseded = None
        with self._mode_req_lock:
            superseded, self._mode_req = self._mode_req, (str(mode).upper(), on_done)
            self._start_mode_worker()
        if superseded is not None:
            self._notify_mode(superseded, False)

    def request_refresh(self):
        """Re-read the base mode and re-assert the FFB source on the worker thread."""
        with self._mode_req_lock:
            self._refresh_req = True
            self._start_mode_worker()

    def _start_mode_worker(self):          # with _mode_req_lock held
        if self._mode_worker is not None and self._mode_worker.is_alive():
            return
        self._mode_worker = threading.Thread(target=self._mode_worker_loop,
                                             name="MozaBaseMode", daemon=True)
        self._mode_worker.start()

    @staticmethod
    def _notify_mode(req, ok):
        mode, on_done = req
        if on_done is not None:
            try:
                on_done(mode, ok)
            except Exception:
                logging.exception("MOZA: base mode callback failed")

    def _mode_worker_loop(self):
        while True:
            with self._mode_req_lock:
                refresh, self._refresh_req = self._refresh_req, False
                req, self._mode_req = self._mode_req, None
                stopping = self._keepalive_stop.is_set()
                if stopping or (req is None and not refresh):
                    self._mode_worker = None
            if stopping:
                if req is not None:
                    self._notify_mode(req, False)
                return
            if req is None and not refresh:
                return
            if refresh:
                # done only when the mode was read, the effects follow it and the FFB
                # source is confirmed; anything else is retried shortly
                moved = False
                try:
                    mode = self._read_base_mode()
                    followed = mode is not None and self._follow_base_mode(mode)
                    source_ok = self.ensure_telemetry_ffb_source()
                    moved = bool(followed and source_ok)        # set only once both checks ran
                except Exception:
                    logging.exception("MOZA: refreshing the base state after reconnect failed")
                self._refresh_retries = 0 if moved else self._refresh_retries + 1
                if (not moved and req is None and self._refresh_retries <= 10
                        and not self._keepalive_stop.wait(1.0)):
                    with self._mode_req_lock:       # effects not moved: try again shortly
                        self._refresh_req = True
            if req is not None:
                try:
                    ok = self.set_base_mode(req[0])
                except Exception:
                    logging.exception("MOZA: base mode switch failed")
                    ok = False
                self._notify_mode(req, ok)

    def _try_reconnect(self):
        super()._try_reconnect()
        if not self._reconnecting and not self._shutdown:
            # re-opened (Qt timer, main thread): the base may have been power-cycled or
            # changed by Cockpit - re-read it on the worker thread, not here
            self.connection_generation += 1
            self._refresh_retries = 0           # a new connection gets the full retry budget
            self.request_refresh()

    def _read_base_mode(self, expect=None, attempts=4, timeout_ms=500) -> Optional[str]:
        """Read Main_SteerForceMode from the base ("FFB" / "FS"), None if unreadable.
        With ``expect``, re-read briefly until the base reports that value (the write
        is asynchronous)."""
        actual = None
        for i in range(attempts):
            v = self.sdk.get_param_int(PARAM_STEER_FORCE_MODE, timeout_ms=timeout_ms)
            if v is not None:
                actual = "FS" if v == 1 else "FFB"
                if expect is None or v == expect:
                    return actual
            if i + 1 < attempts:
                time.sleep(0.1)
        return actual

    def _follow_base_mode(self, mode: str) -> bool:
        """Put the effect channel on ``mode``.  Returns False while the move of the
        existing effects is not complete (it is retried on the next call)."""
        if mode == self._base_mode and not self._migration_pending:
            return True
        self._base_mode = mode
        self._migration_pending = True
        # move the effects to the other channel: drop every device effect; their
        # HapticEffect owners re-create them (now on the new channel) on next start
        try:
            reset_ok = self.reset_effects()
        except Exception:
            logging.exception(f"MOZA: moving the effects to {self._channel_name()} failed; will retry")
            return False
        if reset_ok is False:
            logging.warning(f"MOZA: the base did not confirm the effect reset for the move to "
                            f"{self._channel_name()}; will retry")
            return False
        self._migration_pending = False
        logging.info(f"MOZA: effects now via {self._channel_name()}")
        return True

    def ensure_telemetry_ffb_source(self) -> bool:
        """Make the base render telemetry (MOZA SDK) effects as well as DirectInput ones."""
        if not self.sdk.is_open:
            return False
        e = self.sdk.set_param_uint(PARAM_CTRL_MODE, CTRL_MODE_TELEMETRY_AND_DINPUT, signed=True)
        if e not in (_ERR_OK, _ERR_PENDING, _ERR_BUSY):
            logging.warning(f"MOZA: setting FFB source (Main_CtrlModeSet=2) failed (err {e})")
            return False
        for i in range(4):          # confirm (the write is asynchronous)
            v = self.sdk.get_param_int(PARAM_CTRL_MODE, timeout_ms=500)
            if v == CTRL_MODE_TELEMETRY_AND_DINPUT:
                return True
            if i < 3:
                time.sleep(0.1)
        logging.warning(f"MOZA: FFB source not confirmed (Main_CtrlModeSet reads {v})")
        return False

    # -- keepalive -------------------------------------------------------------------
    def _keepalive_loop(self):
        bad = 0
        while not self._keepalive_stop.wait(KEEPALIVE_INTERVAL_S):
            if not self._use_sdk_effects():
                continue    # Force Feedback: DirectInput path, like official TelemFFB - no polling
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
        ka = getattr(self, "_keepalive", None)
        if ka is not None and ka.is_alive() and ka is not threading.current_thread():
            ka.join(timeout=2.0)     # no keepalive send may reach a closed SDK
        mw = self._mode_worker
        if mw is not None and mw.is_alive() and mw is not threading.current_thread():
            mw.join(timeout=8.0)     # a running mode switch finishes before the SDK closes
        try:
            for h in self._live_moza_handles():
                h.stop()
            self.sdk.send(CMD_CONTROL, [("i", 2)], sync=True)
        except Exception:
            logging.exception("MOZA shutdown")
        super().shutdown()
        self.sdk.close()


def same_model_count(info, devices) -> int:
    """How many DirectInput devices share ``info``'s USB vendor/product id."""
    key = (getattr(info, "vendor_id", None), getattr(info, "product_id", None))
    return sum(1 for d in devices or ()
               if (getattr(d, "vendor_id", None), getattr(d, "product_id", None)) == key)


def sdk_backend_blocker(info, devices=None) -> Optional[str]:
    """Why the MOZA SDK backend would not be used for DirectInput device ``info``
    (None = it would be).  The one check behind both the backend and the
    MOZA_SDK.dll download offer."""
    if os.environ.get("TELEMFFB_MOZA_DISABLE"):
        return "MOZA backend disabled (TELEMFFB_MOZA_DISABLE)"
    if info is None:
        return None
    if not is_ab9(info):
        # other MOZA bases have no Force Sensing mode: like the official TelemFFB
        return f"'{getattr(info, 'product_string', '?')}' is not an AB9"
    n = same_model_count(info, devices)
    if n > 1:
        # two bases of one model: USB ids cannot tell which physical unit the SDK would
        # open, so never risk driving the other one
        return (f"{n} devices with USB id {info.vendor_id:04X}:{info.product_id:04X} - cannot match "
                "the SDK base to the selected one")
    return None


def open_moza_or_none(guid: str, info=None, devices=None) -> Optional[MozaFFBDevice]:
    """Return a MozaFFBDevice for a MOZA DirectInput device when the MOZA SDK is usable,
    else None (the caller falls back to plain DirectInput)."""
    if info is not None and not is_moza_device(info):
        return None
    reason = sdk_backend_blocker(info, devices)
    if reason:
        logging.info(f"MOZA: {reason} - using plain DirectInput")
        return None
    dll = sdk_dll_path()
    if not dll:
        logging.warning("MOZA device found but MOZA_SDK.dll is missing - using plain DirectInput "
                        "(effects will not play in Force Sensing mode)")
        return None
    try:
        sdk = MozaSdk(dll)
        if not sdk.open(product_id=getattr(info, "product_id", None)):
            return None
    except Exception:
        logging.exception("MOZA SDK load failed - using plain DirectInput")
        return None
    try:
        return MozaFFBDevice(guid, sdk)
    except Exception:
        sdk.close()
        raise
