"""Update source for TelemFFB MOZA builds: the fork's GitHub releases.

Official TelemFFB checks vpforcecontrols.com; a MOZA build must never take that
(it would drop the MOZA backend), so the version check and the built-in updater
ask the fork's GitHub releases instead.  Stdlib only - also used by updater.py.
"""
import json
import re
import ssl
import urllib.request
from typing import Optional, Tuple

REPO = "AlexZabol/TelemFFB-MOZA-AB9"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


def is_moza_version(version: str) -> bool:
    """A MOZA build's version string starts with "MOZA", e.g.
    "MOZA 0.1.9 (TelemFFB 3.0.261019)"."""
    return str(version or "").startswith("MOZA")


def version_tuple(version: str) -> Tuple[int, ...]:
    """(0, 1, 9) from "MOZA 0.1.9 (TelemFFB ...)", "v0.1.9" or "0.1.9"; () if none."""
    m = _VERSION_RE.search(str(version or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def fetch_latest(timeout: float = 10) -> Tuple[str, str, str]:
    """Latest release of the fork: (version "0.1.9", zip download url, release page url).
    Raises on network/format errors."""
    req = urllib.request.Request(API_LATEST, headers={"User-Agent": "TelemFFB-MOZA",
                                                      "Accept": "application/vnd.github+json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = str(data.get("tag_name", ""))
    asset = next((a for a in data.get("assets", []) if str(a.get("name", "")).lower().endswith(".zip")), None)
    if not tag or asset is None:
        raise RuntimeError(f"No TelemFFB MOZA release package found in {RELEASES_PAGE}")
    version = ".".join(str(x) for x in version_tuple(tag))
    return version, asset["browser_download_url"], data.get("html_url") or RELEASES_PAGE


def newer_release(current_version: str, timeout: float = 10) -> Optional[Tuple[str, str, str]]:
    """The fork's latest release if it is newer than ``current_version``, else None."""
    latest, url, page = fetch_latest(timeout)
    if version_tuple(latest) > version_tuple(current_version):
        return latest, url, page
    return None
