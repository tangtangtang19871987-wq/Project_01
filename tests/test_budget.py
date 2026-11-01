from pathlib import Path
import tempfile
import unittest

from iltkb.budget import TokenBudget
from iltkb.storage import read_json


class TokenBudgetTests(unittest.TestCase):
    def test_reservation_blocks_requests_before_limit_is_crossed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token_budget.json"
            budget = TokenBudget(path, 100)
            budget.reserve(70)
            with self.assertRaisesRegex(ValueError, "exhausted"):
                TokenBudget(path, 100).reserve(40)
            budget.settle(70, 30)
            budget.reserve(60)
            budget.settle(60, None)
            self.assertEqual(read_json(path), {"limit": 100, "spent": 90, "reserved": 0})
            with self.assertRaisesRegex(ValueError, "exhausted"):
                budget.reserve(11)

    def test_existing_ledger_cannot_silently_raise_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token_budget.json"
            TokenBudget(path, 100).reserve(50)
            with self.assertRaisesRegex(ValueError, "differs"):
                TokenBudget(path, 200).reserve(1)


if __name__ == "__main__":
    unittest.main()
