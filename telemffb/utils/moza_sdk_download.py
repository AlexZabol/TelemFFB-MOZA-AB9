"""Fetch MOZA_SDK.dll from MOZA's own website, with the user's consent.

TelemFFB MOZA cannot ship MOZA_SDK.dll (the MOZA SDK has no redistribution licence),
so at start-up, when the DLL is missing and a MOZA base is configured, the user is
offered a download of the official MOZA Flight SDK from mozaracing.com.  Only the
64-bit release DLL is taken from the archive and placed next to the executable
(``dll/`` when run from source).

Flow: :func:`fetch_sdk` (worker thread) downloads and verifies, leaving the DLL in a
staging file; :func:`install_staged` (main thread, after any confirmation) moves it
into place atomically.

Safety: HTTPS with certificate verification; every URL, each redirect hop included,
must be on a MOZA host; the archive member must be a 64-bit Windows DLL; for a known
SDK version the DLL's SHA-256 must match before anything is written.  The SDK DLL is
not Authenticode-signed, so a version without a known hash is only installed after
the user confirms it (the UI shows version and SHA-256).
"""
import hashlib
import logging
import os
import re
import ssl
import struct
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

SDK_PAGE = "https://mozaracing.com/pages/sdk"
#: Flight SDK (English) as linked from the SDK page, used when the page cannot be read
FALLBACK_URL = "https://image.mozaracing.com/as23repository/MOZA_SDK_1.0.0.6_en.zip"
ALLOWED_HOSTS = {"mozaracing.com", "www.mozaracing.com", "image.mozaracing.com"}
DLL_NAME = "MOZA_SDK.dll"
#: SDK version -> SHA-256 of x64-release/bin/MOZA_SDK.dll in the official archive
KNOWN_SHA256 = {
    "1.0.0.6": "9ea187a85461aa2562b8715991b573d6ceb593ac23d0370e636fadfc831b0c5f",
}
_LINK_RE = re.compile(r"https://image\.mozaracing\.com/[\w./-]*?MOZA_SDK_(\d+(?:\.\d+)+)_en\.zip", re.I)
_VERSION_RE = re.compile(r"MOZA_SDK_(\d+(?:\.\d+)+)_en\.zip$", re.I)
_MEMBER_RE = re.compile(r"(^|/)x64-release/bin/MOZA_SDK\.dll$", re.I)
_UA = "TelemFFB-MOZA"


class SdkDownloadError(Exception):
    """A user-presentable reason the SDK could not be installed."""


class SdkCancelled(SdkDownloadError):
    """The user cancelled."""


@dataclass
class StagedSdk:
    """A downloaded and checked DLL waiting to be installed."""
    staged_path: str
    target: str
    version: Optional[str]
    sha256: str

    @property
    def verified(self) -> bool:
        """The DLL matches the known hash of its SDK version."""
        return bool(self.version) and KNOWN_SHA256.get(self.version) == self.sha256

    def discard(self):
        try:
            os.remove(self.staged_path)
        except OSError:
            pass


def _check_host(url: str):
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise SdkDownloadError(f"Refusing to download from {url}: not an official MOZA address")


class _MozaOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Check each redirect target before it is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float):
    _check_host(url)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), _MozaOnlyRedirects())
    resp = opener.open(urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=timeout)
    _check_host(resp.geturl())
    return resp


def sdk_version(url: str) -> Optional[str]:
    m = _VERSION_RE.search(urllib.parse.urlparse(url).path)
    return m.group(1) if m else None


def find_sdk_url(timeout: float = 15.0) -> str:
    """The newest Flight SDK (English) zip linked from MOZA's SDK page; the known
    link when the page cannot be read or has none."""
    try:
        with _open(SDK_PAGE, timeout) as resp:
            html = resp.read(2_000_000).decode("utf-8", "replace")
        links = {m.group(0): tuple(int(x) for x in m.group(1).split(".")) for m in _LINK_RE.finditer(html)}
        if links:
            return max(links, key=links.get)
        logging.warning("MOZA SDK page has no Flight SDK link; using the known one")
    except Exception as e:
        logging.warning(f"MOZA SDK page not readable ({e}); using the known link")
    return FALLBACK_URL


