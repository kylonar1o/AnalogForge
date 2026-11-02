"""SQLite result store.

Default location: ``$ANALOGFORGE_DATA`` or ``~/.analogforge`` (``analogforge.db``
plus an ``artifacts/`` directory with gzip-compressed JSON per run holding
curves, netlists and extra data, which keeps the database small).
"""

from __future__ import annotations

import gzip
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, kind TEXT, name TEXT, status TEXT, progress REAL DEFAULT 0,
    total INTEGER DEFAULT 0, done INTEGER DEFAULT 0, created REAL, started REAL, finished REAL,
    config TEXT, summary TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, idx INTEGER, design TEXT, backend TEXT,
    pdk TEXT, corner TEXT, temperature REAL, vdd_scale REAL, params TEXT, metrics TEXT, label TEXT,
    ok INTEGER, errors TEXT, warnings TEXT, elapsed REAL, created REAL, artifact TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS runs_job ON runs(job_id, idx);
"""


def data_dir() -> Path:
    return Path(os.environ.get("ANALOGFORGE_DATA") or (Path.home() / ".analogforge")).expanduser()


def _clean(obj):
    """JSON-safe copy: NaN/inf -> None, numpy -> python."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "tolist"):
        return _clean(obj.tolist())
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def dumps(obj) -> str:
    return json.dumps(_clean(obj), allow_nan=False)


class Store:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser() if root else data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(exist_ok=True)
        self.path = self.root / "analogforge.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _query(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args).fetchall())

    # -------------------------------------------------------------- jobs
    def create_job(self, kind: str, config: dict, name: str = "") -> str:
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self._exec("INSERT INTO jobs (id, kind, name, status, created, config) VALUES (?,?,?,?,?,?)",
                   (jid, kind, name or kind, "queued", time.time(), dumps(config)))
        return jid

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        for k in ("config", "summary"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    @staticmethod
    def _job_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("config", "summary"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def get_job(self, job_id: str) -> dict | None:
        rows = self._query("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._job_row(rows[0]) if rows else None

    def list_jobs(self, limit: int = 100) -> list[dict]:
        return [self._job_row(r) for r in self._query("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))]

    def delete_job(self, job_id: str) -> None:
        for r in self._query("SELECT artifact FROM runs WHERE job_id=?", (job_id,)):
            if r["artifact"]:
                Path(self.root / r["artifact"]).unlink(missing_ok=True)
        self._exec("DELETE FROM runs WHERE job_id=?", (job_id,))
        self._exec("DELETE FROM jobs WHERE id=?", (job_id,))

    # -------------------------------------------------------------- runs
    def add_run(self, job_id: str, idx: int, result, label: dict | None = None) -> int:
        """Store an :class:`~analogforge.core.design.EvaluationResult`."""
        d = result.to_dict(curves=True, netlists=True)
        art_rel = f"artifacts/{job_id}/{idx:05d}.json.gz"
        art = self.root / art_rel
        art.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(art, "wt") as fh:
            fh.write(dumps({"curves": d.get("curves", []), "netlists": d.get("netlists", {}),
                            "extra": d.get("extra", {}), "runs": d.get("runs", {}),
                            "options": d.get("options", {}), "simulator_version": d.get("simulator_version")}))
        cur = self._exec(
            "INSERT INTO runs (job_id, idx, design, backend, pdk, corner, temperature, vdd_scale, params, metrics,"
            " label, ok, errors, warnings, elapsed, created, artifact) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, idx, result.design, result.backend, result.pdk, result.corner, result.temperature,
             result.vdd_scale, dumps(result.params), dumps(result.metrics), dumps(label or {}), int(result.ok),
             dumps(result.errors), dumps(result.warnings), result.elapsed_s, time.time(), art_rel))
        return int(cur.lastrowid)

    @staticmethod
    def _run_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("params", "metrics", "label", "errors", "warnings"):
            d[k] = json.loads(d[k]) if d.get(k) else ({} if k in ("params", "metrics", "label") else [])
        d["ok"] = bool(d["ok"])
        return d

    def list_runs(self, job_id: str) -> list[dict]:
        return [self._run_row(r) for r in self._query("SELECT * FROM runs WHERE job_id=? ORDER BY idx", (job_id,))]

    def get_run(self, run_id: int, artifact: bool = True) -> dict | None:
        rows = self._query("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        d = self._run_row(rows[0])
        if artifact and d.get("artifact"):
            p = self.root / d["artifact"]
            if p.exists():
                with gzip.open(p, "rt") as fh:
                    d.update(json.loads(fh.read()))
        return d

    def compare(self, run_ids: list[int]) -> dict:
        runs = [r for r in (self.get_run(i) for i in run_ids) if r is not None]
        metrics = sorted({k for r in runs for k in r["metrics"]})
        params = sorted({k for r in runs for k in r["params"]})
        return {"runs": runs, "metrics": metrics, "params": params}
