from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from framework import F
from plugin import ModelBase

from .setup import P

db = F.db


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _json_load(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class ModelScanRun(ModelBase):
    P = P
    __tablename__ = "scan_run"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    status = db.Column(db.String(32), nullable=False, index=True)
    progress = db.Column(db.Integer, default=0, nullable=False)
    status_message = db.Column(db.String(512), default="")
    section_ids_json = db.Column(db.Text, default="[]")
    settings_snapshot_json = db.Column(db.Text, default="{}")
    server_machine_id = db.Column(db.String(128), default="")
    server_version = db.Column(db.String(64), default="")
    total_sections = db.Column(db.Integer, default=0)
    completed_sections = db.Column(db.Integer, default=0)
    total_groups = db.Column(db.Integer, default=0)
    safe_groups = db.Column(db.Integer, default=0)
    unsafe_groups = db.Column(db.Integer, default=0)
    successful_deletions = db.Column(db.Integer, default=0)
    deletion_attempts = db.Column(db.Integer, default=0, nullable=False)
    cancellation_requested = db.Column(db.Boolean, default=False)
    error_summary = db.Column(db.Text, default="")

    def as_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "status": self.status,
            "progress": self.progress or 0,
            "status_message": self.status_message or "",
            "section_ids": _json_load(self.section_ids_json, []),
            "server_machine_id": self.server_machine_id or "",
            "server_version": self.server_version or "",
            "total_sections": self.total_sections or 0,
            "completed_sections": self.completed_sections or 0,
            "total_groups": self.total_groups or 0,
            "safe_groups": self.safe_groups or 0,
            "unsafe_groups": self.unsafe_groups or 0,
            "successful_deletions": self.successful_deletions or 0,
            "deletion_attempts": self.deletion_attempts or 0,
            "cancellation_requested": bool(self.cancellation_requested),
            "error_summary": self.error_summary or "",
        }

    @classmethod
    def get(cls, run_id: Any) -> Optional["ModelScanRun"]:
        return db.session.query(cls).filter_by(id=int(run_id)).first()

    @classmethod
    def active(cls) -> Optional["ModelScanRun"]:
        return (
            db.session.query(cls)
            .filter(cls.status.in_(["queued", "running", "cancelling"]))
            .order_by(cls.id.desc())
            .first()
        )

    @classmethod
    def recent(cls, limit: int = 30) -> List["ModelScanRun"]:
        return db.session.query(cls).order_by(cls.id.desc()).limit(max(1, min(limit, 100))).all()

    @classmethod
    def claim_deletion_slot(cls, run_id: Any, limit: int) -> bool:
        """Atomically reserve one DELETE attempt across SQLite/MySQL workers."""
        updated = (
            db.session.query(cls)
            .filter(
                cls.id == int(run_id),
                cls.status.in_(["completed", "completed_with_warnings"]),
                cls.deletion_attempts < max(1, int(limit)),
            )
            .update(
                {cls.deletion_attempts: cls.deletion_attempts + 1},
                synchronize_session=False,
            )
        )
        return updated == 1


