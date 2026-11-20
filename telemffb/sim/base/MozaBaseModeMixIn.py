"""Switch a MOZA flight base between Force Feedback and Force Sensing per aircraft."""
import logging
from typing import override

from telemffb.hw.ffb_rhino import HapticEffect
from telemffb.sim.base.AircraftEffectUtilsBase import AircraftEffectUtilsBase
from telemffb.sim.BaseTelemetryData import BaseTelemetryData


class MozaBaseModeMixIn(AircraftEffectUtilsBase):
    """Apply the ``moza_base_mode`` setting (KEEP / FFB / FS) to a MOZA base.

    The mode is written once per change (aircraft load or the user changing the
    setting), from the telemetry loop, and only when the connected device is a
    MOZA base driven by the MOZA SDK backend (it exposes ``set_base_mode``).
    Other devices ignore the setting.
    """
    moza_base_mode: str = "KEEP"

    def __init__(self):
        super().__init__()
        self._moza_mode_applied: str = "KEEP"

    def _apply_moza_base_mode(self):
        mode = getattr(self.moza_base_mode, "name", self.moza_base_mode) or "KEEP"
        if mode == self._moza_mode_applied:
            return
        dev = HapticEffect.device
        setter = getattr(dev, "set_base_mode", None) if dev is not None else None
        if setter is None or not getattr(dev, "connected", False):
            self._moza_mode_applied = mode        # not a MOZA base: nothing to do
            return
        if mode == "KEEP":
            self._moza_mode_applied = mode
            return
        try:
            if setter(mode):
                logging.info(f"MOZA: base mode set to {'Force Sensing' if mode == 'FS' else 'Force Feedback'}")
                self._moza_mode_applied = mode
        except Exception:
            logging.exception("MOZA: base mode switch failed")
            self._moza_mode_applied = mode       # do not retry every frame

    @override
    def on_telemetry(self, telem_data: BaseTelemetryData):
        super().on_telemetry(telem_data)
        self._apply_moza_base_mode()
