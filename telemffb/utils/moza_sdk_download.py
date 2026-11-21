"""Fetch MOZA_SDK.dll from MOZA's own website, with the user's consent.

TelemFFB MOZA cannot ship MOZA_SDK.dll (the MOZA SDK has no redistribution licence),
so at start-up, when the DLL is missing, the user is offered a download of the
official MOZA Flight SDK from mozaracing.com.  Only the 64-bit release DLL is taken
from the archive and placed next to the executable (``dll/`` when run from source).

Safety: HTTPS with certificate verification, MOZA hosts only, the archive member is
checked to be a 64-bit Windows DLL before it replaces anything, and the write is
atomic.  The SDK DLL is not Authenticode-signed; a SHA-256 outside the known list
is accepted (newer SDK versions) but logged.
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
from typing import Callable, Optional

SDK_PAGE = "https://mozaracing.com/pages/sdk"
#: Flight SDK (English) as linked from the SDK page, used when the page cannot be read
FALLBACK_URL = "https://image.mozaracing.com/as23repository/MOZA_SDK_1.0.0.6_en.zip"
ALLOWED_HOSTS = {"mozaracing.com", "www.mozaracing.com", "image.mozaracing.com"}
DLL_NAME = "MOZA_SDK.dll"
#: SHA-256 of x64-release/bin/MOZA_SDK.dll in known official SDK archives
KNOWN_SHA256 = {
    "9ea187a85461aa2562b8715991b573d6ceb593ac23d0370e636fadfc831b0c5f": "1.0.0.6",
}
_LINK_RE = re.compile(r"https://image\.mozaracing\.com/[\w./-]*?MOZA_SDK_(\d+(?:\.\d+)+)_en\.zip", re.I)
_MEMBER_RE = re.compile(r"(^|/)x64-release/bin/MOZA_SDK\.dll$", re.I)
_UA = "TelemFFB-MOZA"


class SdkDownloadError(Exception):
    """A user-presentable reason the SDK could not be installed."""


def _check_host(url: str):
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise SdkDownloadError(f"Refusing to download from {url}: not an official MOZA address")


def _open(url: str, timeout: float):
    _check_host(url)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    resp = urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=timeout)
    _check_host(resp.geturl())      # a redirect must stay on MOZA hosts too
    return resp


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


def download(url: str, dest: str, progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None, timeout: float = 60.0) -> None:
    """Download ``url`` to ``dest``.  ``progress(received, total)``; ``cancelled()``
    returning True aborts with SdkDownloadError."""
    with _open(url, timeout) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            if cancelled is not None and cancelled():
                raise SdkDownloadError("Download cancelled")
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


def extract_dll(zip_path: str, target: str) -> str:
    """Take x64-release/bin/MOZA_SDK.dll out of the SDK archive and write it to
    ``target`` atomically.  Returns the DLL's SHA-256."""
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
    digest = hashlib.sha256(data).hexdigest()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="MOZA_SDK_", suffix=".tmp", dir=os.path.dirname(target) or ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if digest in KNOWN_SHA256:
        logging.info(f"MOZA SDK {KNOWN_SHA256[digest]}: {DLL_NAME} installed to {target}")
    else:
        logging.warning(f"MOZA SDK: installed {DLL_NAME} with an unlisted SHA-256 {digest} "
                        f"(newer SDK version?) to {target}")
    return digest


def install_dir() -> str:
    """Where TelemFFB looks for MOZA_SDK.dll first: next to the exe, or dll/ from source."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "dll")


def install_sdk(progress=None, cancelled=None, status=None) -> str:
    """Find, download and install MOZA_SDK.dll; returns the installed path.
    ``status(text)`` reports the current step."""
    say = status or (lambda s: None)
    say("Looking up the MOZA SDK download...")
    url = find_sdk_url()
    target = os.path.join(install_dir(), DLL_NAME)
    fd, zip_path = tempfile.mkstemp(prefix="MOZA_SDK_", suffix=".zip")
    os.close(fd)
    try:
        say(f"Downloading {url.rsplit('/', 1)[-1]} from mozaracing.com...")
        logging.info(f"MOZA SDK: downloading {url}")
        download(url, zip_path, progress, cancelled)
        say("Installing MOZA_SDK.dll...")
        try:
            extract_dll(zip_path, target)
        except PermissionError as e:
            raise SdkDownloadError(f"No permission to write {target}. Copy MOZA_SDK.dll there by hand "
                                   "(see INSTALL.txt) or run TelemFFB from a folder you can write to.") from e
        return target
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