class ModelDuplicateGroup(ModelBase):
    P = P
    __tablename__ = "duplicate_group"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    section_key = db.Column(db.String(32), nullable=False)
    section_title = db.Column(db.String(255), default="")
    rating_key = db.Column(db.String(64), nullable=False)
    guid = db.Column(db.String(512), default="")
    media_type = db.Column(db.String(32), default="")
    title = db.Column(db.String(512), default="")
    year = db.Column(db.Integer)
    grandparent_title = db.Column(db.String(512), default="")
    grandparent_rating_key = db.Column(db.String(64), default="")
    parent_index = db.Column(db.Integer)
    media_index = db.Column(db.Integer)
    identity_fingerprint = db.Column(db.String(64), nullable=False)
    candidate_count = db.Column(db.Integer, default=0)
    safe_to_delete = db.Column(db.Boolean, default=False, index=True)
    safety_flags_json = db.Column(db.Text, default="[]")
    safety_details_json = db.Column(db.Text, default="{}")
    recommended_candidate_id = db.Column(db.Integer)
    resolution_status = db.Column(db.String(32), default="open")

    def as_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "created_at": _iso(self.created_at),
            "section_key": self.section_key,
            "section_title": self.section_title or "",
            "rating_key": self.rating_key,
            "guid": self.guid or "",
            "media_type": self.media_type or "",
            "title": self.title or "",
            "year": self.year,
            "grandparent_title": self.grandparent_title or "",
            "grandparent_rating_key": self.grandparent_rating_key or "",
            "parent_index": self.parent_index,
            "index": self.media_index,
            "identity_fingerprint": self.identity_fingerprint,
            "candidate_count": self.candidate_count or 0,
            "safe_to_delete": bool(self.safe_to_delete),
            "safety_flags": _json_load(self.safety_flags_json, []),
            "safety_details": _json_load(self.safety_details_json, {}),
            "recommended_candidate_id": self.recommended_candidate_id,
            "resolution_status": self.resolution_status or "open",
        }

    @classmethod
    def get(cls, group_id: Any) -> Optional["ModelDuplicateGroup"]:
        return db.session.query(cls).filter_by(id=int(group_id)).first()

    @classmethod
    def claim_for_delete(cls, group_id: Any) -> bool:
        """Atomically transition an eligible group to delete_in_progress."""
        updated = (
            db.session.query(cls)
            .filter(
                cls.id == int(group_id),
                cls.safe_to_delete == True,  # noqa: E712 - SQLAlchemy expression
                cls.resolution_status == "open",
            )
            .update(
                {
                    cls.safe_to_delete: False,
                    cls.resolution_status: "delete_in_progress",
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @classmethod
    def by_run(cls, run_id: Any, limit: int = 500) -> List["ModelDuplicateGroup"]:
        return (
            db.session.query(cls)
            .filter_by(run_id=int(run_id))
            .order_by(cls.id.asc())
            .limit(max(1, min(limit, 2000)))
            .all()
        )

    @classmethod
    def search(
        cls,
        run_id: Any,
        page: int = 1,
        page_size: int = 50,
        media_type: str = "",
        safety: str = "",
        keyword: str = "",
    ) -> Dict[str, Any]:
        query = db.session.query(cls).filter_by(run_id=int(run_id))
        if media_type in ("movie", "episode"):
            query = query.filter_by(media_type=media_type)
        if safety == "safe":
            query = query.filter_by(safe_to_delete=True)
        elif safety == "blocked":
            query = query.filter_by(safe_to_delete=False)
        keyword = (keyword or "").strip()
        if keyword:
            pattern = "%" + keyword + "%"
            query = query.filter(
                cls.title.like(pattern)
                | cls.grandparent_title.like(pattern)
                | cls.section_title.like(pattern)
                | cls.guid.like(pattern)
            )
        total = query.count()
        items = (
            query.order_by(cls.id.asc())
            .limit(page_size)
            .offset((page - 1) * page_size)
            .all()
        )
        return {"items": items, "total": total}


class ModelMediaCandidate(ModelBase):
    P = P
    __tablename__ = "media_candidate"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, nullable=False, index=True)
    media_id = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    duration = db.Column(db.Integer, default=0)
    bitrate = db.Column(db.Integer, default=0)
    width = db.Column(db.Integer, default=0)
    height = db.Column(db.Integer, default=0)
    video_resolution = db.Column(db.String(32), default="")
    video_codec = db.Column(db.String(32), default="")
    audio_codec = db.Column(db.String(32), default="")
    audio_channels = db.Column(db.Integer, default=0)
    container = db.Column(db.String(32), default="")
    total_size = db.Column(db.BigInteger, default=0)
    parts_json = db.Column(db.Text, default="[]")
    audio_tracks_json = db.Column(db.Text, default="[]")
    fingerprint = db.Column(db.String(64), nullable=False)
    score = db.Column(db.Float, default=0)
    score_breakdown_json = db.Column(db.Text, default="{}")
    deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime)

    def as_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "group_id": self.group_id,
            "media_id": self.media_id,
            "created_at": _iso(self.created_at),
            "duration": self.duration or 0,
            "bitrate": self.bitrate or 0,
            "width": self.width or 0,
            "height": self.height or 0,
            "video_resolution": self.video_resolution or "",
            "video_codec": self.video_codec or "",
            "audio_codec": self.audio_codec or "",
            "audio_channels": self.audio_channels or 0,
            "container": self.container or "",
            "total_size": self.total_size or 0,
            "parts": _json_load(self.parts_json, []),
            "audio_tracks": _json_load(self.audio_tracks_json, []),
            "fingerprint": self.fingerprint,
            "score": round(self.score or 0, 3),
            "score_breakdown": _json_load(self.score_breakdown_json, {}),
            "deleted": bool(self.deleted),
            "deleted_at": _iso(self.deleted_at),
        }

    @classmethod
    def get(cls, candidate_id: Any) -> Optional["ModelMediaCandidate"]:
        return db.session.query(cls).filter_by(id=int(candidate_id)).first()

    @classmethod
    def by_group(cls, group_id: Any, include_deleted: bool = True) -> List["ModelMediaCandidate"]:
        query = db.session.query(cls).filter_by(group_id=int(group_id))
        if not include_deleted:
            query = query.filter_by(deleted=False)
        return query.order_by(cls.score.desc(), cls.id.asc()).all()


