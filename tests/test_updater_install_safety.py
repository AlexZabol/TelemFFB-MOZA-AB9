"""The updater checks the package before touching the installation, restores the
previous version when installing fails, and verifies TLS on the download."""
import inspect
import types
import zipfile

import pytest

import updater
from updater import EXECUTABLE_NAME, UpdateWorker

pytestmark = [pytest.mark.unit]


def _worker(app_path):
    w = UpdateWorker.__new__(UpdateWorker)
    w.app_path = str(app_path)
    w.status = types.SimpleNamespace(emit=lambda *a: None)
    w.log = types.SimpleNamespace(emit=lambda *a: None)
    w.progress = types.SimpleNamespace(emit=lambda *a: None)
    return w


def _zip(path, names):
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, "x")
    return str(path)


def test_valid_package_passes(tmp_path):
    _worker(tmp_path)._validate_zip(_zip(tmp_path / "u.zip", [EXECUTABLE_NAME, "assets/a.dll"]))


def test_package_without_executable_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="does not contain"):
        _worker(tmp_path)._validate_zip(_zip(tmp_path / "u.zip", ["assets/a.dll"]))


def test_non_zip_is_rejected(tmp_path):
    bad = tmp_path / "u.zip"
    bad.write_bytes(b"<html>not a zip</html>")
    with pytest.raises(RuntimeError, match="not a valid zip"):
        _worker(tmp_path)._validate_zip(str(bad))


def test_failed_install_restores_previous_version(tmp_path):
    app = tmp_path / "app"
    (app / "assets").mkdir(parents=True)
    (app / EXECUTABLE_NAME).write_text("old")
    (app / "assets" / "lib.dll").write_text("old")
    w = _worker(app)
    w._backup()
    assert not (app / EXECUTABLE_NAME).exists()
    (app / "assets").mkdir()
    (app / "assets" / "half.dll").write_text("new")       # a half-finished install
    w._restore_backup()
    assert (app / EXECUTABLE_NAME).read_text() == "old"
    assert (app / "assets" / "lib.dll").read_text() == "old"
    assert not (app / "assets" / "half.dll").exists()


def test_download_verifies_tls():
    src = inspect.getsource(UpdateWorker._download)
    assert "_create_unverified_context" not in src
    assert "create_default_context" in src
