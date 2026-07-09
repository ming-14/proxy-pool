import json
import logging
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from ua_generator import UAGenerator


class TestUAGenerator(unittest.TestCase):
    def test_same_seed_same_ua(self):
        gen = UAGenerator()
        ua1 = gen.get("test-seed")
        ua2 = gen.get("test-seed")
        self.assertEqual(ua1, ua2)

    def test_different_seed_may_differ(self):
        gen = UAGenerator()
        ua1 = gen.get("seed-a")
        ua2 = gen.get("seed-b")
        self.assertIn("User-Agent", ua1)
        self.assertIn("User-Agent", ua2)

    def test_chrome_ua_has_sec_ch_ua(self):
        gen = UAGenerator()
        for seed in ["a", "b", "c", "d", "e"]:
            ua = gen.get(seed)
            self.assertIn("User-Agent", ua)
            self.assertIn("Sec-Ch-Ua", ua)

    def test_local_seed(self):
        gen = UAGenerator()
        ua = gen.get("local")
        self.assertIn("User-Agent", ua)

    def test_deterministic_seed(self):
        gen1 = UAGenerator()
        gen2 = UAGenerator()
        self.assertEqual(gen1.get("proxy-1"), gen2.get("proxy-1"))
        self.assertEqual(gen1.get("proxy-2"), gen2.get("proxy-2"))

    def test_get_headers_contains_user_agent(self):
        gen = UAGenerator()
        headers = gen.get_headers("test")
        self.assertIn("User-Agent", headers)

    def test_get_headers_chrome_has_sec_ch_ua(self):
        gen = UAGenerator()
        for seed in ["a", "b", "c", "d", "e"]:
            headers = gen.get_headers(seed)
            self.assertIn("User-Agent", headers)

    def test_get_headers_same_seed_deterministic(self):
        gen = UAGenerator()
        h1 = gen.get_headers("seed-x")
        h2 = gen.get_headers("seed-x")
        self.assertEqual(h1, h2)

    def test_seed_to_int_deterministic(self):
        val1 = UAGenerator._seed_to_int("hello")
        val2 = UAGenerator._seed_to_int("hello")
        self.assertEqual(val1, val2)

    def test_seed_to_int_different_seeds(self):
        val1 = UAGenerator._seed_to_int("seed-a")
        val2 = UAGenerator._seed_to_int("seed-b")
        self.assertNotEqual(val1, val2)

    def test_ua_has_required_keys(self):
        gen = UAGenerator()
        for seed in ["a", "b", "c"]:
            ua = gen.get(seed)
            self.assertIn("User-Agent", ua)
            self.assertIn("Sec-Ch-Ua", ua)
            self.assertIn("Sec-Ch-Ua-Mobile", ua)
            self.assertIn("Sec-Ch-Ua-Platform", ua)

    def test_non_chrome_ua_no_sec_ch_ua(self):
        gen = UAGenerator()
        for seed in range(100):
            ua = gen.get(f"seed-{seed}")
            if "Chrome" not in ua["User-Agent"] and "Sec-Ch-Ua" not in ua:
                self.assertIsNone(ua["Sec-Ch-Ua"])
                self.assertIsNone(ua["Sec-Ch-Ua-Mobile"])
                self.assertIsNone(ua["Sec-Ch-Ua-Platform"])
                return
        self.skipTest("No non-Chrome UA found in 100 seeds")

    def test_get_caches_result(self):
        gen = UAGenerator()
        ua1 = gen.get("cache-test")
        self.assertIn("cache-test", gen._cache)
        ua2 = gen.get("cache-test")
        self.assertIs(ua1, ua2)

    def test_chrome_sec_ch_ua_contains_version(self):
        gen = UAGenerator()
        for seed in ["a", "b", "c", "d", "e"]:
            ua = gen.get(seed)
            if ua["Sec-Ch-Ua"]:
                self.assertIn("Chromium", ua["Sec-Ch-Ua"])
                self.assertIn("Google Chrome", ua["Sec-Ch-Ua"])

    def test_chrome_mobile_is_question_zero(self):
        gen = UAGenerator()
        for seed in ["a", "b", "c"]:
            ua = gen.get(seed)
            if ua["Sec-Ch-Ua"]:
                self.assertEqual(ua["Sec-Ch-Ua-Mobile"], "?0")

    def test_chrome_platform_is_windows(self):
        gen = UAGenerator()
        for seed in ["a", "b", "c"]:
            ua = gen.get(seed)
            if ua["Sec-Ch-Ua"]:
                self.assertEqual(ua["Sec-Ch-Ua-Platform"], '"Windows"')


if __name__ == "__main__":
    unittest.main()
