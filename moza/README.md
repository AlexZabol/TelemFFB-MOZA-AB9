# TelemFFB — MOZA flight base support (fork)

This fork of [walmis/VPforce-TelemFFB](https://github.com/walmis/VPforce-TelemFFB) (GPL-3.0) adds a
MOZA SDK backend so TelemFFB's telemetry effects play on the MOZA AB9 flight base — including the
base's **Force Sensing** mode, where DirectInput effects are ignored. Other MOZA bases have no Force
Sensing mode and run on plain DirectInput, exactly like the official TelemFFB.

## Key finding
The AB9 renders `MainCtrl_Ffb*` effects only while the host keeps reading the motor output torque
(`MainGet_Steer_TorqueOutX`, MOZA SDK parameter id 95) — MOZA Cockpit polls it ~10x/s. Without the poll
the base accepts every command and produces no force, in either mode.

## Changes vs upstream
- `telemffb/hw/ffb_moza.py` — `MozaFFBDevice(DInputFFBDevice)`: in **Force Feedback** mode every effect goes through DirectInput exactly as in official TelemFFB; in **Force Sensing** mode constant / ramp / periodic effects go via
  the MOZA SDK (slots 1–80, magnitude ×32767, direction in 1/100°), conditions/axes/buttons stay on
  DirectInput; a keepalive thread polls the torque and refreshes playing effects every 100 ms.
- `telemffb/hw/ffb_rhino.py` — `HapticEffect.open_dinput` picks the MOZA backend for VID 0x346E
  (falls back to plain DirectInput if the SDK is unavailable; `TELEMFFB_MOZA_DISABLE=1` turns it off).
- `main.py` — preloads the system `msvcp140*.dll` before PyQt6 (PyQt6's bundled 14.26 runtime crashes
  `MOZA_SDK.dll`).
- Basic Settings → **MOZA Base Mode** (Don't change / Force Feedback / Force Sensing), stored per sim/class/aircraft like any setting and applied when the aircraft loads (`telemffb/sim/base/MozaBaseModeMixIn.py`, `MozaFFBDevice.set_base_mode`).
- `.github/workflows/moza-build.yaml` — daily: merge upstream, test, build, release.
- `moza/update_telemffb_moza.ps1` — local installer/updater.

## Requirements
- MOZA DeviceService (installed with MOZA Cockpit); the Cockpit **window** must be closed while TelemFFB runs.
- DirectLink (TelemFFB's DirectInput bridge), as for any non-VPforce device.
- `MOZA_SDK.dll` (x64) from the [MOZA SDK](https://mozaracing.com/pages/sdk). It is **not** included
  here or in the releases (the SDK has no redistribution licence). When it is missing, TelemFFB offers at
  start to download the official Flight SDK from mozaracing.com (HTTPS, MOZA hosts only) and installs the
  x64 `MOZA_SDK.dll` next to the exe; the updater script can also copy it from your own SDK download.

## Install (ready-made build)
1. Install **MOZA Cockpit** (provides MOZA DeviceService) and **DirectLink** (TelemFFB's DirectInput bridge,
   linked from TelemFFB's System Settings > Integrations).
2. Download the **MOZA SDK** zip from https://mozaracing.com/pages/sdk.
3. Either run `moza/update_telemffb_moza.ps1` (downloads the latest release to `C:\TelemFFB-MOZA` and adds
   `MOZA_SDK.dll` from the SDK zip in your Downloads folder), or by hand: download `TelemFFB-MOZA-x.y.z.zip`
   from [Releases](../../releases), unzip it anywhere, and copy `MOZA_SDK\x64-release\bin\MOZA_SDK.dll` from the
   SDK zip next to `VPforce-TelemFFB.exe` (or skip the SDK step and accept TelemFFB's download offer at start).
4. Close the MOZA Cockpit window and start `VPforce-TelemFFB.exe`.

## Build from source
Requires Windows x64 and Python 3.12 (64-bit).
```powershell
git clone -b moza https://github.com/AlexZabol/VPforce-TelemFFB.git   # or download the "moza" branch as a zip
cd VPforce-TelemFFB
py -3.12 -m venv venv
.\venv\Scripts\pip install -r requirements.txt pyinstaller
.\venv\Scripts\pyinstaller --noconfirm VPforce-TelemFFB.spec
```
The program is in `dist\VPforce-TelemFFB\`; copy `MOZA_SDK.dll` (from the MOZA SDK) next to the exe.
To run without building: `.\venv\Scripts\python main.py` with `MOZA_SDK.dll` placed in `dll\`.

## Updating
`powershell -ExecutionPolicy Bypass -File moza/update_telemffb_moza.ps1`
