import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timetrace.adapters.cursor import CursorAdapter, safe_capabilities
from timetrace.adapters.gemini import GeminiAdapter
from timetrace.billing import StaticBilling


class CapabilitiesTest(unittest.TestCase):
    def test_management_only_default(self):
        result = safe_capabilities()
        self.assertTrue(result["can_record"])
        self.assertFalse(result["can_dispatch"])
        self.assertFalse(result["can_enforce_zero_spend"])

    def test_acp_adapters_without_their_binary_are_management_only(self):
        for adapter in (CursorAdapter({}, which=lambda name: None),
                        GeminiAdapter({}, billing=StaticBilling(False, "settings_missing"), which=lambda name: None)):
            caps = adapter.capabilities()
            self.assertEqual(set(caps), {"can_record", "can_read_quota", "can_dispatch",
                                         "can_resume", "can_interact", "can_enforce_zero_spend"})
            self.assertTrue(caps["can_record"])
            self.assertFalse(caps["can_dispatch"])
            self.assertFalse(caps["can_resume"])
            self.assertFalse(caps["can_read_quota"])
            self.assertFalse(caps["can_interact"])
            self.assertFalse(caps["can_enforce_zero_spend"])
            self.assertTrue(adapter.adapter_version)

    def test_a_run_whose_binary_is_missing_fails_without_raising(self):
        # Start / resume are implemented (through ACP) for both; the gate, not a NotImplementedError, keeps an
        # unverified tool from running. A binary that does not exist is an ordinary failed run.
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HOME": d}):
            missing = str(Path(d) / "missing-agent")
            for adapter in (CursorAdapter({"bin": missing}), GeminiAdapter({"bin": missing}, billing=StaticBilling(True))):
                for operation in ("start", "resume"):
                    with self.subTest(adapter=adapter.name, operation=operation):
                        result = getattr(adapter, operation)("prompt", d, "sess", str(Path(d) / "log"))
                        self.assertFalse(result.ok)
                        self.assertIn("could not start", result.error)


if __name__ == "__main__":
    unittest.main()
