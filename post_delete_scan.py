from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from framework import F

from .deletion_lease import (
    DeletionLeaseBusy,
    DeletionLeaseError,
    DeletionLeaseLost,
    DeletionLeaseService,
)
from .models import (
    ModelDeletionLease,
    ModelDirectDeleteJournal,
    ModelPostDeleteScanJob,
    ModelQuarantineJournal,
)
from .scan_manager import current_safety_policy
from .services.plex_gateway import (
    PlexAuthenticationError,
    PlexGateway,
    PlexGatewayError,
    PlexHTTPError,
)
from .services.plex_mate_provider import PlexMateProvider, PlexMateUnavailable
from .services.post_delete_scan_targets import (
    build_scan_targets,
    validate_scan_target,
)
from .setup import P


_MODES = {"none", "binary", "web"}
_MAX_ATTEMPTS = 3
_WORKER_LEASE_SECONDS = 20 * 60
_BINARY_TIMEOUT_SECONDS = 15 * 60
_BINARY_KILL_GRACE_SECONDS = 5
_BINARY_QUARANTINE_SECONDS = 60 * 60
_WEB_POLL_INTERVAL_SECONDS = 5.0
_WEB_POLL_TIMEOUT_SECONDS = 120.0
_TERMINAL_FAILURE_STATUSES = ("blocked", "failed", "unverified")


class PostDeleteScanBlocked(RuntimeError):
    pass


class PostDeleteScanRetryable(RuntimeError):
    pass


class PostDeleteScanRefreshRequired(PostDeleteScanRetryable):
    """Filesystem state changed and requires one new Web refresh command."""

    pass


class PostDeleteScanUnverified(RuntimeError):
    pass


class PostDeleteScanQuarantined(RuntimeError):
    """A Binary child may still be alive, so both DB leases must expire."""

    pass


class PostDeleteScanPrearmFailed(RuntimeError):
    """The DB quarantine could not be proven before spawning a child."""

    pass


def configured_scan_mode() -> str:
    value = str(P.ModelSetting.get("setting_post_delete_scan_mode") or "none")
    value = value.strip().lower()
    return value if value in _MODES else "none"


def _request_timeout() -> int:
    try:
        return max(5, min(120, int(P.ModelSetting.get("setting_request_timeout") or "20")))
    except (TypeError, ValueError):
        return 20


def _digest(*values: Any) -> str:
    payload = "\x00".join(str(value or "") for value in values).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _action_ids(job: ModelPostDeleteScanJob) -> List[int]:
    try:
        values = json.loads(job.action_ids_json or "[]")
    except (TypeError, ValueError):
        values = []
    result: List[int] = []
    for value in values if isinstance(values, list) else []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed not in result:
            result.append(parsed)
    return result


