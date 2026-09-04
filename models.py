from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from framework import F
from plugin import ModelBase

from .setup import P


db = F.db

ACTIVE_RUN_STATUSES = frozenset(("queued", "running", "stopping"))


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _json_load(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class ModelCleanupRun(ModelBase):
    """One dry-run or live immediate-cleanup invocation."""

    P = P
    __tablename__ = "plex_dupefinder_ff_cleanup_run"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    mode = db.Column(db.String(16), nullable=False, index=True)
    status = db.Column(db.String(32), nullable=False, index=True)
    stop_requested = db.Column(db.Boolean, default=False, nullable=False)
    current_json = db.Column(db.Text, default="{}")
    settings_json = db.Column(db.Text, default="{}")
    processed_groups = db.Column(db.Integer, default=0, nullable=False)
    total_groups = db.Column(db.Integer, default=0, nullable=False)
    groups_found = db.Column(db.Integer, default=0, nullable=False)
    would_delete_count = db.Column(db.Integer, default=0, nullable=False)
    would_delete_bytes = db.Column(db.BigInteger, default=0, nullable=False)
    deleted_bytes = db.Column(db.BigInteger, default=0, nullable=False)
    deleted_count = db.Column(db.Integer, default=0, nullable=False)
    partial_count = db.Column(db.Integer, default=0, nullable=False)
    error_count = db.Column(db.Integer, default=0, nullable=False)
    status_message = db.Column(db.Text, default="")
    error_message = db.Column(db.Text, default="")

    @classmethod
    def create(cls, mode: str, settings: Dict[str, Any]) -> "ModelCleanupRun":
        item = cls()
        item.created_at = datetime.now()
        item.mode = str(mode)
        item.status = "queued"
        item.stop_requested = False
        item.current_json = "{}"
        item.settings_json = _json_dump(settings)
        item.processed_groups = 0
        item.total_groups = 0
        item.groups_found = 0
        item.would_delete_count = 0
        item.would_delete_bytes = 0
        item.deleted_bytes = 0
        item.deleted_count = 0
        item.partial_count = 0
        item.error_count = 0
        item.status_message = "대기"
        item.error_message = ""
        db.session.add(item)
        db.session.commit()
        return item

    def as_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode or "",
            "status": self.status or "",
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "stop_requested": bool(self.stop_requested),
            "current": _json_load(self.current_json, {}),
            "progress": {
                "processed": self.processed_groups or 0,
                "total": self.total_groups or 0,
            },
            "summary": {
                "groups": self.groups_found or 0,
                "would_delete": self.would_delete_count or 0,
                "bytes": self.deleted_bytes or 0,
                "would_delete_bytes": self.would_delete_bytes or 0,
                "deleted": self.deleted_count or 0,
                "partial": self.partial_count or 0,
                "errors": self.error_count or 0,
            },
            "message": self.error_message or self.status_message or "",
        }

    @classmethod
    def get(cls, run_id: Any) -> Optional["ModelCleanupRun"]:
        return db.session.query(cls).filter_by(id=int(run_id)).first()

    @classmethod
    def active(cls) -> Optional["ModelCleanupRun"]:
        return (
            db.session.query(cls)
            .filter(cls.status.in_(ACTIVE_RUN_STATUSES))
            .order_by(cls.id.desc())
            .first()
        )

    @classmethod
    def request_stop(cls, run_id: Any) -> bool:
        """Atomically mark an active run as stopping.

        The status predicate prevents a late stop request from overwriting a
        terminal status committed by the worker at the same time.
        """

        count = (
            db.session.query(cls)
            .filter(cls.id == int(run_id))
            .filter(cls.status.in_(ACTIVE_RUN_STATUSES))
            .update(
                {
                    cls.stop_requested: True,
                    cls.status: "stopping",
                    cls.status_message: "중지 요청됨",
                },
                synchronize_session=False,
            )
        )
        db.session.commit()
        return bool(count)

    @classmethod
    def latest(cls) -> Optional["ModelCleanupRun"]:
        return db.session.query(cls).order_by(cls.id.desc()).first()

    @classmethod
    def recent(cls, limit: int = 30) -> List["ModelCleanupRun"]:
        return (
            db.session.query(cls)
            .order_by(cls.id.desc())
            .limit(max(1, min(int(limit), 100)))
            .all()
        )

    @classmethod
    def recover_interrupted(cls) -> int:
        """Never resume an immediate-delete run whose process disappeared."""

        now = datetime.now()
        count = (
            db.session.query(cls)
            .filter(cls.status.in_(ACTIVE_RUN_STATUSES))
            .update(
                {
                    cls.status: "interrupted",
                    cls.finished_at: now,
                    cls.status_message: "플러그인 재시작으로 중단됨",
                },
                synchronize_session=False,
            )
        )
        return int(count or 0)


