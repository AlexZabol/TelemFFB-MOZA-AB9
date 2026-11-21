"""Start-up offer to download MOZA_SDK.dll from mozaracing.com when it is missing."""
import logging
import threading

from PyQt6.QtCore import QEventLoop, QTimer, Qt
from PyQt6.QtWidgets import QCheckBox, QMessageBox, QProgressDialog

import telemffb.globals as G
from telemffb.hw import ffb_moza
from telemffb.utils import moza_sdk_download as dl

NO_PROMPT_KEY = "mozaSdkNoPrompt"


def offer_moza_sdk_download(parent=None) -> bool:
    """When MOZA_SDK.dll is missing, ask the user whether to download it from MOZA's
    website and, on yes, install it.  Returns True when the DLL is present afterwards.
    Runs on the main thread before the device is opened."""
    if ffb_moza.sdk_dll_path():
        return True
    settings = getattr(G, "system_settings", None)
    if settings is not None and settings.get(NO_PROMPT_KEY, False):
        logging.info("MOZA_SDK.dll missing; download offer switched off by the user")
        return False

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("MOZA SDK required")
    box.setText("<b>MOZA_SDK.dll was not found.</b>")
    box.setInformativeText(
        "TelemFFB MOZA needs MOZA_SDK.dll from the official MOZA Flight SDK to play "
        "effects in the base's Force Sensing mode. It cannot be shipped with TelemFFB.<br><br>"
        "Download the MOZA Flight SDK from mozaracing.com (about 100 MB) and install "
        f"MOZA_SDK.dll into<br><code>{dl.install_dir()}</code>?<br><br>"
        "The MOZA SDK is provided by MOZA for personal, non-commercial use.")
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.Yes)
    dont_ask = QCheckBox("Don't ask again")
    box.setCheckBox(dont_ask)
    answer = box.exec()
    if answer != QMessageBox.StandardButton.Yes:
        if dont_ask.isChecked() and settings is not None:
            settings.setValue(NO_PROMPT_KEY, True)
        logging.info("MOZA_SDK.dll missing; the user declined the download")
        return False
    return _run_download(parent)


def _run_download(parent) -> bool:
    state = {"got": 0, "total": 0, "status": "Starting...", "done": False, "error": None,
             "cancel": False, "path": None}

    def work():
        try:
            state["path"] = dl.install_sdk(
                progress=lambda got, total: state.update(got=got, total=total),
                cancelled=lambda: state["cancel"],
                status=lambda s: state.update(status=s))
        except Exception as e:           # reported to the user below
            state["error"] = e
        finally:
            state["done"] = True

    dlg = QProgressDialog("Starting...", "Cancel", 0, 100, parent)
    dlg.setWindowTitle("MOZA SDK download")
    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.canceled.connect(lambda: state.update(cancel=True))
    loop = QEventLoop()
    timer = QTimer()

    def tick():
        if state["total"]:
            dlg.setMaximum(100)
            dlg.setValue(int(state["got"] * 100 / state["total"]))
        else:
            dlg.setMaximum(0)
        mb = state["got"] / 1e6
        dlg.setLabelText(f"{state['status']}\n{mb:.1f} MB" if state["got"] else state["status"])
        if state["done"]:
            timer.stop()
            loop.quit()

    timer.timeout.connect(tick)
    threading.Thread(target=work, name="MozaSdkDownload", daemon=True).start()
    dlg.show()
    timer.start(100)
    loop.exec()
    dlg.close()

    err = state["error"]
    if err is None and state["path"]:
        QMessageBox.information(parent, "MOZA SDK installed",
                                f"MOZA_SDK.dll was installed to:\n{state['path']}")
        return True
    if state["cancel"]:
        logging.info("MOZA SDK download cancelled by the user")
        return False
    logging.error(f"MOZA SDK download failed: {err}")
    QMessageBox.warning(parent, "MOZA SDK download failed",
                        f"MOZA_SDK.dll could not be installed:\n\n{err}\n\n"
                        "TelemFFB continues with plain DirectInput (no effects in Force Sensing "
                        "mode). See INSTALL.txt to add MOZA_SDK.dll by hand.")
    return False
