from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from framework import F

from .models import (
    ModelActionLog,
    ModelDirectDeleteJournal,
    ModelDuplicateGroup,
    ModelPostDeleteScanJob,
)
from .services.direct_delete import (
    DirectDeletePlan,
    DirectDeletePlanError,
    DirectDeletePlanner,
)
from .services.quarantine_delete import (
    DirectorySnapshot,
    FileSnapshot,
    QuarantinePlanError,
    _directory_snapshot,
    capture_file_snapshot,
    snapshot_matches,
)
from .setup import P


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    )
    if value is not None
)
_FULL_CONTENT_PROOF_BYTES = 64 * 1024 * 1024
_CONTENT_PROOF_BLOCK_BYTES = 256 * 1024
_CONTENT_PROOF_BLOCKS = 32


def _fsync_directory(path: str) -> bool:
    """Durably flush a directory where the filesystem supports it.

    A number of FUSE filesystems, including some mergerfs configurations,
    reject directory fsync with an explicit "unsupported" errno.  That is not
    evidence that the rename/unlink failed, so it is recorded as a warning and
    execution continues.  All other errors remain fatal.
    """

    if os.name == "nt":
        return True
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
        return True
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise
        P.logger.warning(
            "Direct delete directory fsync unsupported: errno=%s", exc.errno
        )
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _same_identity(expected: FileSnapshot, current: FileSnapshot) -> bool:
    return (
        expected.size == current.size
        and expected.mtime_ns == current.mtime_ns
        and expected.device == current.device
        and expected.inode == current.inode
        and expected.links == current.links
        and (not expected.sha256 or expected.sha256 == current.sha256)
    )


def _mtime_ns(value: os.stat_result) -> int:
    return int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1e9)))


def _safe_regular_stat(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and not bool(getattr(value, "st_file_attributes", 0) & 0x0400)
    )


def _stat_matches_snapshot(
    expected: FileSnapshot, current: os.stat_result, require_identity: bool
) -> bool:
    if not _safe_regular_stat(current):
        return False
    if (
        int(current.st_size) != expected.size
        or _mtime_ns(current) != expected.mtime_ns
        or int(getattr(current, "st_nlink", 1) or 1) != expected.links
    ):
        return False
    return not require_identity or (
        int(current.st_dev) == expected.device
        and int(current.st_ino) == expected.inode
    )


def _open_regular_nofollow(path: str) -> Tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not _safe_regular_stat(opened):
            raise DirectDeletePlanError("직접 삭제 대상이 안전한 일반 파일이 아닙니다.")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _pread_exact(descriptor: int, length: int, offset: int) -> bytes:
    chunks: List[bytes] = []
    remaining = max(0, int(length))
    position = max(0, int(offset))
    while remaining:
        if hasattr(os, "pread"):
            chunk = os.pread(descriptor, remaining, position)
        else:  # pragma: no cover - POSIX builds used by FlaskFarm expose pread.
            original = os.lseek(descriptor, 0, os.SEEK_CUR)
            try:
                os.lseek(descriptor, position, os.SEEK_SET)
                chunk = os.read(descriptor, remaining)
            finally:
                os.lseek(descriptor, original, os.SEEK_SET)
        if not chunk:
            break
        chunks.append(chunk)
        position += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _content_proof_offsets(expected: FileSnapshot) -> Tuple[int, ...]:
    size = expected.size
    if size <= _FULL_CONTENT_PROOF_BYTES:
        return (0,)
    maximum = max(0, size - _CONTENT_PROOF_BLOCK_BYTES)
    offsets = {0, maximum}
    seed = (
        "%s:%s:%s:%s" % (
            expected.size,
            expected.mtime_ns,
            expected.device,
            expected.inode,
        )
    ).encode("ascii")
    counter = 0
    while len(offsets) < min(_CONTENT_PROOF_BLOCKS, maximum + 1):
        digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        offsets.add(int.from_bytes(digest[:8], "big") % (maximum + 1))
        counter += 1
    return tuple(sorted(offsets))


def _descriptor_content_proof(
    descriptor: int,
    expected: FileSnapshot,
    full_content: bool,
    heartbeat: Optional[Any] = None,
) -> str:
    hasher = hashlib.sha256()
    if full_content or expected.size <= _FULL_CONTENT_PROOF_BYTES:
        offset = 0
        while offset < expected.size:
            if callable(heartbeat):
                heartbeat()
            length = min(1024 * 1024, expected.size - offset)
            value = _pread_exact(descriptor, length, offset)
            if len(value) != length:
                raise DirectDeletePlanError("직접 삭제 파일의 전체 해시를 읽을 수 없습니다.")
            hasher.update(value)
            offset += length
        return hasher.hexdigest()

    hasher.update(str(expected.size).encode("ascii"))
    for offset in _content_proof_offsets(expected):
        if callable(heartbeat):
            heartbeat()
        length = min(_CONTENT_PROOF_BLOCK_BYTES, expected.size - offset)
        value = _pread_exact(descriptor, length, offset)
        if len(value) != length:
            raise DirectDeletePlanError("직접 삭제 파일의 내용 증명을 읽을 수 없습니다.")
        hasher.update(offset.to_bytes(8, "big"))
        hasher.update(length.to_bytes(8, "big"))
        hasher.update(value)
    return hasher.hexdigest()


def _descriptor_owns_path(
    descriptor: int, path: str, expected: FileSnapshot
) -> bool:
    opened = os.fstat(descriptor)
    current = os.lstat(path)
    return (
        _stat_matches_snapshot(expected, opened, require_identity=False)
        and _stat_matches_snapshot(expected, current, require_identity=False)
        and int(opened.st_dev) == int(current.st_dev)
        and int(opened.st_ino) == int(current.st_ino)
    )


def _snapshot_matches_full_hash(
    expected: FileSnapshot, heartbeat: Optional[Any] = None
) -> bool:
    descriptor: Optional[int] = None
    try:
        descriptor, opened = _open_regular_nofollow(expected.path)
        if not _stat_matches_snapshot(expected, opened, require_identity=True):
            return False
        proof = _descriptor_content_proof(
            descriptor, expected, True, heartbeat=heartbeat
        )
        return bool(
            expected.sha256
            and secrets.compare_digest(expected.sha256, proof)
            and _descriptor_owns_path(descriptor, expected.path, expected)
        )
    except (OSError, DirectDeletePlanError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_parent_nofollow(path: str) -> Tuple[int, str]:
    """Open the lexical parent used by a later unlinkat-style operation."""

    parent = os.path.normpath(os.path.abspath(os.path.dirname(path)))
    name = os.path.basename(path)
    if not name or name in (".", "..") or os.path.sep in name:
        raise DirectDeletePlanError("직접 삭제 대상 파일명이 올바르지 않습니다.")
    if os.path.altsep and os.path.altsep in name:
        raise DirectDeletePlanError("직접 삭제 대상 파일명이 올바르지 않습니다.")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(parent, flags)
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(parent)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or int(opened.st_dev) != int(current.st_dev)
            or int(opened.st_ino) != int(current.st_ino)
            or os.path.normcase(os.path.realpath(parent))
            != os.path.normcase(parent)
        ):
            raise DirectDeletePlanError(
                "직접 삭제 대상 폴더 identity가 변경되었습니다."
            )
        return descriptor, name
    except Exception:
        os.close(descriptor)
        raise


def _descriptor_owns_dirfd_entry(
    source_descriptor: int,
    parent_descriptor: int,
    name: str,
    expected: FileSnapshot,
) -> bool:
    """Prove that an opened inode is still the exact entry under *dirfd*."""

    opened = os.fstat(source_descriptor)
    current = os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    return (
        _stat_matches_snapshot(expected, opened, require_identity=False)
        and _stat_matches_snapshot(expected, current, require_identity=False)
        and int(opened.st_dev) == int(current.st_dev)
        and int(opened.st_ino) == int(current.st_ino)
    )


def _within_path(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except (OSError, ValueError):
        return False


def _secure_directory(path: str, create: bool = False) -> os.stat_result:
    lexical = os.path.normpath(os.path.abspath(path))
    if create:
        os.makedirs(lexical, mode=0o700, exist_ok=True)
    current = os.lstat(lexical)
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or bool(getattr(current, "st_file_attributes", 0) & 0x0400)
        or os.path.normcase(os.path.realpath(lexical)) != os.path.normcase(lexical)
    ):
        raise DirectDeletePlanError("자막 보호 저장 경로가 안전한 폴더가 아닙니다.")
    try:
        os.chmod(lexical, 0o700)
    except OSError as exc:
        # Windows ACLs are not represented by POSIX mode bits.  On POSIX,
        # however, a chmod failure means this directory cannot be trusted to
        # hold the only durable copies used around PMS DELETE.
        if os.name != "nt":
            raise DirectDeletePlanError(
                "자막 보호 저장 폴더 권한을 안전하게 제한할 수 없습니다."
            ) from exc
    before_chmod = current
    current = os.lstat(lexical)
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or bool(getattr(current, "st_file_attributes", 0) & 0x0400)
        or int(current.st_dev) != int(before_chmod.st_dev)
        or int(current.st_ino) != int(before_chmod.st_ino)
        or os.path.normcase(os.path.realpath(lexical)) != os.path.normcase(lexical)
    ):
        raise DirectDeletePlanError(
            "자막 보호 저장 폴더 identity가 권한 검증 중 변경되었습니다."
        )
    if os.name != "nt":
        if int(current.st_mode) & 0o077:
            raise DirectDeletePlanError(
                "자막 보호 저장 폴더가 다른 사용자에게 노출되어 있습니다."
            )
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and int(current.st_uid) != int(geteuid()):
            raise DirectDeletePlanError(
                "자막 보호 저장 폴더 소유자가 현재 프로세스와 다릅니다."
            )
    return current


def _verify_data_root(path: str) -> os.stat_result:
    """Validate FlaskFarm's shared data root without changing its permissions."""

    lexical = os.path.normpath(os.path.abspath(path))
    current = os.lstat(lexical)
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or bool(getattr(current, "st_file_attributes", 0) & 0x0400)
        or os.path.normcase(os.path.realpath(lexical)) != os.path.normcase(lexical)
    ):
        raise DirectDeletePlanError("FlaskFarm path_data가 안전한 폴더가 아닙니다.")
    return current


