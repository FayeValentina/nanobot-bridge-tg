"""Local question/address checks and explicit waiting deadlines; no model calls."""

from __future__ import annotations

import math
from datetime import datetime


def wait_duration(seconds: float | None, *, infinite: bool, until: str | None,
                  on_timeout: bool, now: float, maximum: float) -> tuple[float | None, float | None]:
    """Return duration and an optional absolute deadline, with exactly one mode."""
    if sum((seconds is not None, infinite, until is not None)) != 1:
        raise ValueError("choose exactly one of seconds, infinite=true, or until")
    if infinite:
        if on_timeout:
            raise ValueError("an infinite wait cannot request a timeout wake")
        return None, None
    if until is not None:
        try:
            date = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if "T" not in until or date.tzinfo is None or date.utcoffset() is None:
                raise ValueError("until requires an ISO 8601 timestamp with an explicit timezone offset or Z")
            deadline = date.timestamp()
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("until requires an ISO 8601 timestamp with an explicit timezone offset or Z") from exc
        duration = deadline - now
        if not math.isfinite(duration) or not 0 < duration <= maximum:
            raise ValueError("absolute wait deadline must be future and within the operator limit")
        return duration, deadline
    assert seconds is not None
    if not math.isfinite(seconds) or not 1 <= seconds <= maximum:
        raise ValueError("wait duration exceeds the operator limit")
    return seconds, None
