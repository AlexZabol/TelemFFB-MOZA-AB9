"""MOZA_SDK.dll download: link discovery, host checks, archive extraction (no network)."""
import io
import struct
import zipfile

import pytest

from telemffb.utils import moza_sdk_download as dl

pytestmark = [pytest.mark.unit]


def _fake_dll(machine=0x8664, dll=True):
    pe = 0x80
    data = bytearray(0x200)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe)
    data[pe:pe + 4] = b"PE\0\0"
    struct.pack_into("<H", data, pe + 4, machine)
    struct.pack_into("<H", data, pe + 22, 0x2022 if dll else 0x0022)
    return bytes(data)


def _sdk_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def test_pe_check():
    assert dl.is_x64_dll(_fake_dll())
    assert not dl.is_x64_dll(_fake_dll(machine=0x14C))     # 32-bit
    assert not dl.is_x64_dll(_fake_dll(dll=False))         # an exe
    assert not dl.is_x64_dll(b"<html>")


def test_extracts_only_the_x64_release_dll(tmp_path):
    good = _fake_dll()
    z = _sdk_zip(tmp_path / "sdk.zip", {
        "MOZA_SDK_1.0.0.6/MOZA_SDK/x64-release/bin/MOZA_SDK.dll": good,
        "MOZA_SDK_1.0.0.6/MOZA_SDK/x64-debug/bin/MOZA_SDK.dll": b"debug",
        "MOZA_SDK_1.0.0.6/MOZA_SDK/x86-release/bin/MOZA_SDK.dll": _fake_dll(machine=0x14C),
        "MOZA_SDK_1.0.0.6/MOZA_SDK/x64-release/tools/SdkDebugTool/MOZA_SDK.dll": b"tool",
    })
    assert dl.read_dll(z) == good


def test_rejects_archive_without_valid_dll(tmp_path):
    z = _sdk_zip(tmp_path / "a.zip", {"x/x64-release/bin/MOZA_SDK.dll": b"not a dll"})
    with pytest.raises(dl.SdkDownloadError, match="64-bit"):
        dl.read_dll(z)
    z = _sdk_zip(tmp_path / "b.zip", {"readme.txt": b"x"})
    with pytest.raises(dl.SdkDownloadError):
        dl.read_dll(z)


@pytest.mark.parametrize("url", ["http://image.mozaracing.com/a.zip",
                                 "https://evil.example.com/MOZA_SDK_9.9.9.9_en.zip",
                                 "https://image.mozaracing.com.evil.io/a.zip"])
def test_only_official_https_hosts(url):
    with pytest.raises(dl.SdkDownloadError):
        dl._check_host(url)


