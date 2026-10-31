import unittest

from opticrip.model import fmt_capacity, fmt_duration, fmt_size


class ModelTests(unittest.TestCase):
    def test_size_truncates_like_the_reference(self):
        # Verified against the disc capture: 512417792 bytes -> 488.6 MB.
        self.assertEqual(fmt_size(512417792), "488.6 MB")
        self.assertEqual(fmt_size(198668288), "189.4 MB")
        self.assertEqual(fmt_size(106754048), "101.8 MB")
        self.assertEqual(fmt_size(0), "")

    def test_duration_truncates(self):
        self.assertEqual(fmt_duration(223.93), "0:03:43")
        self.assertEqual(fmt_duration(4991.8), "1:23:11")
        self.assertEqual(fmt_duration(0), "0:00:00")

    def test_capacity_uses_lower_case_b(self):
        self.assertEqual(fmt_capacity(7340244992), "6.83 Gb")
        self.assertEqual(fmt_capacity(100 * 1024 * 1024), "100 MB")


if __name__ == "__main__":
    unittest.main()
