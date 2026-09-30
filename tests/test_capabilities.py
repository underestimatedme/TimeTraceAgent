import unittest

from timetrace.adapters.cursor import CursorAdapter, safe_capabilities
from timetrace.adapters.gemini import GeminiAdapter


class CapabilitiesTest(unittest.TestCase):
    def test_management_only_default(self):
        result = safe_capabilities()
        self.assertTrue(result["can_record"])
        self.assertFalse(result["can_dispatch"])
        self.assertFalse(result["can_enforce_zero_spend"])

    def test_adapters_are_management_only(self):
        for adapter in (CursorAdapter({}), GeminiAdapter({})):
            caps = adapter.capabilities()
            self.assertEqual(set(caps), {"can_record", "can_read_quota", "can_dispatch",
                                         "can_resume", "can_enforce_zero_spend"})
            self.assertTrue(caps["can_record"])
            self.assertFalse(caps["can_dispatch"])
            self.assertFalse(caps["can_resume"])
            self.assertFalse(caps["can_enforce_zero_spend"])
            self.assertTrue(adapter.adapter_version)

    def test_management_only_adapters_do_not_execute(self):
        # Unverified tools must not provide either execution path.
        for adapter in (CursorAdapter({}), GeminiAdapter({})):
            with self.subTest(adapter=adapter.name, operation="start"):
                with self.assertRaises(NotImplementedError):
                    adapter.start("prompt", "/tmp", "sess", "log")
            with self.subTest(adapter=adapter.name, operation="resume"):
                with self.assertRaises(NotImplementedError):
                    adapter.resume("prompt", "/tmp", "sess", "log")


if __name__ == "__main__":
    unittest.main()
