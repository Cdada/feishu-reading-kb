from decimal import Decimal
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("probe", Path(__file__).resolve().parents[1] / "real_vision_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class VisionBudgetTests(unittest.TestCase):
    def test_full_context_peak_output_is_below_reservation_and_cap(self):
        worst = probe.cost_upper({"prompt_tokens": 1_048_576, "completion_tokens": probe.MAX_OUTPUT})
        self.assertLess(worst, probe.RESERVATION_CNY)
        self.assertLess(probe.RESERVATION_CNY, probe.CAP_CNY)

    def test_unknown_intent_cannot_be_reused_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intent.json"
            probe.reserve(path, "synthetic", Decimal("3"))
            with self.assertRaises(FileExistsError):
                probe.reserve(path, "synthetic", Decimal("3"))
            self.assertIn("reserved_unknown", path.read_text())

    def test_insufficient_balance_does_not_create_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intent.json"
            with self.assertRaises(ValueError):
                probe.reserve(path, "synthetic", Decimal("2.49"))
            self.assertFalse(path.exists())