class ModelActionLog(ModelBase):
    P = P
    __tablename__ = "action_log"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    run_id = db.Column(db.Integer, index=True)
    group_id = db.Column(db.Integer, index=True)
    candidate_id = db.Column(db.Integer)
    keep_candidate_id = db.Column(db.Integer)
    action = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(32), nullable=False)
    message = db.Column(db.Text, default="")
    response_status = db.Column(db.Integer)
    before_json = db.Column(db.Text, default="{}")
    after_json = db.Column(db.Text, default="{}")

    def as_api(self, include_snapshots: bool = True) -> Dict[str, Any]:
        value = {
            "id": self.id,
            "created_at": _iso(self.created_at),
            "run_id": self.run_id,
            "group_id": self.group_id,
            "candidate_id": self.candidate_id,
            "keep_candidate_id": self.keep_candidate_id,
            "action": self.action,
            "status": self.status,
            "message": self.message or "",
            "response_status": self.response_status,
        }
        if include_snapshots:
            value["before"] = _json_load(self.before_json, {})
            value["after"] = _json_load(self.after_json, {})
        return value

    @classmethod
    def get(cls, action_id: Any) -> Optional["ModelActionLog"]:
        return db.session.query(cls).filter_by(id=int(action_id)).first()

    @classmethod
    def recent(cls, limit: int = 100) -> List["ModelActionLog"]:
        return db.session.query(cls).order_by(cls.id.desc()).limit(max(1, min(limit, 500))).all()

    @classmethod
    def interrupted(cls) -> List["ModelActionLog"]:
        return (
            db.session.query(cls)
            .filter(cls.status.in_(["validating", "deleting"]))
            .order_by(cls.id.asc())
            .all()
        )

    @classmethod
    def search(
        cls,
        page: int = 1,
        page_size: int = 50,
        run_id: Optional[int] = None,
        status: str = "",
    ) -> Dict[str, Any]:
        query = db.session.query(cls)
        if run_id is not None:
            query = query.filter_by(run_id=int(run_id))
        if status:
            query = query.filter_by(status=status)
        total = query.count()
        items = (
            query.order_by(cls.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
            .all()
        )
        return {"items": items, "total": total}
