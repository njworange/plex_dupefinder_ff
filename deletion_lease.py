from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from framework import F

from .models import ModelDeletionLease
from .setup import P


class DeletionLeaseError(RuntimeError):
    """Sanitized public error for the internal deletion mutex."""


class DeletionLeaseBusy(DeletionLeaseError):
    pass


class DeletionLeaseLost(DeletionLeaseError):
    pass


@dataclass(frozen=True)
class RecoveryLeaseClaim:
    token: str
    previous_kind: str
    previous_ref: str
    previous_expired: bool


def _lease_seconds() -> int:
    try:
        request_timeout = max(
            5, min(120, int(P.ModelSetting.get("setting_request_timeout") or "20"))
        )
    except (TypeError, ValueError):
        request_timeout = 20
    # A single item performs identity/read/DELETE/post-read calls. Even at the
    # maximum configured request timeout this 20-minute minimum leaves ample
    # room; a batch renews before every item.
    return max(20 * 60, (request_timeout + 5) * 6)


def _expiry(now: datetime) -> datetime:
    return now + timedelta(seconds=_lease_seconds())


def _rollback_quietly() -> None:
    """Never let a rollback driver error expose SQL parameters to callers."""
    try:
        F.db.session.rollback()
    except Exception:
        pass


class DeletionLeaseService:
    @staticmethod
    def _ensure_row() -> None:
        with F.app.app_context():
            try:
                if ModelDeletionLease.get_singleton() is not None:
                    return
                F.db.session.add(
                    ModelDeletionLease(
                        id=1,
                        owner_token="",
                        owner_kind="",
                        owner_ref="",
                    )
                )
                F.db.session.commit()
            except Exception:
                # Concurrent initialization is expected on multi-worker startup.
                _rollback_quietly()
                try:
                    if ModelDeletionLease.get_singleton() is not None:
                        return
                except Exception:
                    _rollback_quietly()
                    raise DeletionLeaseError(
                        "삭제 전역 잠금을 초기화할 수 없습니다."
                    ) from None
                raise DeletionLeaseError(
                    "삭제 전역 잠금을 초기화할 수 없습니다."
                ) from None

    def acquire(self, owner_kind: str, owner_ref: str) -> str:
        self._ensure_row()
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        with F.app.app_context():
            try:
                if ModelDeletionLease.claim_free(
                    token,
                    str(owner_kind),
                    str(owner_ref),
                    now,
                    _expiry(now),
                ):
                    F.db.session.commit()
                    return token
                F.db.session.rollback()
                current = ModelDeletionLease.get_singleton()
                if current is None:
                    raise DeletionLeaseError(
                        "삭제 전역 잠금을 확보할 수 없습니다."
                    )
                expired = bool(current.expires_at and current.expires_at < now)
                suffix = " 만료 복구가 필요합니다." if expired else ""
                raise DeletionLeaseBusy(
                    "다른 삭제 작업이 전역 잠금을 보유 중입니다.%s" % suffix
                )
            except DeletionLeaseError:
                _rollback_quietly()
                raise
            except Exception:
                _rollback_quietly()
                raise DeletionLeaseError(
                    "삭제 전역 잠금을 확보할 수 없습니다."
                ) from None

    def renew(self, token: str, owner_kind: str, owner_ref: str) -> None:
        now = datetime.now()
        with F.app.app_context():
            try:
                if not ModelDeletionLease.renew(
                    str(token),
                    str(owner_kind),
                    str(owner_ref),
                    now,
                    _expiry(now),
                ):
                    _rollback_quietly()
                    raise DeletionLeaseLost(
                        "삭제 전역 잠금을 잃었거나 만료되었습니다. 작업을 중단합니다."
                    )
                F.db.session.commit()
            except DeletionLeaseLost:
                _rollback_quietly()
                raise
            except Exception:
                # A driver/connection error means ownership cannot be proven.
                # Treat it exactly like a lost lease so no old worker mutates
                # audit or batch state, and never expose SQL parameters/token.
                _rollback_quietly()
                raise DeletionLeaseLost(
                    "삭제 전역 잠금 확인에 실패했습니다. 작업을 중단합니다."
                ) from None

    def release(self, token: str) -> bool:
        if not token:
            return False
        with F.app.app_context():
            try:
                released = ModelDeletionLease.release(str(token))
                F.db.session.commit()
                return released
            except Exception:
                _rollback_quietly()
                raise DeletionLeaseError(
                    "삭제 전역 잠금을 해제할 수 없습니다."
                ) from None

    def acquire_for_recovery(self) -> Optional[RecoveryLeaseClaim]:
        """CAS-acquire the singleton before any interrupted-state mutation.

        A currently valid owner is always treated as another live web worker.
        Only an empty row or an expired owner can be claimed for recovery.
        """
        self._ensure_row()
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        with F.app.app_context():
            try:
                lease = ModelDeletionLease.get_singleton()
                if lease is None:
                    raise DeletionLeaseError(
                        "삭제 복구 잠금을 확보할 수 없습니다."
                    )
                previous_kind = lease.owner_kind or ""
                previous_ref = lease.owner_ref or ""
                previous_token = lease.owner_token or ""
                if not previous_token:
                    if ModelDeletionLease.claim_free(
                        token, "recovery", "plugin_load", now, _expiry(now)
                    ):
                        F.db.session.commit()
                        return RecoveryLeaseClaim(token, "", "", False)
                    F.db.session.rollback()
                    return None
                if lease.expires_at is None or lease.expires_at >= now:
                    return None
                if ModelDeletionLease.claim_expired_for_recovery(
                    previous_token,
                    token,
                    "%s:%s" % (previous_kind, previous_ref),
                    now,
                    _expiry(now),
                ):
                    F.db.session.commit()
                    return RecoveryLeaseClaim(
                        token, previous_kind, previous_ref, True
                    )
                F.db.session.rollback()
                return None
            except DeletionLeaseError:
                _rollback_quietly()
                raise
            except Exception:
                _rollback_quietly()
                raise DeletionLeaseError(
                    "삭제 복구 잠금을 확보할 수 없습니다."
                ) from None

    def recovery_state(self) -> str:
        """Read-only fast path: ``free``, ``busy`` or ``expired``."""
        self._ensure_row()
        now = datetime.now()
        with F.app.app_context():
            try:
                lease = ModelDeletionLease.get_singleton()
                if lease is None or not lease.owner_token:
                    return "free"
                if lease.expires_at is not None and lease.expires_at < now:
                    return "expired"
                return "busy"
            except Exception:
                _rollback_quietly()
                raise DeletionLeaseError(
                    "삭제 전역 잠금 상태를 확인할 수 없습니다."
                ) from None

    def active_batch_id(self) -> Optional[int]:
        self._ensure_row()
        now = datetime.now()
        with F.app.app_context():
            try:
                lease = ModelDeletionLease.get_singleton()
                if (
                    lease is None
                    or not lease.owner_token
                    or lease.owner_kind != "batch"
                    or lease.expires_at is None
                    or lease.expires_at < now
                ):
                    return None
                try:
                    return int(lease.owner_ref)
                except (TypeError, ValueError):
                    return None
            except Exception:
                _rollback_quietly()
                raise DeletionLeaseError(
                    "삭제 전역 잠금 상태를 확인할 수 없습니다."
                ) from None
