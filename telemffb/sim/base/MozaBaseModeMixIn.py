"""Switch a MOZA flight base between Force Feedback and Force Sensing per aircraft."""
import logging
import time
from typing import override

from telemffb.hw.ffb_moza import base_mode_supported
from telemffb.hw.ffb_rhino import HapticEffect
from telemffb.sim.base.AircraftEffectUtilsBase import AircraftEffectUtilsBase
from telemffb.sim.BaseTelemetryData import BaseTelemetryData

RETRY_INTERVAL_S = 5.0


class MozaBaseModeMixIn(AircraftEffectUtilsBase):
    """Apply the ``moza_base_mode`` setting (KEEP / FFB / FS) to a MOZA base.

    The mode is written once per change (aircraft load or the user changing the
    setting), from the telemetry loop, and only when the connected device is a
    MOZA AB9 driven by the MOZA SDK backend (it exposes ``set_base_mode``).
    Other devices ignore the setting.  A mode the base did not confirm, or one
    requested while the base was disconnected, is retried every few seconds; a
    reconnected base gets the mode again.
    """
    moza_base_mode: str = "KEEP"

    def __init__(self):
        super().__init__()
        self._moza_mode_applied: str = "KEEP"
        self._moza_device = None          # the device the mode was applied to
        self._moza_retry_at: float = 0.0

    def _apply_moza_base_mode(self):
        mode = getattr(self.moza_base_mode, "name", self.moza_base_mode) or "KEEP"
        dev = HapticEffect.device
        if dev is not self._moza_device:
            # another (or a re-opened) device: what was applied before no longer counts
            self._moza_device = dev
            self._moza_mode_applied = "KEEP"
            self._moza_retry_at = 0.0
        if mode == "KEEP":
            self._moza_mode_applied = "KEEP"
            return
        if mode == self._moza_mode_applied:
            return
        if not base_mode_supported(dev) or not getattr(dev, "connected", False):
            return                                # not (yet) a connected MOZA AB9
        now = time.monotonic()
        if now < self._moza_retry_at:
            return
        try:
            ok = dev.set_base_mode(mode)
        except Exception:
            logging.exception("MOZA: base mode switch failed")
            ok = False
        if ok:
            logging.info(f"MOZA: base mode set to {'Force Sensing' if mode == 'FS' else 'Force Feedback'}")
            self._moza_mode_applied = mode
        else:
            self._moza_retry_at = now + RETRY_INTERVAL_S

    @override
    def on_telemetry(self, telem_data: BaseTelemetryData):
        super().on_telemetry(telem_data)
        self._apply_moza_base_mode()
