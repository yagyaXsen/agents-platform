"""Appointment slots: generate free times and understand which one a customer picked."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Hours:
    timezone: str = "Asia/Kolkata"
    open_hour: int = 10
    close_hour: int = 18
    slot_minutes: int = 60
    workdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5)  # Mon=0 … Sat=5
    lead_minutes: int = 120  # earliest bookable time from now
    days_ahead: int = 7


def label(dt: datetime) -> str:
    return f"{dt:%a} {dt.day} {dt:%b}, {dt:%H:%M}"


_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}


def not_before(text: str | None, local_today: date) -> date | None:
    """Earliest day the customer asked for, from phrases like 'tomorrow',
    'next week', 'this weekend' or 'thursday'. None means 'as soon as possible'."""
    if not text:
        return None
    low = text.lower()
    if "day after tomorrow" in low:
        return local_today + timedelta(days=2)
    if re.search(r"\btomorrow\b|\bkal\b", low):
        return local_today + timedelta(days=1)
    if "next week" in low:
        return local_today + timedelta(days=7 - local_today.weekday())
    if "weekend" in low:
        return local_today + timedelta(days=(5 - local_today.weekday()) % 7)
    for word, wd in _WEEKDAYS.items():
        if re.search(rf"\b{word}\b", low):
            return local_today + timedelta(days=(wd - local_today.weekday()) % 7)
    return None


def upcoming(
    now: datetime, hours: Hours, taken: set[datetime], limit: int = 3, start: date | None = None
) -> list[datetime]:
    """Next free slots in the business's timezone, from `start` (a local date) if given.
    `taken` holds UTC datetimes."""
    tz = ZoneInfo(hours.timezone)
    local_now = now.astimezone(tz)
    earliest = local_now + timedelta(minutes=hours.lead_minutes)
    taken_utc = {t.astimezone(timezone.utc).replace(second=0, microsecond=0) for t in taken}

    out: list[datetime] = []
    day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if start and start > day.date():
        day = day.replace(year=start.year, month=start.month, day=start.day)
    for _ in range(hours.days_ahead + 1):
        if day.weekday() in hours.workdays:
            t = day.replace(hour=hours.open_hour)
            end = day.replace(hour=hours.close_hour)
            while t + timedelta(minutes=hours.slot_minutes) <= end:
                if t >= earliest and t.astimezone(timezone.utc) not in taken_utc:
                    out.append(t)
                    if len(out) >= limit:
                        return out
                t += timedelta(minutes=hours.slot_minutes)
        day = (day + timedelta(days=1)).replace(hour=0)
    return out


_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": -1}


def pick(text: str, labels: list[str]) -> int | None:
    """Return the index of the slot the customer chose, or None if unclear."""
    if not labels:
        return None
    low = text.lower().strip()

    m = re.fullmatch(r"\D*(\d)\D*", low)
    if m and not re.search(r"\d\s*(am|pm|o'?clock)\b", low):
        i = int(m.group(1)) - 1
        return i if 0 <= i < len(labels) else None

    for word, i in _ORDINALS.items():
        if re.search(rf"\b{word}\b", low):
            return i % len(labels)

    # Match by weekday and/or time, e.g. "thursday", "16:30", "4:30"
    matches = []
    for i, lab in enumerate(labels):
        day = lab[:3].lower()
        time = lab.split(", ")[-1]
        h, mnt = time.split(":")
        h12 = str(int(h) % 12 or 12)
        time_hit = time in low or f"{h12}:{mnt}" in low or re.search(rf"\b{h12}\s*(am|pm)\b", low)
        day_hit = day in low
        if day_hit and time_hit:
            return i
        if day_hit or time_hit:
            matches.append(i)
    return matches[0] if len(matches) == 1 else None
