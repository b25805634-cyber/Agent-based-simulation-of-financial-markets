"""Contract tests for the isolated V2 price/volume teacher boundary."""
from __future__ import annotations

import json
import math
import unittest

from nmsim.v2_attention import (
    ACTION_ORDER,
    ANCHOR_FAMILY_ORDER,
    CONTRACT_HASH,
    STATE_DESIGN_VERSION,
    FEATURE_NAMES,
    FEATURE_ORDER,
    FEATURE_RANGES,
    FEATURE_SEMANTICS,
    FORBIDDEN_PROMPT_TERMS,
    ParsedTeacherResponse,
    PublicTeacherDecision,
    StateValidationError,
    TeacherObservation,
    TeacherReplicateResult,
    TeacherResponseError,
    V2AttentionState,
    V2ContractError,
    aggregate_replicates,
    aggregate_teacher_samples,
    build_teacher_prompt,
    canonical_json,
    contract_descriptor,
    derive_seed,
    design_anchor_states,
    design_latin_hypercube_states,
    fake_null_teacher,
    fake_provider_response,
    fake_test_teacher,
    generate_state_design,
    make_observation,
    parse_teacher_response,
    render_teacher_prompt,
    sha256_hex,
    state_design_descriptor,
)


def _state(**changes):
    values = {
        "return_1d": 0.01,
        "return_5d": 0.03,
        "return_20d": 0.08,
        "realized_vol_20d": 0.18,
        "drawdown_20d": -0.05,
        "volume_z": 0.4,
        "position_fraction": 0.5,
        "unrealized_return": 0.06,
        "unrealized_return_mask": 1,
        "days_since_trade_scaled": 0.2,
        "days_since_trade_scaled_mask": 1,
        "post_sale_return": 0.0,
        "post_sale_return_mask": 0,
        "log10_wealth": 5.5,
    }
    values.update(changes)
    return V2AttentionState.from_mapping(values)


def _observation(**state_changes):
    return make_observation("study-a", "family-a", 0, _state(**state_changes))


class CanonicalContractTests(unittest.TestCase):
    def test_feature_order_and_descriptor_hash_are_frozen(self):
        self.assertIs(FEATURE_NAMES, FEATURE_ORDER)
        self.assertEqual(len(FEATURE_NAMES), 14)
        self.assertEqual(len(set(FEATURE_NAMES)), len(FEATURE_NAMES))
        self.assertEqual(set(FEATURE_NAMES), set(FEATURE_RANGES))
        self.assertEqual(set(FEATURE_NAMES), set(FEATURE_SEMANTICS))
        self.assertEqual(ACTION_ORDER, ("buy", "hold", "sell"))
        self.assertEqual(
            CONTRACT_HASH,
            "b98badc9c60d8e226621bcb3834c7f08ff04c5abdc1c9f82e6006051e6f97792",
        )
        self.assertEqual(CONTRACT_HASH, sha256_hex(contract_descriptor()))
        self.assertEqual(
            state_design_descriptor()["schema_version"], STATE_DESIGN_VERSION
        )

    def test_canonical_json_and_seed_are_order_stable(self):
        left = {"z": [1, -0.0], "a": {"b": "中文", "a": True}}
        right = {"a": {"a": True, "b": "中文"}, "z": [1, 0.0]}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(sha256_hex(left), sha256_hex(right))
        self.assertEqual(derive_seed("a", 2), derive_seed("a", 2))
        self.assertNotEqual(derive_seed("a", 2), derive_seed(2, "a"))
        self.assertGreaterEqual(derive_seed("a"), 0)
        self.assertLess(derive_seed("a"), 2 ** 64)

    def test_canonical_json_rejects_nonfinite_and_private_container(self):
        with self.assertRaises(V2ContractError):
            canonical_json({"bad": math.nan})
        parsed = ParsedTeacherResponse(PublicTeacherDecision("hold", 0.0), "private")
        with self.assertRaises(V2ContractError):
            canonical_json(parsed)


