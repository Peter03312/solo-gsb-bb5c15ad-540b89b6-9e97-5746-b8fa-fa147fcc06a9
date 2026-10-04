"""Whole-request semantic validation.

Any failure rejects the entire submission; the list contains one entry per
violated rule.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .schemas import (
    ApplicationCreate,
    AvailableRangesRequest,
    RestrictionWindowRequest,
)

RADIUS_NOT_POSITIVE = "radius_not_positive"
FREQUENCY_LOW_ABOVE_HIGH = "frequency_low_above_high"
TIME_START_NOT_BEFORE_END = "time_start_not_before_end"
VALUE_NOT_FINITE = "value_not_finite"
NAIVE_DATETIME = "naive_datetime"
GUARD_BAND_NEGATIVE = "guard_band_negative"
CANDIDATE_LOW_MIN_ABOVE_MAX = "candidate_low_min_above_max"
RESTRICTION_WINDOW_EXPIRED = "restriction_window_expired"


def _shape_errors(
    circle: Any,
    frequency: Any,
    time_window: Any,
    *,
    radius_field: str = "circle.radius",
) -> list[dict[str, str]]:
    """Shared geometry/band/half-open-time validation.

    Used by both application submission and restriction-window proposals:
    the two shapes carry identical constraints (finite coordinates, strictly
    positive radius, ordered closed frequency band, aware half-open time).
    """
    errors: list[dict[str, str]] = []

    numbers = {
        "circle.cx": circle.cx,
        "circle.cy": circle.cy,
        radius_field: circle.radius,
        "frequency.low": frequency.low,
        "frequency.high": frequency.high,
    }
    for field, value in numbers.items():
        if not math.isfinite(value):
            errors.append(
                {
                    "field": field,
                    "code": VALUE_NOT_FINITE,
                    "message": f"{field} must be a finite number",
                }
            )

    if circle.radius <= 0:
        errors.append(
            {
                "field": radius_field,
                "code": RADIUS_NOT_POSITIVE,
                "message": "radius must be strictly positive",
            }
        )

    if frequency.low > frequency.high:
        errors.append(
            {
                "field": "frequency.low",
                "code": FREQUENCY_LOW_ABOVE_HIGH,
                "message": "frequency low bound must not exceed high bound",
            }
        )

    start = time_window.start
    end = time_window.end
    if start.tzinfo is None or end.tzinfo is None:
        errors.append(
            {
                "field": "time_window",
                "code": NAIVE_DATETIME,
                "message": "time_window.start and end must carry a timezone (use UTC, e.g. ...Z)",
            }
        )
    elif start >= end:
        # Equal instants are also rejected: start must be strictly earlier.
        errors.append(
            {
                "field": "time_window.start",
                "code": TIME_START_NOT_BEFORE_END,
                "message": "time_window start must be strictly earlier than end",
            }
        )

    return errors


def validate_application(payload: ApplicationCreate) -> list[dict[str, str]]:
    return _shape_errors(payload.circle, payload.frequency, payload.time_window)


def validate_restriction_window(
    payload: RestrictionWindowRequest, *, now: datetime | None = None
) -> list[dict[str, str]]:
    """Validate a restriction-window proposal.

    Beyond the shared shape rules a window may not already be expired at
    confirmation/preview time: its half-open interval must extend past the
    current UTC instant (``end == now`` is already expired).  Windows that
    start in the past but are still ongoing are allowed (the control starts
    immediately).  An expired proposal is refused as an illegal range without
    touching licenses, the queue or the revision.
    """
    errors = _shape_errors(
        payload.circle, payload.frequency, payload.time_window
    )
    start, end = payload.time_window.start, payload.time_window.end
    if start.tzinfo is not None and end.tzinfo is not None and start < end:
        current = now if now is not None else datetime.now(timezone.utc)
        if end <= current.astimezone(timezone.utc):
            errors.append(
                {
                    "field": "time_window.end",
                    "code": RESTRICTION_WINDOW_EXPIRED,
                    "message": (
                        "restriction window time_window must end in the future "
                        "(a half-open interval ending at or before now is expired)"
                    ),
                }
            )
    return errors


def validate_guard_band(value: float) -> list[dict[str, str]]:
    """A guard band must be a finite, non-negative number."""
    errors: list[dict[str, str]] = []
    if not math.isfinite(value):
        errors.append(
            {
                "field": "guard_band",
                "code": VALUE_NOT_FINITE,
                "message": "guard_band must be a finite number",
            }
        )
    elif value < 0:
        errors.append(
            {
                "field": "guard_band",
                "code": GUARD_BAND_NEGATIVE,
                "message": "guard_band must be non-negative",
            }
        )
    return errors


def validate_candidate_low_range(
    payload: AvailableRangesRequest,
) -> list[dict[str, str]]:
    """Candidate lower-bound interval: finite numbers, min <= max.

    Equal bounds are allowed (a single candidate position); only an
    inverted interval is rejected.
    """
    errors: list[dict[str, str]] = []
    numbers = {
        "candidate_low_min": payload.candidate_low_min,
        "candidate_low_max": payload.candidate_low_max,
    }
    for field, value in numbers.items():
        if not math.isfinite(value):
            errors.append(
                {
                    "field": field,
                    "code": VALUE_NOT_FINITE,
                    "message": f"{field} must be a finite number",
                }
            )

    if payload.candidate_low_min > payload.candidate_low_max:
        errors.append(
            {
                "field": "candidate_low_min",
                "code": CANDIDATE_LOW_MIN_ABOVE_MAX,
                "message": "candidate_low_min must not exceed candidate_low_max",
            }
        )
    return errors