class ModelCleanupAction(ModelBase):
    """One candidate that was reported or sent to Plex DELETE."""

    P = P
    __tablename__ = "plex_dupefinder_ff_cleanup_action"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    finished_at = db.Column(db.DateTime)
    mode = db.Column(db.String(16), nullable=False, index=True)
    section_id = db.Column(db.String(64), default="", index=True)
    rating_key = db.Column(db.String(64), default="", index=True)
    media_type = db.Column(db.String(32), default="")
    title = db.Column(db.Text, default="")
    keep_media_id = db.Column(db.String(64), default="")
    delete_media_id = db.Column(db.String(64), default="")
    keep_score = db.Column(db.Float, default=0)
    delete_score = db.Column(db.Float, default=0)
    file_size = db.Column(db.BigInteger, default=0)
    file_path = db.Column(db.Text, default="")
    sidecars_json = db.Column(db.Text, default="[]")
    candidate_snapshot_json = db.Column(db.Text, default="{}")
    status = db.Column(db.String(32), nullable=False, index=True)
    response_status = db.Column(db.Integer)
    message = db.Column(db.Text, default="")

    @classmethod
    def create(cls, **values: Any) -> "ModelCleanupAction":
        item = cls()
        item.created_at = datetime.now()
        item.finished_at = None
        item.run_id = int(values.get("run_id"))
        item.mode = str(values.get("mode") or "")
        item.section_id = str(values.get("section_id") or "")
        item.rating_key = str(values.get("rating_key") or "")
        item.media_type = str(values.get("media_type") or "")
        item.title = str(values.get("title") or "")
        item.keep_media_id = str(values.get("keep_media_id") or "")
        item.delete_media_id = str(values.get("delete_media_id") or "")
        item.keep_score = float(values.get("keep_score") or 0)
        item.delete_score = float(values.get("delete_score") or 0)
        item.file_size = max(0, int(values.get("file_size") or 0))
        item.file_path = str(values.get("file_path") or "")
        item.sidecars_json = _json_dump(values.get("sidecars") or [])
        item.candidate_snapshot_json = _json_dump(values.get("candidate_snapshot") or {})
        item.status = str(values.get("status") or "discovered")
        item.response_status = values.get("response_status")
        item.message = str(values.get("message") or "")
        db.session.add(item)
        db.session.commit()
        return item

    def as_api(self, include_snapshot: bool = False) -> Dict[str, Any]:
        value = {
            "id": self.id,
            "run_id": self.run_id,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
            "mode": self.mode or "",
            "section_id": self.section_id or "",
            "rating_key": self.rating_key or "",
            "media_type": self.media_type or "",
            "title": self.title or "",
            "keep_media_id": self.keep_media_id or "",
            "delete_media_id": self.delete_media_id or "",
            "keep_score": self.keep_score or 0,
            "delete_score": self.delete_score or 0,
            "file_size": self.file_size or 0,
            "file_path": self.file_path or "",
            "sidecars": _json_load(self.sidecars_json, []),
            "status": self.status or "",
            "response_status": self.response_status,
            "message": self.message or "",
        }
        if include_snapshot:
            value["candidate_snapshot"] = _json_load(
                self.candidate_snapshot_json, {}
            )
        return value

    @classmethod
    def get(cls, action_id: Any) -> Optional["ModelCleanupAction"]:
        return db.session.query(cls).filter_by(id=int(action_id)).first()

    @classmethod
    def recent(
        cls, limit: int = 100, run_id: Optional[Any] = None
    ) -> List["ModelCleanupAction"]:
        query = db.session.query(cls)
        if run_id is not None:
            query = query.filter_by(run_id=int(run_id))
        return (
            query.order_by(cls.id.desc())
            .limit(max(1, min(int(limit), 500)))
            .all()
        )

    @classmethod
    def recover_interrupted(cls) -> int:
        """A DELETE in flight at process loss has an unknowable outcome."""

        now = datetime.now()
        count = (
            db.session.query(cls)
            .filter(cls.status == "deleting")
            .update(
                {
                    cls.status: "unknown",
                    cls.finished_at: now,
                    cls.message: "플러그인 재시작으로 DELETE 결과를 확인할 수 없음",
                },
                synchronize_session=False,
            )
        )
        return int(count or 0)


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ModelCleanupAction",
    "ModelCleanupRun",
]