class StateContractTests(unittest.TestCase):
    def test_mapping_order_does_not_change_state_vector_or_hash(self):
        state = _state()
        reverse = V2AttentionState.from_mapping(
            {name: state.to_dict()[name] for name in reversed(FEATURE_ORDER)}
        )
        self.assertEqual(state, reverse)
        self.assertEqual(
            state.to_feature_vector(),
            tuple(float(state.to_dict()[name]) for name in FEATURE_ORDER),
        )
        self.assertEqual(state.scientific_state_hash, reverse.scientific_state_hash)

    def test_unknown_missing_and_nonstring_fields_fail_closed(self):
        values = _state().to_dict()
        values["unknown"] = 1
        with self.assertRaisesRegex(StateValidationError, "extra"):
            V2AttentionState.from_mapping(values)
        values = _state().to_dict()
        del values["return_1d"]
        with self.assertRaisesRegex(StateValidationError, "missing"):
            V2AttentionState.from_mapping(values)
        values = _state().to_dict()
        values[7] = 1
        with self.assertRaisesRegex(StateValidationError, "names must be strings"):
            V2AttentionState.from_mapping(values)

    def test_ranges_finiteness_masks_and_account_semantics_are_enforced(self):
        for bad in (math.nan, math.inf, -math.inf, True, "0.1"):
            with self.subTest(bad=bad):
                with self.assertRaises(StateValidationError):
                    _state(return_1d=bad)
        with self.assertRaisesRegex(StateValidationError, "outside"):
            _state(position_fraction=1.01)
        with self.assertRaisesRegex(StateValidationError, "exactly 0 or 1"):
            _state(unrealized_return_mask=0.5)
        with self.assertRaisesRegex(StateValidationError, "placeholder"):
            _state(post_sale_return=0.1, post_sale_return_mask=0)
        with self.assertRaisesRegex(StateValidationError, "cannot be known"):
            _state(
                position_fraction=0.0,
                unrealized_return=0.0,
                unrealized_return_mask=1,
            )
        with self.assertRaisesRegex(StateValidationError, "drawdown_20d"):
            _state(
                return_1d=-0.10,
                return_5d=-0.20,
                return_20d=-0.30,
                drawdown_20d=-0.05,
            )
        with self.assertRaisesRegex(StateValidationError, "prior-trade"):
            _state(
                days_since_trade_scaled=0.0,
                days_since_trade_scaled_mask=0,
                post_sale_return=0.10,
                post_sale_return_mask=1,
            )

    def test_identity_hierarchy_and_content_address_are_stable(self):
        state = _state()
        first = make_observation("study-a", "family-a", 3, state)
        repeat = make_observation("study-a", "family-a", 3, state)
        another_study = make_observation("study-b", "family-a", 3, state)
        self.assertEqual(first, repeat)
        self.assertEqual(first.state_id, another_study.state_id)
        self.assertTrue(first.state_id.startswith(first.family_id + "/"))
        with self.assertRaisesRegex(StateValidationError, "child"):
            TeacherObservation("study-a", "family-a", "other/00000-x", state)


