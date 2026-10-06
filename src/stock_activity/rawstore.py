from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode())


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class RawStore:
    def __init__(self, root: Path, config_snapshot: dict[str, Any]):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = root / "manifest.json"
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text())
        else:
            self.manifest = {"schema_version": 1, "configurations": [], "artifacts": []}
        configurations = self.manifest.setdefault("configurations", [])
        config_hash = hashlib.sha256(json.dumps(config_snapshot, sort_keys=True).encode()).hexdigest()
        if not any(item.get("sha256") == config_hash for item in configurations):
            configurations.append({
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "sha256": config_hash,
                "config": config_snapshot,
            })
            atomic_json(self.manifest_path, self.manifest)

    @staticmethod
    def key(source: str, params: dict[str, Any]) -> str:
        canonical = json.dumps({"source": source, "params": params}, sort_keys=True, default=str).encode()
        return hashlib.sha256(canonical).hexdigest()[:20]

    def cached(self, source: str, params: dict[str, Any], suffix: str) -> bytes | None:
        path = self.root / source / f"{self.key(source, params)}.{suffix}"
        return path.read_bytes() if path.exists() else None

    def save(self, source: str, params: dict[str, Any], payload: bytes, suffix: str) -> Path:
        key = self.key(source, params)
        path = self.root / source / f"{key}.{suffix}"
        checksum = hashlib.sha256(payload).hexdigest()
        if not path.exists():
            atomic_bytes(path, payload)
        relative = str(path.relative_to(self.root))
        if not any(item["path"] == relative for item in self.manifest["artifacts"]):
            self.manifest["artifacts"].append({
                "source": source,
                "path": relative,
                "parameters": params,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "sha256": checksum,
                "bytes": len(payload),
            })
            atomic_json(self.manifest_path, self.manifest)
        return path

    def record_run(self, started_at: str, metadata: dict[str, Any]) -> None:
        self.manifest.setdefault("collections", []).append({
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            **metadata,
        })
        atomic_json(self.manifest_path, self.manifest)
