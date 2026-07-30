"""Integrity and scientific-content tests for the persona fixture bundle.

The protocol defines four logical fixtures.  F1--F3 each have one concrete
qualification-style Observation, while F4 is one controlled logical fixture
with two concrete observations whose belief-source memories differ.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest

from experiments import model_qualification as qualification


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PERSONA_FIXTURES_PATH = REPOSITORY_ROOT / "qualification" / "persona_fixtures.json"
QUALIFICATION_ROOT = REPOSITORY_ROOT / "qualification"
EXPECTED_OBSERVATION_FIELDS = {
    "fixture_id",
    "protocol_version",
    "round",
    "market_state",
    "visible_news",
    "visible_social_feed",
    "cash",
    "shares",
    "memory",
    "fundamental_value",
    "invisible_fields",
    "input_hash",
}
EXPECTED_INVISIBLE_FIELDS = {
    "future_prices",
    "market_clearing_formula",
    "other_agent_private_rationale",
    "private_margin_reference_book",
    "expected_answer",
    "evaluation_rubric",
}
ORIGINAL_QUALIFICATION_FILE_HASHES = {
    "observations.json": "ea34eede13b37bd68625ff11109ebf1fc0fe5551ab39b0e66ea3c62b212ce993",
    "protocol.json": "9bfc873422e227b77f4e77a84259ad4c0f1edef976626883366e1bbf43039ff4",
    "rubric.json": "18e14362d5890c1c74b0578f259f8e59f633a0f262461aefce0793fa1925a6fb",
    "visibility_contract.json": "366a1c154b4ccc0ea40f2dcd11388254d7949050fb398607ea1c3e135e2bd2e3",
}


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("expected a JSON object at {}".format(path))
    return value


class PersonaFixtureBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = _read_json(PERSONA_FIXTURES_PATH)
        cls.fixtures = {
            fixture["fixture_id"]: fixture for fixture in cls.bundle["fixtures"]
        }
        cls.logical = {
            fixture["logical_fixture_id"]: fixture
            for fixture in cls.bundle["logical_fixtures"]
        }

    def test_bundle_is_standalone_versioned_and_has_four_logical_fixtures(self):
        self.assertEqual(self.bundle["bundle_schema_version"], "1.0")
        self.assertEqual(self.bundle["bundle_id"], "persona_fixtures")
        self.assertEqual(self.bundle["bundle_version"], "1.0")
        self.assertEqual(self.bundle["observation_protocol_version"], "1.0")
        self.assertEqual(
            set(self.logical),
            {
                "sideways_2y_hot_peers",
                "deep_loss_bad_news",
                "public_holding_friends_ask",
                "belief_source_pair",
            },
        )
        self.assertEqual(len(self.bundle["logical_fixtures"]), 4)
        self.assertEqual(len(self.fixtures), 5)

    def test_logical_members_partition_the_five_concrete_observations(self):
        members = [
            member
            for logical in self.bundle["logical_fixtures"]
            for member in logical["member_fixture_ids"]
        ]
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(set(members), set(self.fixtures))
        self.assertEqual(
            self.logical["belief_source_pair"]["member_fixture_ids"],
            ["belief_source_research", "belief_source_peers"],
        )
        for logical_id in (
            "sideways_2y_hot_peers",
            "deep_loss_bad_news",
            "public_holding_friends_ask",
        ):
            self.assertEqual(
                self.logical[logical_id]["member_fixture_ids"], [logical_id]
            )

    def test_each_concrete_observation_uses_existing_schema_and_frozen_hash(self):
        for fixture_id, fixture in self.fixtures.items():
            with self.subTest(fixture=fixture_id):
                self.assertEqual(set(fixture), EXPECTED_OBSERVATION_FIELDS)
                self.assertEqual(fixture["protocol_version"], "1.0")
                self.assertEqual(
                    set(fixture["market_state"]),
                    {"latest_price", "recent_prices"},
                )
                self.assertTrue(fixture["market_state"]["recent_prices"])
                self.assertTrue(
                    all(
                        isinstance(price, (int, float))
                        for price in fixture["market_state"]["recent_prices"]
                    )
                )
                self.assertTrue(
                    all(
                        set(item) == {"sentiment", "public_take"}
                        for item in fixture["visible_social_feed"]
                    )
                )
                self.assertEqual(
                    set(fixture["invisible_fields"]), EXPECTED_INVISIBLE_FIELDS
                )
                self.assertEqual(
                    fixture["input_hash"],
                    qualification.fixture_input_hash(fixture),
                )
                self.assertRegex(fixture["input_hash"], r"^[0-9a-f]{64}$")

    def test_bundle_hash_has_an_independently_verifiable_canonical_rule(self):
        self.assertEqual(
            self.bundle["hash_contract"],
            {
                "canonical_json": (
                    "json.dumps(value, ensure_ascii=False, sort_keys=True, "
                    "separators=(',', ':'), allow_nan=False).encode('utf-8')"
                ),
                "digest": "sha256",
                "encoding": "lowercase hexadecimal",
                "input_hash_payload": (
                    "the complete concrete fixture object after removing only "
                    "its input_hash field"
                ),
                "bundle_hash_payload": (
                    "the complete bundle object after removing only its "
                    "bundle_hash field"
                ),
            },
        )
        payload = copy.deepcopy(self.bundle)
        recorded_hash = payload.pop("bundle_hash")
        self.assertEqual(recorded_hash, qualification.stable_json_hash(payload))
        self.assertRegex(recorded_hash, r"^[0-9a-f]{64}$")

        payload["fixtures"][0]["cash"] += 1.0
        self.assertNotEqual(recorded_hash, qualification.stable_json_hash(payload))

    def test_f1_encodes_two_year_sideways_and_hot_alternatives_without_bad_news(self):
        fixture = self.fixtures["sideways_2y_hot_peers"]
        self.assertEqual(fixture["round"], 24)
        self.assertTrue(
            all(98.0 <= price <= 103.0 for price in fixture["market_state"]["recent_prices"])
        )
        self.assertIn("24 个月", fixture["memory"][0])
        self.assertIn("98 元至 103 元", fixture["memory"][0])
        self.assertIn("18 个月", fixture["visible_news"])
        self.assertIn("45%", fixture["visible_news"])
        self.assertIn("没有任何利空", fixture["visible_news"])
        self.assertTrue(fixture["visible_social_feed"])
        self.assertIn("热门股票", fixture["visible_social_feed"][0]["public_take"])
        self.assertEqual(self.logical["sideways_2y_hot_peers"]["primary_variable_ids"], ["D2"])
        self.assertEqual(
            self.logical["sideways_2y_hot_peers"]["compatible_variable_ids"], ["C4"]
        )

    def test_f2_encodes_exact_loss_guidance_cut_and_public_sentiment(self):
        fixture = self.fixtures["deep_loss_bad_news"]
        self.assertEqual(fixture["market_state"]["latest_price"], 50.0)
        self.assertIn("成本是每股 100 元", fixture["memory"][0])
        self.assertIn("浮亏 50%", fixture["memory"][0])
        self.assertIn("业绩指引下修 20%", fixture["visible_news"])
        self.assertEqual(
            [item["sentiment"] for item in fixture["visible_social_feed"]],
            [-0.6],
        )
        self.assertEqual(
            self.logical["deep_loss_bad_news"]["primary_variable_ids"],
            ["A3", "C1"],
        )

    def test_f3_encodes_public_recommendations_two_questions_and_no_news(self):
        fixture = self.fixtures["public_holding_friends_ask"]
        self.assertEqual(fixture["market_state"]["latest_price"], 75.0)
        self.assertIsNone(fixture["visible_news"])
        memory = " ".join(fixture["memory"])
        self.assertIn("买入成本是每股 100 元", memory)
        self.assertIn("多次向朋友推荐", memory)
        self.assertIn("两位朋友", memory)
        self.assertEqual(len(fixture["visible_social_feed"]), 2)
        for item in fixture["visible_social_feed"]:
            self.assertEqual(item["sentiment"], 0.0)
            self.assertIn("还拿着吗？", item["public_take"])
        self.assertEqual(
            self.logical["public_holding_friends_ask"]["primary_variable_ids"],
            ["C2", "C3"],
        )

    def test_f4_is_one_controlled_pair_differing_only_in_belief_source_memory(self):
        research = copy.deepcopy(self.fixtures["belief_source_research"])
        peers = copy.deepcopy(self.fixtures["belief_source_peers"])
        self.assertIn("三周研究", research["memory"][0])
        self.assertIn("财报", research["memory"][0])
        self.assertIn("饭局", peers["memory"][0])
        self.assertIn("朋友极力推荐", peers["memory"][0])
        self.assertIn("核心产品的销量增速正在放缓", research["visible_news"])
        self.assertEqual(research["visible_news"], peers["visible_news"])

        for fixture in (research, peers):
            fixture.pop("fixture_id")
            fixture.pop("input_hash")
            fixture.pop("memory")
        self.assertEqual(research, peers)
        self.assertEqual(
            self.logical["belief_source_pair"]["primary_variable_ids"],
            ["B1", "B2"],
        )

    def test_generic_negative_news_is_read_only_reference_not_a_copy(self):
        references = self.bundle["fixture_references"]
        self.assertEqual(len(references), 1)
        reference = references[0]
        self.assertEqual(reference["access"], "read_only")
        self.assertEqual(reference["source_path"], "qualification/observations.json")
        self.assertEqual(reference["fixture_id"], "negative_news_price_unchanged")
        self.assertNotIn(reference["fixture_id"], self.fixtures)

        observations = _read_json(QUALIFICATION_ROOT / "observations.json")
        source = next(
            fixture
            for fixture in observations["fixtures"]
            if fixture["fixture_id"] == reference["fixture_id"]
        )
        self.assertEqual(reference["input_hash"], source["input_hash"])
        self.assertEqual(source["input_hash"], qualification.fixture_input_hash(source))

    def test_fixture_mappings_and_reference_cover_all_fourteen_new_variables(self):
        mapped = [
            variable
            for logical in self.bundle["logical_fixtures"]
            for field in ("primary_variable_ids", "compatible_variable_ids")
            for variable in logical[field]
        ]
        mapped.extend(
            self.bundle["fixture_references"][0]["applicable_variable_ids"]
        )
        self.assertEqual(len(mapped), len(set(mapped)))
        self.assertEqual(
            set(mapped),
            {
                "A1",
                "A2",
                "A3",
                "A4",
                "B1",
                "B2",
                "B3",
                "C1",
                "C2",
                "C3",
                "C4",
                "D1",
                "D2",
                "D3",
            },
        )

    def test_original_qualification_bundle_files_remain_byte_identical(self):
        for name, expected in ORIGINAL_QUALIFICATION_FILE_HASHES.items():
            with self.subTest(path=name):
                actual = hashlib.sha256((QUALIFICATION_ROOT / name).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)
        existing = qualification.load_protocol_bundle()
        self.assertEqual(
            existing["fixture_set_hash"],
            "95109438f101ea1251520b3deb71fed9b96b097d2c9b89c1a6b73e16294aaf34",
        )
        self.assertEqual(len(existing["observations"]["fixtures"]), 8)


if __name__ == "__main__":
    unittest.main()