def _copy_snapshot_to_backup(
    snapshot: FileSnapshot, destination: str, heartbeat: Optional[Any] = None
) -> FileSnapshot:
    if not snapshot.sha256:
        raise DirectDeletePlanError("보호할 자막의 전체 해시가 없습니다.")
    source_descriptor, source_opened = _open_regular_nofollow(snapshot.path)
    target_descriptor: Optional[int] = None
    target_created = False
    copy_complete = False
    try:
        if not _stat_matches_snapshot(snapshot, source_opened, require_identity=True):
            raise DirectDeletePlanError("보호할 자막이 사전확인 이후 변경되었습니다.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if callable(heartbeat):
            heartbeat()
        target_descriptor = os.open(destination, flags, 0o600)
        target_created = True
        digest = hashlib.sha256()
        copied = 0
        while True:
            if callable(heartbeat):
                heartbeat()
            chunk = os.read(source_descriptor, 128 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short subtitle protection write")
                copied += written
                view = view[written:]
        if copied != snapshot.size or not secrets.compare_digest(
            digest.hexdigest(), snapshot.sha256
        ):
            raise DirectDeletePlanError("자막 보호본 내용이 원본과 일치하지 않습니다.")
        if not _descriptor_owns_path(source_descriptor, snapshot.path, snapshot):
            raise DirectDeletePlanError("보호할 자막 경로가 복사 중 변경되었습니다.")
        os.fsync(target_descriptor)
        copy_complete = True
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        os.close(source_descriptor)
        if target_created and not copy_complete:
            try:
                os.unlink(destination)
            except OSError:
                pass
    try:
        _fsync_directory(os.path.dirname(destination))
    except Exception:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise
    try:
        backup = capture_file_snapshot(destination, content_hash=True)
    except Exception:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise
    if backup.size != snapshot.size or not secrets.compare_digest(
        backup.sha256, snapshot.sha256
    ):
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise DirectDeletePlanError("생성한 자막 보호본을 재검증할 수 없습니다.")
    return backup


def _restore_snapshot_from_backup(
    snapshot: FileSnapshot,
    backup: FileSnapshot,
    mode: int,
    heartbeat: Optional[Any] = None,
) -> None:
    if os.path.lexists(snapshot.path):
        raise DirectDeletePlanError("복원 대상 경로에 다른 파일이 있어 덮어쓰지 않습니다.")
    if not _snapshot_matches_full_hash(
        backup, heartbeat=heartbeat
    ) or not secrets.compare_digest(
        snapshot.sha256, backup.sha256
    ):
        raise DirectDeletePlanError("자막 보호본이 변경되어 자동 복원하지 않습니다.")
    source_descriptor, _opened = _open_regular_nofollow(backup.path)
    target_descriptor: Optional[int] = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if callable(heartbeat):
            heartbeat()
        target_descriptor = os.open(snapshot.path, flags, int(mode) & 0o777)
        digest = hashlib.sha256()
        copied = 0
        while True:
            if callable(heartbeat):
                heartbeat()
            chunk = os.read(source_descriptor, 128 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short subtitle restore write")
                copied += written
                view = view[written:]
        if copied != snapshot.size or not secrets.compare_digest(
            digest.hexdigest(), snapshot.sha256
        ):
            raise DirectDeletePlanError("복원한 자막 내용이 보호본과 일치하지 않습니다.")
        try:
            os.fchmod(target_descriptor, int(mode) & 0o777)
        except (AttributeError, OSError):
            pass
        os.fsync(target_descriptor)
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        os.close(source_descriptor)
    try:
        os.utime(snapshot.path, ns=(snapshot.mtime_ns, snapshot.mtime_ns))
    except OSError:
        pass
    _fsync_directory(os.path.dirname(snapshot.path))
    current = capture_file_snapshot(snapshot.path, content_hash=True)
    if current.size != snapshot.size or not secrets.compare_digest(
        current.sha256, snapshot.sha256
    ):
        raise DirectDeletePlanError("복원된 자막을 재검증할 수 없습니다.")


def _unlink_exact_sidecar(
    snapshot: FileSnapshot, heartbeat: Optional[Any] = None
) -> None:
    descriptor, opened = _open_regular_nofollow(snapshot.path)
    descriptor_open = True
    parent_descriptor: Optional[int] = None
    try:
        if not _stat_matches_snapshot(snapshot, opened, require_identity=True):
            raise DirectDeletePlanError("삭제 대상 자막이 사전확인 이후 변경되었습니다.")
        proof = _descriptor_content_proof(
            descriptor, snapshot, True, heartbeat=heartbeat
        )
        if not snapshot.sha256 or not secrets.compare_digest(snapshot.sha256, proof):
            raise DirectDeletePlanError("삭제 대상 자막 내용이 변경되었습니다.")
        if os.name != "nt" and os.unlink in getattr(os, "supports_dir_fd", set()):
            parent_descriptor, name = _open_parent_nofollow(snapshot.path)
            if not _descriptor_owns_dirfd_entry(
                descriptor, parent_descriptor, name, snapshot
            ):
                raise DirectDeletePlanError(
                    "삭제 대상 자막 경로가 열린 원본과 일치하지 않습니다."
                )
            if callable(heartbeat):
                heartbeat()
            os.unlink(name, dir_fd=parent_descriptor)
        elif os.name == "nt":
            # Python's Windows file handle does not opt into delete sharing, so
            # unlinking while the proof descriptor is open returns WinError 32.
            # Capture one final full-hash identity proof, close that handle,
            # and immediately delete the same lexical entry.
            if not _descriptor_owns_path(descriptor, snapshot.path, snapshot):
                raise DirectDeletePlanError(
                    "삭제 대상 자막 경로가 열린 원본과 일치하지 않습니다."
                )
            os.close(descriptor)
            descriptor_open = False
            final_current = capture_file_snapshot(snapshot.path, content_hash=True)
            if not _same_identity(snapshot, final_current):
                raise DirectDeletePlanError(
                    "삭제 대상 자막이 최종 확인 직전에 변경되었습니다."
                )
            if callable(heartbeat):
                heartbeat()
            os.unlink(snapshot.path)
        else:
            if not _descriptor_owns_path(descriptor, snapshot.path, snapshot):
                raise DirectDeletePlanError(
                    "삭제 대상 자막 경로가 열린 원본과 일치하지 않습니다."
                )
            if callable(heartbeat):
                heartbeat()
            os.unlink(snapshot.path)
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        if descriptor_open:
            os.close(descriptor)
    _fsync_directory(os.path.dirname(snapshot.path))


def _prove_posix_handoff(
    source_descriptor: int,
    tombstone: str,
    expected: FileSnapshot,
    full_content: bool,
    keep_target_open: bool = False,
) -> Optional[int]:
    source_after = os.fstat(source_descriptor)
    if not _stat_matches_snapshot(expected, source_after, require_identity=False):
        raise DirectDeletePlanError("열어 둔 삭제 원본의 상태가 변경되었습니다.")
    target_descriptor, target_opened = _open_regular_nofollow(tombstone)
    try:
        if not _stat_matches_snapshot(expected, target_opened, require_identity=False):
            raise DirectDeletePlanError("직접 삭제 handoff 파일 상태가 일치하지 않습니다.")
        if not _descriptor_owns_path(target_descriptor, tombstone, expected):
            raise DirectDeletePlanError("직접 삭제 handoff 경로 identity가 변경되었습니다.")
        source_proof = _descriptor_content_proof(
            source_descriptor, expected, full_content
        )
        target_proof = _descriptor_content_proof(
            target_descriptor, expected, full_content
        )
        if not secrets.compare_digest(source_proof, target_proof):
            raise DirectDeletePlanError("직접 삭제 handoff 파일 내용이 원본과 다릅니다.")
        if expected.sha256 and not secrets.compare_digest(
            expected.sha256, target_proof
        ):
            raise DirectDeletePlanError("직접 삭제 자막의 전체 해시가 변경되었습니다.")
        if not _descriptor_owns_path(target_descriptor, tombstone, expected):
            raise DirectDeletePlanError("직접 삭제 handoff 경로가 검사 중 변경되었습니다.")
        if keep_target_open:
            kept = target_descriptor
            target_descriptor = -1
            return kept
        return None
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)


def _operation_parent(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.dirname(path))))


def _verify_owner_directories(
    plan: DirectDeletePlan,
    removed_paths: Sequence[str] = (),
    operation_paths: Sequence[str] = (),
) -> None:
    removed_by_parent: Dict[str, set] = {}
    for path in removed_paths:
        removed_by_parent.setdefault(_operation_parent(path), set()).add(
            os.path.basename(path)
        )
    added_by_parent: Dict[str, set] = {}
    for path in operation_paths:
        added_by_parent.setdefault(_operation_parent(path), set()).add(
            os.path.basename(path)
        )
    for approved in plan.watched_directories:
        try:
            current = _directory_snapshot(approved.path)
        except QuarantinePlanError as exc:
            raise DirectDeletePlanError(str(exc)) from exc
        key = os.path.normcase(approved.path)
        removed = removed_by_parent.get(key, set())
        added = added_by_parent.get(key, set())
        expected_entries = tuple(
            sorted((set(approved.entries) - removed) | added)
        )
        if (
            current.device != approved.device
            or current.inode != approved.inode
            or current.entries != expected_entries
            or (
                not removed
                and not added
                and (
                    current.mtime_ns != approved.mtime_ns
                    or current.ctime_ns != approved.ctime_ns
                )
            )
        ):
            raise DirectDeletePlanError(
                "영상·자막 폴더 내용이 사전확인 이후 변경되었습니다. 수동 확인이 필요합니다."
            )


def _verify_operation_paths(values: Sequence[Dict[str, Any]]) -> None:
    for raw in values:
        if raw.get("state") != "created":
            continue
        path = os.path.normpath(os.path.abspath(str(raw.get("path") or "")))
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise DirectDeletePlanError("직접 삭제 작업 폴더가 사라졌습니다.") from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or bool(getattr(current, "st_file_attributes", 0) & 0x0400)
            or int(current.st_dev) != int(raw.get("device", -1))
            or int(current.st_ino) != int(raw.get("inode", -1))
            or os.path.normcase(os.path.realpath(path)) != os.path.normcase(path)
        ):
            raise DirectDeletePlanError("직접 삭제 작업 폴더 identity가 변경되었습니다.")


def _snapshot_from_dict(raw: Dict[str, Any]) -> FileSnapshot:
    try:
        return FileSnapshot(
            path=str(raw["path"]),
            size=int(raw["size"]),
            mtime_ns=int(raw["mtime_ns"]),
            device=int(raw["device"]),
            inode=int(raw["inode"]),
            links=int(raw["links"]),
            sha256=str(raw.get("sha256") or ""),
        )
    except (KeyError, TypeError, ValueError):
        raise DirectDeletePlanError("직접 삭제 파일 snapshot 기록이 올바르지 않습니다.") from None


_HYBRID_STRATEGY = "plex_media_delete_sidecar_v1"


def _config_path_data() -> str:
    config = getattr(F, "config", None)
    value = None
    if hasattr(config, "get"):
        value = config.get("path_data")
    elif config is not None:
        value = getattr(config, "path_data", None)
    path = str(value or "").strip()
    if not path or not os.path.isabs(path):
        raise DirectDeletePlanError("FlaskFarm path_data 보호 저장 경로를 확인할 수 없습니다.")
    return os.path.normpath(os.path.abspath(path))


def _paths_overlap(first: str, second: str) -> bool:
    first = os.path.normcase(os.path.realpath(os.path.abspath(first)))
    second = os.path.normcase(os.path.realpath(os.path.abspath(second)))
    return _within_path(first, second) or _within_path(second, first)


def _ensure_secure_child(
    parent: str, name: str, secure_parent: bool = True
) -> str:
    if not name or name in (".", "..") or os.path.basename(name) != name:
        raise DirectDeletePlanError("자막 보호 저장 폴더 이름이 올바르지 않습니다.")
    if secure_parent:
        _secure_directory(parent)
    else:
        _verify_data_root(parent)
    child = os.path.join(parent, name)
    try:
        os.mkdir(child, 0o700)
        _fsync_directory(parent)
    except FileExistsError:
        pass
    _secure_directory(child)
    return child


