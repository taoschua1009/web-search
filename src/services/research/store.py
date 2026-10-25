"""Owner-scoped SQLite records and transactional approval/action claims."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from src.schemas.errors import ResearchError
from src.schemas.research import Fact, KnowledgeEdit
from src.services.research.utils import canonical, digest, utcnow


class Store:
    """SQLite store for batches, runs, reports, company knowledge and approval actions."""

    def __init__(self, path: str):
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            target, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.path = str(target)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT NOT NULL, id TEXT NOT NULL, owner TEXT NOT NULL,
                    data TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(kind,id)
                );
                CREATE INDEX IF NOT EXISTS objects_owner ON objects(owner,kind,updated);
                CREATE TABLE IF NOT EXISTS submissions (
                    owner TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    batch_id TEXT NOT NULL, PRIMARY KEY(owner,request_key)
                );
                CREATE TABLE IF NOT EXISTS companies (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, domain TEXT NOT NULL,
                    version INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE(owner,domain)
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    company_id TEXT NOT NULL, owner TEXT NOT NULL, version INTEGER NOT NULL,
                    actor TEXT NOT NULL, changed_at TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(company_id,version)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, version INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL, state TEXT NOT NULL, execution TEXT NOT NULL,
                    expires REAL NOT NULL, data TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)
        self.recover()

    @contextmanager
    def connection(self):
        """A transactional connection that always closes."""
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        """Fail interrupted jobs; an uncertain external write must never be replayed."""
        with self.connection() as db:
            db.execute(
                "UPDATE actions SET execution='outcome_unknown' WHERE execution='executing'"
            )
            rows = db.execute(
                "SELECT kind,id,data FROM objects WHERE kind IN ('batch','run')"
            ).fetchall()
            for row in rows:
                value = json.loads(row["data"])
                if value.get("status") in {"running", "queued"}:
                    value.update(
                        status="failed",
                        error={
                            "code": "INTERRUPTED",
                            "message": "Tác vụ bị gián đoạn. Hãy tạo lượt nghiên cứu mới.",
                        },
                    )
                    db.execute(
                        "UPDATE objects SET data=? WHERE kind=? AND id=?",
                        (canonical(value), row["kind"], row["id"]),
                    )

    def put(self, kind: str, owner: str, record_id: str, data: dict):
        """Insert or replace one owner-scoped record."""
        value = dict(data, id=record_id)
        with self.connection() as db:
            db.execute(
                """INSERT INTO objects VALUES (?,?,?,?,?)
                ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated
                WHERE objects.owner=excluded.owner""",
                (kind, record_id, owner, canonical(value), time.time()),
            )
        return value

    def get(self, kind: str, owner: str, record_id: str) -> dict:
        """One owner-scoped record, or NOT_FOUND."""
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM objects WHERE kind=? AND owner=? AND id=?",
                (kind, owner, record_id),
            ).fetchone()
        if not row:
            raise ResearchError("NOT_FOUND", "Không tìm thấy dữ liệu.", 404)
        return json.loads(row["data"])

    def list(self, kind: str, owner: str, limit: int = 100) -> list[dict]:
        """The owner's most recent records of a kind."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT data FROM objects WHERE kind=? AND owner=? ORDER BY updated DESC LIMIT ?",
                (kind, owner, limit),
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def create_batch(self, owner: str, key: str, request: dict, mode: str):
        """Create a batch, or return the existing one for the same idempotency key."""
        signature = digest({"request": request, "mode": mode})
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM submissions WHERE owner=? AND request_key=?",
                (owner, key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != signature:
                    raise ResearchError(
                        "IDEMPOTENCY_CONFLICT",
                        "Mã yêu cầu đã dùng với nội dung khác.",
                        409,
                    )
                row = db.execute(
                    "SELECT data FROM objects WHERE kind='batch' AND id=? AND owner=?",
                    (previous["batch_id"], owner),
                ).fetchone()
                return json.loads(row["data"]), False
            active = db.execute(
                "SELECT data FROM objects WHERE kind='batch' AND owner=?", (owner,)
            ).fetchall()
            if (
                sum(
                    json.loads(row["data"]).get("status") in {"queued", "running"}
                    for row in active
                )
                >= 3
            ):
                raise ResearchError(
                    "RATE_LIMIT",
                    "Đang có ba lượt nghiên cứu. Chờ một lượt hoàn thành trước khi tạo tiếp.",
                    429,
                )
            batch = {
                "id": str(uuid.uuid4()),
                "status": "queued",
                "mode": mode,
                "created_at": utcnow(),
                "runs": [],
                "unresolved": [],
                "errors": [],
            }
            db.execute(
                "INSERT INTO objects VALUES ('batch',?,?,?,?)",
                (batch["id"], owner, canonical(batch), time.time()),
            )
            db.execute(
                "INSERT INTO submissions VALUES (?,?,?,?)",
                (owner, key, signature, batch["id"]),
            )
            return batch, True

    def company(self, owner: str, domain: str):
        """Stored knowledge for a domain, if any."""
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM companies WHERE owner=? AND domain=?", (owner, domain)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def get_company(self, owner: str, company_id: str):
        """Stored knowledge by id, or NOT_FOUND."""
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM companies WHERE owner=? AND id=?", (owner, company_id)
            ).fetchone()
        if not row:
            raise ResearchError("NOT_FOUND", "Không tìm thấy công ty.", 404)
        return json.loads(row["data"])

    def save_company(
        self,
        owner: str,
        domain: str,
        name: str,
        facts: list,
        sources: list,
        research_scope: dict | None = None,
    ):
        """Merge new facts and sources into the company's next version."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM companies WHERE owner=? AND domain=?", (owner, domain)
            ).fetchone()
            previous = json.loads(row["data"]) if row else {}
            # Retain dated earlier evidence and explicit user edits; do not rewrite history.
            merged_facts = {item["id"]: item for item in previous.get("facts", [])}
            merged_facts.update({item["id"]: item for item in facts})
            merged_sources = {item["id"]: item for item in previous.get("sources", [])}
            merged_sources.update({item["id"]: item for item in sources})
            value = {
                "id": previous.get("id", str(uuid.uuid4())),
                "domain": domain,
                "name": name,
                "version": previous.get("version", 0) + 1,
                "facts": list(merged_facts.values()),
                "sources": list(merged_sources.values()),
                "updated_at": utcnow(),
                "refreshed_at": utcnow() if facts else previous.get("refreshed_at"),
                "research_scope": research_scope
                if research_scope is not None
                else previous.get("research_scope"),
            }
            self._write_company(db, owner, value, "research")
            return value

    @staticmethod
    def _write_company(db, owner, value, actor):
        db.execute(
            """INSERT INTO companies VALUES (?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET version=excluded.version,data=excluded.data
            WHERE companies.owner=excluded.owner""",
            (value["id"], owner, value["domain"], value["version"], canonical(value)),
        )
        db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?)",
            (value["id"], owner, value["version"], actor, utcnow(), canonical(value)),
        )

    def edit_company(self, owner: str, company_id: str, edit: KnowledgeEdit):
        """Add a user-provided fact; fails if the version moved on."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM companies WHERE id=? AND owner=?", (company_id, owner)
            ).fetchone()
            if not row:
                raise ResearchError("NOT_FOUND", "Không tìm thấy công ty.", 404)
            value = json.loads(row["data"])
            if value["version"] != edit.expected_version:
                raise ResearchError(
                    "VERSION_CONFLICT",
                    "Tri thức đã thay đổi. Tải lại trước khi chỉnh sửa.",
                    409,
                )
            source_id = "user-" + str(uuid.uuid4())
            value["sources"].append(
                {
                    "id": source_id,
                    "url": edit.source_url,
                    "title": "Người dùng bổ sung — chưa xác minh độc lập",
                    "kind": "user",
                    "retrieved_at": utcnow(),
                    "published_at": None,
                    "text": edit.text,
                    "access": "user_provided",
                    "demo": False,
                }
            )
            fact = Fact(
                id="user-" + edit.category,
                category=edit.category,
                text=edit.text,
                source_ids=[source_id],
                provenance="user",
                status="user_provided",
            ).model_dump()
            value["facts"] = [
                item for item in value["facts"] if item["id"] != fact["id"]
            ] + [fact]
            value.update(version=value["version"] + 1, updated_at=utcnow())
            self._write_company(db, owner, value, owner)
            return value

    def history(self, owner: str, company_id: str):
        """All stored versions of a company's knowledge."""
        self.get_company(owner, company_id)
        with self.connection() as db:
            rows = db.execute(
                "SELECT version,actor,changed_at,data FROM revisions WHERE owner=? AND company_id=? ORDER BY version DESC LIMIT 100",
                (owner, company_id),
            ).fetchall()
        return [
            {
                "version": row["version"],
                "actor": row["actor"],
                "changed_at": row["changed_at"],
                "record": json.loads(row["data"]),
            }
            for row in rows
        ]

    def propose(self, owner: str, kind: str, payload: dict, metadata: dict, ttl: int):
        """Create a pending send/trash action that expires unless approved."""
        action = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "payload": payload,
            "metadata": metadata,
            "created_at": utcnow(),
            "decided_at": None,
            "result": None,
            "error": None,
        }
        signature = digest(
            {
                "owner": owner,
                "kind": kind,
                "payload": payload,
                "metadata": metadata,
                "version": 1,
            }
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM actions WHERE owner=? AND payload_hash=? AND (state='approved' OR (state='pending' AND expires>?)) ORDER BY rowid DESC LIMIT 1",
                (owner, signature, time.time()),
            ).fetchone()
            if existing:
                return self._decode_action(existing)
            db.execute(
                "INSERT INTO actions VALUES (?,?,1,?,'pending','not_started',?,?)",
                (action["id"], owner, signature, time.time() + ttl, canonical(action)),
            )
        return self.action(owner, action["id"])

    @staticmethod
    def _decode_action(row):
        value = json.loads(row["data"])
        value.update(
            version=row["version"],
            payload_hash=row["payload_hash"],
            state=row["state"],
            execution=row["execution"],
            expires_at=row["expires"],
        )
        if value["state"] == "pending" and value["expires_at"] <= time.time():
            value["state"] = "expired"
        return value

    def action(self, owner: str, action_id: str):
        """One action, or NOT_FOUND."""
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM actions WHERE owner=? AND id=?", (owner, action_id)
            ).fetchone()
        if not row:
            raise ResearchError("NOT_FOUND", "Không tìm thấy yêu cầu phê duyệt.", 404)
        return self._decode_action(row)

    def actions(self, owner: str):
        """The owner's actions, newest first."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM actions WHERE owner=? ORDER BY rowid DESC LIMIT 100",
                (owner,),
            ).fetchall()
        return [self._decode_action(row) for row in rows]

    def edit_action(self, owner: str, action_id: str, version: int, payload: dict):
        """Replace a pending action's payload as a new version."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM actions WHERE owner=? AND id=?", (owner, action_id)
            ).fetchone()
            if not row:
                raise ResearchError("NOT_FOUND", "Không tìm thấy yêu cầu.", 404)
            action = self._decode_action(row)
            if (
                action["version"] != version
                or action["state"] != "pending"
                or action["kind"] != "send_email"
            ):
                raise ResearchError(
                    "ACTION_CHANGED",
                    "Yêu cầu đã thay đổi hoặc không còn sửa được.",
                    409,
                )
            action["payload"].update(payload)
            signature = digest(
                {
                    "owner": owner,
                    "kind": action["kind"],
                    "payload": action["payload"],
                    "metadata": action["metadata"],
                    "version": version + 1,
                }
            )
            for key in ("version", "payload_hash", "state", "execution", "expires_at"):
                action.pop(key, None)
            db.execute(
                "UPDATE actions SET version=?,payload_hash=?,data=? WHERE id=? AND owner=?",
                (version + 1, signature, canonical(action), action_id, owner),
            )
        return self.action(owner, action_id)

    def claim(
        self, owner: str, action_id: str, decision: str, version: int, signature: str
    ):
        """Atomically claim an action for execution or rejection; replays do nothing."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM actions WHERE owner=? AND id=?", (owner, action_id)
            ).fetchone()
            if not row:
                raise ResearchError("NOT_FOUND", "Không tìm thấy yêu cầu.", 404)
            action = self._decode_action(row)
            if action["version"] != version or action["payload_hash"] != signature:
                raise ResearchError(
                    "ACTION_CHANGED", "Nội dung đã thay đổi. Hãy xem và duyệt lại.", 409
                )
            if action["state"] == "expired":
                raise ResearchError(
                    "APPROVAL_EXPIRED", "Yêu cầu đã hết hạn. Hãy tạo bản nháp mới.", 409
                )
            if action["state"] != "pending":
                if decision == "approve" and action["state"] == "approved":
                    return action, False
                if decision == "reject" and action["state"] == "rejected":
                    return action, False
                raise ResearchError(
                    "ACTION_ALREADY_PROCESSED", "Yêu cầu đã được xử lý.", 409
                )
            action["decided_at"] = utcnow()
            db.execute(
                "UPDATE actions SET state=?,execution=?,data=? WHERE owner=? AND id=?",
                (
                    "approved" if decision == "approve" else "rejected",
                    "executing" if decision == "approve" else "not_started",
                    canonical(action),
                    owner,
                    action_id,
                ),
            )
        return self.action(owner, action_id), decision == "approve"

    def finish(
        self, owner: str, action_id: str, execution: str, result=None, error=None
    ):
        """Record the execution outcome of a claimed action."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM actions WHERE owner=? AND id=?", (owner, action_id)
            ).fetchone()
            if not row or row["execution"] != "executing":
                raise ResearchError(
                    "ACTION_ALREADY_PROCESSED",
                    "Không thể ghi đè trạng thái hành động.",
                    409,
                )
            value = json.loads(row["data"])
            value.update(result=result, error=error, finished_at=utcnow())
            db.execute(
                "UPDATE actions SET execution=?,data=? WHERE owner=? AND id=?",
                (execution, canonical(value), owner, action_id),
            )
        return self.action(owner, action_id)