def _check_cancel(cancelled):
    if cancelled is not None and cancelled():
        raise SdkCancelled("Download cancelled")


def download(url: str, dest: str, progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None, timeout: float = 60.0) -> None:
    """Download ``url`` to ``dest``.  ``progress(received, total)``; ``cancelled()``
    returning True aborts with SdkCancelled."""
    with _open(url, timeout) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            _check_cancel(cancelled)
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress is not None:
                progress(got, total)
    if total and got != total:
        raise SdkDownloadError(f"Download incomplete ({got:,} of {total:,} bytes)")


def is_x64_dll(data: bytes) -> bool:
    """A 64-bit (AMD64) Windows DLL image."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        return False
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        return False
    machine, = struct.unpack_from("<H", data, pe + 4)
    characteristics, = struct.unpack_from("<H", data, pe + 22)
    return machine == 0x8664 and bool(characteristics & 0x2000)


def read_dll(zip_path: str) -> bytes:
    """x64-release/bin/MOZA_SDK.dll from the SDK archive, checked to be an x64 DLL."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [n for n in zf.namelist() if _MEMBER_RE.search(n.replace("\\", "/"))]
            if len(members) != 1:
                raise SdkDownloadError("The MOZA SDK archive has no (single) x64 MOZA_SDK.dll")
            data = zf.read(members[0])
    except zipfile.BadZipFile as e:
        raise SdkDownloadError(f"The downloaded MOZA SDK archive is damaged ({e})") from e
    if not is_x64_dll(data):
        raise SdkDownloadError("MOZA_SDK.dll in the archive is not a 64-bit Windows DLL")
    return data


def install_dir() -> str:
    """Where TelemFFB looks for MOZA_SDK.dll first: next to the exe, or dll/ from source."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "dll")


def fetch_sdk(progress=None, cancelled=None, status=None) -> StagedSdk:
    """Find, download and verify the SDK; the DLL is left in a staging file next to
    the target.  A known version with a different hash is refused here."""
    say = status or (lambda s: None)
    say("Looking up the MOZA SDK download...")
    url = find_sdk_url()
    version = sdk_version(url)
    target = os.path.join(install_dir(), DLL_NAME)
    fd, zip_path = tempfile.mkstemp(prefix="MOZA_SDK_", suffix=".zip")
    os.close(fd)
    try:
        say(f"Downloading {url.rsplit('/', 1)[-1]} from mozaracing.com...")
        logging.info(f"MOZA SDK: downloading {url}")
        download(url, zip_path, progress, cancelled)
        _check_cancel(cancelled)
        say("Checking MOZA_SDK.dll...")
        data = read_dll(zip_path)
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    digest = hashlib.sha256(data).hexdigest()
    expected = KNOWN_SHA256.get(version or "")
    if expected and digest != expected:
        raise SdkDownloadError(f"MOZA_SDK.dll of SDK {version} does not match its known checksum "
                               f"(SHA-256 {digest}); not installed.")
    _check_cancel(cancelled)
    try:
        os.makedirs(install_dir(), exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix="MOZA_SDK_", suffix=".tmp", dir=install_dir())
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except PermissionError as e:
        raise SdkDownloadError(f"No permission to write to {install_dir()}. Copy MOZA_SDK.dll there by hand "
                               "(see INSTALL.txt) or run TelemFFB from a folder you can write to.") from e
    return StagedSdk(staged, target, version, digest)


def install_staged(staged: StagedSdk) -> str:
    """Move a staged DLL into place (atomic).  Returns the installed path."""
    os.replace(staged.staged_path, staged.target)
    if staged.verified:
        logging.info(f"MOZA SDK {staged.version}: {DLL_NAME} installed to {staged.target} (checksum verified)")
    else:
        logging.warning(f"MOZA SDK {staged.version or '?'}: {DLL_NAME} installed to {staged.target} with "
                        f"SHA-256 {staged.sha256}, confirmed by the user (no known checksum for this version)")
    return staged.target