class StateDesignTests(unittest.TestCase):
    def test_anchor_order_and_ids_are_frozen(self):
        anchors = design_anchor_states("study-a")
        self.assertEqual(tuple(item.family_id for item in anchors), ANCHOR_FAMILY_ORDER)
        self.assertEqual(
            tuple(item.state_id for item in anchors),
            (
                "anchor.neutral_cash/00000-807f75798051",
                "anchor.cash_rally/00000-bd7f711be4e6",
                "anchor.holder_rally/00000-2cbee827482c",
                "anchor.holder_drawdown/00000-cfce8479a038",
                "anchor.post_sale_rally/00000-00ad33a65353",
                "anchor.fully_invested/00000-ea422c3912e4",
                "anchor.max_trade_age/00000-422a0cddf756",
                "anchor.volume_dry_jump/00000-e0f319dc2fec",
                "anchor.volume_dry_drop/00000-24772e483227",
            ),
        )
        self.assertEqual(
            {item.state.position_fraction for item in anchors} & {0.0, 1.0},
            {0.0, 1.0},
        )
        self.assertIn(1.0, {item.state.days_since_trade_scaled for item in anchors})
        self.assertTrue(
            {-6.0, 6.0}.issubset({item.state.volume_z for item in anchors})
        )

    def test_latin_hypercube_is_deterministic_ordered_and_stratified(self):
        first = design_latin_hypercube_states("study-a", 12, 901)
        repeat = design_latin_hypercube_states("study-a", 12, 901)
        changed = design_latin_hypercube_states("study-a", 12, 902)
        self.assertEqual(first, repeat)
        self.assertNotEqual(first, changed)
        self.assertEqual(
            [item.family_id for item in first],
            ["lhs.{:05d}".format(index) for index in range(12)],
        )
        self.assertEqual(len({item.family_id for item in first}), 12)
        self.assertTrue(
            all("/00000-" in item.state_id for item in first),
        )
        low, high = FEATURE_RANGES["return_1d"]
        bins = {
            min(
                11,
                int((item.state.return_1d - low) / (high - low) * 12),
            )
            for item in first
        }
        self.assertEqual(bins, set(range(12)))
        for item in first:
            self.assertEqual(
                item.state.unrealized_return == 0.0,
                item.state.unrealized_return_mask == 0,
            )

    def test_combined_design_has_all_anchors_and_exact_requested_count(self):
        design = generate_state_design(9, 17, "study-a")
        self.assertEqual(len(design), 9)
        self.assertEqual(
            tuple(item.family_id for item in design[: len(ANCHOR_FAMILY_ORDER)]),
            ANCHOR_FAMILY_ORDER,
        )
        self.assertEqual(len({item.family_id for item in design}), len(design))
        full_design = generate_state_design(96, 17, "study-a")
        self.assertEqual(len({item.family_id for item in full_design}), 96)
        with self.assertRaisesRegex(StateValidationError, "at least"):
            generate_state_design(len(ANCHOR_FAMILY_ORDER) - 1, 17, "study-a")

    def test_default_design_is_coherent_balanced_and_reproducible(self):
        design = generate_state_design(96, 20260811, "study-a")
        self.assertEqual(design, generate_state_design(96, 20260811, "study-a"))
        strata = {}
        for observation in design:
            state = observation.state
            self.assertLessEqual(
                state.drawdown_20d,
                min(0.0, state.return_1d, state.return_5d, state.return_20d)
                + 1e-12,
            )
            self.assertFalse(
                state.post_sale_return_mask
                and not state.days_since_trade_scaled_mask
            )
            tape = (
                "decline"
                if state.return_20d < -0.10
                else "rise" if state.return_20d > 0.10 else "flat"
            )
            position = (
                "cash"
                if state.position_fraction < 0.20
                else "invested" if state.position_fraction > 0.80 else "mixed"
            )
            key = (tape, position)
            strata[key] = strata.get(key, 0) + 1
        self.assertEqual(len(strata), 9)
        self.assertGreaterEqual(min(strata.values()), 5)


