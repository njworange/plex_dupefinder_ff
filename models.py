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
    def safe_open_by_run(cls, run_id: Any) -> List["ModelDuplicateGroup"]:
        """Return batch-plan candidates in a deterministic order.

        Candidate-count and recommendation checks intentionally happen against
        active ``media_candidate`` rows in the batch planner, rather than trusting
        the denormalized snapshot columns on this table.
        """
        return (
            db.session.query(cls)
            .filter_by(
                run_id=int(run_id),
                safe_to_delete=True,
                resolution_status="open",
            )
            .order_by(cls.id.asc())
            .all()
        )

    @classmethod
    def all_by_run(cls, run_id: Any) -> List["ModelDuplicateGroup"]:
        return (
            db.session.query(cls)
            .filter_by(run_id=int(run_id))
            .order_by(cls.id.asc())
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
        try:
            journal = (
                ModelQuarantineJournal.for_action(self.id)
                if self.id is not None
                else None
            )
        except Exception:
            # Stub/unit-test DBs and a pre-create_all startup window must not
            # break legacy audit serialization.
            journal = None
        if journal is not None:
            cleanup = journal.cleanup_api(include_paths=include_snapshots)
            value["subtitle_cleanup"] = cleanup
            value["subtitle_cleanup_summary"] = journal.cleanup_api(False)
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
            .filter(cls.status.in_(["validating", "deleting", "quarantining"]))
            .order_by(cls.id.asc())
            .all()
        )

    @classmethod
    def active_delete(cls) -> Optional["ModelActionLog"]:
        return (
            db.session.query(cls)
            .filter(
                cls.action == "delete_media",
                cls.status.in_(["validating", "deleting", "quarantining"]),
            )
            .order_by(cls.id.asc())
            .first()
        )

    @classmethod
    def latest_for_delete(
        cls, run_id: Any, group_id: Any, candidate_id: Any
    ) -> Optional["ModelActionLog"]:
        return (
            db.session.query(cls)
            .filter_by(
                run_id=int(run_id),
                group_id=int(group_id),
                candidate_id=int(candidate_id),
                action="delete_media",
            )
            .order_by(cls.id.desc())
            .first()
        )

    @classmethod
    def search(
        cls,
        page: int = 1,
        page_size: int = 50,
        run_id: Optional[int] = None,
        status: str = "",
        subtitle_filter: str = "",
    ) -> Dict[str, Any]:
        query = db.session.query(cls)
        if run_id is not None:
            query = query.filter_by(run_id=int(run_id))
        if status:
            query = query.filter_by(status=status)
        if subtitle_filter == "excluded":
            query = query.join(
                ModelQuarantineJournal,
                ModelQuarantineJournal.action_log_id == cls.id,
            ).filter(ModelQuarantineJournal.excluded_count > 0)
        elif subtitle_filter == "quarantined":
            query = query.join(
                ModelQuarantineJournal,
                ModelQuarantineJournal.action_log_id == cls.id,
            ).filter(ModelQuarantineJournal.quarantined_count > 0)
        total = query.count()
        items = (
            query.order_by(cls.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
            .all()
        )
        return {"items": items, "total": total}


class ModelPostDeleteScanJob(ModelBase):
    """Durable, non-destructive scan work created by a confirmed DELETE.

    ``dedupe_key`` is scoped to an action for manual deletes and to a batch for
    batch deletes.  The latter lets several confirmed deletes in the same
    folder share one physical scan while retaining every ActionLog id in
    ``action_ids_json``.  ``lease_key`` is nullable/unique so SQLite and MySQL
    both enforce a single active scan worker across FlaskFarm processes.
    """

    P = P
    __tablename__ = "post_delete_scan_job"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    action_log_id = db.Column(db.Integer, nullable=False, index=True)
    action_ids_json = db.Column(db.Text, default="[]", nullable=False)
    batch_run_id = db.Column(db.Integer, index=True)
    run_id = db.Column(db.Integer, nullable=False, index=True)
    group_id = db.Column(db.Integer, nullable=False, index=True)
    candidate_id = db.Column(db.Integer, nullable=False)
    server_machine_id = db.Column(db.String(128), nullable=False)
    mode = db.Column(db.String(16), nullable=False)
    section_key = db.Column(db.String(32), nullable=False)
    media_type = db.Column(db.String(32), nullable=False)
    target_path = db.Column(db.Text, nullable=False)
    target_key = db.Column(db.String(64), nullable=False, index=True)
    dedupe_key = db.Column(db.String(64), nullable=False, unique=True)
    status = db.Column(db.String(32), nullable=False, index=True)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=3, nullable=False)
    next_attempt_at = db.Column(db.DateTime, nullable=False, index=True)
    response_status = db.Column(db.Integer)
    last_error = db.Column(db.Text, default="")
    # Cross-process worker internals.  None of these values is serialized.
    lease_key = db.Column(db.String(32), unique=True, nullable=True)
    worker_token = db.Column(db.String(128), default="", nullable=False)
    lease_expires_at = db.Column(db.DateTime)

    def as_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "action_id": self.action_log_id,
            "action_ids": _json_load(self.action_ids_json, []),
            "batch_id": self.batch_run_id,
            "run_id": self.run_id,
            "group_id": self.group_id,
            "candidate_id": self.candidate_id,
            "mode": self.mode,
            "section_key": self.section_key,
            "media_type": self.media_type,
            "target_path": self.target_path,
            "status": self.status,
            "attempts": self.attempts or 0,
            "max_attempts": self.max_attempts or 0,
            "next_attempt_at": _iso(self.next_attempt_at),
            "response_status": self.response_status,
            "last_error": self.last_error or "",
        }

    @classmethod
    def get(cls, job_id: Any) -> Optional["ModelPostDeleteScanJob"]:
        return db.session.query(cls).filter_by(id=int(job_id)).first()

    @classmethod
    def by_dedupe_key(cls, dedupe_key: str) -> Optional["ModelPostDeleteScanJob"]:
        return db.session.query(cls).filter_by(dedupe_key=str(dedupe_key)).first()

    @classmethod
    def recent(cls, limit: int = 100) -> List["ModelPostDeleteScanJob"]:
        return (
            db.session.query(cls)
            .order_by(cls.id.desc())
            .limit(max(1, min(int(limit), 500)))
            .all()
        )

    @classmethod
    def by_batch(cls, batch_id: Any) -> List["ModelPostDeleteScanJob"]:
        return (
            db.session.query(cls)
            .filter_by(batch_run_id=int(batch_id))
            .order_by(cls.id.asc())
            .all()
        )

    @classmethod
    def active_for_action(cls, action_id: Any) -> Optional["ModelPostDeleteScanJob"]:
        target = int(action_id)
        jobs = (
            db.session.query(cls)
            .filter(cls.status.in_(["queued", "running", "retry_wait"]))
            .order_by(cls.id.asc())
            .all()
        )
        for job in jobs:
            if int(job.action_log_id or 0) == target:
                return job
            values = _json_load(job.action_ids_json, [])
            for value in values if isinstance(values, list) else []:
                try:
                    if int(value) == target:
                        return job
                except (TypeError, ValueError):
                    continue
        return None

    @classmethod
    def eligible_next(cls, now: datetime) -> Optional["ModelPostDeleteScanJob"]:
        return (
            db.session.query(cls)
            .filter(
                cls.status.in_(["queued", "retry_wait"]),
                cls.next_attempt_at <= now,
            )
            .order_by(cls.id.asc())
            .first()
        )

    @classmethod
    def claim_for_worker(
        cls, job_id: Any, worker_token: str, now: datetime, lease_expires_at: datetime
    ) -> bool:
        updated = (
            db.session.query(cls)
            .filter(
                cls.id == int(job_id),
                cls.status.in_(["queued", "retry_wait"]),
                cls.next_attempt_at <= now,
            )
            .update(
                {
                    cls.status: "running",
                    cls.started_at: now,
                    cls.updated_at: now,
                    cls.attempts: cls.attempts + 1,
                    cls.lease_key: "global",
                    cls.worker_token: str(worker_token),
                    cls.lease_expires_at: lease_expires_at,
                    cls.last_error: "",
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @classmethod
    def stale_running(cls, now: datetime) -> List["ModelPostDeleteScanJob"]:
        return (
            db.session.query(cls)
            .filter(
                cls.status == "running",
                cls.lease_expires_at < now,
            )
            .order_by(cls.id.asc())
            .all()
        )

    @classmethod
    def recover_stale_one(cls, job_id: Any, worker_token: str, now: datetime) -> bool:
        job = cls.get(job_id)
        if job is None:
            return False
        terminal = int(job.attempts or 0) >= int(job.max_attempts or 3)
        values = {
            cls.status: "failed" if terminal else "retry_wait",
            cls.updated_at: now,
            cls.finished_at: now if terminal else None,
            cls.next_attempt_at: now,
            cls.last_error: "작업 worker 응답이 없어 만료 복구되었습니다.",
            cls.lease_key: None,
            cls.worker_token: "",
            cls.lease_expires_at: None,
        }
        updated = (
            db.session.query(cls)
            .filter(
                cls.id == int(job_id),
                cls.status == "running",
                cls.worker_token == str(worker_token),
                cls.lease_expires_at < now,
            )
            .update(values, synchronize_session=False)
        )
        return updated == 1


class ModelQuarantineJournal(ModelBase):
    """Durable manifest for the opt-in filesystem quarantine backend.

    A new table preserves v1.2 upgrade compatibility because FlaskFarm's
    ``create_all`` lifecycle does not ALTER existing plugin tables.
    """

    P = P
    __tablename__ = "quarantine_journal"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    finished_at = db.Column(db.DateTime)
    action_log_id = db.Column(db.Integer, index=True)
    batch_run_id = db.Column(db.Integer, index=True)
    run_id = db.Column(db.Integer, nullable=False, index=True)
    group_id = db.Column(db.Integer, nullable=False, index=True)
    candidate_id = db.Column(db.Integer, nullable=False)
    keep_candidate_id = db.Column(db.Integer, nullable=False)
    operation_key = db.Column(db.String(64), nullable=False, unique=True)
    status = db.Column(db.String(32), nullable=False, index=True)
    plan_digest = db.Column(db.String(64), nullable=False, index=True)
    manifest_json = db.Column(db.Text, default="{}", nullable=False)
    moved_json = db.Column(db.Text, default="[]", nullable=False)
    backups_json = db.Column(db.Text, default="[]", nullable=False)
    operation_path = db.Column(db.Text, default="")
    eligible_count = db.Column(db.Integer, default=0, nullable=False)
    excluded_count = db.Column(db.Integer, default=0, nullable=False, index=True)
    protected_count = db.Column(db.Integer, default=0, nullable=False)
    quarantined_count = db.Column(db.Integer, default=0, nullable=False, index=True)
    last_error = db.Column(db.Text, default="")

    def cleanup_api(self, include_paths: bool = True) -> Dict[str, Any]:
        manifest = _json_load(self.manifest_json, {})
        moved = _json_load(self.moved_json, [])
        eligible = manifest.get("eligible", []) if isinstance(manifest, dict) else []
        excluded = manifest.get("excluded", []) if isinstance(manifest, dict) else []
        moved_subtitles = {
            str(raw.get("source_path") or ""): str(
                raw.get("destination_path") or ""
            )
            for raw in moved
            if isinstance(raw, dict)
            and raw.get("kind") == "subtitle"
            and raw.get("source_path")
        } if isinstance(moved, list) else {}

        def public_entries(values: Any) -> List[Dict[str, Any]]:
            result: List[Dict[str, Any]] = []
            if not isinstance(values, list):
                return result
            for raw in values:
                if not isinstance(raw, dict):
                    continue
                path = str(raw.get("path") or raw.get("source_path") or "")
                result.append(
                    {
                        "path": path,
                        "source_path": path,
                        "reason": str(raw.get("reason") or ""),
                    }
                )
                destination = moved_subtitles.get(path, "")
                if destination:
                    result[-1]["quarantine_path"] = destination
            return result

        value: Dict[str, Any] = {
            "enabled": True,
            "backend": "quarantine",
            "status": self.status or "",
            "operation_id": self.operation_key,
            "eligible": public_entries(eligible) if include_paths else [],
            "excluded": public_entries(excluded) if include_paths else [],
            "counts": {
                "eligible": self.eligible_count or 0,
                "excluded": self.excluded_count or 0,
                "protected": self.protected_count or 0,
                "quarantined": self.quarantined_count or 0,
            },
        }
        if include_paths and self.operation_path:
            value["quarantine_dir"] = self.operation_path
        if self.last_error:
            value["message"] = self.last_error
        return value

    def as_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "finished_at": _iso(self.finished_at),
            "action_id": self.action_log_id,
            "batch_id": self.batch_run_id,
            "run_id": self.run_id,
            "group_id": self.group_id,
            "candidate_id": self.candidate_id,
            "keep_candidate_id": self.keep_candidate_id,
            "plan_digest": self.plan_digest,
            "subtitle_cleanup": self.cleanup_api(True),
        }

    @classmethod
    def get(cls, journal_id: Any) -> Optional["ModelQuarantineJournal"]:
        return db.session.query(cls).filter_by(id=int(journal_id)).first()

    @classmethod
    def for_action(cls, action_id: Any) -> Optional["ModelQuarantineJournal"]:
        return (
            db.session.query(cls)
            .filter_by(action_log_id=int(action_id))
            .order_by(cls.id.desc())
            .first()
        )

    @classmethod
    def for_plan(
        cls, run_id: Any, group_id: Any, candidate_id: Any, plan_digest: str
    ) -> Optional["ModelQuarantineJournal"]:
        return (
            db.session.query(cls)
            .filter_by(
                run_id=int(run_id),
                group_id=int(group_id),
                candidate_id=int(candidate_id),
                plan_digest=str(plan_digest),
            )
            .order_by(cls.id.desc())
            .first()
        )

    @classmethod
    def for_batch_candidate(
        cls, batch_id: Any, candidate_id: Any, status: str = ""
    ) -> Optional["ModelQuarantineJournal"]:
        query = db.session.query(cls).filter_by(
            batch_run_id=int(batch_id), candidate_id=int(candidate_id)
        )
        if status:
            query = query.filter_by(status=str(status))
        return query.order_by(cls.id.desc()).first()

    @classmethod
    def unfinished(cls) -> List["ModelQuarantineJournal"]:
        return (
            db.session.query(cls)
            .filter(
                cls.status.in_(
                    [
                        "planned",
                        "backing_up",
                        "quarantining",
                        "quarantined_pending_scan",
                        "scan_running",
                    ]
                )
            )
            .order_by(cls.id.asc())
            .all()
        )


class ModelBatchRun(ModelBase):
    P = P
    __tablename__ = "batch_run"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    scan_run_id = db.Column(db.Integer, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    approved_at = db.Column(db.DateTime)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(32), nullable=False, index=True)
    # A nullable unique value provides a cross-process/global batch lease on
    # both SQLite and MySQL. Preview rows keep NULL; only an approved batch owns
    # the literal ``global`` value until it reaches a terminal state.
    lease_key = db.Column(db.String(32), unique=True, nullable=True)
    deletion_lease_token = db.Column(db.String(128), default="")
    confirmation = db.Column(db.String(128), default="")
    # Only a SHA-256 digest is persisted. The raw nonce exists in the user's
    # signed Flask session for the short preview window and is never stored here.
    nonce_hash = db.Column(db.String(64), default="")
    total_items = db.Column(db.Integer, default=0, nullable=False)
    processed_items = db.Column(db.Integer, default=0, nullable=False)
    succeeded_items = db.Column(db.Integer, default=0, nullable=False)
    failed_items = db.Column(db.Integer, default=0, nullable=False)
    skipped_items = db.Column(db.Integer, default=0, nullable=False)
    cancellation_requested = db.Column(db.Boolean, default=False, nullable=False)
    current_message = db.Column(db.String(512), default="")
    error_summary = db.Column(db.Text, default="")

    def as_api(self) -> Dict[str, Any]:
        value = {
            "plan_id": self.id,
            "run_id": self.scan_run_id,
            "created_at": _iso(self.created_at),
            "approved_at": _iso(self.approved_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "expires_at": _iso(self.expires_at),
            "status": self.status,
            "total": self.total_items or 0,
            "processed": self.processed_items or 0,
            "succeeded": self.succeeded_items or 0,
            "failed": self.failed_items or 0,
            "skipped": self.skipped_items or 0,
            "cancel_requested": bool(self.cancellation_requested),
            "current_message": self.current_message or "",
            "error_summary": self.error_summary or "",
        }
        return value

    @classmethod
    def get(cls, batch_id: Any) -> Optional["ModelBatchRun"]:
        return db.session.query(cls).filter_by(id=int(batch_id)).first()

    @classmethod
    def active(cls) -> Optional["ModelBatchRun"]:
        return (
            db.session.query(cls)
            .filter(cls.status.in_(["queued", "running", "cancelling"]))
            .order_by(cls.id.desc())
            .first()
        )

    @classmethod
    def latest_for_scan(cls, run_id: Any) -> Optional["ModelBatchRun"]:
        return (
            db.session.query(cls)
            .filter_by(scan_run_id=int(run_id))
            .order_by(cls.id.desc())
            .first()
        )

    @classmethod
    def claim_for_approval(
        cls,
        batch_id: Any,
        nonce_hash: str,
        deletion_lease_token: str,
        now: datetime,
    ) -> bool:
        updated = (
            db.session.query(cls)
            .filter(
                cls.id == int(batch_id),
                cls.status == "preview",
                cls.expires_at >= now,
                cls.nonce_hash == str(nonce_hash),
            )
            .update(
                {
                    cls.status: "queued",
                    cls.approved_at: now,
                    cls.current_message: "승인됨 · 백그라운드 작업 대기 중",
                    cls.nonce_hash: "",
                    cls.lease_key: "global",
                    cls.deletion_lease_token: str(deletion_lease_token),
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @classmethod
    def claim_for_worker(cls, batch_id: Any, now: datetime) -> bool:
        updated = (
            db.session.query(cls)
            .filter(cls.id == int(batch_id), cls.status == "queued")
            .update(
                {
                    cls.status: "running",
                    cls.started_at: now,
                    cls.current_message: "일괄 승인 삭제 시작",
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @classmethod
    def unfinished(cls) -> List["ModelBatchRun"]:
        return (
            db.session.query(cls)
            .filter(cls.status.in_(["queued", "running", "cancelling"]))
            .order_by(cls.id.asc())
            .all()
        )


class ModelBatchItem(ModelBase):
    P = P
    __tablename__ = "batch_item"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    batch_run_id = db.Column(db.Integer, nullable=False, index=True)
    scan_run_id = db.Column(db.Integer, nullable=False, index=True)
    group_id = db.Column(db.Integer, nullable=False, index=True)
    keep_candidate_id = db.Column(db.Integer, nullable=False)
    delete_candidate_id = db.Column(db.Integer, nullable=False)
    action_log_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    status = db.Column(db.String(32), nullable=False, index=True)
    message = db.Column(db.Text, default="")
    title = db.Column(db.String(512), default="")
    media_type = db.Column(db.String(32), default="")
    keep_media_id = db.Column(db.String(64), nullable=False)
    delete_media_id = db.Column(db.String(64), nullable=False)
    keep_score = db.Column(db.Float, default=0)
    delete_score = db.Column(db.Float, default=0)
    keep_paths_json = db.Column(db.Text, default="[]")
    delete_paths_json = db.Column(db.Text, default="[]")

    def as_api(self) -> Dict[str, Any]:
        value = {
            "id": self.id,
            "plan_id": self.batch_run_id,
            "run_id": self.scan_run_id,
            "group_id": self.group_id,
            "title": self.title or "",
            "media_type": self.media_type or "",
            "keep": {
                "candidate_id": self.keep_candidate_id,
                "media_id": self.keep_media_id,
                "score": round(self.keep_score or 0, 3),
                "paths": _json_load(self.keep_paths_json, []),
            },
            "delete": {
                "candidate_id": self.delete_candidate_id,
                "media_id": self.delete_media_id,
                "score": round(self.delete_score or 0, 3),
                "paths": _json_load(self.delete_paths_json, []),
            },
            "status": self.status,
            "message": self.message or "",
            "action_id": self.action_log_id,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }
        try:
            journal = ModelQuarantineJournal.for_batch_candidate(
                self.batch_run_id, self.delete_candidate_id
            )
        except Exception:
            # Serialization of a legacy v1.2 row must remain available even if
            # the new journal table has not yet been created in this process.
            journal = None
        if journal is not None:
            value["plan_digest"] = journal.plan_digest
            value["subtitle_cleanup"] = journal.cleanup_api(True)
        return value

    @classmethod
    def get(cls, item_id: Any) -> Optional["ModelBatchItem"]:
        return db.session.query(cls).filter_by(id=int(item_id)).first()

    @classmethod
    def by_batch(cls, batch_id: Any) -> List["ModelBatchItem"]:
        return (
            db.session.query(cls)
            .filter_by(batch_run_id=int(batch_id))
            .order_by(cls.id.asc())
            .all()
        )

    @classmethod
    def claim_for_worker(cls, item_id: Any, now: datetime) -> bool:
        updated = (
            db.session.query(cls)
            .filter(cls.id == int(item_id), cls.status == "planned")
            .update(
                {
                    cls.status: "running",
                    cls.started_at: now,
                    cls.message: "삭제 전 재검증 중",
                },
                synchronize_session=False,
            )
        )
        return updated == 1


class ModelDeletionLease(ModelBase):
    """Singleton cross-process mutex for every Plex deletion transaction."""

    P = P
    __tablename__ = "deletion_lease"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    owner_token = db.Column(db.String(128), default="", nullable=False)
    owner_kind = db.Column(db.String(32), default="", nullable=False)
    owner_ref = db.Column(db.String(128), default="", nullable=False)
    acquired_at = db.Column(db.DateTime)
    heartbeat_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime)

    @classmethod
    def get_singleton(cls) -> Optional["ModelDeletionLease"]:
        return db.session.query(cls).filter_by(id=1).first()

    @classmethod
    def claim_free(
        cls,
        owner_token: str,
        owner_kind: str,
        owner_ref: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        updated = (
            db.session.query(cls)
            .filter(cls.id == 1, cls.owner_token == "")
            .update(
                {
                    cls.owner_token: owner_token,
                    cls.owner_kind: owner_kind,
                    cls.owner_ref: owner_ref,
                    cls.acquired_at: now,
                    cls.heartbeat_at: now,
                    cls.expires_at: expires_at,
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @classmethod
    def renew(
        cls,
        owner_token: str,
        owner_kind: str,
        owner_ref: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        updated = (
            db.session.query(cls)
            .filter(
                cls.id == 1,
                cls.owner_token == owner_token,
                cls.owner_kind == owner_kind,
                cls.owner_ref == owner_ref,
                cls.expires_at >= now,
            )
            .update(
                {cls.heartbeat_at: now, cls.expires_at: expires_at},
                synchronize_session=False,
            )
        )
        return updated == 1

    @classmethod
    def release(cls, owner_token: str) -> bool:
        updated = (
            db.session.query(cls)
            .filter(cls.id == 1, cls.owner_token == owner_token)
            .update(
                {
                    cls.owner_token: "",
                    cls.owner_kind: "",
                    cls.owner_ref: "",
                    cls.acquired_at: None,
                    cls.heartbeat_at: None,
                    cls.expires_at: None,
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @classmethod
    def claim_expired_for_recovery(
        cls,
        previous_owner_token: str,
        recovery_token: str,
        owner_ref: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        updated = (
            db.session.query(cls)
            .filter(
                cls.id == 1,
                cls.owner_token == previous_owner_token,
                cls.owner_token != "",
                cls.expires_at < now,
            )
            .update(
                {
                    cls.owner_token: recovery_token,
                    cls.owner_kind: "recovery",
                    cls.owner_ref: owner_ref,
                    cls.acquired_at: now,
                    cls.heartbeat_at: now,
                    cls.expires_at: expires_at,
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @classmethod
    def clear_expired_owner(
        cls, owner_kind: str, owner_ref: str, now: datetime
    ) -> bool:
        """Release only the named expired owner; never steal a live lease."""

        updated = (
            db.session.query(cls)
            .filter(
                cls.id == 1,
                cls.owner_token != "",
                cls.owner_kind == str(owner_kind),
                cls.owner_ref == str(owner_ref),
                cls.expires_at < now,
            )
            .update(
                {
                    cls.owner_token: "",
                    cls.owner_kind: "",
                    cls.owner_ref: "",
                    cls.acquired_at: None,
                    cls.heartbeat_at: None,
                    cls.expires_at: None,
                },
                synchronize_session=False,
            )
        )
        return updated == 1
