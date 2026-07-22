"""Reproduce and verify the Nasdaq extraction evidence for reference data.

Nothing in this module runs at import time. Tests use only committed snapshots;
``fetch_nasdaq_window`` is an explicit maintenance operation that contacts the
catalog URL and returns bytes/metadata to the caller without overwriting files.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from . import ReferenceDataError


@dataclass(frozen=True)
class NasdaqExtractionEvidence:
    raw_response_bytes: int
    raw_response_sha256: str
    total_records: int
    rows: tuple[dict[str, str], ...]
    canonical_date_close_sha256: str


def _iso_nasdaq_date(value: object) -> str:
    try:
        return datetime.strptime(str(value), "%m/%d/%Y").date().isoformat()
    except ValueError as error:
        raise ReferenceDataError(f"invalid Nasdaq date: {value!r}") from error


def _close_text(value: object) -> str:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if not text:
        raise ReferenceDataError("missing Nasdaq close")
    try:
        parsed = float(text)
    except ValueError as error:
        raise ReferenceDataError(f"invalid Nasdaq close: {value!r}") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ReferenceDataError(
            f"non-finite or non-positive Nasdaq close: {value!r}"
        )
    return text


def canonical_date_close_bytes(rows: tuple[dict[str, str], ...]) -> bytes:
    ordered = sorted(rows, key=lambda row: _iso_nasdaq_date(row.get("date")))
    return "".join(
        f"{_iso_nasdaq_date(row.get('date'))},{_close_text(row.get('close'))}\n"
        for row in ordered
    ).encode("utf-8")


def canonical_date_close_sha256(rows: tuple[dict[str, str], ...]) -> str:
    return hashlib.sha256(canonical_date_close_bytes(rows)).hexdigest()


def _extract_rows(
    payload: object, start: date, end: date
) -> tuple[int, tuple[dict[str, str], ...]]:
    if not isinstance(payload, dict):
        raise ReferenceDataError("Nasdaq response root must be an object")
    try:
        response_code = int(payload["status"]["rCode"])
        data = payload["data"]
        total_records = int(data["totalRecords"])
        raw_rows = data["tradesTable"]["rows"]
    except (KeyError, TypeError, ValueError) as error:
        raise ReferenceDataError(
            "Nasdaq response is missing success status or tradesTable rows"
        ) from error
    if response_code != 200:
        raise ReferenceDataError(f"Nasdaq response rCode is not 200: {response_code}")
    if not isinstance(raw_rows, list):
        raise ReferenceDataError("Nasdaq tradesTable.rows must be a list")

    required = ("date", "close", "volume", "open", "high", "low")
    selected: list[dict[str, str]] = []
    selected_dates: set[date] = set()
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            raise ReferenceDataError("Nasdaq row must be an object")
        row_date = date.fromisoformat(_iso_nasdaq_date(raw_row.get("date")))
        if start <= row_date <= end:
            if row_date in selected_dates:
                raise ReferenceDataError(
                    f"Nasdaq response contains duplicate date: {row_date.isoformat()}"
                )
            selected_dates.add(row_date)
            if any(key not in raw_row for key in required):
                raise ReferenceDataError("Nasdaq row is missing an OHLCV field")
            selected.append({key: str(raw_row[key]) for key in required})
    selected.sort(key=lambda row: _iso_nasdaq_date(row["date"]))
    if not selected:
        raise ReferenceDataError("Nasdaq response has no rows in the requested window")
    return total_records, tuple(selected)


def fetch_nasdaq_window(
    url: str,
    start: date,
    end: date,
    *,
    timeout: float = 30.0,
    opener: Callable[..., object] = urlopen,
) -> NasdaqExtractionEvidence:
    """Fetch an exact catalog URL and extract an inclusive historical window.

    Catalog URLs intentionally contain ``fromdate`` and ``limit=5000`` but no
    ``todate``. The full response is hashed before the requested old window is
    filtered and sorted locally.
    """

    if start > end:
        raise ReferenceDataError("Nasdaq extraction start must not follow end")
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    try:
        response = opener(request, timeout=timeout)
        with response:  # type: ignore[attr-defined]
            raw = response.read()  # type: ignore[attr-defined]
    except OSError as error:
        raise ReferenceDataError(f"Nasdaq retrieval failed: {error}") from error
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ReferenceDataError("Nasdaq response is not valid JSON") from error
    total_records, rows = _extract_rows(payload, start, end)
    return NasdaqExtractionEvidence(
        raw_response_bytes=len(raw),
        raw_response_sha256=hashlib.sha256(raw).hexdigest(),
        total_records=total_records,
        rows=rows,
        canonical_date_close_sha256=canonical_date_close_sha256(rows),
    )


def load_source_snapshot(path: str | Path) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ReferenceDataError(
                    f"invalid source snapshot JSON at line {line_number}"
                ) from error
            if not isinstance(value, dict):
                raise ReferenceDataError(
                    f"source snapshot line {line_number} must be an object"
                )
            rows.append({str(key): str(item) for key, item in value.items()})
    if not rows:
        raise ReferenceDataError("source snapshot contains no rows")
    return tuple(rows)


def verify_snapshot_matches_csv(
    snapshot_path: str | Path, reference_csv_path: str | Path
) -> str:
    """Fail if a committed CSV's date/price pairs differ from source evidence.

    Returns the canonical date/close SHA-256 used by the catalog.
    """

    snapshot_rows = load_source_snapshot(snapshot_path)
    source_pairs = canonical_date_close_bytes(snapshot_rows).decode("utf-8").splitlines()
    csv_pairs: list[str] = []
    with Path(reference_csv_path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = {str(name).lower(): name for name in (reader.fieldnames or [])}
        timestamp_column = columns.get("timestamp") or columns.get("date")
        price_column = columns.get("price") or columns.get("close")
        if timestamp_column is None or price_column is None:
            raise ReferenceDataError("reference CSV lacks timestamp/price columns")
        for row in reader:
            csv_pairs.append(
                f"{str(row[timestamp_column]).strip()},{_close_text(row[price_column])}"
            )
    if csv_pairs != source_pairs:
        raise ReferenceDataError("reference CSV differs from its Nasdaq snapshot")
    return canonical_date_close_sha256(snapshot_rows)


__all__ = [
    "NasdaqExtractionEvidence",
    "canonical_date_close_bytes",
    "canonical_date_close_sha256",
    "fetch_nasdaq_window",
    "load_source_snapshot",
    "verify_snapshot_matches_csv",
]
