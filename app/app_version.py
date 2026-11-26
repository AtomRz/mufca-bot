"""
Application version + "is a newer version available?" check.

The running version is the `VERSION` file baked into the image (MAJOR.MINOR.PATCH,
bumped by hand in the commit that changes something). The exact build is also
identified by the git commit CI passes in as MUFCA_COMMIT.

"Latest" is the VERSION file of the newest commit on main whose Docker image
build has FINISHED successfully (GitHub Actions run of docker-image.yml) — not
just the newest commit on main, so the dashboard never offers an update whose
image isn't pushed to GHCR yet. Result is cached; on any network failure the
check stays silent (no false "update available").
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

GITHUB_REPO = "AtomRz/mufca-bot"
WORKFLOW_FILE = "docker-image.yml"
BRANCH = "main"

CHECK_TTL_OK = 3600      # re-check GitHub at most once an hour
CHECK_TTL_FAIL = 600     # after a failed check, retry sooner

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

_lock = threading.Lock()
_cache = {"ts": 0.0, "latest": None, "ttl": 0, "error": None}


def _read_version() -> str:
    here = Path(__file__).resolve()
    # in the container VERSION sits next to the modules (/app/VERSION);
    # when running from a checkout it's one level up from app/
    for p in (here.with_name("VERSION"), here.parent.parent / "VERSION"):
        try:
            v = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            if _VERSION_RE.match(v):
                return v
        except (OSError, IndexError):
            continue
    return "0.0.0"


CURRENT_VERSION = _read_version()
CURRENT_COMMIT = (os.environ.get("MUFCA_COMMIT") or "").strip()[:7] or None


def _parse(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def _http_get(url: str, timeout: int = 8) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "mufca-bot-update-check", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _fetch_latest() -> str:
    runs_url = (
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?branch={BRANCH}&status=success&per_page=1"
    )
    runs = json.loads(_http_get(runs_url)).get("workflow_runs") or []
    if not runs:
        raise RuntimeError("no successful image build found")
    sha = runs[0]["head_sha"]
    text = _http_get(f"https://raw.githubusercontent.com/{GITHUB_REPO}/{sha}/VERSION")
    v = text.strip().splitlines()[0].strip()
    if not _VERSION_RE.match(v):
        raise RuntimeError(f"unexpected VERSION content: {v!r}")
    return v


def get_info(force: bool = False) -> dict:
    """Blocking (does a network call at most once per TTL) — call via asyncio.to_thread."""
    with _lock:
        now = time.time()
        if force or now - _cache["ts"] >= _cache["ttl"]:
            try:
                _cache["latest"] = _fetch_latest()
                _cache["error"] = None
                _cache["ttl"] = CHECK_TTL_OK
            except Exception as e:
                logger.warning(f"[VERSION] update check failed: {e}")
                _cache["error"] = str(e)
                _cache["ttl"] = CHECK_TTL_FAIL
            _cache["ts"] = now
        latest = _cache["latest"]
        update = bool(latest) and _parse(latest) > _parse(CURRENT_VERSION)
        return {
            "version": CURRENT_VERSION,
            "commit": CURRENT_COMMIT,
            "latest": latest,
            "update_available": update,
            "check_failed": _cache["error"] is not None and latest is None,
        }