class PostDeleteScanManager:
    """Durable post-delete partial scans, serialized across web workers."""

    def __init__(self) -> None:
        self.lease_service = DeletionLeaseService()
        self.deletion_recovery_callback: Optional[Any] = None
        self.completion_callback: Optional[Any] = None
        self.failure_callback: Optional[Any] = None
        self._wake = threading.Event()
        self._unloading = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._worker_context = threading.local()

    def enqueue_confirmed(
        self,
        run: Any,
        group: Any,
        candidate: Any,
        action_log: Any,
        current_item: Any,
        section_locations: Sequence[str],
        mode: Optional[str] = None,
        batch_run_id: Optional[int] = None,
        scan_candidate: Optional[Any] = None,
    ) -> List[ModelPostDeleteScanJob]:
        """Add jobs to the caller's current success transaction; never commit."""

        selected_mode = str(mode or configured_scan_mode()).strip().lower()
        if selected_mode == "none":
            return []
        if selected_mode not in ("binary", "web"):
            raise RuntimeError("삭제 후 Plex 스캔 방식 설정이 올바르지 않습니다.")

        # The audit/candidate columns always identify the deleted version, but
        # direct PMS deletion must scan the selected surviving version.  The
        # deleted version's now-empty directory can legitimately disappear
        # before this asynchronous worker starts.
        target_source = scan_candidate if scan_candidate is not None else candidate
        targets = build_scan_targets(
            group, target_source, current_item, section_locations
        )
        if not targets:
            raise RuntimeError(
                "삭제 대상의 정확한 Plex 부분 스캔 폴더를 확인할 수 없습니다."
            )

        now = datetime.now()
        jobs: List[ModelPostDeleteScanJob] = []
        action_id = int(action_log.id)
        parsed_batch_id = int(batch_run_id) if batch_run_id is not None else None
        machine_id = str(getattr(run, "server_machine_id", "") or "")
        if not machine_id:
            raise RuntimeError("스캔 당시 Plex Machine ID가 비어 있습니다.")

        for target in targets:
            target_key = _digest(
                machine_id,
                selected_mode,
                group.section_key,
                target,
            )
            source_key = (
                "batch:%s" % parsed_batch_id
                if parsed_batch_id is not None
                else "action:%s" % action_id
            )
            dedupe_key = _digest(source_key, target_key)
            existing = ModelPostDeleteScanJob.by_dedupe_key(dedupe_key)
            if existing is not None:
                ids = _action_ids(existing)
                if action_id not in ids:
                    ids.append(action_id)
                    existing.action_ids_json = json.dumps(ids, separators=(",", ":"))
                    existing.updated_at = now
                jobs.append(existing)
                continue

            job = ModelPostDeleteScanJob(
                created_at=now,
                updated_at=now,
                action_log_id=action_id,
                action_ids_json=json.dumps([action_id], separators=(",", ":")),
                batch_run_id=parsed_batch_id,
                run_id=int(run.id),
                group_id=int(group.id),
                candidate_id=int(candidate.id),
                server_machine_id=machine_id,
                mode=selected_mode,
                section_key=str(group.section_key),
                media_type=str(group.media_type or ""),
                target_path=str(target),
                target_key=target_key,
                dedupe_key=dedupe_key,
                status="queued",
                attempts=0,
                max_attempts=_MAX_ATTEMPTS,
                next_attempt_at=now,
                last_error="",
                worker_token="",
            )
            F.db.session.add(job)
            jobs.append(job)
        return jobs

    def plugin_load(self) -> int:
        recovered = self.recover_stale()
        with self._thread_lock:
            # A bounded unload can return while a Binary child is still being
            # stopped.  Never reuse its stop/wake events: the replacement
            # generation must be able to start immediately, while the old
            # generation must remain irrevocably stopped.
            previous_stop = self._unloading
            previous_wake = self._wake
            previous_stop.set()
            previous_wake.set()
            self._unloading = threading.Event()
            self._wake = threading.Event()
            self._thread = threading.Thread(
                target=self._worker,
                args=(self._unloading, self._wake),
                name="plex-dupefinder-post-delete-scan",
                daemon=True,
            )
            self._thread.start()
        self.wake()
        return recovered

    def unload(self) -> None:
        with self._thread_lock:
            stop = self._unloading
            wake = self._wake
            thread = self._thread
            stop.set()
            wake.set()
        if thread is not None and thread.is_alive():
            # The DB job/deletion leases prevent a replacement worker from
            # claiming the same scan if this bounded join returns first.
            thread.join(timeout=10)

    def wake(self) -> None:
        with self._thread_lock:
            wake = self._wake
        wake.set()

    def _worker(
        self,
        stop_event: Optional[threading.Event] = None,
        wake_event: Optional[threading.Event] = None,
    ) -> None:
        stop = stop_event or self._unloading
        wake = wake_event or self._wake
        self._worker_context.stop_event = stop
        try:
            while not stop.is_set():
                try:
                    # A process may restart before a running job's TTL expires.
                    # Re-check on every bounded worker tick so it becomes
                    # recoverable later without requiring another plugin reload.
                    self.recover_stale()
                    processed = self.process_one()
                    if stop.is_set():
                        break
                    if processed:
                        continue
                    wake.wait(timeout=15)
                    wake.clear()
                except Exception:
                    # One DB/driver failure must not permanently kill the
                    # durable worker.  Never include raw exception text here.
                    try:
                        with F.app.app_context():
                            F.db.session.rollback()
                            F.db.session.remove()
                    except Exception:
                        pass
                    try:
                        logger = getattr(P, "logger", None)
                        if logger is not None:
                            logger.error(
                                "Post-delete scan worker iteration failed; it will retry"
                            )
                    except Exception:
                        pass
                    wake.wait(timeout=5)
                    wake.clear()
        finally:
            try:
                del self._worker_context.stop_event
            except (AttributeError, TypeError):
                pass
            try:
                with F.app.app_context():
                    F.db.session.remove()
            except Exception:
                pass

    @staticmethod
    def _recover_expired_post_scan_job(owner_ref: str) -> Tuple[bool, int]:
        """Make the owner job non-running before its global lease is cleared."""

        try:
            job_id = int(owner_ref)
        except (TypeError, ValueError):
            return False, 0
        if job_id <= 0:
            return False, 0

        with F.app.app_context():
            try:
                now = datetime.now()
                job = ModelPostDeleteScanJob.get(job_id)
                if job is None or job.status != "running":
                    F.db.session.rollback()
                    return True, 0
                if (
                    job.lease_expires_at is None
                    or job.lease_expires_at >= now
                ):
                    F.db.session.rollback()
                    return False, 0
                worker_token = str(job.worker_token or "")
                if ModelPostDeleteScanJob.recover_stale_one(
                    job_id, worker_token, now
                ):
                    F.db.session.commit()
                    return True, 1
                F.db.session.rollback()
                current = ModelPostDeleteScanJob.get(job_id)
                safe = current is None or current.status != "running"
                F.db.session.rollback()
                return safe, 0
            except Exception:
                F.db.session.rollback()
                return False, 0

    def recover_stale(self) -> int:
        recovered = 0
        terminal_recovered: List[int] = []
        with F.app.app_context():
            now = datetime.now()
            stale = [
                (
                    job.id,
                    job.worker_token or "",
                    int(job.attempts or 0) >= int(job.max_attempts or _MAX_ATTEMPTS),
                )
                for job in ModelPostDeleteScanJob.stale_running(now)
            ]
            for job_id, worker_token, terminal in stale:
                if ModelPostDeleteScanJob.recover_stale_one(
                    job_id, worker_token, now
                ):
                    recovered += 1
                    if terminal:
                        terminal_recovered.append(int(job_id))
            if recovered:
                F.db.session.commit()
            else:
                F.db.session.rollback()
        try:
            expired_owner = self.lease_service.expired_owner()
            if expired_owner is not None:
                owner_kind, owner_ref = expired_owner
                if owner_kind == "post_scan":
                    safe_to_clear, additionally_recovered = (
                        self._recover_expired_post_scan_job(owner_ref)
                    )
                    recovered += additionally_recovered
                    if safe_to_clear:
                        self.lease_service.clear_expired_owner(
                            "post_scan", owner_ref
                        )
                elif self.deletion_recovery_callback is not None:
                    # manual/batch recovery must classify interrupted DELETE
                    # audit rows before its CAS owner releases the global
                    # lease.  Clearing it directly would lose unknown-state
                    # evidence and permit an overlapping delete.
                    self.deletion_recovery_callback()
        except DeletionLeaseError:
            # The next bounded worker tick repeats this exact-owner cleanup.
            pass
        # A worker can crash after its terminal job CAS but before the
        # quarantine failure callback. Newly recovered terminal jobs are
        # reconciled once even in test/fallback environments without journal
        # query support; older crash gaps are found durably below.
        for job_id in terminal_recovered:
            self._reconcile_terminal_job(job_id, require_pending=False)
        self.reconcile_terminal_quarantine_failures()
        return recovered

    @staticmethod
    def _has_pending_quarantine_journal(job: ModelPostDeleteScanJob) -> bool:
        terminal = {"verified", "trash_pending", "critical", "recovery_required"}
        for action_id in _action_ids(job):
            for model in (ModelQuarantineJournal, ModelDirectDeleteJournal):
                journal = model.for_action(action_id)
                if journal is not None and str(journal.status or "") not in terminal:
                    return True
        return False

    def _reconcile_terminal_job(
        self, job_id: int, require_pending: bool = True
    ) -> bool:
        """Idempotently apply quarantine failure state under the global lease."""

        if self.failure_callback is None:
            return False
        with F.app.app_context():
            job = ModelPostDeleteScanJob.get(job_id)
            if job is None or job.status not in _TERMINAL_FAILURE_STATUSES:
                F.db.session.rollback()
                return False
            if require_pending and not self._has_pending_quarantine_journal(job):
                F.db.session.rollback()
                return False

        try:
            token = self.lease_service.acquire("post_scan", str(job_id))
        except DeletionLeaseError:
            return False
        try:
            self.lease_service.renew(token, "post_scan", str(job_id))
            with F.app.app_context():
                job = ModelPostDeleteScanJob.get(job_id)
                if job is None or job.status not in _TERMINAL_FAILURE_STATUSES:
                    F.db.session.rollback()
                    return False
                if require_pending and not self._has_pending_quarantine_journal(job):
                    F.db.session.rollback()
                    return False

                def heartbeat() -> None:
                    self.lease_service.renew(token, "post_scan", str(job_id))

                setattr(job, "_pdff_heartbeat", heartbeat)
                try:
                    self.failure_callback(
                        job,
                        str(job.status),
                        str(job.last_error or "삭제 후 부분 스캔이 완료되지 않았습니다."),
                    )
                finally:
                    try:
                        delattr(job, "_pdff_heartbeat")
                    except (AttributeError, TypeError):
                        pass
            self.lease_service.renew(token, "post_scan", str(job_id))
            return True
        except Exception:
            try:
                with F.app.app_context():
                    F.db.session.rollback()
            except Exception:
                pass
            return False
        finally:
            try:
                self.lease_service.release(token)
            except Exception:
                pass

    def reconcile_terminal_quarantine_failures(self, limit: int = 500) -> int:
        """Repair terminal-job/journal crash gaps on every bounded worker tick."""

        if self.failure_callback is None:
            return 0
        with F.app.app_context():
            candidates = [
                int(job.id)
                for job in ModelPostDeleteScanJob.recent(limit)
                if job.status in _TERMINAL_FAILURE_STATUSES
                and self._has_pending_quarantine_journal(job)
            ]
            F.db.session.rollback()
        reconciled = 0
        for job_id in candidates:
            if self._reconcile_terminal_job(job_id, require_pending=True):
                reconciled += 1
        return reconciled

    def _claim_next(self) -> Tuple[Optional[int], str, str]:
        """Return ``(job_id, worker_token, deletion_lease_token)``."""

        with F.app.app_context():
            now = datetime.now()
            candidate = ModelPostDeleteScanJob.eligible_next(now)
            if candidate is None:
                return None, "", ""
            job_id = int(candidate.id)

        try:
            deletion_token = self.lease_service.acquire("post_scan", str(job_id))
        except DeletionLeaseBusy:
            return None, "", ""
        except DeletionLeaseError:
            return None, "", ""

        worker_token = secrets.token_urlsafe(32)
        try:
            with F.app.app_context():
                now = datetime.now()
                claimed = ModelPostDeleteScanJob.claim_for_worker(
                    job_id,
                    worker_token,
                    now,
                    now + timedelta(seconds=_WORKER_LEASE_SECONDS),
                )
                if not claimed:
                    F.db.session.rollback()
                    self.lease_service.release(deletion_token)
                    return None, "", ""
                try:
                    F.db.session.commit()
                except Exception:
                    F.db.session.rollback()
                    self.lease_service.release(deletion_token)
                    return None, "", ""
            return job_id, worker_token, deletion_token
        except Exception:
            try:
                self.lease_service.release(deletion_token)
            except Exception:
                pass
            return None, "", ""

    @staticmethod
    def _section_for_job(gateway: PlexGateway, job: ModelPostDeleteScanJob) -> Any:
        expected_type = "show" if job.media_type == "episode" else "movie"
        for section in gateway.list_sections():
            if section.key != str(job.section_key):
                continue
            if section.section_type != expected_type:
                raise PostDeleteScanBlocked(
                    "Plex library 유형이 삭제 당시 항목과 일치하지 않습니다."
                )
            if not section.locations:
                raise PostDeleteScanBlocked(
                    "Plex library Location을 확인할 수 없습니다."
                )
            return section
        raise PostDeleteScanBlocked("Plex library section을 찾을 수 없습니다.")

    def _validated_runtime(
        self, job: ModelPostDeleteScanJob
    ) -> Tuple[Any, PlexGateway, Any]:
        provider = PlexMateProvider()
        connection = provider.resolve(require_machine_id=True)
        gateway = PlexGateway(connection, timeout=(5, _request_timeout()))
        identity = gateway.validate_identity(connection.machine_id, require_match=False)
        if identity.machine_id != str(connection.machine_id):
            raise PostDeleteScanBlocked(
                "현재 Plex 서버가 plex_mate Machine ID와 일치하지 않습니다."
            )
        if identity.machine_id != str(job.server_machine_id):
            raise PostDeleteScanBlocked(
                "삭제 당시 Plex 서버와 현재 서버가 일치하지 않습니다."
            )
        section = self._section_for_job(gateway, job)
        policy = current_safety_policy()
        if not validate_scan_target(
            job.target_path, section.locations, policy.allowed_roots
        ):
            raise PostDeleteScanBlocked(
                "부분 스캔 경로가 현재 library Location 또는 허용 루트 밖입니다."
            )
        return provider, gateway, section

    @staticmethod
    def _process_returncode(process: Any) -> Optional[int]:
        if process is None:
            return None
        poll = getattr(process, "poll", None)
        try:
            value = poll() if callable(poll) else getattr(process, "returncode", None)
        except Exception:
            return None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _binary_returncode(cls, handle: Any) -> Optional[int]:
        process = getattr(handle, "process", None)
        if process is None and hasattr(handle, "returncode"):
            process = handle
        return cls._process_returncode(process)

    def _current_stop_event(self) -> threading.Event:
        return getattr(self._worker_context, "stop_event", self._unloading)

    @classmethod
    def _terminate_binary_handle(cls, handle: Any) -> bool:
        """Best-effort stop with proof from the actual child return code.

        PlexMate's wrapper may swallow a timeout or a failed kill.  A joined
        wrapper thread alone is therefore not proof that its subprocess died.
        The captured process object remains authoritative even if
        ``process_close`` later clears ``handle.process``.
        """

        process = getattr(handle, "process", None)
        if process is None and hasattr(handle, "returncode"):
            process = handle
        if process is None:
            return False

        close = getattr(handle, "process_close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

        if cls._process_returncode(process) is None:
            kill = getattr(process, "kill", None)
            if callable(kill):
                try:
                    kill()
                except Exception:
                    pass

        wait_result: Any = None
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                wait_result = wait(timeout=_BINARY_KILL_GRACE_SECONDS)
            except Exception:
                pass

        thread = getattr(handle, "thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=_BINARY_KILL_GRACE_SECONDS)

        return (
            cls._process_returncode(process) is not None
            or isinstance(wait_result, int)
        )

    def _execute_binary(
        self,
        provider: PlexMateProvider,
        job: ModelPostDeleteScanJob,
        worker_token: str,
        deletion_token: str,
    ) -> int:
        plex_mate, scanner = provider.binary_scanner()
        job_id = int(job.id)
        section_id = int(job.section_key)
        target_path = str(job.target_path)
        setting = plex_mate.ModelSetting
        binary = str(setting.get("base_bin_scanner") or "")
        metadata = str(setting.get("base_path_metadata") or "")
        program = str(setting.get("base_path_program") or "")
        if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
            raise PostDeleteScanBlocked(
                "plex_mate의 Plex Media Scanner 실행 파일을 확인할 수 없습니다."
            )
        if not metadata or not os.path.isdir(metadata) or not program or not os.path.isdir(program):
            raise PostDeleteScanBlocked(
                "plex_mate의 Plex 프로그램/메타데이터 경로 설정을 확인하세요."
            )
        if not os.path.isdir(target_path):
            raise PostDeleteScanBlocked(
                "Binary 스캐너에서 부분 스캔 폴더에 접근할 수 없습니다."
            )
        if not self._arm_binary_claim(job_id, worker_token, deletion_token):
            raise PostDeleteScanPrearmFailed(
                "Binary 실행 전 안전 격리 잠금을 확정할 수 없습니다."
            )
        try:
            handle = scanner.scan_refresh(
                section_id,
                target_path,
                # PlexMate's SupportSubprocess timeout path does not reliably
                # close a timed-out child.  Own the deadline/kill below.
                timeout=None,
                join=False,
            )
        except Exception:
            # The helper can throw after its background thread has already
            # started.  Pre-arming makes this outcome safe to quarantine.
            raise PostDeleteScanQuarantined(
                "Plex Media Scanner 시작 결과를 확인할 수 없어 격리합니다."
            ) from None
        if handle is None:
            raise PostDeleteScanQuarantined(
                "Plex Media Scanner 프로세스 시작 결과를 확인할 수 없어 격리합니다."
            )
        try:
            stop_event = self._current_stop_event()
            deadline = time.monotonic() + _BINARY_TIMEOUT_SECONDS
            returncode: Optional[int] = None
            while time.monotonic() < deadline:
                returncode = self._binary_returncode(handle)
                if returncode is not None:
                    break
                thread = getattr(handle, "thread", None)
                process = getattr(handle, "process", None)
                if thread is not None and not thread.is_alive() and process is None:
                    raise PostDeleteScanQuarantined(
                        "Plex Media Scanner 시작 결과를 확인할 수 없어 격리합니다."
                    )
                if stop_event.wait(timeout=0.2):
                    break

            if returncode is None:
                if not self._terminate_binary_handle(handle):
                    raise PostDeleteScanQuarantined(
                        "Plex Media Scanner child 종료를 확인할 수 없어 잠금 만료까지 격리합니다."
                    )
                if stop_event.is_set():
                    raise PostDeleteScanRetryable(
                        "플러그인 종료로 Plex Media Scanner 작업을 중단했습니다."
                    )
                raise PostDeleteScanRetryable(
                    "Plex Media Scanner 제한 시간을 초과해 프로세스를 종료했습니다."
                )
            thread = getattr(handle, "thread", None)
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)
            if returncode != 0:
                raise PostDeleteScanRetryable(
                    "Plex Media Scanner가 비정상 종료되었습니다."
                )
            return 0
        except (PostDeleteScanQuarantined, PostDeleteScanRetryable):
            raise
        except Exception:
            # Any unexpected wrapper failure after spawn is outcome-unknown.
            raise PostDeleteScanQuarantined(
                "Plex Media Scanner 상태를 확인할 수 없어 격리합니다."
            ) from None

    def _execute(
        self,
        job: ModelPostDeleteScanJob,
        worker_token: str,
        deletion_token: str,
    ) -> int:
        provider, gateway, _section = self._validated_runtime(job)
        if job.mode == "web":
            # A 2xx refresh is an accepted one-shot command. If metadata
            # propagation was not visible during the bounded poll, later
            # retry attempts re-run only the read-only finalizer instead of
            # sending the same refresh command again.
            try:
                previous_status = int(getattr(job, "response_status", 0) or 0)
            except (TypeError, ValueError):
                previous_status = 0
            if 200 <= previous_status < 300:
                return previous_status
            return gateway.refresh_section_path(job.section_key, job.target_path)
        if job.mode == "binary":
            return self._execute_binary(
                provider, job, worker_token, deletion_token
            )
        raise PostDeleteScanBlocked("지원하지 않는 삭제 후 스캔 방식입니다.")

    @staticmethod
    def _retry_delay(attempts: int) -> int:
        return min(5 * 60, 30 * (2 ** max(0, int(attempts) - 1)))

    def _finish_claimed(
        self,
        job_id: int,
        worker_token: str,
        status: str,
        message: str,
        response_status: Optional[int] = None,
        restore_retry_budget: bool = False,
    ) -> bool:
        with F.app.app_context():
            job = ModelPostDeleteScanJob.get(job_id)
            if (
                job is None
                or job.status != "running"
                or job.worker_token != worker_token
            ):
                F.db.session.rollback()
                return False
            now = datetime.now()
            job.status = status
            job.updated_at = now
            job.response_status = response_status
            job.last_error = message[:2000]
            if restore_retry_budget:
                job.max_attempts = _MAX_ATTEMPTS
            job.lease_key = None
            job.worker_token = ""
            job.lease_expires_at = None
            if status == "retry_wait":
                job.next_attempt_at = now + timedelta(
                    seconds=self._retry_delay(job.attempts or 1)
                )
                job.finished_at = None
            else:
                job.finished_at = now
            F.db.session.commit()
            return True

    def _arm_binary_claim(
        self,
        job_id: int,
        worker_token: str,
        deletion_token: str,
    ) -> bool:
        """Atomically extend both leases and close automatic retries."""

        with F.app.app_context():
            now = datetime.now()
            quarantine_until = now + timedelta(
                seconds=_BINARY_QUARANTINE_SECONDS
            )
            try:
                if not ModelDeletionLease.renew(
                    str(deletion_token),
                    "post_scan",
                    str(job_id),
                    now,
                    quarantine_until,
                ):
                    F.db.session.rollback()
                    return False
                updated = (
                    F.db.session.query(ModelPostDeleteScanJob)
                    .filter(
                        ModelPostDeleteScanJob.id == int(job_id),
                        ModelPostDeleteScanJob.status == "running",
                        ModelPostDeleteScanJob.worker_token == str(worker_token),
                    )
                    .update(
                        {
                            ModelPostDeleteScanJob.updated_at: now,
                            ModelPostDeleteScanJob.lease_expires_at: quarantine_until,
                            # An unknown child must never be launched
                            # automatically again. Expiry recovery makes this
                            # attempt terminal/manual-check.
                            ModelPostDeleteScanJob.max_attempts: ModelPostDeleteScanJob.attempts,
                            ModelPostDeleteScanJob.last_error: (
                                "Binary child 종료를 확인할 수 없어 잠금 만료까지 격리 중입니다."
                            ),
                        },
                        synchronize_session=False,
                    )
                )
                if updated != 1:
                    F.db.session.rollback()
                    return False
                F.db.session.commit()
                return True
            except Exception:
                F.db.session.rollback()
                # Never expose SQL parameters or the internal owner token.
                return False

    @staticmethod
    def _renew_job_claim(job_id: int, worker_token: str) -> bool:
        """CAS-prove and extend the durable job claim before callbacks."""

        with F.app.app_context():
            now = datetime.now()
            job = ModelPostDeleteScanJob.get(job_id)
            if (
                job is None
                or job.status != "running"
                or job.worker_token != str(worker_token)
            ):
                F.db.session.rollback()
                return False
            desired = now + timedelta(seconds=_WORKER_LEASE_SECONDS)
            if job.lease_expires_at is not None and job.lease_expires_at > desired:
                desired = job.lease_expires_at
            updated = (
                F.db.session.query(ModelPostDeleteScanJob)
                .filter(
                    ModelPostDeleteScanJob.id == int(job_id),
                    ModelPostDeleteScanJob.status == "running",
                    ModelPostDeleteScanJob.worker_token == str(worker_token),
                )
                .update(
                    {
                        ModelPostDeleteScanJob.updated_at: now,
                        ModelPostDeleteScanJob.lease_expires_at: desired,
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                F.db.session.rollback()
                return False
            F.db.session.commit()
            return True

    def _run_completion_callback(
        self, job: ModelPostDeleteScanJob, heartbeat: Any
    ) -> None:
        """Poll Web metadata propagation without repeating its refresh PUT."""

        is_web = str(job.mode or "") == "web"
        deadline = time.monotonic() + (
            _WEB_POLL_TIMEOUT_SECONDS if is_web else 0.0
        )
        setattr(job, "_pdff_heartbeat", heartbeat)
        try:
            while True:
                heartbeat()
                try:
                    self.completion_callback(job)
                    return
                except PostDeleteScanRefreshRequired:
                    # A protected sidecar was restored. The current refresh
                    # predates that filesystem mutation, so polling the same
                    # metadata state cannot prove completion.
                    raise
                except (PostDeleteScanRetryable, PlexGatewayError):
                    if not is_web:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    stop_event = self._current_stop_event()
                    wait_for = min(_WEB_POLL_INTERVAL_SECONDS, remaining)
                    if stop_event.wait(timeout=max(0.0, wait_for)):
                        raise PostDeleteScanRetryable(
                            "플러그인 종료로 Plex 격리 반영 확인을 중단했습니다."
                        ) from None
                    heartbeat()
        finally:
            try:
                delattr(job, "_pdff_heartbeat")
            except (AttributeError, TypeError):
                pass

    def process_one(self) -> bool:
        job_id, worker_token, deletion_token = self._claim_next()
        if job_id is None:
            return False

        status = "success"
        message = "Plex 부분 스캔 요청 완료"
        response_status: Optional[int] = None
        restore_retry_budget = False
        refresh_required = False
        try:
            with F.app.app_context():
                job = ModelPostDeleteScanJob.get(job_id)
                if job is None:
                    try:
                        self.lease_service.release(deletion_token)
                    except Exception:
                        pass
                    return True
                response_status = self._execute(
                    job, worker_token, deletion_token
                )
        except PostDeleteScanQuarantined:
            # The subprocess might still be mutating Plex.  Keeping both the
            # job CAS lease and the global deletion lease until their TTL is
            # the only safe cross-process quarantine.  Stale recovery owns the
            # later retry/terminal transition.
            # Same-bind CAS updates are committed together; a partial lease
            # extension could otherwise allow a later automatic retry.
            # Refresh from detection time when possible.  If this follow-up
            # transaction fails, the mandatory pre-arm still preserves the
            # one-hour/no-retry safety state from before child spawn.
            self._arm_binary_claim(job_id, worker_token, deletion_token)
            return True
        except PostDeleteScanPrearmFailed:
            status = "blocked"
            message = "Binary 실행 전 안전 격리 잠금을 확정하지 못해 실행하지 않았습니다."
        except PostDeleteScanBlocked as exc:
            status = "blocked"
            # These messages are authored fixed strings and contain neither
            # credentials nor driver parameters.  Preserve the exact safe
            # reason so operators can distinguish a vanished keep folder from
            # scanner configuration or section identity problems.
            message = str(exc)[:2000] or (
                "삭제 후 스캔 환경 검증에 실패했습니다. 설정과 경로를 확인하세요."
            )
        except (PlexAuthenticationError, PlexMateUnavailable):
            status = "blocked"
            message = "삭제 후 스캔 환경 검증에 실패했습니다. 설정과 경로를 확인하세요."
        except PostDeleteScanUnverified:
            status = "unverified"
            message = "Binary 스캔 종료 결과를 검증할 수 없어 성공 처리하지 않았습니다."
        except PlexHTTPError as exc:
            response_status = exc.status_code
            if exc.status_code in (400, 404):
                status = "blocked"
                message = "Plex가 section 또는 부분 스캔 경로를 거부했습니다."
            else:
                status = "retry_wait"
                message = "Plex 부분 스캔 요청이 실패해 제한적으로 재시도합니다."
        except PostDeleteScanRetryable:
            status = "retry_wait"
            restore_retry_budget = True
            message = "Plex 부분 스캔 요청이 실패해 제한적으로 재시도합니다."
        except PlexGatewayError:
            status = "retry_wait"
            message = "Plex 부분 스캔 요청이 실패해 제한적으로 재시도합니다."
        except Exception:
            status = "retry_wait"
            message = "삭제 후 스캔 중 내부 오류가 발생해 제한적으로 재시도합니다."

        finished = False
        release_global_lease = True
        try:
            # Prove both the global deletion lease and the durable job-token
            # claim before a completion callback mutates audit/group state.
            self.lease_service.renew(deletion_token, "post_scan", str(job_id))
            if not self._renew_job_claim(job_id, worker_token):
                raise DeletionLeaseLost("삭제 후 스캔 작업 소유권을 확인할 수 없습니다.")

            def heartbeat() -> None:
                self.lease_service.renew(
                    deletion_token, "post_scan", str(job_id)
                )
                if not self._renew_job_claim(job_id, worker_token):
                    raise DeletionLeaseLost(
                        "삭제 후 스캔 작업 소유권을 확인할 수 없습니다."
                    )

            if status == "success" and self.completion_callback is not None:
                try:
                    with F.app.app_context():
                        job = ModelPostDeleteScanJob.get(job_id)
                        if job is None:
                            raise DeletionLeaseLost(
                                "삭제 후 스캔 작업을 찾을 수 없습니다."
                            )
                        self._run_completion_callback(job, heartbeat)
                except PostDeleteScanBlocked:
                    status = "blocked"
                    message = "격리 후 Plex 재검증에서 수동 확인이 필요합니다."
                except PostDeleteScanRefreshRequired:
                    status = "retry_wait"
                    restore_retry_budget = True
                    refresh_required = True
                    response_status = None
                    message = "복구된 유지 자막을 Plex에 반영하기 위해 부분 스캔을 다시 요청합니다."
                except PostDeleteScanRetryable:
                    status = "retry_wait"
                    restore_retry_budget = True
                    message = "Plex 격리 반영을 제한적으로 재확인합니다."
                except PlexGatewayError:
                    status = "retry_wait"
                    restore_retry_budget = True
                    message = "Plex 격리 재검증 요청이 실패해 제한적으로 재시도합니다."
                except DeletionLeaseLost:
                    raise
                except Exception:
                    status = "retry_wait"
                    restore_retry_budget = True
                    message = "격리 사후검증 중 내부 오류가 발생해 제한적으로 재시도합니다."

            # The callback may have processed many coalesced actions. Prove
            # both leases again immediately before the terminal/retry CAS.
            self.lease_service.renew(deletion_token, "post_scan", str(job_id))
            if not self._renew_job_claim(job_id, worker_token):
                raise DeletionLeaseLost("삭제 후 스캔 작업 소유권을 확인할 수 없습니다.")

            with F.app.app_context():
                job = ModelPostDeleteScanJob.get(job_id)
                attempts = int(job.attempts or 0) if job is not None else _MAX_ATTEMPTS
                maximum = (
                    _MAX_ATTEMPTS
                    if restore_retry_budget
                    else (
                        int(job.max_attempts or _MAX_ATTEMPTS)
                        if job is not None
                        else _MAX_ATTEMPTS
                    )
                )
            if (
                status == "retry_wait"
                and attempts >= maximum
                and not refresh_required
            ):
                status = "failed"
                message = "삭제 후 부분 스캔이 최대 재시도 횟수를 초과했습니다."

            if status in _TERMINAL_FAILURE_STATUSES and self.failure_callback is not None:
                # Journal/action/group failure state must be durable before
                # the job becomes terminal, while both ownership proofs are
                # still live. A callback error deliberately leaves both
                # leases in place for stale recovery/reconciliation.
                heartbeat()
                try:
                    with F.app.app_context():
                        job = ModelPostDeleteScanJob.get(job_id)
                        if job is None:
                            raise DeletionLeaseLost(
                                "삭제 후 스캔 작업을 찾을 수 없습니다."
                            )
                        setattr(job, "_pdff_heartbeat", heartbeat)
                        try:
                            self.failure_callback(job, status, message)
                        finally:
                            try:
                                delattr(job, "_pdff_heartbeat")
                            except (AttributeError, TypeError):
                                pass
                except DeletionLeaseLost:
                    raise
                except Exception:
                    try:
                        with F.app.app_context():
                            F.db.session.rollback()
                    except Exception:
                        pass
                    release_global_lease = False
                    return True
                heartbeat()
            # Transition the worker-token CAS while the global lease is still
            # held. Only then may another delete/scan transaction proceed.
            finished = self._finish_claimed(
                job_id,
                worker_token,
                status,
                message,
                response_status=response_status,
                restore_retry_budget=restore_retry_budget,
            )
        except DeletionLeaseLost:
            # Stale-job recovery owns the next transition and audit decision.
            return True
        finally:
            if release_global_lease:
                try:
                    self.lease_service.release(deletion_token)
                except Exception:
                    pass
        return True

    def status(
        self,
        action_id: Optional[int] = None,
        batch_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        with F.app.app_context():
            if batch_id is not None:
                jobs = ModelPostDeleteScanJob.by_batch(batch_id)
            else:
                jobs = ModelPostDeleteScanJob.recent(limit)
            if action_id is not None:
                jobs = [job for job in jobs if int(action_id) in _action_ids(job)]
            return [job.as_api() for job in jobs]
