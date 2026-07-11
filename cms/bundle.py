"""Portable memory bundle — share AI-generated data without re-processing.

The ``.memory/`` layer is the expensive part: summaries, the knowledge graph,
feature traces, reviews — all produced by an LLM. Once built, anyone can *view*
it with no API key and no cost. A bundle packages that generated data (and,
optionally, a snapshot of the scoped source) into a single ``.cmsbundle`` (a
zip) so you can hand someone the result instead of making them (and their API
budget) repeat work that's already done.

``export_bundle`` writes the bundle; ``open_bundle`` unpacks it into a folder
that ``cms ui`` can serve directly. Extraction is guarded against zip-slip
because bundles may come from someone else.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .scope import load_scope, scope_path

BUNDLE_SUFFIX = ".cmsbundle"


def _version() -> str:
    try:
        import importlib.metadata
        return importlib.metadata.version("cms")
    except Exception:
        return "0.1.0"


def default_bundle_name(root: Path) -> str:
    return f"{Path(root).resolve().name}-atlas{BUNDLE_SUFFIX}"


def export_bundle(root: Path, out_path: Path | None = None,
                  include_source: bool = False, echo=lambda *_: None) -> Path:
    """Zip the generated memory (+ optional source snapshot) into a bundle."""
    root = Path(root).resolve()
    memory_dir = root / config.MEMORY_DIR_NAME
    if not (memory_dir / "graph.json").is_file():
        raise FileNotFoundError(
            f"No {config.MEMORY_DIR_NAME}/graph.json under {root} — build it first (cms run-all)."
        )
    out_path = Path(out_path).resolve() if out_path else Path.cwd() / default_bundle_name(root)
    if out_path.suffix.lower() not in (BUNDLE_SUFFIX, ".zip"):
        out_path = out_path.with_suffix(BUNDLE_SUFFIX)

    source_records = []
    if include_source:
        from .scanner import scan
        source_records = scan(root)

    manifest = {
        "atlas_bundle": 1,
        "name": root.name,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cms_version": _version(),
        "include_source": bool(include_source),
        "has_source": bool(source_records),
        "source_file_count": len(source_records),
        "scope": sorted(load_scope(root) or []),
    }

    mem_count = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(memory_dir.rglob("*")):
            if p.is_file():
                arc = (Path(config.MEMORY_DIR_NAME) / p.relative_to(memory_dir)).as_posix()
                z.write(p, arcname=arc)
                mem_count += 1
        sp = scope_path(root)
        if sp.is_file():
            z.write(sp, arcname=sp.name)
        for rec in source_records:
            z.write(rec.abs_path, arcname=f"source/{rec.rel_path}")
        manifest["memory_file_count"] = mem_count
        z.writestr("manifest.json", json.dumps(manifest, indent=2))

    echo(f"Bundle written: {out_path}  ({mem_count} memory files"
         + (f" + {len(source_records)} source files" if source_records else ", no source") + ")")
    return out_path


def read_manifest(bundle_path: Path) -> dict:
    try:
        with zipfile.ZipFile(bundle_path) as z:
            return json.loads(z.read("manifest.json"))
    except (KeyError, OSError, zipfile.BadZipFile, json.JSONDecodeError):
        return {}


def open_bundle(bundle_path: Path, dest: Path, echo=lambda *_: None) -> Path:
    """Extract a bundle into ``dest`` (zip-slip guarded); return the project dir."""
    bundle_path = Path(bundle_path).resolve()
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            rel = name[len("source/"):] if name.startswith("source/") else name
            target = (dest / rel).resolve()
            if dest not in target.parents and target != dest:
                raise ValueError(f"unsafe path in bundle: {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
    echo(f"Bundle opened at: {dest}")
    return dest
