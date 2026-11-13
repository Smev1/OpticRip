import unittest

from opticrip.model import Disc, Title
from opticrip.naming import default_template, expand, sanitize, unique_path


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.disc = Disc(kind="DVD", label="OVER_THE_HEDGE")

    def title(self, **kwargs):
        return Title(index=0, **kwargs)

    def test_documented_default_template(self):
        self.assertEqual(default_template(), "{NAME2}{-:CMNT2}{-:DT}{title:+DFLT}{_t:N2}")

    def test_unnamed_title_falls_back_to_title(self):
        name = expand(default_template(), self.title())
        self.assertEqual(name, "title_t00.mkv")

    def test_comment_only_matches_verified_capture(self):
        # The reference produced YB8_t00.mkv for a title whose comment is YB8.
        name = expand(default_template(), self.title(comment="YB8"))
        self.assertEqual(name, "YB8_t00.mkv")

    def test_name_and_comment_are_separated(self):
        name = expand(default_template(), self.title(name="Movie", comment="Tag"))
        self.assertEqual(name, "Movie-Tag_t00.mkv")

    def test_index_is_two_digits(self):
        title = Title(index=12, name="Clip")
        self.assertEqual(expand(default_template(), title), "Clip_t12.mkv")

    def test_invalid_characters_are_removed(self):
        title = Title(index=0, name='Bad:/\\*?"<>|Name')
        self.assertEqual(expand(default_template(), title), "BadName_t00.mkv")

    def test_empty_result_is_never_empty(self):
        self.assertTrue(expand("", self.title()).endswith(".mkv"))
        self.assertEqual(expand("{NAME2}", self.title()), "title.mkv")

    def test_sanitize_trims_dots(self):
        self.assertEqual(sanitize("name..."), "name")

    def test_unique_path(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            first = unique_path(Path(tmp), "a.mkv")
            first.write_bytes(b"x")
            second = unique_path(Path(tmp), "a.mkv")
            self.assertEqual(second.name, "a_1.mkv")


if __name__ == "__main__":
    unittest.main()