class _Resp(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self._url = url
        self.headers = {"Content-Length": str(len(data))}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_newest_flight_sdk_link_is_picked(monkeypatch):
    page = (b'<a href="https://image.mozaracing.com/as23repository/MOZA_SDK_1.0.0.6_en.zip">Download</a>'
            b'<a href="https://image.mozaracing.com/as23repository/MOZA_SDK_1.0.0.13_en.zip">Download</a>'
            b'<a href="https://cdn.gudsen.vip/simulation_game/rs21repository/installer/MOZA_SDK.zip">x</a>')
    monkeypatch.setattr(dl, "_open", lambda url, timeout: _Resp(page, url))
    assert dl.find_sdk_url().endswith("MOZA_SDK_1.0.0.13_en.zip")


def test_fallback_link_when_page_unreadable(monkeypatch):
    def boom(url, timeout):
        raise OSError("offline")
    monkeypatch.setattr(dl, "_open", boom)
    assert dl.find_sdk_url() == dl.FALLBACK_URL


def test_truncated_download_is_rejected(monkeypatch, tmp_path):
    r = _Resp(b"abc", dl.FALLBACK_URL)
    r.headers = {"Content-Length": "10"}
    monkeypatch.setattr(dl, "_open", lambda url, timeout: r)
    with pytest.raises(dl.SdkDownloadError, match="incomplete"):
        dl.download(dl.FALLBACK_URL, str(tmp_path / "z.zip"))


NEW_URL = "https://image.mozaracing.com/as23repository/MOZA_SDK_1.0.0.13_en.zip"


def _serve(monkeypatch, tmp_path, url, dll):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("S/MOZA_SDK/x64-release/bin/MOZA_SDK.dll", dll)
    monkeypatch.setattr(dl, "find_sdk_url", lambda: url)
    monkeypatch.setattr(dl, "_open", lambda u, timeout: _Resp(payload.getvalue(), u))
    monkeypatch.setattr(dl, "install_dir", lambda: str(tmp_path))


def test_known_version_installs_after_hash_check(monkeypatch, tmp_path):
    dll = _fake_dll()
    monkeypatch.setitem(dl.KNOWN_SHA256, "1.0.0.6", __import__("hashlib").sha256(dll).hexdigest())
    _serve(monkeypatch, tmp_path, dl.FALLBACK_URL, dll)
    seen = []
    staged = dl.fetch_sdk(progress=lambda g, t: seen.append((g, t)))
    assert staged.verified and staged.version == "1.0.0.6"
    assert not (tmp_path / "MOZA_SDK.dll").exists()          # nothing in place before install
    assert dl.install_staged(staged) == str(tmp_path / "MOZA_SDK.dll")
    assert (tmp_path / "MOZA_SDK.dll").read_bytes() == dll
    assert seen and seen[-1][0] == seen[-1][1]


def test_known_version_with_wrong_hash_is_refused_before_writing(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path, dl.FALLBACK_URL, _fake_dll())      # not the real 1.0.0.6 DLL
    with pytest.raises(dl.SdkDownloadError, match="checksum"):
        dl.fetch_sdk()
    assert list(tmp_path.iterdir()) == []


def test_new_version_is_staged_unverified(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path, NEW_URL, _fake_dll())
    staged = dl.fetch_sdk()
    assert staged.version == "1.0.0.13" and not staged.verified       # the UI asks first
    staged.discard()
    assert list(tmp_path.iterdir()) == []


def test_cancel_at_the_end_installs_nothing(monkeypatch, tmp_path):
    _serve(monkeypatch, tmp_path, NEW_URL, _fake_dll())
    done = []

    def progress(g, t):
        if g == t:
            done.append(True)                 # the download just completed
    with pytest.raises(dl.SdkCancelled):
        dl.fetch_sdk(progress=progress, cancelled=lambda: bool(done))
    assert list(tmp_path.iterdir()) == []


def test_redirect_off_moza_hosts_is_refused_before_following():
    import urllib.request
    h = dl._MozaOnlyRedirects()
    req = urllib.request.Request(dl.FALLBACK_URL)
    with pytest.raises(dl.SdkDownloadError):
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x.zip")
    assert h.redirect_request(req, None, 302, "Found", {},
                              "https://image.mozaracing.com/other/MOZA_SDK_1.0.0.6_en.zip") is not None


def test_offer_only_for_moza_devices(monkeypatch):
    from types import SimpleNamespace as NS
    import telemffb.globals as G
    from telemffb.hw.ffb_dinput import DInputFFBDevice
    from telemffb.ui.dialogs import moza_sdk_offer as offer
    moza = NS(guid="m", vendor_id=0x346E, product_id=0x1000)
    other = NS(guid="o", vendor_id=0x1234, product_id=0x1)
    monkeypatch.setattr(DInputFFBDevice, "enumerate", staticmethod(lambda: [moza, other]))
    monkeypatch.delenv("TELEMFFB_MOZA_DISABLE", raising=False)
    monkeypatch.setattr(G, "device_usbpid", None, raising=False)
    monkeypatch.setattr(G, "device_di_guid", "m", raising=False)
    assert offer.moza_sdk_wanted()
    monkeypatch.setattr(G, "device_di_guid", "o", raising=False)
    assert not offer.moza_sdk_wanted()                 # another DirectInput device
    monkeypatch.setattr(G, "device_di_guid", None, raising=False)
    monkeypatch.setattr(G, "device_usbpid", "2055", raising=False)
    assert not offer.moza_sdk_wanted()                 # a VPforce Rhino (HID)
    monkeypatch.setattr(G, "device_usbpid", None, raising=False)
    assert offer.moza_sdk_wanted()                     # nothing configured, a MOZA connected
    monkeypatch.setenv("TELEMFFB_MOZA_DISABLE", "1")
    assert not offer.moza_sdk_wanted()