def _expected_hybrid_backup_base(plan: DirectDeletePlan) -> Tuple[str, str, str]:
    data_root = _config_path_data()
    _verify_data_root(data_root)
    package_name = str(getattr(P, "package_name", "plex_dupefinder_ff") or "")
    if os.path.basename(package_name) != package_name:
        raise DirectDeletePlanError("플러그인 보호 저장 폴더 이름이 올바르지 않습니다.")
    package_root = os.path.join(data_root, package_name)
    prospective_base = os.path.join(package_root, "direct-delete-backups")
    for media_root in tuple(plan.allowed_roots) + tuple(plan.section_locations):
        if _paths_overlap(prospective_base, str(media_root)):
            raise DirectDeletePlanError(
                "자막 보호 저장 경로가 Plex 또는 허용 미디어 루트와 겹칩니다."
            )
    return data_root, package_root, prospective_base


def _hybrid_backup_base(plan: DirectDeletePlan) -> str:
    # ``path_data`` is shared by FlaskFarm and other plugins.  We only inspect
    # it; chmod is restricted to the directories owned by this plugin below.
    data_root, package_root, prospective_base = _expected_hybrid_backup_base(plan)
    package_name = os.path.basename(package_root)
    package_root = _ensure_secure_child(
        data_root, package_name, secure_parent=False
    )
    base = _ensure_secure_child(package_root, "direct-delete-backups")
    if os.path.normcase(base) != os.path.normcase(prospective_base):
        raise DirectDeletePlanError("자막 보호 저장 경로가 예상과 다릅니다.")
    return base


def _directory_record(path: str, kind: str) -> Dict[str, Any]:
    current = _secure_directory(path)
    return {
        "kind": kind,
        "path": path,
        "state": "created",
        "device": int(current.st_dev),
        "inode": int(current.st_ino),
    }


def _verify_directory_record(raw: Dict[str, Any]) -> None:
    path = os.path.normpath(os.path.abspath(str(raw.get("path") or "")))
    current = _secure_directory(path)
    if (
        int(current.st_dev) != int(raw.get("device", -1))
        or int(current.st_ino) != int(raw.get("inode", -1))
    ):
        raise DirectDeletePlanError("자막 보호 저장 폴더 identity가 변경되었습니다.")


