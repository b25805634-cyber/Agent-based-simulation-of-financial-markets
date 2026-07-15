"""Regression tests for the public/private social boundary."""
from __future__ import annotations

import json
import random
import unittest

from nmsim.agents import make_agents
from nmsim.config import Config
from nmsim.contagion import neighbor_feed
from nmsim.llm import parse_order


class PrivateRationaleInvariantTests(unittest.TestCase):
    def test_legacy_rationale_is_not_promoted_to_public_take_or_feed(self):
        secret = "PRIVATE_LEGACY_RATIONALE"
        raw = json.dumps({
            "side": "sell",
            "quantity": 3,
            "limit_price": 99.0,
            "sentiment": -0.8,
            "rationale": secret,
        })
        order = parse_order(raw, 100.0)

        self.assertEqual(order["rationale"], secret)
        self.assertEqual(order["public_take"], "")

        agent = make_agents(Config(n_llm_agents=1, n_noise_agents=0))[0]
        order["agent"] = agent.name
        statement = agent.statement(order)
        self.assertEqual(statement["text"], "")
        feed, _ = neighbor_feed(
            "neighbor",
            {agent.name: statement},
            {"neighbor": [agent.name]},
            "network",
            4,
            random.Random(1),
        )
        self.assertNotIn(secret, json.dumps(feed))


if __name__ == "__main__":
    unittest.main()
