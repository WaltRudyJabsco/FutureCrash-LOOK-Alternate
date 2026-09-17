"""Hand things to the desktop. Results are `dispatched`, never `playing`."""
from __future__ import annotations

import platform
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import List, Optional

from .. import store
from ..kernel import fsops
from ..kernel.policy import check_path, resolve
from ..kernel.results import Result
from ..kernel.tools import DISPATCH, OPEN, Context, Param, registry
from ..paths import roots

CATEGORIES = {
    "video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"},
    "audio": {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".aiff"},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".tiff", ".bmp", ".svg"},
    "document": {".pdf", ".epub", ".docx", ".doc", ".rtf", ".pages", ".odt"},
    "text": {".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".sh", ".css", ".html", ".csv"},
}


def category(path: Path) -> str:
    if path.is_dir():
        return "folder"
    ext = path.suffix.lower()
    return next((name for name, exts in CATEGORIES.items() if ext in exts), "file")


def app_for(cat: str) -> str:
    return str((store.read_json(roots.apps, {}) or {}).get(cat, "system"))


def set_app(cat: str, app: str) -> None:
    data = store.read_json(roots.apps, {}) or {}
    data[cat] = app
    store.write_json(roots.apps, data)


def _spawn(cmd: List[str]) -> Optional[str]:
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return None
    except OSError as exc:
        return str(exc)


def launch(target: Path, preview: bool = False) -> Result:
    if not target.exists():
        return Result.refused(f"{fsops.display(target)} does not exist")
    system = platform.system()
    cat = category(target)
    app = app_for(cat)
    if preview and system == "Darwin" and shutil.which("qlmanage"):
        err = _spawn(["qlmanage", "-p", str(target)])
        return Result.failed(f"preview failed: {err}") if err else Result.dispatched(
            f"handed {fsops.display(target)} to Quick Look", path=str(target), app="Quick Look")
    if system == "Darwin":
        cmd = ["open"] + (["-a", app] if app != "system" else []) + [str(target)]
    else:
        if app != "system":
            binary = shutil.which(app)
            if not binary:
                return Result.failed(f"preferred {cat} app {app!r} is not installed")
            cmd = [binary, str(target)]
        else:
            opener = shutil.which("xdg-open")
            if not opener:
                return Result.failed("no desktop opener (xdg-open) on this machine")
            cmd = [opener, str(target)]
    err = _spawn(cmd)
    if err:
        return Result.failed(f"could not launch: {err}")
    shown = app if app != "system" else "the default app"
    return Result.dispatched(f"handed {fsops.display(target)} to {shown}", path=str(target), app=shown, category=cat)


def reveal(target: Path) -> Result:
    if not target.exists():
        return Result.refused(f"{fsops.display(target)} does not exist")
    if platform.system() == "Darwin":
        err = _spawn(["open", "-R", str(target)])
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            return Result.failed("no desktop file manager opener on this machine")
        err = _spawn([opener, str(target if target.is_dir() else target.parent)])
    return Result.failed(f"reveal failed: {err}") if err else Result.dispatched(
        f"asked the file manager to show {fsops.display(target)}", path=str(target))


@registry.tool("open_path", "Open a local file or folder in its desktop app (media players, viewers, editors).", OPEN, DISPATCH,
               params={"path": Param("string", "Path to open")})
def open_path(ctx: Context, path: str) -> Result:
    target = resolve(ctx, path)
    return check_path(ctx, target, "read") or launch(target)


@registry.tool("preview_path", "Quick external preview of a file (Quick Look on macOS).", OPEN, DISPATCH,
               params={"path": Param("string", "Path to preview")})
def preview_path(ctx: Context, path: str) -> Result:
    target = resolve(ctx, path)
    return check_path(ctx, target, "read") or launch(target, preview=True)


@registry.tool("reveal_path", "Show a file in Finder / the file manager.", OPEN, DISPATCH,
               params={"path": Param("string", "Path to reveal")})
def reveal_path(ctx: Context, path: str) -> Result:
    target = resolve(ctx, path)
    return check_path(ctx, target, "read") or reveal(target)


def open_url_host(url: str) -> Result:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return Result.refused("only complete http(s) URLs can be opened")
    cmd = ["open", url] if platform.system() == "Darwin" else [shutil.which("xdg-open") or "xdg-open", url]
    err = _spawn(cmd)
    return Result.failed(f"could not open browser: {err}") if err else Result.dispatched(
        f"handed {parsed.netloc}{parsed.path[:60]} to the browser", url=url, kind=resource_kind(url))


def resource_kind(url: str) -> str:
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host in {"youtube.com", "youtu.be"}:
        if host == "youtu.be" or p.path == "/watch" or p.path.startswith("/shorts/"):
            return "video"
        if p.path.startswith("/playlist"):
            return "playlist"
        if p.path.startswith(("/@", "/channel", "/c/", "/user")):
            return "channel"
        if p.path.startswith("/results"):
            return "search"
    if host.endswith("vimeo.com") and p.path.strip("/").isdigit():
        return "video"
    return "page"


@registry.tool("open_url", "Open an exact http(s) URL from a result in the browser. For 'play/watch', the URL must be a video, not a channel or search page.",
               OPEN, DISPATCH, params={"url": Param("string", "Complete URL copied from a result"),
                                       "intent": Param("string", "open or play", required=False, enum=["open", "play"])})
def open_url(ctx: Context, url: str, intent: str = "open") -> Result:
    if intent == "play" and resource_kind(url) not in {"video", "playlist"}:
        return Result.refused(f"that URL is a {resource_kind(url)}, not something playable; find the video's own URL first")
    return open_url_host(url)