class TeacherPromptTests(unittest.TestCase):
    def test_prompt_uses_only_numeric_state_and_fixed_daily_semantics(self):
        observation = _observation()
        prompt = render_teacher_prompt(observation)
        payload = json.loads(prompt.user)
        self.assertIn("daily close t", payload["time_semantics"])
        self.assertEqual(
            payload["market_price_volume_summary"]["return_1d"],
            observation.state.return_1d,
        )
        self.assertNotIn(observation.study_id, prompt.user)
        self.assertNotIn(observation.family_id, prompt.user)
        self.assertNotIn(observation.state_id, prompt.user)
        self.assertEqual(build_teacher_prompt(observation), prompt)
        self.assertEqual(build_teacher_prompt(observation.state), prompt)

    def test_prompt_contains_no_identity_or_effect_labels(self):
        prompt = render_teacher_prompt(_observation())
        combined = (prompt.system + "\n" + prompt.user).casefold()
        for term in FORBIDDEN_PROMPT_TERMS:
            with self.subTest(term=term):
                self.assertNotIn(term.casefold(), combined)

    def test_prompt_hash_changes_only_with_prompt_content(self):
        first = render_teacher_prompt(_observation())
        same_state_new_identity = render_teacher_prompt(
            make_observation("study-b", "family-b", 9, _state())
        )
        changed_state = render_teacher_prompt(_observation(return_1d=0.02))
        self.assertEqual(first.prompt_hash, same_state_new_identity.prompt_hash)
        self.assertNotEqual(first.prompt_hash, changed_state.prompt_hash)


class TeacherResponseTests(unittest.TestCase):
    def test_valid_response_splits_public_label_from_private_rationale(self):
        parsed = parse_teacher_response(
            '{"action":"buy","intensity":0.4,"reasoning":"private detail"}',
            _state(),
        )
        self.assertEqual(parsed.public.action, "buy")
        self.assertEqual(parsed.public.intensity, 0.4)
        self.assertNotIn("reasoning", parsed.public_record())
        self.assertNotIn("private detail", canonical_json(parsed.public_record()))
        self.assertEqual(parsed.private_record()["reasoning"], "private detail")

    def test_malformed_or_semantically_invalid_responses_are_rejected(self):
        ordinary = _state()
        empty = _state(
            position_fraction=0.0,
            unrealized_return=0.0,
            unrealized_return_mask=0,
        )
        full = _state(position_fraction=1.0)
        cases = (
            ("not json", ordinary),
            ('{"action":"hold","intensity":0}', ordinary),
            ('{"action":"hold","intensity":0,"reasoning":"x","extra":1}', ordinary),
            ('{"action":"hold","action":"buy","intensity":0,"reasoning":"x"}', ordinary),
            ('{"action":"hold","intensity":NaN,"reasoning":"x"}', ordinary),
            ('{"action":"hold","intensity":0.1,"reasoning":"x"}', ordinary),
            ('{"action":"buy","intensity":0,"reasoning":"x"}', ordinary),
            ('{"action":"sell","intensity":0.2,"reasoning":"x"}', empty),
            ('{"action":"buy","intensity":0.2,"reasoning":"x"}', full),
            ('{"action":"BUY","intensity":0.2,"reasoning":"x"}', ordinary),
            ('{"action":"buy","intensity":true,"reasoning":"x"}', ordinary),
            ('{"action":"buy","intensity":0.2,"reasoning":" "}', ordinary),
            ('```json\n{"action":"hold","intensity":0,"reasoning":"x"}\n```', ordinary),
        )
        for raw, state in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(TeacherResponseError):
                    parse_teacher_response(raw, state)

    def test_fake_teacher_is_deterministic_strict_and_feasible(self):
        observations = design_anchor_states("fake-study")
        for observation in observations:
            for replicate_index in range(15):
                with self.subTest(
                    state=observation.state_id, replicate=replicate_index
                ):
                    raw = fake_test_teacher(observation, replicate_index)
                    self.assertEqual(raw, fake_provider_response(observation, replicate_index))
                    self.assertEqual(raw, fake_test_teacher(observation, replicate_index))
                    parsed = parse_teacher_response(raw, observation.state)
                    self.assertIn(parsed.public.action, ACTION_ORDER)
                    self.assertIn("not empirical", parsed.private_rationale)

    def test_null_teacher_is_a_constant_valid_control(self):
        observations = design_anchor_states("fake-study")
        raw_values = {
            fake_null_teacher(observation, replicate_index)
            for observation in observations
            for replicate_index in range(3)
        }
        self.assertEqual(len(raw_values), 1)
        for observation in observations:
            parsed = parse_teacher_response(next(iter(raw_values)), observation.state)
            self.assertEqual(parsed.public, PublicTeacherDecision("hold", 0.0))


