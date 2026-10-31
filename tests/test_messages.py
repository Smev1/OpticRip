import unittest

from opticrip import messages


class MessageTests(unittest.TestCase):
    def test_render_replaces_all_parameters(self):
        self.assertEqual(
            messages.render(3025, 1, 63, 120),
            "Title #1 has length of 63 seconds which is less than minimum title "
            "length of 120 seconds and was therefore skipped",
        )

    def test_rendered_text_has_no_placeholders(self):
        text = messages.render(3028, 2, 1, "0:03:43")
        self.assertFalse(messages.has_placeholders(text))

    def test_unknown_id_is_safe(self):
        text = messages.render(999999)
        self.assertIn("unknown message", text)

    def test_flags(self):
        self.assertEqual(messages.flags(5014), messages.HAVE_URL)
        self.assertEqual(messages.flags(5036), messages.BOXOK)
        self.assertEqual(messages.flags(3028), 0)
        self.assertEqual(messages.flags(2003), messages.ERROR)

    def test_verified_ids_exist(self):
        for ident in (1005, 1011, 2003, 2024, 3006, 3007, 3010, 3025, 3028,
                      3038, 3305, 5005, 5006, 5010, 5011, 5014, 5036, 5042):
            self.assertNotIn("unknown message", messages.template(ident))


if __name__ == "__main__":
    unittest.main()