def _backup_filename(index: int, source_path: str) -> str:
    digest = hashlib.sha256(
        source_path.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return "%03d-%s.backup" % (index, digest)


def _backup_entries(values: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        raw
        for raw in values
        if isinstance(raw, dict) and raw.get("kind") == "subtitle_backup"
    ]


def _ranked_hybrid_backups(
    plan: DirectDeletePlan,
) -> List[Tuple[str, str, FileSnapshot]]:
    """Return the one exact, deterministic backup specification for a plan."""

    ranked: Dict[str, Tuple[int, str, FileSnapshot]] = {}
    for rank, role, decisions in (
        (1, "protected", plan.excluded),
        (2, "target", plan.eligible),
        (3, "protected", plan.protected),
    ):
        for decision in decisions:
            if decision.snapshot is None:
                continue
            key = os.path.normcase(decision.snapshot.path)
            previous = ranked.get(key)
            if previous is None or rank > previous[0]:
                ranked[key] = (rank, role, decision.snapshot)

    expected = {
        os.path.normcase(decision.path)
        for decision in tuple(plan.eligible)
        + tuple(plan.protected)
        + tuple(plan.excluded)
        if decision.snapshot is not None
    }
    if set(ranked) != expected:
        raise DirectDeletePlanError("PMS DELETE 전 자막 보호 대상 집합이 다릅니다.")
    return [
        (key, role, snapshot)
        for key, (_rank, role, snapshot) in sorted(
            ranked.items(), key=lambda item: item[0]
        )
    ]


def _snapshot_still_present(
    snapshot: FileSnapshot, heartbeat: Optional[Any] = None
) -> bool:
    return _snapshot_matches_full_hash(snapshot, heartbeat=heartbeat)


def _path_proven_absent(path: str) -> bool:
    try:
        os.lstat(path)
        return False
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise DirectDeletePlanError("파일 경로의 부재를 확정할 수 없습니다.") from exc


def _parent_descriptor_still_named(parent_descriptor: int, parent_path: str) -> bool:
    try:
        opened = os.fstat(parent_descriptor)
        current = os.lstat(parent_path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(opened.st_mode)
        and stat.S_ISDIR(current.st_mode)
        and not stat.S_ISLNK(current.st_mode)
        and int(opened.st_dev) == int(current.st_dev)
        and int(opened.st_ino) == int(current.st_ino)
    )


def _restore_backup_no_overwrite(
    source: FileSnapshot,
    backup: FileSnapshot,
    source_mode: int,
    heartbeat: Optional[Any] = None,
) -> FileSnapshot:
    if os.path.lexists(source.path):
        raise DirectDeletePlanError("보호 자막 경로에 다른 파일이 있어 덮어쓰지 않습니다.")
    if (
        not source.sha256
        or not backup.sha256
        or not secrets.compare_digest(source.sha256, backup.sha256)
        or not _snapshot_matches_full_hash(backup, heartbeat=heartbeat)
    ):
        raise DirectDeletePlanError("자막 보호본의 전체 해시를 확인할 수 없습니다.")

    if os.name == "nt" or os.link not in getattr(os, "supports_dir_fd", set()):
        _restore_snapshot_from_backup(
            source, backup, source_mode, heartbeat=heartbeat
        )
        return capture_file_snapshot(source.path, content_hash=True)

    parent_descriptor, name = _open_parent_nofollow(source.path)
    backup_descriptor: Optional[int] = None
    temporary = ".pdff-restore-%s" % secrets.token_hex(12)
    writer: Optional[int] = None
    try:
        parent_path = os.path.dirname(source.path)
        if not _parent_descriptor_still_named(parent_descriptor, parent_path):
            raise DirectDeletePlanError("복원 대상 자막 폴더 identity가 변경되었습니다.")
        backup_descriptor, backup_opened = _open_regular_nofollow(backup.path)
        if not _stat_matches_snapshot(backup, backup_opened, require_identity=True):
            raise DirectDeletePlanError("자막 보호본 identity가 변경되었습니다.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if callable(heartbeat):
            heartbeat()
        writer = os.open(temporary, flags, int(source_mode) & 0o777, dir_fd=parent_descriptor)
        digest = hashlib.sha256()
        copied = 0
        while True:
            if callable(heartbeat):
                heartbeat()
            chunk = os.read(backup_descriptor, 128 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(writer, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short protected subtitle restore")
                copied += written
                view = view[written:]
        if copied != source.size or not secrets.compare_digest(
            digest.hexdigest(), source.sha256
        ):
            raise DirectDeletePlanError("복원 자막이 보호본과 일치하지 않습니다.")
        os.fsync(writer)
        os.close(writer)
        writer = None
        if not _parent_descriptor_still_named(parent_descriptor, parent_path):
            raise DirectDeletePlanError("복원 직전 자막 폴더 identity가 변경되었습니다.")
        if callable(heartbeat):
            heartbeat()
        os.link(
            temporary,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=parent_descriptor)
        try:
            os.utime(
                name,
                ns=(source.mtime_ns, source.mtime_ns),
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except (NotImplementedError, TypeError):
            os.utime(source.path, ns=(source.mtime_ns, source.mtime_ns))
        if not _parent_descriptor_still_named(parent_descriptor, parent_path):
            raise DirectDeletePlanError("복원 후 자막 폴더 identity가 변경되었습니다.")
        _fsync_directory(parent_path)
    finally:
        if writer is not None:
            os.close(writer)
        if backup_descriptor is not None:
            os.close(backup_descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except OSError:
            pass
        os.close(parent_descriptor)
    restored = capture_file_snapshot(source.path, content_hash=True)
    if restored.size != source.size or not secrets.compare_digest(
        restored.sha256, source.sha256
    ):
        raise DirectDeletePlanError("복원된 자막을 재검증할 수 없습니다.")
    return restored


def _validate_exact_pms_postread(before: Any, after: Any, delete_media_id: str) -> None:
    if after.identity_fingerprint() != before.identity_fingerprint():
        raise DirectDeletePlanError("Plex 항목 identity가 DELETE 직후 변경되었습니다.")
    before_by_id = {str(value.media_id): value for value in before.media}
    after_by_id = {str(value.media_id): value for value in after.media}
    expected_ids = set(before_by_id) - {str(delete_media_id)}
    if set(after_by_id) != expected_ids:
        raise DirectDeletePlanError("Plex DELETE 직후 Media 집합이 예상과 다릅니다.")
    for media_id in expected_ids:
        if after_by_id[media_id].fingerprint() != before_by_id[media_id].fingerprint():
            raise DirectDeletePlanError("Plex DELETE 후 유지 Media snapshot이 변경되었습니다.")


class DirectDeleteManager:
    def __init__(self) -> None:
        self.planner = DirectDeletePlanner()

    @staticmethod
    def enabled() -> bool:
        return (
            str(P.ModelSetting.get("setting_delete_backend") or "plex")
            .strip()
            .lower()
            == "direct"
        )

    @staticmethod
    def _scan_mode() -> str:
        return (
            str(P.ModelSetting.get("setting_post_delete_scan_mode") or "none")
            .strip()
            .lower()
        )

    def preview(
        self,
        item: Any,
        delete_media_id: str,
        allowed_roots: Sequence[str],
        section_locations: Sequence[str],
    ) -> DirectDeletePlan:
        if not self.enabled():
            raise DirectDeletePlanError("직접 파일 삭제 방식이 활성화되지 않았습니다.")
        mode = self._scan_mode()
        if mode not in ("binary", "web"):
            raise DirectDeletePlanError("직접 삭제는 Binary 또는 Web 부분 스캔이 필수입니다.")
        return self.planner.plan(
            item,
            str(delete_media_id),
            tuple(allowed_roots),
            tuple(section_locations),
            mode,
        )

    @staticmethod
    def _commit(journal: ModelDirectDeleteJournal) -> None:
        journal.updated_at = datetime.now()
        F.db.session.commit()

    @staticmethod
    def _verify_protected(
        plan: DirectDeletePlan, heartbeat: Optional[Any] = None
    ) -> None:
        for value in plan.survivors:
            if not snapshot_matches(value, verify_hash=False):
                raise DirectDeletePlanError("유지 영상이 사전확인 이후 변경되었습니다.")
        for decision in plan.protected:
            if decision.snapshot is None or not _snapshot_matches_full_hash(
                decision.snapshot, heartbeat=heartbeat
            ):
                raise DirectDeletePlanError("유지본 자막이 사전확인 이후 변경되었습니다.")

    @staticmethod
    def _verify_related_sidecars(
        plan: DirectDeletePlan, heartbeat: Optional[Any] = None
    ) -> None:
        seen = set()
        for decision in tuple(plan.eligible) + tuple(plan.protected) + tuple(
            plan.excluded
        ):
            if decision.snapshot is None:
                continue
            key = os.path.normcase(decision.snapshot.path)
            if key in seen:
                continue
            seen.add(key)
            if not _snapshot_matches_full_hash(
                decision.snapshot, heartbeat=heartbeat
            ):
                raise DirectDeletePlanError(
                    "관련 자막이 사전확인 이후 변경되었습니다."
                )

    def _create_hybrid_backups(
        self,
        plan: DirectDeletePlan,
        journal: ModelDirectDeleteJournal,
        heartbeat: Optional[Any],
    ) -> None:
        """Durably back up every related regular sidecar before PMS DELETE."""

        base = _hybrid_backup_base(plan)
        operation_path = _ensure_secure_child(base, "op-%s" % journal.operation_key)
        records: List[Dict[str, Any]] = [
            _directory_record(operation_path, "backup_operation")
        ]
        journal.operation_paths_json = _json(records)
        self._commit(journal)
        backup_path = _ensure_secure_child(operation_path, "sidecars")
        records.append(_directory_record(backup_path, "backup_directory"))
        journal.operation_paths_json = _json(records)
        self._commit(journal)

        expected_backups = _ranked_hybrid_backups(plan)
        for index, (_key, role, snapshot) in enumerate(expected_backups):
            if callable(heartbeat):
                heartbeat()
            source_mode = int(os.lstat(snapshot.path).st_mode) & 0o777
            destination = os.path.join(
                backup_path, _backup_filename(index, snapshot.path)
            )
            backup = _copy_snapshot_to_backup(
                snapshot, destination, heartbeat=heartbeat
            )
            records.append(
                {
                    "kind": "subtitle_backup",
                    "role": role,
                    "state": "ready",
                    "source_snapshot": snapshot.as_dict(),
                    "source_mode": source_mode,
                    "backup_snapshot": backup.as_dict(),
                }
            )
            journal.operation_paths_json = _json(records)
            self._commit(journal)

        if len(_backup_entries(records)) != len(expected_backups):
            raise DirectDeletePlanError("PMS DELETE 전 자막 보호 기록이 완결되지 않았습니다.")

    def _verify_persisted_hybrid_backups(
        self,
        plan: DirectDeletePlan,
        journal: ModelDirectDeleteJournal,
        heartbeat: Optional[Any],
    ) -> ModelDirectDeleteJournal:
        """Re-read and fully prove the durable backup set before PMS DELETE."""

        if callable(heartbeat):
            heartbeat()
        expire = getattr(F.db.session, "expire", None)
        if callable(expire):
            expire(journal)
        persisted = ModelDirectDeleteJournal.get(journal.id)
        if persisted is None:
            raise DirectDeletePlanError("자막 보호 작업 기록을 다시 읽을 수 없습니다.")
        if (
            str(getattr(persisted, "operation_key", "") or "")
            != str(journal.operation_key)
            or not secrets.compare_digest(
                str(getattr(persisted, "plan_digest", "") or ""),
                str(plan.plan_digest),
            )
        ):
            raise DirectDeletePlanError("자막 보호 작업 기록 identity가 변경되었습니다.")
        try:
            manifest = json.loads(persisted.manifest_json or "{}")
            records = json.loads(persisted.operation_paths_json or "[]")
        except (TypeError, ValueError):
            raise DirectDeletePlanError("자막 보호 작업 기록을 읽을 수 없습니다.") from None
        if (
            not isinstance(manifest, dict)
            or str(manifest.get("execution_strategy") or "") != _HYBRID_STRATEGY
            or manifest != plan.manifest_dict()
            or not isinstance(records, list)
        ):
            raise DirectDeletePlanError("자막 보호 작업 기록 형식이 올바르지 않습니다.")
        if any(
            not isinstance(raw, dict)
            or raw.get("kind")
            not in ("backup_operation", "backup_directory", "subtitle_backup")
            for raw in records
        ):
            raise DirectDeletePlanError("자막 보호 작업 기록에 알 수 없는 항목이 있습니다.")

        _data_root, _package_root, base = _expected_hybrid_backup_base(plan)
        operation_path = os.path.join(base, "op-%s" % persisted.operation_key)
        backup_path = os.path.join(operation_path, "sidecars")
        directory_records = {
            kind: [raw for raw in records if raw.get("kind") == kind]
            for kind in ("backup_operation", "backup_directory")
        }
        if any(len(values) != 1 for values in directory_records.values()):
            raise DirectDeletePlanError("자막 보호 저장 폴더 기록이 완결되지 않았습니다.")
        expected_directory_paths = {
            "backup_operation": operation_path,
            "backup_directory": backup_path,
        }
        for kind, expected_path in expected_directory_paths.items():
            raw = directory_records[kind][0]
            actual_path = os.path.normpath(os.path.abspath(str(raw.get("path") or "")))
            if (
                raw.get("state") != "created"
                or os.path.normcase(actual_path) != os.path.normcase(expected_path)
            ):
                raise DirectDeletePlanError("자막 보호 저장 폴더 기록이 예상과 다릅니다.")
            _verify_directory_record(raw)

        specifications = _ranked_hybrid_backups(plan)
        backup_records = _backup_entries(records)
        if len(backup_records) != len(specifications):
            raise DirectDeletePlanError("PMS DELETE 전 자막 보호본 개수가 다릅니다.")
        expected_names = set()
        for index, ((_key, role, expected_source), raw) in enumerate(
            zip(specifications, backup_records)
        ):
            if callable(heartbeat):
                heartbeat()
            if raw.get("state") != "ready" or str(raw.get("role") or "") != role:
                raise DirectDeletePlanError("자막 보호본 상태 또는 역할이 올바르지 않습니다.")
            source_raw = raw.get("source_snapshot")
            backup_raw = raw.get("backup_snapshot")
            if not isinstance(source_raw, dict) or not isinstance(backup_raw, dict):
                raise DirectDeletePlanError("자막 보호 snapshot 기록이 없습니다.")
            source = _snapshot_from_dict(source_raw)
            backup = _snapshot_from_dict(backup_raw)
            if source.as_dict() != expected_source.as_dict():
                raise DirectDeletePlanError("자막 원본 snapshot 기록이 계획과 다릅니다.")
            expected_name = _backup_filename(index, expected_source.path)
            expected_names.add(expected_name)
            expected_backup_path = os.path.join(backup_path, expected_name)
            if os.path.normcase(backup.path) != os.path.normcase(expected_backup_path):
                raise DirectDeletePlanError("자막 보호본 경로가 예상과 다릅니다.")
            if (
                not source.sha256
                or not backup.sha256
                or source.size != backup.size
                or not secrets.compare_digest(source.sha256, backup.sha256)
                or not _snapshot_matches_full_hash(source, heartbeat=heartbeat)
                or not _snapshot_matches_full_hash(backup, heartbeat=heartbeat)
            ):
                raise DirectDeletePlanError("자막 보호본과 원본의 전체 해시가 다릅니다.")

        try:
            operation_entries = set(os.listdir(operation_path))
            backup_entries = set(os.listdir(backup_path))
        except OSError as exc:
            raise DirectDeletePlanError("자막 보호 저장 폴더를 재검증할 수 없습니다.") from exc
        if operation_entries != {"sidecars"} or backup_entries != expected_names:
            raise DirectDeletePlanError("자막 보호 저장 폴더 내용이 예상과 다릅니다.")
        # This is also the final ownership proof.  The caller invokes PMS
        # DELETE immediately after return with no intervening state mutation.
        if callable(heartbeat):
            heartbeat()
        return persisted

    def _restore_hybrid_backups(
        self,
        journal: ModelDirectDeleteJournal,
        include_target: bool,
        heartbeat: Optional[Any] = None,
        skip_source_paths: Sequence[str] = (),
    ) -> Dict[str, int]:
        try:
            records = json.loads(journal.operation_paths_json or "[]")
        except (TypeError, ValueError):
            raise DirectDeletePlanError("자막 보호 기록을 읽을 수 없습니다.") from None
        if not isinstance(records, list):
            raise DirectDeletePlanError("자막 보호 기록 형식이 올바르지 않습니다.")
        for raw in records:
            if isinstance(raw, dict) and raw.get("kind") in (
                "backup_operation",
                "backup_directory",
            ):
                _verify_directory_record(raw)

        roles = {"protected", "target"} if include_target else {"protected"}
        skipped = {
            os.path.normcase(os.path.abspath(str(path)))
            for path in skip_source_paths
            if str(path or "")
        }
        verified = 0
        restored = 0
        for raw in _backup_entries(records):
            if str(raw.get("role") or "") not in roles:
                continue
            if callable(heartbeat):
                heartbeat()
            source_raw = raw.get("source_snapshot")
            backup_raw = raw.get("backup_snapshot")
            if not isinstance(source_raw, dict) or not isinstance(backup_raw, dict):
                raise DirectDeletePlanError("자막 보호 snapshot 기록이 없습니다.")
            source = _snapshot_from_dict(source_raw)
            backup = _snapshot_from_dict(backup_raw)
            if os.path.normcase(os.path.abspath(source.path)) in skipped:
                # A later item in the same approved auto group intentionally
                # removed this former survivor. Its own journal is the proof;
                # restoring this older protection copy would resurrect it.
                continue
            restored_snapshot = (
                _snapshot_from_dict(raw["restored_snapshot"])
                if isinstance(raw.get("restored_snapshot"), dict)
                else None
            )
            if _snapshot_still_present(source, heartbeat=heartbeat) or (
                restored_snapshot is not None
                and _snapshot_still_present(
                    restored_snapshot, heartbeat=heartbeat
                )
            ):
                verified += 1
                continue
            if os.path.lexists(source.path):
                raise DirectDeletePlanError(
                    "보호 자막 경로의 다른 파일은 덮어쓰지 않습니다."
                )
            restored_snapshot = _restore_backup_no_overwrite(
                source,
                backup,
                int(raw.get("source_mode", 0o600)),
                heartbeat=heartbeat,
            )
            if callable(heartbeat):
                heartbeat()
            raw["restored_snapshot"] = restored_snapshot.as_dict()
            raw["restore_count"] = int(raw.get("restore_count", 0) or 0) + 1
            journal.operation_paths_json = _json(records)
            self._commit(journal)
            restored += 1
        if callable(heartbeat):
            heartbeat()
        return {"verified": verified, "restored": restored}

    def cleanup_backups(
        self, journal: ModelDirectDeleteJournal, heartbeat: Optional[Any] = None
    ) -> Dict[str, int]:
        """Remove only hash-verified internal copies after final post-scan proof."""

        try:
            manifest = json.loads(journal.manifest_json or "{}")
            records = json.loads(journal.operation_paths_json or "[]")
        except (TypeError, ValueError):
            raise DirectDeletePlanError("자막 보호 정리 기록을 읽을 수 없습니다.") from None
        if not isinstance(manifest, dict) or not isinstance(records, list):
            raise DirectDeletePlanError("자막 보호 정리 기록 형식이 올바르지 않습니다.")
        if str(manifest.get("execution_strategy") or "") != _HYBRID_STRATEGY:
            return {"removed": 0}
        if not records:
            return {"removed": 0}
        directories = [
            raw
            for raw in records
            if isinstance(raw, dict)
            and raw.get("kind") in ("backup_operation", "backup_directory")
        ]
        directory_kinds = [str(raw.get("kind") or "") for raw in directories]
        if (
            len(directory_kinds) != len(set(directory_kinds))
            or any(
                value not in ("backup_operation", "backup_directory")
                for value in directory_kinds
            )
            or "backup_operation" not in directory_kinds
        ):
            raise DirectDeletePlanError("자막 보호 저장 폴더 기록이 올바르지 않습니다.")

        removed = 0
        for raw in _backup_entries(records):
            if callable(heartbeat):
                heartbeat()
            if raw.get("state") == "removed":
                continue
            backup_raw = raw.get("backup_snapshot")
            if not isinstance(backup_raw, dict):
                raise DirectDeletePlanError("자막 보호본 snapshot 기록이 없습니다.")
            backup = _snapshot_from_dict(backup_raw)
            if os.path.lexists(backup.path):
                _unlink_exact_sidecar(backup, heartbeat=heartbeat)
            raw["state"] = "removed"
            removed += 1
            journal.operation_paths_json = _json(records)
            self._commit(journal)

        for kind in ("backup_directory", "backup_operation"):
            raw = next(
                (value for value in directories if value.get("kind") == kind),
                None,
            )
            if raw is None:
                continue
            if raw.get("state") == "removed":
                continue
            try:
                missing = _path_proven_absent(str(raw["path"]))
            except (KeyError, TypeError, ValueError):
                raise DirectDeletePlanError(
                    "자막 보호 저장 폴더 기록이 올바르지 않습니다."
                ) from None
            if missing:
                raw["state"] = "removed"
                journal.operation_paths_json = _json(records)
                self._commit(journal)
                continue
            _verify_directory_record(raw)
            os.rmdir(str(raw["path"]))
            _fsync_directory(os.path.dirname(str(raw["path"])))
            raw["state"] = "removed"
            journal.operation_paths_json = _json(records)
            self._commit(journal)
        # Once every private copy and directory is proven absent, scrub the
        # internal paths and hashes from the durable row as well.
        journal.operation_paths_json = "[]"
        self._commit(journal)
        return {"removed": removed}

    def _pms_delete_and_reconcile(
        self,
        plan: DirectDeletePlan,
        journal: ModelDirectDeleteJournal,
        operations: List[Dict[str, Any]],
        gateway: Any,
        current_item: Any,
        group: Any,
        candidate: Any,
        action_log: ModelActionLog,
        heartbeat: Optional[Any],
    ) -> Tuple[Any, Optional[int], Dict[str, int]]:
        """Send PMS DELETE once, prove its exact result, then reconcile sidecars."""

        plex_operation = operations[0]
        plex_operation["state"] = "pms_delete_prepared"
        journal.unlink_json = _json(operations)
        journal.status = "deleting"
        action_log.status = "direct_deleting"
        action_log.message = "Plex Media DELETE 전송 준비 완료"
        self._commit(journal)

        response_status: Optional[int] = None
        outcome_error = ""
        try:
            response_status = gateway.delete_media(
                group.rating_key, candidate.media_id
            )
        except Exception as exc:
            # DELETE transport errors are outcome-unknown. Never resend; the
            # immediately following exact GET is the sole reconciliation.
            outcome_error = exc.__class__.__name__
        if callable(heartbeat):
            heartbeat()
        plex_operation["state"] = "pms_delete_returned"
        plex_operation["response_status"] = response_status
        plex_operation["outcome_error"] = outcome_error
        journal.unlink_json = _json(operations)
        action_log.response_status = response_status
        self._commit(journal)

        if callable(heartbeat):
            heartbeat()
        after = gateway.get_metadata(group.rating_key)
        if callable(heartbeat):
            heartbeat()
        _validate_exact_pms_postread(current_item, after, str(candidate.media_id))
        if not _path_proven_absent(plan.video.path):
            raise DirectDeletePlanError(
                "Plex Media는 사라졌지만 대상 영상 파일이 남아 있어 확정하지 않습니다."
            )
        if callable(heartbeat):
            heartbeat()
        plex_operation["state"] = "pms_delete_confirmed"
        plex_operation["confirmed_at"] = datetime.now().isoformat(
            timespec="seconds"
        )
        journal.unlink_json = _json(operations)
        action_log.after_json = _json(after.as_dict())
        self._commit(journal)

        if callable(heartbeat):
            heartbeat()
        protected = self._restore_hybrid_backups(
            journal, include_target=False, heartbeat=heartbeat
        )
        for raw in operations[1:]:
            if callable(heartbeat):
                heartbeat()
            snapshot = _snapshot_from_dict(raw["snapshot"])
            if _path_proven_absent(snapshot.path):
                reconciled_state = "removed_by_plex"
            else:
                _unlink_exact_sidecar(snapshot, heartbeat=heartbeat)
                reconciled_state = "deleted_by_plugin"
            if callable(heartbeat):
                heartbeat()
            raw["state"] = reconciled_state
            raw["deleted_at"] = datetime.now().isoformat(timespec="seconds")
            journal.deleted_count = sum(
                1
                for value in operations[1:]
                if value.get("state") in ("removed_by_plex", "deleted_by_plugin")
            )
            journal.unlink_json = _json(operations)
            self._commit(journal)
        return after, response_status, protected

    def _legacy_execute(
        self,
        plan: DirectDeletePlan,
        expected_digest: str,
        run: Any,
        group: Any,
        candidate: Any,
        keep: Any,
        action_log: ModelActionLog,
        batch_run_id: Optional[int] = None,
        heartbeat: Optional[Any] = None,
    ) -> ModelDirectDeleteJournal:
        # Kept only as a source-compatible symbol for rolling reloads.  The
        # pre-1.5 video rename/unlink implementation is permanently disabled;
        # old DELETE FILES plans must be previewed again as DELETE MEDIA.
        raise DirectDeletePlanError(
            "이전 파일 직접 삭제 방식은 실행할 수 없습니다. 다시 사전확인하세요."
        )

        def beat() -> None:
            if callable(heartbeat):
                heartbeat()

        beat()
        if not expected_digest or not secrets.compare_digest(
            str(expected_digest), str(plan.plan_digest)
        ):
            raise DirectDeletePlanError(
                "직접 삭제 계획이 사전확인 이후 변경되었습니다. 다시 사전확인하세요."
            )
        if not snapshot_matches(plan.video, verify_hash=False):
            raise DirectDeletePlanError("삭제 대상 영상이 사전확인 이후 변경되었습니다.")
        for decision in plan.eligible:
            if decision.snapshot is None or not snapshot_matches(
                decision.snapshot, verify_hash=True
            ):
                raise DirectDeletePlanError("삭제 대상 자막이 사전확인 이후 변경되었습니다.")
        self._verify_protected(plan)
        _verify_owner_directories(plan)

        operation_key = secrets.token_hex(24)
        source_items: List[Tuple[FileSnapshot, str]] = [(plan.video, "video")]
        source_items.extend(
            (decision.snapshot, "subtitle")
            for decision in plan.eligible
            if decision.snapshot is not None
        )
        # Prefer same-parent sibling handoffs.  Some mergerfs path-preserving
        # policies can still return EXDEV when the new name resolves to a
        # different backing branch; POSIX execution has a narrowly-scoped
        # held-FD/dirfd unlink fallback for exactly that errno.
        operation_paths: List[Dict[str, Any]] = []
        operations: List[Dict[str, Any]] = []
        for index, (snapshot, kind) in enumerate(source_items):
            operations.append(
                {
                    "source_path": snapshot.path,
                    "tombstone_path": os.path.join(
                        os.path.dirname(snapshot.path),
                        ".pdff-direct-%s-%03d.tombstone" % (operation_key, index),
                    ),
                    "kind": kind,
                    "state": "pending",
                    "handoff_strategy": "same_parent_v2",
                    "snapshot": snapshot.as_dict(),
                }
            )

        journal = ModelDirectDeleteJournal(
            created_at=datetime.now(),
            updated_at=datetime.now(),
            action_log_id=action_log.id,
            batch_run_id=batch_run_id,
            run_id=run.id,
            group_id=group.id,
            candidate_id=candidate.id,
            keep_candidate_id=keep.id,
            operation_key=operation_key,
            status="planned",
            plan_digest=plan.plan_digest,
            manifest_json=_json(plan.manifest_dict()),
            unlink_json=_json(operations),
            operation_paths_json=_json(operation_paths),
            eligible_count=len(plan.eligible),
            excluded_count=len(plan.excluded),
            protected_count=len(plan.protected),
            deleted_count=0,
        )
        action_log.status = "direct_deleting"
        action_log.message = "영상과 전용 외부 자막의 직접 삭제 준비 중"
        try:
            # This is the mutation boundary: every exact source and randomized
            # same-parent tombstone path is durable before rename is attempted.
            F.db.session.add(journal)
            F.db.session.commit()
            beat()
        except Exception:
            F.db.session.rollback()
            raise RuntimeError("직접 삭제 작업 기록을 저장할 수 없습니다.") from None

        handed_off: List[str] = []
        active_tombstones: List[str] = []
        mutation_started = False
        stage = "journal_prepared"
        try:
            stage = "journal_preparing"
            journal.status = "preparing"
            self._commit(journal)
            stage = "journal_deleting"
            journal.status = "deleting"
            self._commit(journal)
            for index, raw in enumerate(operations):
                beat()
                kind = str(raw.get("kind") or "unknown")
                stage = "pre_handoff_%s_%s" % (kind, index)
                _verify_owner_directories(plan, handed_off, active_tombstones)
                self._verify_protected(plan)
                snapshot = _snapshot_from_dict(raw["snapshot"])
                verify_hash = raw.get("kind") == "subtitle"
                tombstone = str(raw["tombstone_path"])
                if os.path.lexists(tombstone):
                    raise DirectDeletePlanError("직접 삭제 임시 경로 충돌을 감지했습니다.")
                source_descriptor: Optional[int] = None
                unlink_descriptor: Optional[int] = None
                source_content_proof = ""
                try:
                    if os.name != "nt":
                        source_descriptor, source_opened = _open_regular_nofollow(
                            snapshot.path
                        )
                        if not _stat_matches_snapshot(
                            snapshot, source_opened, require_identity=True
                        ):
                            raise DirectDeletePlanError(
                                "직접 삭제 대상 파일 상태가 변경되었습니다."
                            )
                        if not _descriptor_owns_path(
                            source_descriptor, snapshot.path, snapshot
                        ):
                            raise DirectDeletePlanError(
                                "직접 삭제 대상 경로 identity가 변경되었습니다."
                            )
                        if verify_hash:
                            source_content_proof = _descriptor_content_proof(
                                source_descriptor, snapshot, True
                            )
                            if not snapshot.sha256 or not secrets.compare_digest(
                                snapshot.sha256, source_content_proof
                            ):
                                raise DirectDeletePlanError(
                                    "직접 삭제 대상 자막 내용이 변경되었습니다."
                                )
                    elif not snapshot_matches(snapshot, verify_hash=verify_hash):
                        raise DirectDeletePlanError(
                            "직접 삭제 대상 파일 상태가 변경되었습니다."
                        )

                    stage = "rename_%s_%s" % (kind, index)
                    used_direct_unlink = False
                    try:
                        os.rename(snapshot.path, tombstone)
                    except OSError as rename_error:
                        if rename_error.errno != errno.EXDEV or os.name == "nt":
                            raise
                        if source_descriptor is None:
                            raise DirectDeletePlanError(
                                "mergerfs EXDEV fallback 원본 descriptor가 없습니다."
                            )
                        if not source_content_proof:
                            source_content_proof = _descriptor_content_proof(
                                source_descriptor, snapshot, False
                            )
                        # Persist the selected fallback before its one permanent
                        # mutation.  A restart with the source still present is
                        # therefore distinguishable from an unjournaled unlink.
                        raw["handoff_strategy"] = "posix_dirfd_unlink_v1"
                        raw["state"] = "direct_unlink_prepared"
                        journal.unlink_json = _json(operations)
                        stage = "journal_direct_unlink_%s_%s" % (kind, index)
                        self._commit(journal)
                        beat()

                        stage = "direct_unlink_guard_%s_%s" % (kind, index)
                        _verify_owner_directories(
                            plan, handed_off, active_tombstones
                        )
                        self._verify_protected(plan)
                        stage = "direct_unlink_%s_%s" % (kind, index)
                        try:
                            _direct_unlink_open_path(
                                source_descriptor,
                                snapshot,
                                bool(verify_hash),
                                source_content_proof,
                            )
                        except Exception:
                            # The helper performs no fallible checks after its
                            # unlinkat call.  This remains a conservative guard
                            # for an unusual close/error path or FUSE ambiguity.
                            if not os.path.lexists(snapshot.path):
                                mutation_started = True
                                handed_off.append(snapshot.path)
                                raw["state"] = "unlink_unjournaled"
                            raise
                        mutation_started = True
                        handed_off.append(snapshot.path)
                        raw["state"] = "unlink_unjournaled"
                        raw["identity_proof"] = "open_fd_dirfd_identity_v1"
                        used_direct_unlink = True

                    if used_direct_unlink:
                        stage = "unlink_fsync_%s_%s" % (kind, index)
                        _fsync_directory(os.path.dirname(snapshot.path))
                        raw["state"] = "deleted"
                        raw["deleted_at"] = datetime.now().isoformat(
                            timespec="seconds"
                        )
                        journal.unlink_json = _json(operations)
                        journal.deleted_count = sum(
                            1
                            for value in operations
                            if value.get("kind") == "subtitle"
                            and value.get("state") == "deleted"
                        )
                        stage = "journal_deleted_%s_%s" % (kind, index)
                        self._commit(journal)
                        continue

                    mutation_started = True
                    handed_off.append(snapshot.path)
                    active_tombstones.append(tombstone)
                    # Persisted by the common failure handler if fsync or the
                    # open-FD proof fails before the normal tombstoned commit.
                    raw["state"] = "handoff_unverified"

                    stage = "handoff_fsync_%s_%s" % (kind, index)
                    _fsync_directory(os.path.dirname(snapshot.path))
                    stage = "handoff_proof_%s_%s" % (kind, index)
                    if os.name != "nt":
                        if source_descriptor is None:
                            raise DirectDeletePlanError(
                                "직접 삭제 원본 descriptor가 없습니다."
                            )
                        _prove_posix_handoff(
                            source_descriptor,
                            tombstone,
                            snapshot,
                            bool(verify_hash),
                        )
                        raw["identity_proof"] = "open_fd_content_v1"
                    else:
                        moved = capture_file_snapshot(
                            tombstone, content_hash=verify_hash
                        )
                        if not _same_identity(snapshot, moved):
                            raise DirectDeletePlanError(
                                "직접 삭제 이동 identity를 확인할 수 없습니다."
                            )
                        raw["identity_proof"] = "strict_path_identity_v1"
                    raw["state"] = "tombstoned"
                    journal.unlink_json = _json(operations)
                    # No heartbeat between rename and this durable evidence.
                    stage = "journal_tombstoned_%s_%s" % (kind, index)
                    self._commit(journal)

                    beat()
                    stage = "pre_unlink_proof_%s_%s" % (kind, index)
                    _verify_owner_directories(
                        plan, handed_off, active_tombstones
                    )
                    self._verify_protected(plan)
                    if os.name != "nt":
                        if source_descriptor is None:
                            raise DirectDeletePlanError(
                                "직접 삭제 원본 descriptor가 없습니다."
                            )
                        unlink_descriptor = _prove_posix_handoff(
                            source_descriptor,
                            tombstone,
                            snapshot,
                            bool(verify_hash),
                            keep_target_open=True,
                        )
                    else:
                        final_current = capture_file_snapshot(
                            tombstone, content_hash=verify_hash
                        )
                        if not _same_identity(snapshot, final_current):
                            raise DirectDeletePlanError(
                                "영구 삭제 직전 파일 identity가 변경되었습니다."
                            )
                    stage = "unlink_%s_%s" % (kind, index)
                    if os.name != "nt":
                        if unlink_descriptor is None or not _descriptor_owns_path(
                            unlink_descriptor, tombstone, snapshot
                        ):
                            raise DirectDeletePlanError(
                                "영구 삭제 직전 handoff 경로 identity가 변경되었습니다."
                            )
                    os.unlink(tombstone)
                    raw["state"] = "unlink_unjournaled"
                    active_tombstones.remove(tombstone)
                    stage = "unlink_fsync_%s_%s" % (kind, index)
                    _fsync_directory(os.path.dirname(tombstone))
                    raw["state"] = "deleted"
                    raw["deleted_at"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                    journal.unlink_json = _json(operations)
                    journal.deleted_count = sum(
                        1
                        for value in operations
                        if value.get("kind") == "subtitle"
                        and value.get("state") == "deleted"
                    )
                    stage = "journal_deleted_%s_%s" % (kind, index)
                    self._commit(journal)
                finally:
                    if unlink_descriptor is not None:
                        os.close(unlink_descriptor)
                    if source_descriptor is not None:
                        os.close(source_descriptor)

            self._verify_protected(plan)
            stage = "final_directory_proof"
            _verify_owner_directories(plan, handed_off, ())
            self._verify_protected(plan)
            journal.status = "deleted_pending_scan"
            action_log.status = "deleted_pending_scan"
            action_log.message = "영상과 전용 자막 영구 삭제 완료 · Plex 부분 스캔 대기"
            stage = "journal_complete"
            self._commit(journal)
            beat()
            return journal
        except Exception as exc:
            try:
                F.db.session.rollback()
            except Exception:
                pass
            current = ModelDirectDeleteJournal.get(journal.id) or journal
            error_errno = getattr(exc, "errno", None)
            if isinstance(exc, DirectDeletePlanError):
                safe_reason = "안전 검증 조건을 통과하지 못했습니다."
            elif isinstance(exc, OSError):
                safe_reason = "파일시스템 작업이 거부되거나 지원되지 않았습니다."
            else:
                safe_reason = "직접 삭제 내부 단계가 실패했습니다."
            diagnostic = (
                "stage=%s; error=%s; errno=%s; journal=%s; action=%s; reason=%s"
                % (
                    stage,
                    exc.__class__.__name__,
                    error_errno if error_errno is not None else "none",
                    getattr(journal, "id", None),
                    getattr(action_log, "id", None),
                    safe_reason,
                )
            )[:2000]
            no_mutation = not mutation_started
            current.status = (
                "failed_no_mutation" if no_mutation else "recovery_required"
            )
            current.finished_at = datetime.now() if no_mutation else None
            current.last_error = diagnostic
            current.unlink_json = _json(operations)
            current.operation_paths_json = _json(operation_paths)
            current.updated_at = datetime.now()
            current_log = ModelActionLog.get(action_log.id)
            current_group = ModelDuplicateGroup.get(group.id)
            retryable = False
            if no_mutation:
                try:
                    retryable = snapshot_matches(plan.video, verify_hash=False)
                    retryable = retryable and all(
                        decision.snapshot is not None
                        and snapshot_matches(decision.snapshot, verify_hash=True)
                        for decision in plan.eligible
                    )
                    if retryable:
                        self._verify_protected(plan)
                        _verify_owner_directories(plan)
                except Exception:
                    retryable = False
            if current_log is not None:
                current_log.status = "blocked" if no_mutation else "unknown"
                current_log.message = diagnostic
            if current_group is not None:
                if no_mutation:
                    current_group.safe_to_delete = bool(retryable)
                    current_group.resolution_status = "open"
                    current_group.safety_flags_json = _json(
                        [] if retryable else ["direct_delete_repreview_required"]
                    )
                else:
                    current_group.safe_to_delete = False
                    current_group.resolution_status = "manual_check_required"
                    current_group.safety_flags_json = _json(
                        ["direct_delete_recovery_required"]
                    )
            try:
                F.db.session.commit()
            except Exception:
                F.db.session.rollback()
            P.logger.warning(
                "Direct delete failed: journal=%s action=%s stage=%s error=%s errno=%s mutation=%s",
                getattr(journal, "id", None),
                getattr(action_log, "id", None),
                stage,
                exc.__class__.__name__,
                error_errno if error_errno is not None else "none",
                "yes" if mutation_started else "no",
            )
            if no_mutation:
                raise RuntimeError(
                    "직접 삭제 전 단계에서 실패했습니다 "
                    "(journal=%s, action=%s, stage=%s, error=%s, errno=%s). "
                    "원본 삭제는 시작되지 않았습니다."
                    % (
                        getattr(journal, "id", None),
                        getattr(action_log, "id", None),
                        stage,
                        exc.__class__.__name__,
                        error_errno if error_errno is not None else "none",
                    )
                ) from None
            raise RuntimeError(
                "직접 삭제가 완결되지 않았습니다 "
                "(journal=%s, action=%s, stage=%s, error=%s, errno=%s). "
                "작업 이력에서 수동 확인하세요."
                % (
                    getattr(journal, "id", None),
                    getattr(action_log, "id", None),
                    stage,
                    exc.__class__.__name__,
                    error_errno if error_errno is not None else "none",
                )
            ) from None

    def execute(
        self,
        plan: DirectDeletePlan,
        expected_digest: str,
        run: Any,
        group: Any,
        candidate: Any,
        keep: Any,
        action_log: ModelActionLog,
        batch_run_id: Optional[int] = None,
        heartbeat: Optional[Any] = None,
        gateway: Optional[Any] = None,
        current_item: Optional[Any] = None,
    ) -> ModelDirectDeleteJournal:
        """Delete the video through PMS, then clean only exact target sidecars."""

        def beat() -> None:
            if callable(heartbeat):
                heartbeat()

        if gateway is None or current_item is None:
            raise DirectDeletePlanError("Plex Media DELETE 실행 컨텍스트가 없습니다.")
        beat()
        if plan.blocking:
            raise DirectDeletePlanError(
                "보호본을 만들 수 없는 관련 자막이 있어 Plex Media DELETE를 차단했습니다."
            )
        if not expected_digest or not secrets.compare_digest(
            str(expected_digest), str(plan.plan_digest)
        ):
            raise DirectDeletePlanError("직접 삭제 계획이 사전확인 이후 변경되었습니다.")
        if not snapshot_matches(plan.video, verify_hash=False):
            raise DirectDeletePlanError("삭제 대상 영상이 사전확인 이후 변경되었습니다.")
        self._verify_related_sidecars(plan, heartbeat=heartbeat)
        self._verify_protected(plan, heartbeat=heartbeat)
        _verify_owner_directories(plan)

        operations: List[Dict[str, Any]] = [
            {
                "kind": "plex_media",
                "state": "planned",
                "media_id": str(candidate.media_id),
                "rating_key": str(group.rating_key),
                "video_snapshot": plan.video.as_dict(),
                "strategy": _HYBRID_STRATEGY,
            }
        ]
        operations.extend(
            {
                "kind": "subtitle",
                "state": "planned",
                "source_path": decision.snapshot.path,
                "snapshot": decision.snapshot.as_dict(),
            }
            for decision in plan.eligible
            if decision.snapshot is not None
        )
        journal = ModelDirectDeleteJournal(
            created_at=datetime.now(),
            updated_at=datetime.now(),
            action_log_id=action_log.id,
            batch_run_id=batch_run_id,
            run_id=run.id,
            group_id=group.id,
            candidate_id=candidate.id,
            keep_candidate_id=keep.id,
            operation_key=secrets.token_hex(24),
            status="planned",
            plan_digest=plan.plan_digest,
            manifest_json=_json(plan.manifest_dict()),
            unlink_json=_json(operations),
            operation_paths_json="[]",
            eligible_count=len(plan.eligible),
            excluded_count=len(plan.excluded),
            protected_count=len(plan.protected),
            deleted_count=0,
        )
        action_log.status = "direct_deleting"
        action_log.message = "Plex Media DELETE 전 외부 자막 보호본 생성 중"
        try:
            F.db.session.add(journal)
            F.db.session.commit()
        except Exception:
            F.db.session.rollback()
            raise RuntimeError("직접 삭제 작업 기록을 저장할 수 없습니다.") from None

        pms_boundary_reached = False
        pms_confirmed = False
        stage = "journal_prepared"
        try:
            journal.status = "preparing"
            self._commit(journal)
            stage = "backup_sidecars"
            self._create_hybrid_backups(plan, journal, heartbeat)
            beat()
            if not snapshot_matches(plan.video, verify_hash=False):
                raise DirectDeletePlanError("PMS DELETE 직전 영상이 변경되었습니다.")
            self._verify_protected(plan, heartbeat=heartbeat)
            self._verify_related_sidecars(plan, heartbeat=heartbeat)
            _verify_owner_directories(plan)

            # Re-read the durable journal and fully hash both sides of every
            # protection copy while the operation is still unambiguously
            # pre-mutation.  A failure here remains failed_no_mutation.  The
            # callee then commits pms_delete_prepared and immediately sends
            # the one allowed PMS request.
            self._verify_persisted_hybrid_backups(plan, journal, heartbeat)

            # From this point onward restart recovery never resends DELETE.
            pms_boundary_reached = True
            stage = "pms_delete_and_reconcile"
            after, _response, protected = self._pms_delete_and_reconcile(
                plan,
                journal,
                operations,
                gateway,
                current_item,
                group,
                candidate,
                action_log,
                heartbeat,
            )
            pms_confirmed = True
            beat()
            # Protected subtitles may have been removed by PMS and restored
            # from the durable copies above.  A safe restore necessarily has a
            # new inode, so the pre-DELETE identity snapshot is no longer the
            # right proof.  Re-run the persisted backup verifier instead.
            self._restore_hybrid_backups(
                journal, include_target=False, heartbeat=heartbeat
            )
            if not _path_proven_absent(plan.video.path):
                raise DirectDeletePlanError("삭제 대상 영상 경로에 파일이 다시 생겼습니다.")
            beat()
            journal.status = "deleted_pending_scan"
            journal.last_error = (
                "PMS DELETE 중 보호 자막 %s개를 복원했습니다."
                % protected.get("restored", 0)
                if protected.get("restored", 0)
                else ""
            )
            action_log.status = "deleted_pending_scan"
            action_log.message = "Plex Media DELETE와 외부 자막 정리 완료 · 부분 스캔 대기"
            action_log.after_json = _json(after.as_dict())
            stage = "journal_complete"
            self._commit(journal)
            beat()
            return journal
        except Exception as exc:
            try:
                F.db.session.rollback()
            except Exception:
                pass
            if exc.__class__.__name__ == "DeletionLeaseLost":
                raise
            current = ModelDirectDeleteJournal.get(journal.id) or journal
            restore_error = ""
            try:
                if pms_boundary_reached:
                    try:
                        durable_operations = json.loads(current.unlink_json or "[]")
                    except (TypeError, ValueError):
                        durable_operations = []
                    durable_confirmed = any(
                        isinstance(raw, dict)
                        and raw.get("kind") == "plex_media"
                        and raw.get("state") == "pms_delete_confirmed"
                        for raw in durable_operations
                    )
                    self._restore_hybrid_backups(
                        current,
                        include_target=(not durable_confirmed)
                        or not _path_proven_absent(plan.video.path),
                        heartbeat=heartbeat,
                    )
                else:
                    self.cleanup_backups(current, heartbeat=heartbeat)
            except Exception as restore_exc:
                if restore_exc.__class__.__name__ == "DeletionLeaseLost":
                    try:
                        F.db.session.rollback()
                    except Exception:
                        pass
                    raise
                restore_error = restore_exc.__class__.__name__
                try:
                    F.db.session.rollback()
                except Exception:
                    pass
                current = ModelDirectDeleteJournal.get(journal.id) or current

            current.status = (
                "recovery_required" if pms_boundary_reached else "failed_no_mutation"
            )
            current.finished_at = None if pms_boundary_reached else datetime.now()
            current.last_error = (
                "stage=%s; error=%s; pms_boundary=%s; pms_confirmed=%s; restore_error=%s"
                % (
                    stage,
                    exc.__class__.__name__,
                    "yes" if pms_boundary_reached else "no",
                    "yes" if pms_confirmed else "no",
                    restore_error or "none",
                )
            )[:2000]
            current.updated_at = datetime.now()
            current_log = ModelActionLog.get(action_log.id)
            current_group = ModelDuplicateGroup.get(group.id)
            if current_log is not None:
                current_log.status = "unknown" if pms_boundary_reached else "blocked"
                current_log.message = current.last_error
            if current_group is not None:
                current_group.safe_to_delete = False
                current_group.resolution_status = (
                    "manual_check_required" if pms_boundary_reached else "open"
                )
                current_group.safety_flags_json = _json(
                    [
                        "direct_delete_recovery_required"
                        if pms_boundary_reached
                        else "direct_delete_repreview_required"
                    ]
                )
            try:
                F.db.session.commit()
            except Exception:
                F.db.session.rollback()
            P.logger.warning(
                "Hybrid direct delete failed: journal=%s action=%s stage=%s error=%s pms_boundary=%s",
                getattr(journal, "id", None),
                getattr(action_log, "id", None),
                stage,
                exc.__class__.__name__,
                pms_boundary_reached,
            )
            if not pms_boundary_reached:
                raise RuntimeError(
                    "Plex Media DELETE 전 안전 검증에 실패했습니다. 원본 삭제는 시작되지 않았습니다."
                ) from None
            raise RuntimeError(
                "Plex Media DELETE 결과를 완전히 확정하지 못했습니다. "
                "자동 재시도하지 않으며 작업 이력을 확인하세요."
            ) from None

    def _legacy_recover_interrupted(self) -> int:
        count = 0
        for journal in ModelDirectDeleteJournal.unfinished():
            if journal.status in ("deleted_pending_scan", "scan_running"):
                if (
                    journal.action_log_id
                    and ModelPostDeleteScanJob.active_for_action(journal.action_log_id)
                    is not None
                ):
                    continue
            operations: Any = []
            operation_paths: Any = []
            try:
                operations = json.loads(getattr(journal, "unlink_json", "[]") or "[]")
                operation_paths = json.loads(
                    getattr(journal, "operation_paths_json", "[]") or "[]"
                )
            except (TypeError, ValueError):
                operations = []
                operation_paths = []
            new_source_only = bool(operations) and not bool(operation_paths)
            if new_source_only:
                for raw in operations:
                    strategy = (
                        raw.get("handoff_strategy")
                        if isinstance(raw, dict)
                        else None
                    )
                    state = raw.get("state") if isinstance(raw, dict) else None
                    source_only_state = (
                        strategy == "same_parent_v2" and state == "pending"
                    ) or (
                        strategy == "posix_dirfd_unlink_v1"
                        and state == "direct_unlink_prepared"
                    )
                    if (
                        not isinstance(raw, dict)
                        or not source_only_state
                    ):
                        new_source_only = False
                        break
                    source = str(raw.get("source_path") or "")
                    tombstone = str(raw.get("tombstone_path") or "")
                    try:
                        if (
                            not source
                            or not os.path.lexists(source)
                            or not tombstone
                            or os.path.lexists(tombstone)
                        ):
                            new_source_only = False
                            break
                    except (OSError, ValueError):
                        new_source_only = False
                        break
            if new_source_only:
                journal.status = "failed_no_mutation"
                journal.finished_at = datetime.now()
                journal.last_error = (
                    "stage=startup_recovery; error=Interrupted; errno=none; "
                    "journal=%s; action=%s; message=원본 삭제 시작 전 작업이 중단되었습니다."
                    % (journal.id, journal.action_log_id)
                )
                journal.updated_at = datetime.now()
                action = (
                    ModelActionLog.get(journal.action_log_id)
                    if journal.action_log_id
                    else None
                )
                if action is not None:
                    action.status = "blocked"
                    action.message = journal.last_error
                group = ModelDuplicateGroup.get(journal.group_id)
                if group is not None:
                    group.safe_to_delete = False
                    group.resolution_status = "open"
                    group.safety_flags_json = _json(
                        ["direct_delete_repreview_required"]
                    )
                count += 1
                continue
            journal.status = "recovery_required"
            journal.last_error = (
                "FlaskFarm 재시작으로 직접 삭제 단계를 확정할 수 없습니다. "
                "자동 재시도하지 않으며 수동 확인이 필요합니다."
            )
            journal.updated_at = datetime.now()
            action = (
                ModelActionLog.get(journal.action_log_id)
                if journal.action_log_id
                else None
            )
            if action is not None:
                action.status = "unknown"
                action.message = journal.last_error
            group = ModelDuplicateGroup.get(journal.group_id)
            if group is not None:
                group.safe_to_delete = False
                group.resolution_status = "manual_check_required"
                group.safety_flags_json = _json(
                    ["direct_delete_recovery_required"]
                )
            count += 1
        if count:
            F.db.session.commit()
        return count

    def recover_interrupted(self, heartbeat: Optional[Any] = None) -> int:
        """Recover under the DB singleton lease and never resend PMS DELETE.

        Once ``pms_delete_prepared`` is durable, the process cannot know
        whether PMS received the prior request.  A recovery owner therefore
        restores *all* related sidecars from the durable copies, leaves the
        operation for manual reconciliation, and performs no Plex mutation.
        """

        def beat() -> None:
            if callable(heartbeat):
                heartbeat()

        count = 0
        for journal in ModelDirectDeleteJournal.unfinished():
            if journal.status in ("deleted_pending_scan", "scan_running") and (
                journal.action_log_id
                and ModelPostDeleteScanJob.active_for_action(journal.action_log_id)
                is not None
            ):
                continue
            try:
                manifest = json.loads(journal.manifest_json or "{}")
                operations = json.loads(journal.unlink_json or "[]")
            except (TypeError, ValueError):
                manifest, operations = {}, []
            hybrid = isinstance(manifest, dict) and (
                str(manifest.get("execution_strategy") or "") == _HYBRID_STRATEGY
            )
            # Hybrid filesystem recovery is allowed only for the caller that
            # can continuously prove ownership of the DB recovery lease.
            if hybrid and not callable(heartbeat):
                continue
            plex_operation = next(
                (
                    raw
                    for raw in operations
                    if isinstance(raw, dict) and raw.get("kind") == "plex_media"
                ),
                {},
            ) if isinstance(operations, list) else {}
            before_pms = bool(
                hybrid
                and journal.status in ("planned", "preparing")
                and str(plex_operation.get("state") or "") == "planned"
            )
            beat()
            if before_pms:
                cleanup_failed = False
                try:
                    self.cleanup_backups(journal, heartbeat=heartbeat)
                except Exception as exc:
                    F.db.session.rollback()
                    if exc.__class__.__name__ == "DeletionLeaseLost":
                        raise
                    cleanup_failed = True
                beat()
                if cleanup_failed:
                    journal = ModelDirectDeleteJournal.get(journal.id) or journal
                    target_status = "recovery_required"
                    target_finished_at = None
                    message = "PMS DELETE 전 보호 저장 정리를 확정할 수 없어 수동 확인이 필요합니다."
                else:
                    target_status = "failed_no_mutation"
                    target_finished_at = datetime.now()
                    message = "재시작으로 PMS DELETE 전 준비가 중단되었습니다. 다시 사전확인하세요."
            elif hybrid:
                restored = None
                restore_error = ""
                try:
                    restored = self._restore_hybrid_backups(
                        journal, include_target=True, heartbeat=heartbeat
                    )
                except Exception as exc:
                    F.db.session.rollback()
                    if exc.__class__.__name__ == "DeletionLeaseLost":
                        raise
                    restore_error = exc.__class__.__name__
                    journal = ModelDirectDeleteJournal.get(journal.id) or journal
                beat()
                target_status = "recovery_required"
                target_finished_at = None
                if restore_error:
                    message = (
                        "PMS DELETE는 재전송하지 않았습니다. 자막 보호본 자동 복원을 "
                        "완결하지 못해 수동 확인이 필요합니다. (error=%s)"
                        % restore_error
                    )
                else:
                    message = (
                        "PMS DELETE는 재전송하지 않았습니다. 관련 자막 보호본을 "
                        "재검증하고 %s개를 복원했으며 수동 확인이 필요합니다."
                        % int((restored or {}).get("restored", 0))
                    )
            else:
                target_status = "recovery_required"
                target_finished_at = None
                message = "이전 직접 삭제 방식 작업이 중단되어 자동 재시도하지 않습니다."
            beat()
            journal.status = target_status
            journal.finished_at = target_finished_at
            journal.last_error = message
            journal.updated_at = datetime.now()
            action = (
                ModelActionLog.get(journal.action_log_id)
                if journal.action_log_id
                else None
            )
            group = ModelDuplicateGroup.get(journal.group_id)
            if action is not None:
                action.status = (
                    "blocked" if journal.status == "failed_no_mutation" else "unknown"
                )
                action.message = message
            if group is not None:
                group.safe_to_delete = False
                group.resolution_status = (
                    "open"
                    if journal.status == "failed_no_mutation"
                    else "manual_check_required"
                )
                group.safety_flags_json = _json(
                    [
                        "direct_delete_repreview_required"
                        if journal.status == "failed_no_mutation"
                        else "direct_delete_recovery_required"
                    ]
                )
            F.db.session.commit()
            count += 1
        completed = getattr(
            ModelDirectDeleteJournal, "completed_with_backups", None
        )
        if callable(completed) and callable(heartbeat):
            for completed_journal in completed():
                try:
                    beat()
                    completed_manifest = json.loads(
                        completed_journal.manifest_json or "{}"
                    )
                    if not isinstance(completed_manifest, dict) or str(
                        completed_manifest.get("execution_strategy") or ""
                    ) != _HYBRID_STRATEGY:
                        continue
                    self.cleanup_backups(
                        completed_journal, heartbeat=heartbeat
                    )
                    beat()
                    completed_journal.last_error = ""
                    self._commit(completed_journal)
                except Exception as exc:
                    F.db.session.rollback()
                    if exc.__class__.__name__ == "DeletionLeaseLost":
                        raise
                    P.logger.warning(
                        "Deferred direct-delete backup cleanup failed: journal=%s error=%s",
                        getattr(completed_journal, "id", None),
                        exc.__class__.__name__,
                    )
        return count

    def _legacy_verify_deleted(
        self, journal: ModelDirectDeleteJournal, heartbeat: Optional[Any] = None
    ) -> Dict[str, int]:
        try:
            manifest = json.loads(journal.manifest_json or "{}")
            operations = json.loads(journal.unlink_json or "[]")
            operation_paths = json.loads(journal.operation_paths_json or "[]")
        except (TypeError, ValueError):
            raise DirectDeletePlanError("직접 삭제 journal을 읽을 수 없습니다.") from None
        if not isinstance(manifest, dict) or not isinstance(operations, list):
            raise DirectDeletePlanError("직접 삭제 journal이 올바르지 않습니다.")
        if not operations or not isinstance(operation_paths, list):
            raise DirectDeletePlanError("직접 삭제 작업 기록이 비어 있습니다.")

        video_count = 0
        verified = 0
        for raw in operations:
            if callable(heartbeat):
                heartbeat()
            if not isinstance(raw, dict) or raw.get("state") != "deleted":
                raise DirectDeletePlanError("일부 파일의 영구 삭제 상태를 확정할 수 없습니다.")
            source = str(raw.get("source_path") or "")
            tombstone = str(raw.get("tombstone_path") or "")
            if not source or not tombstone or os.path.lexists(source) or os.path.lexists(tombstone):
                raise DirectDeletePlanError("삭제 경로에 파일이 다시 생겨 자동 확정하지 않습니다.")
            if raw.get("kind") == "video":
                video_count += 1
            elif raw.get("kind") != "subtitle":
                raise DirectDeletePlanError("직접 삭제 작업 종류가 올바르지 않습니다.")
            verified += 1
        for raw in operation_paths:
            if not isinstance(raw, dict) or raw.get("state") != "removed":
                raise DirectDeletePlanError("직접 삭제 작업 폴더 정리가 완료되지 않았습니다.")
            if os.path.lexists(str(raw.get("path") or "")):
                raise DirectDeletePlanError("직접 삭제 작업 폴더가 남아 있습니다.")
        if video_count != 1:
            raise DirectDeletePlanError("영구 삭제된 영상 파일 수가 올바르지 않습니다.")

        for raw in manifest.get("survivors", []):
            if callable(heartbeat):
                heartbeat()
            snapshot = _snapshot_from_dict(raw)
            if not snapshot_matches(snapshot, verify_hash=False):
                raise DirectDeletePlanError("유지 영상이 직접 삭제 당시와 달라졌습니다.")
        for decision in manifest.get("protected", []):
            if callable(heartbeat):
                heartbeat()
            if not isinstance(decision, dict) or not isinstance(
                decision.get("snapshot"), dict
            ):
                raise DirectDeletePlanError("유지 자막 snapshot 기록이 없습니다.")
            snapshot = _snapshot_from_dict(decision["snapshot"])
            if not snapshot_matches(snapshot, verify_hash=True):
                raise DirectDeletePlanError("유지 자막이 직접 삭제 당시와 달라졌습니다.")
        return {"verified": verified, "videos": video_count}

    def verify_deleted(
        self,
        journal: ModelDirectDeleteJournal,
        heartbeat: Optional[Any] = None,
        intentionally_deleted_paths: Sequence[str] = (),
    ) -> Dict[str, int]:
        """Verify hybrid video/sidecar absence and restore survivor subtitles."""

        try:
            manifest = json.loads(journal.manifest_json or "{}")
            operations = json.loads(journal.unlink_json or "[]")
        except (TypeError, ValueError):
            raise DirectDeletePlanError("직접 삭제 journal을 읽을 수 없습니다.") from None
        if not isinstance(manifest, dict) or not isinstance(operations, list):
            raise DirectDeletePlanError("직접 삭제 journal 형식이 올바르지 않습니다.")
        if str(manifest.get("execution_strategy") or "") != _HYBRID_STRATEGY:
            return self._legacy_verify_deleted(journal, heartbeat=heartbeat)

        video_raw = manifest.get("video")
        if not isinstance(video_raw, dict):
            raise DirectDeletePlanError("삭제 대상 영상 snapshot 기록이 없습니다.")
        video = _snapshot_from_dict(video_raw)
        if not _path_proven_absent(video.path):
            raise DirectDeletePlanError("PMS DELETE 대상 영상 경로에 파일이 남아 있습니다.")
        plex_operations = [
            raw
            for raw in operations
            if isinstance(raw, dict) and raw.get("kind") == "plex_media"
        ]
        if (
            len(plex_operations) != 1
            or plex_operations[0].get("state") != "pms_delete_confirmed"
        ):
            raise DirectDeletePlanError("PMS Media DELETE 확정 기록이 없습니다.")

        verified = 1
        for raw in operations:
            if callable(heartbeat):
                heartbeat()
            if not isinstance(raw, dict) or raw.get("kind") == "plex_media":
                continue
            if raw.get("kind") != "subtitle" or raw.get("state") not in (
                "removed_by_plex",
                "deleted_by_plugin",
            ):
                raise DirectDeletePlanError("외부 자막 정리 상태를 확정할 수 없습니다.")
            if not _path_proven_absent(str(raw.get("source_path") or "")):
                raise DirectDeletePlanError("정리 대상 자막 경로에 파일이 다시 생겼습니다.")
            verified += 1
        intentional = {
            os.path.normcase(os.path.abspath(str(path)))
            for path in intentionally_deleted_paths
            if str(path or "")
        }
        for raw in manifest.get("survivors", []):
            if callable(heartbeat):
                heartbeat()
            survivor = _snapshot_from_dict(raw)
            if os.path.normcase(os.path.abspath(survivor.path)) in intentional:
                continue
            if not snapshot_matches(survivor, verify_hash=False):
                raise DirectDeletePlanError("유지 영상이 PMS DELETE 당시와 달라졌습니다.")
        protected = self._restore_hybrid_backups(
            journal,
            include_target=False,
            heartbeat=heartbeat,
            skip_source_paths=intentionally_deleted_paths,
        )
        return {
            "verified": verified,
            "videos": 1,
            "restored": int(protected.get("restored", 0)),
        }