class ReplicateAggregationTests(unittest.TestCase):
    def setUp(self):
        self.observation = _observation()

    def _success(self, index, action, intensity):
        parsed = ParsedTeacherResponse(
            PublicTeacherDecision(action, intensity), "private-{}".format(index)
        )
        return TeacherReplicateResult.success(self.observation, index, parsed)

    def test_honest_n_probabilities_and_conditional_intensity(self):
        results = [
            self._success(3, "buy", 0.6),
            TeacherReplicateResult.failure(self.observation, 4, "parse_error"),
            self._success(0, "buy", 0.2),
            self._success(1, "hold", 0.0),
            TeacherReplicateResult.failure(self.observation, 2, "provider_error"),
        ]
        target = aggregate_replicates(self.observation, results)
        self.assertEqual((target.attempted_n, target.valid_n, target.failed_n), (5, 3, 2))
        self.assertAlmostEqual(target.action_probabilities["buy"], 2.0 / 3.0)
        self.assertAlmostEqual(target.action_probabilities["hold"], 1.0 / 3.0)
        self.assertEqual(target.action_probabilities["sell"], 0.0)
        buy_stats = target.conditional_intensity["buy"]
        self.assertEqual(buy_stats.n, 2)
        self.assertAlmostEqual(buy_stats.mean, 0.4)
        self.assertAlmostEqual(buy_stats.variance, 0.04)
        self.assertEqual(target.conditional_intensity["sell"].n, 0)
        self.assertIsNone(target.conditional_intensity["sell"].mean)
        self.assertEqual(
            target.failure_counts, {"parse_error": 1, "provider_error": 1}
        )
        public_blob = canonical_json(target)
        self.assertNotIn("private-", public_blob)

    def test_failures_are_not_counted_as_hold(self):
        failures = [
            TeacherReplicateResult.failure(self.observation, 0, "parse_error"),
            TeacherReplicateResult.failure(self.observation, 1, "parse_error"),
        ]
        target = aggregate_replicates(self.observation, failures)
        self.assertEqual((target.attempted_n, target.valid_n, target.failed_n), (2, 0, 2))
        self.assertEqual(
            target.action_probabilities,
            {"buy": None, "hold": None, "sell": None},
        )
        self.assertEqual(target.conditional_intensity["hold"].n, 0)

    def test_duplicate_or_wrong_identity_fails_closed(self):
        one = self._success(0, "hold", 0.0)
        with self.assertRaisesRegex(V2ContractError, "duplicate"):
            aggregate_replicates(self.observation, [one, one])
        other = make_observation("study-a", "family-b", 0, _state())
        wrong = TeacherReplicateResult.success(
            other,
            1,
            ParsedTeacherResponse(PublicTeacherDecision("hold", 0.0), "private"),
        )
        with self.assertRaisesRegex(V2ContractError, "identity"):
            aggregate_replicates(self.observation, [wrong])

    def test_group_adapter_is_order_stable(self):
        other = make_observation("study-a", "aaa-family", 0, _state())
        rows = [
            self._success(1, "buy", 0.4),
            TeacherReplicateResult.success(
                other,
                0,
                ParsedTeacherResponse(PublicTeacherDecision("hold", 0.0), "private"),
            ),
            self._success(0, "hold", 0.0),
        ]
        forward = aggregate_teacher_samples(rows)
        reverse = aggregate_teacher_samples(list(reversed(rows)))
        self.assertEqual(forward, reverse)
        self.assertEqual([item.family_id for item in forward], ["aaa-family", "family-a"])


if __name__ == "__main__":
    unittest.main()
