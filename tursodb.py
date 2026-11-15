"""
Tiny dependency-free Turso (libSQL) client.

Talks to Turso over its HTTP "Hrana" pipeline API (/v2/pipeline) using only the
Python standard library, so it installs nowhere and works on Termux, Railway,
Render, VPS, etc. (the native `libsql` wheels do not build on Termux).

Tables used by the hosting bot:
  kv(key, value, updated_at)                      -> bot state (projects, users, tokens ...)
  project_archives(pid, ver, chunks, size, ...)   -> latest backup version of a project
  project_chunks(pid, ver, idx, data)             -> backup zip, split in chunks
"""

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request


class TursoError(Exception):
    pass


def _b64decode(value):
    value = value or ""
    return base64.b64decode(value + "=" * (-len(value) % 4))


class Turso:
    def __init__(self, url, token, timeout=40, retries=3):
        url = (url or "").strip()
        for prefix in ("libsql://", "wss://", "ws://"):
            if url.startswith(prefix):
                url = "https://" + url[len(prefix):]
        if not url.startswith(("https://", "http://")):
            url = "https://" + url
        self.url = url.rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout
        self.retries = retries

    # ---------- encoding helpers ----------
    @staticmethod
    def _arg(value):
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "integer", "value": str(int(value))}
        if isinstance(value, int):
            return {"type": "integer", "value": str(value)}
        if isinstance(value, float):
            return {"type": "float", "value": value}
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode()}
        return {"type": "text", "value": str(value)}

    @staticmethod
    def _cell(cell):
        kind = cell.get("type")
        if kind == "null":
            return None
        if kind == "integer":
            return int(cell["value"])
        if kind == "float":
            return float(cell["value"])
        if kind == "blob":
            return _b64decode(cell.get("base64"))
        return cell.get("value")

    # ---------- transport ----------
    def pipeline(self, statements):
        """Run statements [(sql, args), ...] in order on one stream.
        Returns a list of row-lists (one per statement)."""
        requests = []
        for sql, args in statements:
            requests.append({
                "type": "execute",
                "stmt": {"sql": sql, "args": [self._arg(a) for a in (args or ())]},
            })
        requests.append({"type": "close"})
        body = json.dumps({"requests": requests}).encode()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "HostingBot-Turso/2.0",
        }
        last_error = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(self.url + "/v2/pipeline", data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode()[:300]
                except Exception:
                    pass
                last_error = TursoError(f"HTTP {exc.code}: {detail}")
                if exc.code in (400, 401, 403, 404):  # not worth retrying
                    raise last_error
            except Exception as exc:  # network errors, timeouts
                last_error = TursoError(f"network error: {exc}")
            time.sleep(1.5 * (attempt + 1))
        else:
            raise last_error or TursoError("unknown Turso error")

        results = []
        for item in payload.get("results", [])[: len(statements)]:
            if item.get("type") == "error":
                raise TursoError(item.get("error", {}).get("message", "SQL error"))
            result = item.get("response", {}).get("result", {})
            results.append([tuple(self._cell(c) for c in row) for row in result.get("rows", [])])
        return results

    def execute(self, sql, args=()):
        return self.pipeline([(sql, args)])[0]


class Store:
    """Persistent storage used by the bot (state + project backups)."""

    CHUNK_BYTES = 384 * 1024          # raw bytes per row
    PIPELINE_BYTES = 1024 * 1024      # raw bytes per HTTP request

    def __init__(self, url, token):
        self.db = Turso(url, token)

    def init(self):
        self.db.pipeline([
            ("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER)", ()),
            ("CREATE TABLE IF NOT EXISTS project_archives ("
             "pid TEXT PRIMARY KEY, ver INTEGER NOT NULL, chunks INTEGER NOT NULL, size INTEGER NOT NULL, "
             "sha TEXT, updated_at INTEGER)", ()),
            ("CREATE TABLE IF NOT EXISTS project_chunks ("
             "pid TEXT NOT NULL, ver INTEGER NOT NULL, idx INTEGER NOT NULL, data BLOB NOT NULL, "
             "PRIMARY KEY (pid, ver, idx))", ()),
        ])

    # ---------- key/value ----------
    def get_kv(self, key):
        rows = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def set_kv(self, key, value):
        self.db.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, int(time.time())),
        )

    # ---------- project archives ----------
    def archive_info(self, pid):
        rows = self.db.execute("SELECT ver, chunks, size, sha, updated_at FROM project_archives WHERE pid = ?", (pid,))
        if not rows:
            return None
        ver, chunks, size, sha, updated = rows[0]
        return {"ver": ver, "chunks": chunks, "size": size, "sha": sha, "updated_at": updated}

    def list_archives(self):
        return {r[0]: r[1] for r in self.db.execute("SELECT pid, size FROM project_archives")}

    def save_archive(self, pid, blob):
        """Upload a zip as chunks under a new version, then switch the pointer.
        Readers always see either the old complete version or the new one."""
        ver = int(time.time() * 1000)
        pieces = [blob[i:i + self.CHUNK_BYTES] for i in range(0, len(blob), self.CHUNK_BYTES)] or [b""]
        batch, batch_bytes = [], 0
        for idx, piece in enumerate(pieces):
            batch.append(("INSERT INTO project_chunks (pid, ver, idx, data) VALUES (?, ?, ?, ?)", (pid, ver, idx, piece)))
            batch_bytes += len(piece)
            if batch_bytes >= self.PIPELINE_BYTES:
                self.db.pipeline(batch)
                batch, batch_bytes = [], 0
        if batch:
            self.db.pipeline(batch)
        sha = hashlib.sha256(blob).hexdigest()
        self.db.pipeline([
            ("INSERT INTO project_archives (pid, ver, chunks, size, sha, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
             "ON CONFLICT(pid) DO UPDATE SET ver = excluded.ver, chunks = excluded.chunks, size = excluded.size, "
             "sha = excluded.sha, updated_at = excluded.updated_at",
             (pid, ver, len(pieces), len(blob), sha, int(time.time()))),
            ("DELETE FROM project_chunks WHERE pid = ? AND ver <> ?", (pid, ver)),
        ])
        return len(blob)

    def load_archive(self, pid):
        info = self.archive_info(pid)
        if not info:
            return None
        parts = []
        step = 4  # rows per request keeps responses small
        for start in range(0, info["chunks"], step):
            rows = self.db.execute(
                "SELECT data FROM project_chunks WHERE pid = ? AND ver = ? AND idx >= ? AND idx < ? ORDER BY idx",
                (pid, info["ver"], start, start + step),
            )
            parts.extend(r[0] for r in rows)
        blob = b"".join(parts)
        if len(blob) != info["size"]:
            raise TursoError(f"archive for {pid} is incomplete ({len(blob)} of {info['size']} bytes)")
        if info.get("sha") and hashlib.sha256(blob).hexdigest() != info["sha"]:
            raise TursoError(f"archive for {pid} failed checksum")
        return blob

    def delete_archive(self, pid):
        self.db.pipeline([
            ("DELETE FROM project_chunks WHERE pid = ?", (pid,)),
            ("DELETE FROM project_archives WHERE pid = ?", (pid,)),
        ])
