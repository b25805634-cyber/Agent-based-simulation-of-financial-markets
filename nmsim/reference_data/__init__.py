"""Versioned real-event reference data and an opt-in timeline loader.

The established :func:`nmsim.validation.load_reference` function remains the
compatibility surface for ``--reference``.  This module adds a stricter,
structured loader for research code that needs timestamp alignment and a
public-news timeline.  Timeline ingestion is deliberately opt-in: a caller
must provide both the timeline path and ``include_news_timeline=True``.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


NEWS_TIMELINE_SCHEMA_VERSION = "news_timeline_v1"
DEFAULT_EXCHANGE_TIMEZONE = "America/New_York"


class ReferenceDataError(ValueError):
    """Raised when a reference artifact violates the documented schema."""


@dataclass(frozen=True)
class ReferencePoint:
    """One observed market session in a reference episode."""

    timestamp: str
    session_date: date
    price: float
    news: str
    t: int


@dataclass(frozen=True)
class NewsTimelineEvent:
    """One explicitly public and source-traceable timeline item."""

    event_id: str
    timestamp: str
    session_date: date
    price_anchor_t: int
    delivery_t: int
    public_text: str
    source_title: str
    source_url: str
    source_published_date: date


@dataclass(frozen=True)
class ReferenceEpisode:
    """A validated price path with an optional, explicitly loaded timeline."""

    points: tuple[ReferencePoint, ...]
    shock_idx: int
    exchange_timezone: str
    news_timeline: tuple[NewsTimelineEvent, ...] = ()

    @property
    def prices(self) -> list[float]:
        return [point.price for point in self.points]

    @property
    def t0_date(self) -> date:
        return self.points[self.shock_idx].session_date

    def as_legacy_tuple(self) -> tuple[list[float], int]:
        """Return the shape used by ``validation.load_reference``."""

        return self.prices, self.shock_idx


def _exchange_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ReferenceDataError(f"unknown exchange timezone: {name!r}") from error


@dataclass(frozen=True)
class _TemporalValue:
    session_date: date
    local_time: Optional[time]

    @property
    def date_only(self) -> bool:
        return self.local_time is None


def _temporal_value(raw: object, zone: ZoneInfo, *, field: str) -> _TemporalValue:
    """Resolve an ISO date/session label or an offset-aware ISO datetime.

    Date-only values are trading-calendar labels and are not converted between
    zones.  Datetimes denote instants, must carry an offset, and are converted
    to the configured exchange timezone before their session date is taken.
    Naive datetimes are rejected instead of being silently assigned a zone.
    """

    value = str(raw or "").strip()
    if not value:
        raise ReferenceDataError(f"missing {field}")
    try:
        if len(value) == 10:
            return _TemporalValue(date.fromisoformat(value), None)
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        instant = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ReferenceDataError(f"invalid ISO {field}: {value!r}") from error
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ReferenceDataError(
            f"naive datetime is not allowed for {field}: {value!r}"
        )
    local = instant.astimezone(zone)
    return _TemporalValue(local.date(), local.timetz().replace(tzinfo=None))


def _session_date(raw: object, zone: ZoneInfo, *, field: str) -> date:
    return _temporal_value(raw, zone, field=field).session_date


def _calendar_date(raw: object, *, field: str) -> date:
    value = str(raw or "").strip()
    if len(value) != 10:
        raise ReferenceDataError(f"{field} must be a date-only ISO value")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ReferenceDataError(f"invalid ISO {field}: {value!r}") from error


def _price(raw: object, *, row_number: int) -> float:
    value = str(raw or "").strip()
    if not value:
        raise ReferenceDataError(f"missing price at CSV row {row_number}")
    try:
        parsed = float(value)
    except ValueError as error:
        raise ReferenceDataError(
            f"invalid price at CSV row {row_number}: {value!r}"
        ) from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ReferenceDataError(
            f"price must be finite and positive at CSV row {row_number}"
        )
    return parsed


def _column_map(fieldnames: Optional[list[str]]) -> dict[str, str]:
    return {name.strip().lower(): name for name in (fieldnames or []) if name}


def _load_points(path: Path, zone: ZoneInfo) -> list[ReferencePoint]:
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise ReferenceDataError(f"cannot open reference CSV {path}: {error}") from error

    with stream:
        reader = csv.DictReader(stream)
        columns = _column_map(reader.fieldnames)
        timestamp_column = columns.get("timestamp") or columns.get("date")
        price_column = columns.get("price") or columns.get("close")
        news_column = columns.get("news")
        if timestamp_column is None or price_column is None:
            raise ReferenceDataError(
                "reference CSV requires timestamp (or legacy date) and price (or close) columns"
            )

        raw_points: list[tuple[str, date, float, str]] = []
        for row_number, row in enumerate(reader, start=2):
            timestamp = str(row.get(timestamp_column) or "").strip()
            session_date = _session_date(
                timestamp, zone, field=f"timestamp at CSV row {row_number}"
            )
            price = _price(row.get(price_column), row_number=row_number)
            news = str(row.get(news_column) or "").strip() if news_column else ""
            if raw_points and session_date <= raw_points[-1][1]:
                raise ReferenceDataError(
                    "reference timestamps must resolve to unique increasing session dates"
                )
            raw_points.append((timestamp, session_date, price, news))

    if not raw_points:
        raise ReferenceDataError("reference CSV contains no observations")
    return [
        ReferencePoint(
            timestamp=timestamp,
            session_date=session_date,
            price=price,
            news=news,
            t=index,
        )
        for index, (timestamp, session_date, price, news) in enumerate(raw_points)
    ]


def _validated_https_url(raw: object, *, line_number: int) -> str:
    value = str(raw or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ReferenceDataError(
            f"source_url must be an absolute HTTPS URL at JSONL line {line_number}"
        )
    return value


def _price_anchor_index(
    points: list[ReferencePoint], event_date: date, *, event_id: str
) -> int:
    matches = [
        index for index, point in enumerate(points) if point.session_date <= event_date
    ]
    if not matches:
        raise ReferenceDataError(
            f"timeline event {event_id!r} precedes the first reference session"
        )
    index = matches[-1]
    if event_date > points[-1].session_date:
        raise ReferenceDataError(
            f"timeline event {event_id!r} follows the last reference session"
        )
    return index


def _delivery_index(
    points: list[ReferencePoint],
    temporal: _TemporalValue,
    *,
    event_id: str,
) -> int:
    """Return the first session on which information can be delivered safely.

    Date-only events have unknown publication time and conservatively roll to
    the first later session. Offset-aware events before 16:00 New York time may
    map to the same observed session; events at/after 16:00 roll forward. This
    fixed boundary is sufficient for the selected regular-session events and is
    not a general exchange/early-close calendar.
    """

    after_close_or_unknown = (
        temporal.local_time is None or temporal.local_time >= time(16, 0)
    )
    for index, point in enumerate(points):
        if after_close_or_unknown:
            available = point.session_date > temporal.session_date
        else:
            available = point.session_date >= temporal.session_date
        if available:
            return index
    raise ReferenceDataError(
        f"timeline event {event_id!r} has no non-leaky delivery session"
    )


def _load_timeline(
    path: Path, points: list[ReferencePoint], zone: ZoneInfo
) -> list[dict[str, object]]:
    try:
        stream = path.open("r", encoding="utf-8")
    except OSError as error:
        raise ReferenceDataError(f"cannot open news timeline {path}: {error}") from error

    raw_events: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    previous_date: Optional[date] = None
    with stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ReferenceDataError(
                    f"invalid JSON at timeline line {line_number}: {error.msg}"
                ) from error
            if not isinstance(value, dict):
                raise ReferenceDataError(
                    f"timeline line {line_number} must be a JSON object"
                )
            if value.get("schema_version") != NEWS_TIMELINE_SCHEMA_VERSION:
                raise ReferenceDataError(
                    f"unsupported schema_version at timeline line {line_number}"
                )

            event_id = str(value.get("event_id") or "").strip()
            if not event_id:
                raise ReferenceDataError(
                    f"missing event_id at timeline line {line_number}"
                )
            if event_id in seen_ids:
                raise ReferenceDataError(f"duplicate timeline event_id: {event_id!r}")
            seen_ids.add(event_id)

            timestamp = str(value.get("timestamp") or "").strip()
            temporal = _temporal_value(
                timestamp, zone, field=f"timestamp at JSONL line {line_number}"
            )
            session_date = temporal.session_date
            if previous_date is not None and session_date < previous_date:
                raise ReferenceDataError(
                    "timeline events must be ordered by exchange-local session date"
                )
            previous_date = session_date

            public_text = str(value.get("public_text") or "").strip()
            source_title = str(value.get("source_title") or "").strip()
            if not public_text or not source_title:
                raise ReferenceDataError(
                    f"timeline line {line_number} requires public_text and source_title"
                )
            source_url = _validated_https_url(
                value.get("source_url"), line_number=line_number
            )
            source_published_date = _calendar_date(
                value.get("source_published_date"),
                field=f"source_published_date at JSONL line {line_number}",
            )
            price_anchor_index = _price_anchor_index(
                points, session_date, event_id=event_id
            )
            delivery_index = _delivery_index(points, temporal, event_id=event_id)
            raw_events.append(
                {
                    "event_id": event_id,
                    "timestamp": timestamp,
                    "session_date": session_date,
                    "price_anchor_index": price_anchor_index,
                    "delivery_index": delivery_index,
                    "public_text": public_text,
                    "source_title": source_title,
                    "source_url": source_url,
                    "source_published_date": source_published_date,
                }
            )
    if not raw_events:
        raise ReferenceDataError("news timeline contains no events")
    return raw_events


def _legacy_drop_index(points: list[ReferencePoint]) -> int:
    drops = [
        points[index + 1].price / points[index].price - 1
        for index in range(len(points) - 1)
    ]
    # Preserve the established loader's alignment: index of the pre-drop row,
    # not the destination row after the largest one-step fall.
    return drops.index(min(drops)) if drops else 0


def load_reference_episode(
    path: str | Path,
    *,
    news_timeline_path: str | Path | None = None,
    include_news_timeline: bool = False,
    exchange_timezone: str = DEFAULT_EXCHANGE_TIMEZONE,
) -> ReferenceEpisode:
    """Load and align a versioned reference episode.

    ``include_news_timeline`` is an explicit scientific switch.  Supplying a
    timeline path without setting it, or setting it without an exact path, is
    an error.  This keeps the existing CSV-only default and prevents ambient
    sibling files from silently changing a run.

    Alignment priority is: first nonblank inline CSV ``news`` marker; otherwise
    the first explicitly enabled timeline event, mapped to the latest reference
    session on or before its exchange-local date; otherwise the legacy largest
    one-step-drop rule.
    """

    if news_timeline_path is not None and not include_news_timeline:
        raise ReferenceDataError(
            "news_timeline_path requires include_news_timeline=True"
        )
    if include_news_timeline and news_timeline_path is None:
        raise ReferenceDataError(
            "include_news_timeline=True requires an exact news_timeline_path"
        )

    zone = _exchange_zone(exchange_timezone)
    points = _load_points(Path(path), zone)
    raw_timeline: list[dict[str, object]] = []
    if include_news_timeline:
        timeline_path = Path(news_timeline_path)  # type: ignore[arg-type]
        raw_timeline = _load_timeline(timeline_path, points, zone)

    inline_news = [index for index, point in enumerate(points) if point.news]
    if inline_news:
        shock_idx = inline_news[0]
    elif raw_timeline:
        shock_idx = int(raw_timeline[0]["price_anchor_index"])
    else:
        shock_idx = _legacy_drop_index(points)

    timeline = tuple(
        NewsTimelineEvent(
            event_id=str(value["event_id"]),
            timestamp=str(value["timestamp"]),
            session_date=value["session_date"],  # type: ignore[arg-type]
            price_anchor_t=int(value["price_anchor_index"]) - shock_idx,
            delivery_t=int(value["delivery_index"]) - shock_idx,
            public_text=str(value["public_text"]),
            source_title=str(value["source_title"]),
            source_url=str(value["source_url"]),
            source_published_date=value["source_published_date"],  # type: ignore[arg-type]
        )
        for value in raw_timeline
    )
    aligned_points = tuple(
        ReferencePoint(
            timestamp=point.timestamp,
            session_date=point.session_date,
            price=point.price,
            news=point.news,
            t=index - shock_idx,
        )
        for index, point in enumerate(points)
    )
    return ReferenceEpisode(
        points=aligned_points,
        shock_idx=shock_idx,
        exchange_timezone=exchange_timezone,
        news_timeline=timeline,
    )


__all__ = [
    "DEFAULT_EXCHANGE_TIMEZONE",
    "NEWS_TIMELINE_SCHEMA_VERSION",
    "NewsTimelineEvent",
    "ReferenceDataError",
    "ReferenceEpisode",
    "ReferencePoint",
    "load_reference_episode",
]
