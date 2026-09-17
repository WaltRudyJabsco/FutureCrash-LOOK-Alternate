"""Your LOOK is portable.

    lk profile                    what is in it, where it lives
    lk profile backup [DEST]      rotating timestamped snapshots (destination remembered)
    lk profile export [FILE.zip]  one migration archive
    lk profile restore SOURCE     from a zip or a backup directory (safety snapshot first)
    lk profile import-3x          bring memory, skills, settings and hosts over from LOOK 3.x

Only the PROFILE layer travels. Secrets, grants, hosts, the journal, trash and
queues are machine or runtime state and are never exported.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__, store
from .paths import BUNDLED, roots

FORMAT = "look-profile"
SCHEMA = 1


def _files() -> List[Path]:
    base = roots.profile
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*") if p.is_file() and not p.name.startswith(".") and p.suffix != ".lock")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest() -> dict:
    return {"format": FORMAT, "schema": SCHEMA, "look_version": __version__, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": [{"path": str(p.relative_to(roots.profile)), "bytes": p.stat().st_size, "sha256": _sha(p)} for p in _files()]}


def status() -> Dict[str, object]:
    files = _files()
    cfg = store.read_json(roots.backups, {}) or {}
    return {"profile": str(roots.profile), "machine": str(roots.machine), "runtime": str(roots.runtime),
            "files": len(files), "bytes": sum(p.stat().st_size for p in files),
            "backup_root": cfg.get("root"), "keep": cfg.get("keep", 10)}


def backup(dest: Optional[str] = None, keep: Optional[int] = None) -> Path:
    cfg = store.read_json(roots.backups, {}) or {}
    root = Path(dest).expanduser() if dest else Path(cfg["root"]) if cfg.get("root") else None
    if root is None:
        raise ValueError("no backup destination yet: lk profile backup ~/Documents/LOOK")
    keep = int(keep or cfg.get("keep", 10))
    store.write_json(roots.backups, {"root": str(root), "keep": keep})
    series = root / "LOOK-profile-backups"
    target = series / time.strftime("%Y-%m-%d-%H%M%S")
    shutil.copytree(roots.profile, target / "profile", ignore=shutil.ignore_patterns("*.lock", ".*"))
    store.write_json(target / "manifest.json", manifest(), private=False)
    snapshots = sorted(p for p in series.iterdir() if p.is_dir())
    for old in snapshots[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def export(path: Optional[str] = None) -> Path:
    out = Path(path).expanduser() if path else Path.cwd() / f"look-profile-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest(), indent=2))
        for p in _files():
            z.write(p, "profile/" + str(p.relative_to(roots.profile)))
    os.chmod(out, 0o600)
    return out


def _safe_member(name: str) -> bool:
    parts = Path(name).parts
    return bool(parts) and not Path(name).is_absolute() and ".." not in parts


def restore(source: str) -> Tuple[int, Path]:
    src = Path(source).expanduser()
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        if src.is_file():
            with zipfile.ZipFile(src) as z:
                for name in z.namelist():
                    if not _safe_member(name):
                        raise ValueError(f"unsafe path in archive: {name}")
                z.extractall(staging)
        elif (src / "manifest.json").exists():
            shutil.copytree(src, staging / "x")
            staging = staging / "x"
        else:
            raise ValueError("not a LOOK profile archive or backup directory")
        man = store.read_json(staging / "manifest.json", {}) or {}
        if man.get("format") != FORMAT or int(man.get("schema", 0)) > SCHEMA:
            raise ValueError("unrecognised or newer profile format")
        for entry in man.get("files", []):
            p = staging / "profile" / entry["path"]
            if not p.exists() or _sha(p) != entry["sha256"]:
                raise ValueError(f"archive failed verification: {entry['path']}")
        safety = roots.runtime / "restore-snapshots" / time.strftime("%Y%m%d-%H%M%S")
        if roots.profile.exists():
            shutil.copytree(roots.profile, safety)
        count = 0
        for entry in man.get("files", []):
            dst = roots.profile / entry["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / "profile" / entry["path"], dst)
            count += 1
        return count, safety


# ---- 3.x import ------------------------------------------------------------

def import_3x(dry_run: bool = False) -> List[str]:
    """Best-effort, additive, never deletes the legacy files."""
    from .kernel.ollama import load_hosts, save_hosts
    from .mind import personas, skills
    from .mind.memory import Mind
    from .settings import set_secret, settings

    legacy = roots.legacy_state
    notes: List[str] = []
    if not legacy.exists():
        return [f"no LOOK 3.x state found at {legacy}"]
    personas.ensure_profile()

    def note(msg: str):
        notes.append(("would " if dry_run else "") + msg)

    # settings
    mapping = {"lo_access": "lo.access", "lo_personality": "lo.personality", "lo_thinking": "lo.thinking",
               "lo_think_display": "lo.think_display", "ollama_model": "ai.model"}
    for fname, key in mapping.items():
        value = store.read_text(legacy / fname).strip()
        if value:
            try:
                if not dry_run:
                    settings.set(key, value)
                note(f"set {key} = {value}")
            except ValueError as exc:
                notes.append(f"skipped {fname}: {exc}")
    fb = store.read_json(roots.home / ".config" / "look" / "feedback.json", None)
    if isinstance(fb, dict):
        if "sound" in fb:
            if not dry_run:
                settings.set("feedback.sound", bool(fb["sound"]) if isinstance(fb["sound"], bool) else str(fb["sound"]))
            note("imported sound preference")
        if fb.get("motion") in ("off", "subtle", "normal"):
            if not dry_run:
                settings.set("feedback.motion", fb["motion"])
            note("imported motion preference")

    # memory
    old = store.read_json(legacy / "ollama_memory.json", None)
    if isinstance(old, dict):
        mind = Mind("lo")
        with mind.lock():
            data = mind.load()
            added = 0
            for item in (old.get("durable") or []) + (old.get("memories") or []):
                if isinstance(item, dict) and item.get("text"):
                    importance = int(item.get("importance", 80 if item in (old.get("durable") or []) else 50) or 50)
                    if mind.add(data, str(item["text"]), importance) == "added":
                        added += 1
            summary = str(old.get("core_summary") or old.get("summary") or "").strip()
            if summary and not data.get("summary"):
                data["summary"] = " ".join(summary.split())[:1500]
            if not dry_run:
                mind.save(data)
        note(f"imported {added} memories" + (" and the long-term summary" if summary else ""))
    recent = store.read_json(legacy / "lo_recent.json", None)
    if isinstance(recent, dict) and recent.get("exchanges"):
        if not dry_run:
            store.write_json(Mind("lo").recent_path, {"schema": 1, "exchanges": recent["exchanges"][-6:]})
        note(f"imported {min(6, len(recent['exchanges']))} recent exchanges")

    # core, skills, personalities
    core = store.read_text(legacy / "core.md")
    if core.strip() and core.strip() != store.read_text(BUNDLED / "core.md").strip():
        if not dry_run:
            store.write_text(roots.core, core)
        note("kept your customised core.md")
    old_skills = store.read_text(legacy / "skills.md")
    if old_skills:
        learned = re.split(r"(?im)^##\s+learned\s*$", old_skills)
        rows = re.findall(r"(?m)^-\s+(.+)$", learned[1]) if len(learned) > 1 else []
        n = 0
        for row in rows:
            if dry_run or skills.add(row.strip()):
                n += 1
        note(f"imported {n} learned skills")
    for pack in (legacy / "personalities").glob("*.md") if (legacy / "personalities").is_dir() else []:
        target = roots.personalities / pack.name
        bundled = BUNDLED / "personalities" / pack.name
        if not bundled.exists() or store.read_text(bundled).strip() != store.read_text(pack).strip():
            if not dry_run:
                store.write_text(target, store.read_text(pack))
            note(f"imported personality {pack.stem}")

    # machine: hosts, apps, web key
    hosts = store.read_json(legacy / "ollama_hosts.json", None)
    if isinstance(hosts, dict) and hosts.get("hosts"):
        cur = load_hosts()
        cur["hosts"].update(hosts["hosts"])
        cur["default"] = hosts.get("default", cur["default"])
        if not dry_run:
            save_hosts(cur)
        note(f"imported {len(hosts['hosts'])} inference hosts")
    apps = store.read_json(legacy / "apps.json", None)
    if isinstance(apps, dict) and apps:
        if not dry_run:
            store.write_json(roots.apps, apps)
        note("imported desktop app preferences")
    secrets_file = roots.home / ".zsh_secrets"
    m = re.search(r"OLLAMA_API_KEY=['\"]?([^'\"\s]+)", store.read_text(secrets_file))
    if m:
        if not dry_run:
            set_secret("OLLAMA_API_KEY", m.group(1))
        note("found your Ollama web-search key in ~/.zsh_secrets and stored it machine-locally (0600)")

    fc = store.read_json(roots.legacy_future_crash / "memory.json", None)
    if isinstance(fc, dict):
        mind = Mind("future-crash")
        with mind.lock():
            data = mind.load()
            summary = str(fc.get("summary") or fc.get("long_term") or "").strip()
            if summary:
                data["summary"] = summary[:1500]
            if not dry_run:
                mind.save(data)
        note("imported Future Crash's Oracle memory summary" if summary else "Future Crash memory had no summary to import")
    if not notes:
        notes.append("nothing to import")
    return notes
