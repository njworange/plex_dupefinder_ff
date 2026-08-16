from __future__ import annotations

from typing import Any, Dict

from .setup import P


DEFAULT_DELETE_ATTEMPT_LIMIT = 1
MIN_DELETE_ATTEMPT_LIMIT = 1
MAX_DELETE_ATTEMPT_LIMIT = 100


def current_delete_attempt_limit() -> int:
    """Return the live per-scan attempt limit without rewriting settings.

    Missing, empty, or malformed values fail closed to the existing one-attempt
    default. Persisted values are clamped to the range accepted by the settings
    form.
    """

    raw = P.ModelSetting.get("setting_max_delete_per_run")
    if raw is None or not str(raw).strip():
        return DEFAULT_DELETE_ATTEMPT_LIMIT
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DELETE_ATTEMPT_LIMIT
    return max(MIN_DELETE_ATTEMPT_LIMIT, min(MAX_DELETE_ATTEMPT_LIMIT, value))


def delete_attempt_budget(run: Any, limit: Any = None) -> Dict[str, Any]:
    """Build a consistent public budget view for one scan run."""

    effective_limit = (
        current_delete_attempt_limit()
        if limit is None
        else max(MIN_DELETE_ATTEMPT_LIMIT, min(MAX_DELETE_ATTEMPT_LIMIT, int(limit)))
    )
    try:
        attempted = int(getattr(run, "deletion_attempts", 0) or 0)
        if attempted < 0:
            attempted = effective_limit
    except (TypeError, ValueError):
        # A corrupt counter must never create extra deletion capacity.
        attempted = effective_limit
    remaining = max(0, effective_limit - attempted)
    return {
        "limit": effective_limit,
        "attempted": attempted,
        "remaining": remaining,
        "exhausted": remaining == 0,
    }


def delete_attempt_limit_message(budget: Dict[str, Any]) -> str:
    return (
        "이 스캔의 삭제 시도 한도에 도달했습니다. "
        "(사용 %(attempted)s/%(limit)s, 남음 %(remaining)s) "
        "더 처리하려면 설정 > 삭제 안전장치에서 한도를 늘리거나 새 중복 검사를 실행하세요."
        % budget
    )


def require_delete_attempt_available(run: Any) -> Dict[str, Any]:
    budget = delete_attempt_budget(run)
    if budget["exhausted"]:
        raise RuntimeError(delete_attempt_limit_message(budget))
    return budget
