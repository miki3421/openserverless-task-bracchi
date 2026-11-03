"""Create and verify a bounded online SQLite backup without modifying the source."""

from pathlib import Path
import sqlite3
import time


def backup_online(source, destination, timeout_seconds=180):
    """Use SQLite's backup API, including committed WAL data, with a time bound."""
    source, destination = Path(source), Path(destination)
    if destination.exists() or source.resolve() == destination.resolve():
        raise ValueError("Destination must be new and distinct from the source")
    start = time.monotonic()

    def progress(status, remaining, total):
        if time.monotonic() - start > timeout_seconds:
            raise TimeoutError("Online SQLite backup exceeded its time budget")

    reader = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    writer = sqlite3.connect(destination, timeout=5)
    try:
        reader.backup(writer, pages=512, progress=progress, sleep=0.05)
        # Normalize ONLY the destination artifact. A WAL-mode backup can create
        # new -wal/-shm sidecars even when subsequently opened read-only; keep
        # the immutable backup self-contained for packaging and verification.
        writer.execute("PRAGMA journal_mode=DELETE").fetchone()
        checks = writer.execute("PRAGMA integrity_check").fetchall()
        if checks != [("ok",)]:
            raise RuntimeError("Backup SQLite integrity check failed")
        return {"method": "sqlite-online-backup-api", "integrityCheck": "ok",
                "pageCount": writer.execute("PRAGMA page_count").fetchone()[0],
                "elapsedSeconds": round(time.monotonic() - start, 3)}
    finally:
        writer.close()
        reader.close()
