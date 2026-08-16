from __future__ import annotations

from typing import Any, Dict, Optional


def current_delete_attempt_limit() -> Optional[int]:
    """Compatibility shim for callers from older rolling plugin workers.

    Per-scan deletion attempts are no longer capped.  In particular, a legacy
    ``setting_max_delete_per_run`` value may remain in an existing FlaskFarm
    settings table, but it is deliberately ignored and never rewritten.
    """

    return None


def delete_attempt_budget(run: Any, limit: Any = None) -> Dict[str, Any]:
    """Build the public, explicitly-unlimited attempt-counter view."""

    # ``limit`` is intentionally accepted but ignored for compatibility with
    # an already-loaded worker during a rolling plugin reload.
    del limit
    try:
        attempted = int(getattr(run, "deletion_attempts", 0) or 0)
        if attempted < 0:
            attempted = 0
    except (TypeError, ValueError):
        attempted = 0
    return {
        "unlimited": True,
        "attempted": attempted,
        "limit": None,
        "remaining": None,
        "exhausted": False,
    }


def delete_attempt_limit_message(budget: Dict[str, Any]) -> str:
    """Legacy helper retained for rolling imports; no live path calls it."""

    return "삭제 시도 횟수는 무제한입니다. (현재 시도 %s회)" % int(
        budget.get("attempted", 0) or 0
    )


def require_delete_attempt_available(run: Any) -> Dict[str, Any]:
    return delete_attempt_budget(run)
