"""Offline stdlib tests for versioned multi-event reference inputs."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest

from nmsim.reference_data import ReferenceDataError, load_reference_episode
from nmsim.reference_data.source_verification import (
    fetch_nasdaq_window,
    verify_snapshot_matches_csv,
)
from nmsim.validation import load_reference


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = REPO_ROOT / "nmsim" / "reference_data" / "v1"


class MultiEventReferenceDataTests(unittest.TestCase):
    def _temporary_file(self, root: Path, name: str, content: str) -> Path:
        path = root / name
        path.write_text(content, encoding="utf-8")
        return path

    def _date_at_t(self, episode, t: int):
        return next(point.session_date for point in episode.points if point.t == t)

    def _fake_nasdaq_opener(self, payload):
        raw = json.dumps(payload).encode("utf-8")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return raw

        return lambda request, timeout: Response()

    def test_catalog_artifacts_load_and_match_retained_source_snapshots(self):
        catalog = json.loads((DATA_ROOT / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["schema_version"], "reference_data_catalog_v1")
        self.assertEqual(len(catalog["datasets"]), 3)

        expected = {
            "meta_2022_02_crash_v1": (14, "2022-02-02", 4),
            "spy_2020_03_covid_v_recovery_v1": (127, "2020-02-19", 7),
            "meta_2023_02_efficiency_jump_v1": (14, "2023-02-01", 3),
        }
        for item in catalog["datasets"]:
            with self.subTest(dataset_id=item["dataset_id"]):
                csv_path = DATA_ROOT / item["reference_csv"]
                timeline_path = DATA_ROOT / item["news_timeline_jsonl"]
                snapshot_path = DATA_ROOT / item["source_snapshot"]
                episode = load_reference_episode(
                    csv_path,
                    news_timeline_path=timeline_path,
                    include_news_timeline=True,
                )
                rows, t0, events = expected[item["dataset_id"]]
                self.assertEqual(len(episode.points), rows)
                self.assertEqual(episode.t0_date.isoformat(), t0)
                self.assertEqual(episode.points[episode.shock_idx].t, 0)
                self.assertEqual(len(episode.news_timeline), events)
                self.assertEqual(item["source_snapshot_rows"], rows)
                self.assertEqual(
                    verify_snapshot_matches_csv(snapshot_path, csv_path),
                    item["canonical_date_close_sha256"],
                )
                for event in episode.news_timeline:
                    self.assertTrue(event.source_url.startswith("https://"))
                    self.assertGreaterEqual(event.delivery_t, event.price_anchor_t)
                    self.assertNotIn("private", event.public_text.lower())

    def test_committed_v1_artifact_hashes_match(self):
        lines = (DATA_ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 10)
        for line in lines:
            expected, relative_path = line.split("  ", 1)
            artifact = REPO_ROOT / relative_path
            with self.subTest(artifact=relative_path):
                self.assertTrue(artifact.is_file())
                self.assertEqual(
                    hashlib.sha256(artifact.read_bytes()).hexdigest(), expected
                )

    def test_source_fetch_rejects_non_success_status_and_duplicate_dates(self):
        row = {
            "date": "02/01/2023",
            "close": "$100.00",
            "volume": "10",
            "open": "$99.00",
            "high": "$101.00",
            "low": "$98.00",
        }
        base = {
            "status": {"rCode": 200},
            "data": {"totalRecords": 1, "tradesTable": {"rows": [row]}},
        }
        failed = dict(base)
        failed["status"] = {"rCode": 400}
        with self.assertRaisesRegex(ReferenceDataError, "rCode is not 200"):
            fetch_nasdaq_window(
                "https://api.nasdaq.com/example",
                date(2023, 2, 1),
                date(2023, 2, 1),
                opener=self._fake_nasdaq_opener(failed),
            )

        duplicate = dict(base)
        duplicate["data"] = {
            "totalRecords": 2,
            "tradesTable": {"rows": [row, dict(row)]},
        }
        with self.assertRaisesRegex(ReferenceDataError, "duplicate date"):
            fetch_nasdaq_window(
                "https://api.nasdaq.com/example",
                date(2023, 2, 1),
                date(2023, 2, 1),
                opener=self._fake_nasdaq_opener(duplicate),
            )

    def test_source_snapshot_rejects_nonfinite_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._temporary_file(
                root,
                "snapshot.jsonl",
                json.dumps(
                    {
                        "date": "02/01/2023",
                        "close": "nan",
                        "volume": "10",
                        "open": "$99.00",
                        "high": "$101.00",
                        "low": "$98.00",
                    }
                )
                + "\n",
            )
            csv_path = self._temporary_file(
                root,
                "reference.csv",
                "timestamp,price\n2023-02-01,nan\n",
            )
            with self.assertRaisesRegex(ReferenceDataError, "non-finite or non-positive"):
                verify_snapshot_matches_csv(snapshot, csv_path)

    def test_event_outcome_shapes_are_observed_not_labels_only(self):
        crash = load_reference_episode(DATA_ROOT / "meta_2022_02_crash.csv")
        self.assertAlmostEqual(
            min(crash.prices[crash.shock_idx:]) / 323.0 - 1, -0.3617337461
        )
        self.assertEqual(crash.prices[-1], min(crash.prices[crash.shock_idx:]))

        recovery = load_reference_episode(
            DATA_ROOT / "spy_2020_03_covid_v_recovery.csv"
        )
        trough_index = recovery.prices.index(min(recovery.prices))
        self.assertEqual(recovery.points[trough_index].t, 23)
        self.assertAlmostEqual(min(recovery.prices) / 338.34 - 1, -0.3410474670)
        self.assertGreater(recovery.prices[-1], recovery.prices[recovery.shock_idx])
        self.assertEqual(recovery.points[-1].t, 126)

        positive = load_reference_episode(
            DATA_ROOT / "meta_2023_02_efficiency_jump.csv"
        )
        self.assertAlmostEqual(positive.prices[1] / positive.prices[0] - 1, 0.2328239289)
        self.assertGreater(positive.prices[-1], positive.prices[0])

    def test_legacy_reference_loader_and_return_shape_are_unchanged(self):
        legacy_path = REPO_ROOT / "nmsim" / "meta_feb2022_reference.csv"
        old_prices, old_shock = load_reference(str(legacy_path))
        structured = load_reference_episode(legacy_path)
        self.assertEqual(structured.as_legacy_tuple(), (old_prices, old_shock))
        self.assertEqual(old_shock, 1)
        self.assertEqual(old_prices[0], 319.0)
        self.assertEqual(old_prices[-1], 206.16)
        self.assertEqual(structured.news_timeline, ())

    def test_timeline_requires_an_exact_explicit_opt_in(self):
        csv_path = DATA_ROOT / "meta_2023_02_efficiency_jump.csv"
        timeline_path = DATA_ROOT / "meta_2023_02_efficiency_jump_news_timeline.jsonl"
        with self.assertRaisesRegex(ReferenceDataError, "include_news_timeline=True"):
            load_reference_episode(csv_path, news_timeline_path=timeline_path)
        with self.assertRaisesRegex(ReferenceDataError, "exact news_timeline_path"):
            load_reference_episode(csv_path, include_news_timeline=True)

    def test_aware_datetimes_normalize_to_exchange_timezone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = self._temporary_file(
                root,
                "reference.csv",
                "timestamp,price,news\n"
                "2023-02-02T00:30:00Z,100,shock\n"
                "2023-02-02T21:00:00+00:00,101,\n",
            )
            timeline_path = self._temporary_file(
                root,
                "timeline.jsonl",
                json.dumps(
                    {
                        "schema_version": "news_timeline_v1",
                        "event_id": "utc-after-close",
                        "timestamp": "2023-02-02T00:45:00Z",
                        "public_text": "Public event.",
                        "source_title": "Source",
                        "source_url": "https://example.com/source",
                        "source_published_date": "2023-02-01",
                    }
                )
                + "\n",
            )
            episode = load_reference_episode(
                csv_path,
                news_timeline_path=timeline_path,
                include_news_timeline=True,
            )
            self.assertEqual(
                [point.session_date.isoformat() for point in episode.points],
                ["2023-02-01", "2023-02-02"],
            )
            event = episode.news_timeline[0]
            self.assertEqual(event.session_date.isoformat(), "2023-02-01")
            self.assertEqual(event.price_anchor_t, 0)
            self.assertEqual(event.delivery_t, 1)

    def test_delivery_never_points_backwards_for_weekend_after_close_or_date_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = self._temporary_file(
                root,
                "reference.csv",
                "timestamp,price,news\n"
                "2020-03-13,100,shock\n"
                "2020-03-16,90,\n"
                "2020-03-17,95,\n",
            )
            items = [
                ("before-close", "2020-03-13T15:59:00-04:00", "2020-03-13"),
                ("at-close", "2020-03-13T16:00:00-04:00", "2020-03-13"),
                ("weekend", "2020-03-15T17:00:00-04:00", "2020-03-15"),
                ("date-only", "2020-03-16", "2020-03-16"),
            ]
            timeline_path = self._temporary_file(
                root,
                "timeline.jsonl",
                "".join(
                    json.dumps(
                        {
                            "schema_version": "news_timeline_v1",
                            "event_id": event_id,
                            "timestamp": timestamp,
                            "public_text": "Public event.",
                            "source_title": "Source",
                            "source_url": f"https://example.com/{event_id}",
                            "source_published_date": published,
                        }
                    )
                    + "\n"
                    for event_id, timestamp, published in items
                ),
            )
            episode = load_reference_episode(
                csv_path,
                news_timeline_path=timeline_path,
                include_news_timeline=True,
            )
            events = {event.event_id: event for event in episode.news_timeline}
            self.assertEqual(
                self._date_at_t(
                    episode, events["before-close"].delivery_t
                ).isoformat(),
                "2020-03-13",
            )
            self.assertEqual(
                self._date_at_t(episode, events["at-close"].delivery_t).isoformat(),
                "2020-03-16",
            )
            self.assertEqual(events["weekend"].price_anchor_t, 0)
            self.assertEqual(
                self._date_at_t(episode, events["weekend"].delivery_t).isoformat(),
                "2020-03-16",
            )
            self.assertEqual(
                self._date_at_t(episode, events["date-only"].delivery_t).isoformat(),
                "2020-03-17",
            )

    def test_timeline_without_a_safe_delivery_session_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = self._temporary_file(
                root,
                "reference.csv",
                "timestamp,price,news\n2020-03-13,100,shock\n2020-03-16,90,\n",
            )
            timeline_path = self._temporary_file(
                root,
                "timeline.jsonl",
                json.dumps(
                    {
                        "schema_version": "news_timeline_v1",
                        "event_id": "too-late",
                        "timestamp": "2020-03-16",
                        "public_text": "Public event.",
                        "source_title": "Source",
                        "source_url": "https://example.com/late",
                        "source_published_date": "2020-03-16",
                    }
                )
                + "\n",
            )
            with self.assertRaisesRegex(ReferenceDataError, "no non-leaky delivery"):
                load_reference_episode(
                    csv_path,
                    news_timeline_path=timeline_path,
                    include_news_timeline=True,
                )

    def test_naive_datetime_and_non_date_source_publication_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            naive_csv = self._temporary_file(
                root,
                "naive.csv",
                "timestamp,price\n2023-02-01T16:00:00,100\n",
            )
            with self.assertRaisesRegex(ReferenceDataError, "naive datetime"):
                load_reference_episode(naive_csv)

            valid_csv = self._temporary_file(
                root,
                "valid.csv",
                "timestamp,price,news\n2023-02-01,100,shock\n2023-02-02,101,\n",
            )
            timeline = self._temporary_file(
                root,
                "bad-publication.jsonl",
                json.dumps(
                    {
                        "schema_version": "news_timeline_v1",
                        "event_id": "bad-date",
                        "timestamp": "2023-02-01T15:00:00-05:00",
                        "public_text": "Public event.",
                        "source_title": "Source",
                        "source_url": "https://example.com/source",
                        "source_published_date": "2023-02-01T15:00:00-05:00",
                    }
                )
                + "\n",
            )
            with self.assertRaisesRegex(ReferenceDataError, "date-only ISO"):
                load_reference_episode(
                    valid_csv,
                    news_timeline_path=timeline,
                    include_news_timeline=True,
                )

    def test_missing_or_invalid_prices_fail_without_imputation(self):
        invalid_values = ("", "not-a-number", "nan", "inf", "0", "-1")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, value in enumerate(invalid_values):
                with self.subTest(value=value):
                    path = self._temporary_file(
                        root,
                        f"invalid-{index}.csv",
                        f"timestamp,price\n2023-01-01,{value}\n",
                    )
                    with self.assertRaises(ReferenceDataError):
                        load_reference_episode(path)

    def test_missing_timestamp_and_unknown_timezone_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = self._temporary_file(
                root,
                "missing-timestamp.csv",
                "timestamp,price\n,100\n",
            )
            with self.assertRaisesRegex(ReferenceDataError, "missing timestamp"):
                load_reference_episode(missing)

            valid = self._temporary_file(
                root,
                "valid.csv",
                "timestamp,price\n2023-01-01,100\n",
            )
            with self.assertRaisesRegex(ReferenceDataError, "unknown exchange timezone"):
                load_reference_episode(valid, exchange_timezone="Mars/Olympus")

    def test_no_news_fallback_preserves_pre_drop_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._temporary_file(
                Path(temporary),
                "fallback.csv",
                "timestamp,price\n2023-01-01,100\n2023-01-02,98\n2023-01-03,70\n",
            )
            episode = load_reference_episode(path)
            self.assertEqual(episode.shock_idx, 1)
            self.assertEqual([point.t for point in episode.points], [-1, 0, 1])


if __name__ == "__main__":
    unittest.main()
