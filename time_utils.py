"""
time_utils.py — helpers for shift duration and cost calculation.
"""
import datetime


def _parse_hhmm(t: str) -> int:
    """Parse HH:MM string → total minutes. Returns -1 on error."""
    try:
        h, m = t.strip().split(':')
        return int(h) * 60 + int(m)
    except Exception:
        return -1


def shift_duration_hours(date: str, start: str, end: str) -> float | None:
    """
    Compute shift duration in hours.
    Handles overnight shifts (end < start → add 24h).
    Returns None if inputs are invalid.
    """
    s = _parse_hhmm(start)
    e = _parse_hhmm(end)
    if s < 0 or e < 0:
        return None
    minutes = e - s
    if minutes <= 0:
        minutes += 24 * 60  # overnight shift
    return round(minutes / 60, 4)


def compute_amount_vnd(date: str, start: str, end: str, rate_per_hour: int) -> int | None:
    """
    Compute total amount in VND for a shift.
    date: YYYY-MM-DD (unused for now, reserved for future holiday rates)
    start/end: HH:MM
    rate_per_hour: VND per hour (int)
    Returns rounded int VND or None on error.
    """
    try:
        hours = shift_duration_hours(date, start, end)
        if hours is None or hours <= 0:
            return None
        return round(hours * int(rate_per_hour))
    except Exception:
        return None
