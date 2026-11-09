"""插件本地任务账本、内容缓存和有界文件回收。"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

FETCH_KEYS = ("comment_limit", "subcomment_limit", "forum_thread_limit", "sort", "only_author",
              "include_images", "include_emoji", "image_limit")


def cache_key(target: dict, options: dict) -> str:
    content = {"url": target["url"], "options": {k: options.get(k) for k in FETCH_KEYS},
               "identity": hashlib.sha256(options.get("cookie", "").encode()).hexdigest()[:12]}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.db_path = self.root / "state.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, updated REAL, payload TEXT);
                CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, expires REAL, created REAL, payload TEXT);
                CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_job(self, job: dict) -> None:
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?)",
                       (job["id"], time.time(), json.dumps(job, ensure_ascii=False)))

    def get_job(self, job_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM jobs ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            job = json.loads(row[0])
            job.pop("document", None)
            job.pop("files", None)
            job.pop("original_files", None)
            job.pop("options", None)
            result.append(job)
        return result

    def increment(self, key: str) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO counters VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1", (key,))

    def stats(self) -> dict:
        out = {"total": 0, "success": 0, "failed": 0, "cache_hits": 0, "active": 0}
        with self.connection() as db:
            out.update(dict(db.execute("SELECT name,value FROM counters")))
        return out

    def get_cache(self, key: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def put_cache(self, key: str, document: dict, minutes: int) -> None:
        if minutes <= 0:
            return
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)", (key, time.time() + minutes * 60,
                       time.time(), json.dumps(document, ensure_ascii=False)))

    def cache_info(self) -> dict:
        with self.connection() as db:
            count = db.execute("SELECT count(*) FROM cache WHERE expires>?", (time.time(),)).fetchone()[0]
        # Only the ledger and generated results can be reclaimed by this budget.
        # Browser engines, uploaded fonts and intermediate screenshots persist
        # independently; counting them could evict every historical result.
        size = sum(p.stat().st_size for p in self.artifacts.rglob("*") if p.is_file())
        size += sum(p.stat().st_size for p in self.root.glob("state.sqlite3*") if p.is_file())
        return {"entries": count, "bytes": size}

    def clear_cache(self) -> int:
        with self.connection() as db:
            count = db.execute("SELECT count(*) FROM cache").fetchone()[0]
            db.execute("DELETE FROM cache")
        return count

    def clear_history(self, active_ids: set[str]) -> int:
        removed = 0
        for job in self.list_jobs(10000):
            if job["id"] in active_ids:
                continue
            self.delete_job(job["id"])
            removed += 1
        return removed

    def delete_job(self, job_id: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        directory = (self.artifacts / job_id).resolve()
        if directory.parent == self.artifacts and directory.is_dir():
            shutil.rmtree(directory)

    def recover(self) -> None:
        for summary in self.list_jobs(10000):
            if summary["status"] not in {"done", "failed", "delivery_failed"}:
                job = self.get_job(summary["id"])
                delivery = job.get("status") == "sending"
                job.update(status="delivery_failed" if delivery else "failed", stage="发送被重载中断" if delivery else "任务被重载中断，可重试",
                           progress=100 if job.get("result_ready") else 0,
                           error="发送结果可能未知，请核对聊天记录；已保留生成结果。" if delivery else "插件上次停止时任务尚未完成，请点击重新解析。")
                if delivery:
                    job["delivery_status"] = "unknown"
                    job["dedup_until"] = time.time() + min(3600, max(0, job.get("dedup_seconds", 120)))
                self.save_job(job)

    def cleanup(self, hours: int, max_mb: int, active_ids: set[str]) -> dict:
        cutoff = time.time() - hours * 3600
        with self.connection() as db:
            db.execute("DELETE FROM cache WHERE expires < ?", (time.time(),))
            rows = db.execute("SELECT id,updated FROM jobs ORDER BY updated DESC").fetchall()
        removed = 0
        for idx, (job_id, updated) in enumerate(rows):
            if job_id not in active_ids and (updated < cutoff or idx >= 200):
                self.delete_job(job_id)
                removed += 1
        size = self.cache_info()["bytes"]
        budget = max_mb * 1024 * 1024
        if size > budget:
            self.clear_cache()
            self.compact()
            size = self.cache_info()["bytes"]
            for job_id, _ in reversed(rows):
                if size <= budget:
                    break
                if job_id in active_ids:
                    continue
                folder = self.artifacts / job_id
                freed = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else 0
                self.delete_job(job_id)
                size -= freed
                removed += 1
                if freed == 0:
                    self.compact()
                    size = self.cache_info()["bytes"]
            self.compact()
        with self.connection() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"removed": removed, "bytes": self.cache_info()["bytes"]}

    def compact(self):
        """回收 SQLite 已删除缓存占用的页，避免空文件触发误删历史。"""
        with self.connection() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
