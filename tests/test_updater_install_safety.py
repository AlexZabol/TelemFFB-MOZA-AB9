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


FULL = [EXECUTABLE_NAME, "assets/base_library.zip", "assets/defaults.xml", "assets/python312.dll",
        "assets/hidapi.dll", "assets/a.dll"]


def test_package_manifest_is_enforced(tmp_path):
    manifest = "\n".join(FULL + ["assets/b.pyd"])
    p = tmp_path / "u.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for n in FULL:
            zf.writestr(n, "x")
        zf.writestr(updater.PACKAGE_MANIFEST, manifest)
    with pytest.raises(RuntimeError, match="assets/b.pyd"):
        _worker(tmp_path)._validate_zip(str(p))
    with zipfile.ZipFile(p, "a") as zf:
        zf.writestr("assets/b.pyd", "x")
    _worker(tmp_path)._validate_zip(str(p))


def test_valid_package_passes(tmp_path):
    _worker(tmp_path)._validate_zip(_zip(tmp_path / "u.zip", FULL))


@pytest.mark.parametrize("drop", [EXECUTABLE_NAME, "assets/base_library.zip", "assets/defaults.xml",
                                  "assets/python312.dll", "assets/hidapi.dll"])
def test_incomplete_package_is_rejected(tmp_path, drop):
    with pytest.raises(RuntimeError, match="incomplete"):
        _worker(tmp_path)._validate_zip(_zip(tmp_path / "u.zip", [n for n in FULL if n != drop]))


def test_exe_only_package_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="incomplete"):
        _worker(tmp_path)._validate_zip(_zip(tmp_path / "u.zip", [EXECUTABLE_NAME]))


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
