"""Tests for the frozen persona-variable registry and pure render machinery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from nmsim import persona_variables as persona
from nmsim.prompts import PERSONAS_BY_ID


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_LEVELS = {
    "A1": (
        ("a1_2w", "工作几年攒下的 2 万元积蓄"),
        ("a1_5w", "多年攒下的 5 万元积蓄"),
        ("a1_10w", "10 万元可投资资金"),
        ("a1_50w", "50 万元闲置资金"),
    ),
    "A2": (
        ("a2_10", "你把全部可投资资金中的 10% 投在了这只股票上"),
        ("a2_30", "你把全部可投资资金中的 30% 投在了这只股票上"),
        ("a2_50", "你把全部可投资资金中的 50% 投在了这只股票上"),
        ("a2_70", "你把全部可投资资金中的 70% 投在了这只股票上"),
        ("a2_90", "你把全部可投资资金中的 90% 投在了这只股票上"),
    ),
    "A3": (
        ("a3_0", "买入成本 100 元,现价 100 元,基本持平"),
        ("a3_m10", "买入成本 100 元,现价 90 元,浮亏 10%"),
        ("a3_m25", "买入成本 100 元,现价 75 元,浮亏 25%"),
        ("a3_m50", "买入成本 100 元,现价 50 元,浮亏 50%"),
    ),
    "A4": (
        ("a4_stable", "你有稳定的工资收入,近期没有大额支出计划"),
        ("a4_unstable", "你的收入不太稳定,时好时坏"),
        ("a4_needcash", "半年内你需要用到这笔钱(有明确的大额支出计划)"),
    ),
    "B1": (
        (
            "b1_research",
            "这只股票是你自己花了几周时间研究财报和行业后决定买入的",
        ),
        ("b1_trend", "这只股票之前一直在涨,你是顺势而为买入的"),
        (
            "b1_peers",
            "你身边好几位朋友同事都在买这只股票,都说好,你跟着买入的",
        ),
        ("b1_media", "你是在新闻和分析师一致看好中买入这只股票的"),
    ),
    "B2": (
        ("b2_1", "关于这只股票,你只听说过个大概"),
        ("b2_3", "你看过几篇关于这只股票的分析,大致了解"),
        (
            "b2_5",
            "你曾认真写下过买入这只股票的理由,也想过什么情况下自己可能是错的",
        ),
    ),
    "B3": (
        (
            "b3_clear",
            "你心里有明确的标尺:如果出现什么情况,你就承认自己错了并卖出",
        ),
        ("b3_vague", "你没有具体想过,什么情况会让你改变对这只股票的看法"),
    ),
    "C1": (
        ("c1_5", "浮亏超过 5% 你就开始睡不好"),
        ("c1_15", "浮亏超过 15% 你就开始睡不好"),
        ("c1_30", "浮亏 30% 以内你都能承受"),
        ("c1_50", "浮亏一半你也能扛得住"),
    ),
    "C2": (
        ("c2_low", "在投资上承认错误对你不是难事,认错换股是常事"),
        ("c2_mid", "承认错误会让你有些不舒服,但你通常能做到"),
        (
            "c2_high",
            "承认自己做错了决定让你非常难堪,这只股票又是你坚持要买的",
        ),
    ),
    "C3": (
        ("c3_private", "你买这只股票的事没人知道"),
        ("c3_family", "你买这只股票的事家里人知道"),
        ("c3_public", "你买这只股票的事朋友们都知道,还常有人问起"),
    ),
    "C4": (
        ("c4_days", "一只股票几天没动静你就会开始烦躁"),
        ("c4_months", "你愿意拿几个月看看"),
        ("c4_years", "你习惯按年持有"),
    ),
    "D1": (
        ("d1_intraday", "开盘时间你几乎一直看盘,一天看好多次"),
        ("d1_daily", "你每天收盘后看一次"),
        ("d1_weekly", "你一两个星期才看一次"),
    ),
    "D2": (
        ("d2_low", "你只关心自己手里这只股票,别的股票涨跌你不太在意"),
        ("d2_mid", "你会留意别的股票涨得好不好"),
        ("d2_high", "你总在比较哪只股票涨得好,很怕错过别的机会"),
    ),
    "D3": (
        ("d3_trust", "你大体相信新闻和专家的判断"),
        ("d3_half", "你对新闻半信半疑,会和自己的判断对照"),
        ("d3_self", "你只相信自己的研究"),
    ),
}

EXPECTED_REFERENCE = {
    "A1": "a1_10w",
    "A2": "a2_50",
    "A3": "a3_0",
    "A4": "a4_stable",
    "B1": "b1_trend",
    "B2": "b2_3",
    "B3": "b3_vague",
    "C1": "c1_15",
    "C2": "c2_mid",
    "C3": "c3_private",
    "C4": "c4_months",
    "D1": "d1_daily",
    "D2": "d2_mid",
    "D3": "d3_half",
}

EXPECTED_COORDINATES = {
    "retail_crowd": {
        "A1": "a1_5w",
        "A2": "a2_70",
        "A3": "a3_m10",
        "A4": "a4_stable",
        "B1": "b1_peers",
        "B2": "b2_1",
        "B3": "b3_vague",
        "C1": "c1_15",
        "C2": "c2_mid",
        "C3": "c3_family",
        "C4": "c4_months",
        "D1": "d1_intraday",
        "D2": "d2_high",
        "D3": "d3_half",
    },
    "fomo_momentum": {
        "A1": "a1_10w",
        "A2": "a2_50",
        "A3": "a3_0",
        "A4": "a4_stable",
        "B1": "b1_trend",
        "B2": "b2_1",
        "B3": "b3_vague",
        "C1": "c1_5",
        "C2": "c2_low",
        "C3": "c3_private",
        "C4": "c4_days",
        "D1": "d1_intraday",
        "D2": "d2_high",
        "D3": "d3_trust",
    },
    "value_institution": {
        "A1": "a1_50w",
        "A2": "a2_30",
        "A3": "a3_0",
        "A4": "a4_stable",
        "B1": "b1_research",
        "B2": "b2_5",
        "B3": "b3_clear",
        "C1": "c1_50",
        "C2": "c2_low",
        "C3": "c3_private",
        "C4": "c4_years",
        "D1": "d1_daily",
        "D2": "d2_low",
        "D3": "d3_self",
    },
    "contrarian_fund": {
        "A1": "a1_50w",
        "A2": "a2_30",
        "A3": "a3_m25",
        "A4": "a4_stable",
        "B1": "b1_research",
        "B2": "b2_5",
        "B3": "b3_clear",
        "C1": "c1_30",
        "C2": "c2_low",
        "C3": "c3_private",
        "C4": "c4_months",
        "D1": "d1_daily",
        "D2": "d2_low",
        "D3": "d3_self",
    },
    "influencer_amplifier": {
        "A1": "a1_10w",
        "A2": "a2_30",
        "A3": "a3_0",
        "A4": "a4_stable",
        "B1": "b1_media",
        "B2": "b2_3",
        "B3": "b3_vague",
        "C1": "c1_15",
        "C2": "c2_high",
        "C3": "c3_public",
        "C4": "c4_days",
        "D1": "d1_intraday",
        "D2": "d2_mid",
        "D3": "d3_trust",
    },
}


class PersonaVariableRegistryTests(unittest.TestCase):
    def test_registry_has_exact_14_rendered_variables_and_five_total_groups(self):
        self.assertEqual(
            persona.VARIABLE_IDS,
            (
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
            ),
        )
        rendered_groups = {
            definition.group_id for definition in persona.VARIABLE_REGISTRY.values()
        }
        inherited_groups = {variable_id[0] for variable_id in persona.INHERITED_VARIABLES}
        self.assertEqual(rendered_groups | inherited_groups, set("ABCDE"))
        self.assertEqual(set(persona.INHERITED_VARIABLES), {"E1", "E2"})
        self.assertTrue(
            all(not item.rendered for item in persona.INHERITED_VARIABLES.values())
        )

    def test_frozen_level_ids_and_fragments_match_protocol_byte_for_byte(self):
        actual = {
            variable_id: tuple(
                (level.id, level.fragment) for level in definition.levels
            )
            for variable_id, definition in persona.VARIABLE_REGISTRY.items()
        }
        self.assertEqual(actual, EXPECTED_LEVELS)

    def test_reference_theta_is_exact_and_valid(self):
        self.assertEqual(dict(persona.REFERENCE_THETA), EXPECTED_REFERENCE)
        self.assertEqual(
            persona.validate_theta(persona.REFERENCE_THETA),
            EXPECTED_REFERENCE,
        )

    def test_legacy_personas_are_coordinate_rows_and_quant_arb_is_na(self):
        self.assertEqual(
            set(persona.LEGACY_PERSONA_COORDINATES),
            set(EXPECTED_COORDINATES) | {"quant_arb"},
        )
        for persona_id, expected in EXPECTED_COORDINATES.items():
            self.assertEqual(
                dict(persona.LEGACY_PERSONA_COORDINATES[persona_id]),
                expected,
            )
        self.assertIsNone(persona.LEGACY_PERSONA_COORDINATES["quant_arb"])

    def test_e1_e2_are_derived_read_only_from_existing_personas(self):
        for persona_id, existing in PERSONAS_BY_ID.items():
            with self.subTest(persona=persona_id):
                inherited = persona.inherited_values_for_persona(persona_id)
                self.assertEqual(
                    inherited["E1"],
                    float(existing["social_susceptibility"]),
                )
                self.assertEqual(
                    inherited["E2"],
                    "high" if persona_id == "influencer_amplifier" else "low",
                )
        with self.assertRaisesRegex(
            persona.PersonaVariableError,
            "unknown existing persona",
        ):
            persona.inherited_values_for_persona("unknown")

    def test_registry_and_frozen_mappings_are_read_only(self):
        with self.assertRaises(TypeError):
            persona.VARIABLE_REGISTRY["A1"] = None
        with self.assertRaises(TypeError):
            persona.REFERENCE_THETA["A1"] = "a1_2w"
        with self.assertRaises(TypeError):
            persona.LEGACY_PERSONA_COORDINATES["retail_crowd"]["A1"] = "a1_2w"

    def test_flip_endpoints_and_expected_high_minus_low_sell_directions(self):
        expected_endpoints = {
            "A1": ("a1_2w", "a1_50w"),
            "A2": ("a2_10", "a2_90"),
            "A3": ("a3_0", "a3_m50"),
            "A4": ("a4_stable", "a4_needcash"),
            "B1": ("b1_research", "b1_peers"),
            "B2": ("b2_1", "b2_5"),
            "B3": ("b3_vague", "b3_clear"),
            "C1": ("c1_5", "c1_50"),
            "C2": ("c2_low", "c2_high"),
            "C3": ("c3_private", "c3_public"),
            "C4": ("c4_days", "c4_years"),
            "D1": ("d1_weekly", "d1_intraday"),
            "D2": ("d2_low", "d2_high"),
            "D3": ("d3_self", "d3_trust"),
        }
        expected_directions = {
            "A1": None,
            "A2": 1,
            "A3": -1,
            "A4": 1,
            "B1": 1,
            "B2": -1,
            "B3": 1,
            "C1": -1,
            "C2": -1,
            "C3": -1,
            "C4": -1,
            "D1": 1,
            "D2": 1,
            "D3": 1,
        }
        self.assertEqual(dict(persona.FLIP_ENDPOINTS), expected_endpoints)
        self.assertEqual(
            dict(persona.EXPECTED_SELL_DIRECTIONS),
            expected_directions,
        )
        self.assertIs(persona.LOW_HIGH_ENDPOINTS, persona.FLIP_ENDPOINTS)
        self.assertIs(
            persona.EXPECTED_DELTA_DIRECTIONS,
            persona.EXPECTED_SELL_DIRECTIONS,
        )
        for variable_id in persona.VARIABLE_IDS:
            low, high = persona.flip_pair(variable_id)
            self.assertEqual(low[variable_id], expected_endpoints[variable_id][0])
            self.assertEqual(high[variable_id], expected_endpoints[variable_id][1])
            for unchanged in set(persona.VARIABLE_IDS) - {variable_id}:
                self.assertEqual(low[unchanged], persona.REFERENCE_THETA[unchanged])
                self.assertEqual(high[unchanged], persona.REFERENCE_THETA[unchanged])

    def test_fixture_assignment_matches_protocol(self):
        self.assertEqual(persona.fixture_for_variable("D2"), "sideways_2y_hot_peers")
        self.assertEqual(persona.fixture_for_variable("C4"), "sideways_2y_hot_peers")
        self.assertEqual(persona.fixture_for_variable("A3"), "deep_loss_bad_news")
        self.assertEqual(persona.fixture_for_variable("C1"), "deep_loss_bad_news")
        self.assertEqual(
            persona.fixture_for_variable("C2"),
            "public_holding_friends_ask",
        )
        self.assertEqual(
            persona.fixture_for_variable("C3"),
            "public_holding_friends_ask",
        )
        self.assertEqual(persona.fixture_for_variable("B1"), "belief_source_pair")
        self.assertEqual(persona.fixture_for_variable("B2"), "belief_source_pair")
        for variable_id in {"A1", "A2", "A4", "B3", "D1", "D3"}:
            self.assertEqual(
                persona.fixture_for_variable(variable_id),
                "negative_news_price_unchanged",
            )


class PersonaRenderTests(unittest.TestCase):
    def test_render_preserves_fragments_under_first_person_self_memo_wrapper(self):
        rendered = persona.render(persona.REFERENCE_THETA, seed=17)
        self.assertTrue(rendered.startswith("我的处境备忘：“"))
        self.assertTrue(rendered.endswith("”。"))
        for variable_id, level_id in persona.REFERENCE_THETA.items():
            fragment = persona.VARIABLE_REGISTRY[variable_id].level(level_id).fragment
            self.assertEqual(rendered.count(fragment), 1)

    def test_every_level_render_is_within_chinese_character_bound_and_forbidden_free(self):
        vectors = [dict(persona.REFERENCE_THETA)]
        for variable_id, definition in persona.VARIABLE_REGISTRY.items():
            for level in definition.levels:
                theta = dict(persona.REFERENCE_THETA)
                theta[variable_id] = level.id
                vectors.append(theta)
        vectors.extend(
            [
                {
                    variable_id: min(
                        definition.levels,
                        key=lambda level: persona.chinese_character_count(level.fragment),
                    ).id
                    for variable_id, definition in persona.VARIABLE_REGISTRY.items()
                },
                {
                    variable_id: max(
                        definition.levels,
                        key=lambda level: persona.chinese_character_count(level.fragment),
                    ).id
                    for variable_id, definition in persona.VARIABLE_REGISTRY.items()
                },
            ]
        )
        for index, theta in enumerate(vectors):
            with self.subTest(vector=index):
                rendered = persona.render(theta, seed=index)
                self.assertGreaterEqual(persona.chinese_character_count(rendered), 120)
                self.assertLessEqual(persona.chinese_character_count(rendered), 300)
                for forbidden in persona.FORBIDDEN_RENDER_TERMS:
                    self.assertNotIn(forbidden, rendered)
                for variable_id in persona.VARIABLE_IDS:
                    self.assertNotIn(variable_id, rendered)
                for definition in persona.VARIABLE_REGISTRY.values():
                    for level in definition.levels:
                        self.assertNotIn(level.id, rendered)

    def test_same_theta_and_seed_is_byte_identical_and_seed_has_effect_path(self):
        first = persona.render(persona.REFERENCE_THETA, seed=0)
        second = persona.render(dict(reversed(tuple(persona.REFERENCE_THETA.items()))), seed=0)
        alternate = persona.render(persona.REFERENCE_THETA, seed=1)
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))
        self.assertNotEqual(first, alternate)
        prefix = "我的处境备忘：“"
        suffix = "”。"
        self.assertEqual(
            sorted(first[len(prefix) : -len(suffix)].split("；")),
            sorted(alternate[len(prefix) : -len(suffix)].split("；")),
        )

    def test_validate_theta_rejects_partial_unknown_and_invalid_levels(self):
        partial = dict(persona.REFERENCE_THETA)
        del partial["D3"]
        with self.assertRaisesRegex(persona.PersonaVariableError, "missing variables: D3"):
            persona.validate_theta(partial)

        unknown = dict(persona.REFERENCE_THETA)
        unknown["E1"] = "high"
        with self.assertRaisesRegex(persona.PersonaVariableError, "unknown variables: E1"):
            persona.validate_theta(unknown)

        invalid = dict(persona.REFERENCE_THETA)
        invalid["A1"] = "a1_unknown"
        with self.assertRaisesRegex(persona.PersonaVariableError, "unknown level"):
            persona.render(invalid)

        non_string = dict(persona.REFERENCE_THETA)
        non_string["A1"] = 10
        with self.assertRaisesRegex(persona.PersonaVariableError, "must be a string"):
            persona.validate_theta(non_string)


class PersonaSamplingTests(unittest.TestCase):
    def test_derive_seed_and_uniform_sample_have_frozen_snapshots(self):
        self.assertEqual(
            persona.derive_seed(42, "x"),
            79747168605700929917006498173361875468412856387696119734179750654320848281042,
        )
        self.assertEqual(
            persona.sample_uniform(42),
            {
                "A1": "a1_2w",
                "A2": "a2_70",
                "A3": "a3_m25",
                "A4": "a4_stable",
                "B1": "b1_trend",
                "B2": "b2_5",
                "B3": "b3_clear",
                "C1": "c1_15",
                "C2": "c2_high",
                "C3": "c3_public",
                "C4": "c4_years",
                "D1": "d1_intraday",
                "D2": "d2_high",
                "D3": "d3_self",
            },
        )
        persona.validate_theta(persona.sample_uniform(42))

    def test_region_constrains_dimensions_and_normalises_candidate_order(self):
        first = persona.sample_region(
            {
                "A1": ["a1_2w", "a1_5w"],
                "D1": {"d1_intraday", "d1_weekly"},
                "B3": "b3_clear",
            },
            seed=9,
        )
        second = persona.sample_region(
            {
                "B3": "b3_clear",
                "D1": {"d1_weekly", "d1_intraday"},
                "A1": ["a1_5w", "a1_2w"],
            },
            seed=9,
        )
        self.assertEqual(first, second)
        self.assertIn(first["A1"], {"a1_2w", "a1_5w"})
        self.assertIn(first["D1"], {"d1_intraday", "d1_weekly"})
        self.assertEqual(first["B3"], "b3_clear")
        persona.validate_theta(first)

    def test_sampling_is_independent_of_pythonhashseed_across_processes(self):
        script = (
            "import json;"
            "from nmsim.persona_variables import render,sample_region,sample_uniform;"
            "payload={'uniform':sample_uniform(731),"
            "'region':sample_region({'A1':{'a1_2w','a1_5w'},"
            "'D1':{'d1_daily','d1_weekly'}},731)};"
            "payload['render']=render(payload['uniform'],731);"
            "print(json.dumps(payload,ensure_ascii=False,sort_keys=True,"
            "separators=(',',':')))"
        )
        outputs = []
        for hash_seed in ("1", "8675309"):
            environment = os.environ.copy()
            environment["PYTHONHASHSEED"] = hash_seed
            environment["PYTHONPATH"] = str(ROOT)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            outputs.append(result.stdout)
        self.assertEqual(outputs[0].encode("utf-8"), outputs[1].encode("utf-8"))
        json.loads(outputs[0])

    def test_invalid_seed_and_region_fail_closed(self):
        for invalid_seed in (True, 1.5, "1", None):
            with self.subTest(seed=invalid_seed), self.assertRaises(TypeError):
                persona.sample_uniform(invalid_seed)
        with self.assertRaisesRegex(persona.PersonaVariableError, "region must be"):
            persona.sample_region(["a1_2w"], seed=1)
        with self.assertRaisesRegex(persona.PersonaVariableError, "unknown variables"):
            persona.sample_region({"E1": ["high"]}, seed=1)
        with self.assertRaisesRegex(persona.PersonaVariableError, "unknown levels"):
            persona.sample_region({"A1": ["not-a-level"]}, seed=1)
        with self.assertRaisesRegex(
            persona.PersonaVariableError,
            "at least one level",
        ):
            persona.sample_region({"A1": []}, seed=1)
        with self.assertRaisesRegex(persona.PersonaVariableError, "non-string level"):
            persona.sample_region({"A1": [1]}, seed=1)


if __name__ == "__main__":
    unittest.main()
