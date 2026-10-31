import unittest

from opticrip.model import FLAG_HAS_CORE, Stream, Title
from opticrip.selection import DEFAULT_RULE, apply, parse


def sample_title():
    return Title(index=0, streams=[
        Stream(kind="video", index=0, codec="MPEG-2"),
        Stream(kind="audio", index=1, codec="AC-3", language="eng", channels=6),
        Stream(kind="audio", index=2, codec="AC-3", language="eng", channels=2),
        Stream(kind="audio", index=3, codec="AC-3", language="fre", channels=2),
        Stream(kind="subtitle", index=4, codec="DVD subtitles", language="eng"),
    ])


class SelectionTests(unittest.TestCase):
    def test_parse_default_rule(self):
        rules = parse(DEFAULT_RULE)
        self.assertEqual(rules[0].action, "-")
        self.assertEqual(rules[0].groups, ["all"])
        self.assertEqual(rules[1].action, "+")
        self.assertEqual(rules[1].groups, ["favlang", "nolang", "single"])
        weight_rules = [r for r in rules if r.action == "="]
        self.assertEqual(weight_rules[0].weight, 100)
        self.assertEqual(weight_rules[1].weight, -10)

    def test_default_rule_with_preferred_language(self):
        title = sample_title()
        apply(title, DEFAULT_RULE, "eng")
        selected = {s.index: s.selected for s in title.streams}
        self.assertTrue(selected[0], "video must stay selected")
        self.assertFalse(selected[1], "multi-channel audio is dropped")
        self.assertTrue(selected[2], "preferred stereo audio is kept")
        self.assertFalse(selected[3], "foreign audio is not selected")
        self.assertTrue(selected[4], "the only subtitle track is kept")

    def test_video_is_never_dropped(self):
        title = sample_title()
        apply(title, "-sel:all", "")
        self.assertTrue(any(s.selected for s in title.video_streams()))

    def test_havecore_group(self):
        title = Title(index=0, streams=[
            Stream(kind="audio", index=0, codec="TrueHD", flags=FLAG_HAS_CORE),
            Stream(kind="audio", index=1, codec="AC-3"),
        ])
        apply(title, "-sel:all,+sel:havecore", "")
        self.assertTrue(title.streams[0].selected)
        self.assertFalse(title.streams[1].selected)

    def test_single_group(self):
        title = Title(index=0, streams=[
            Stream(kind="audio", index=0, codec="AC-3"),
        ])
        apply(title, "-sel:all,+sel:single", "")
        self.assertTrue(title.streams[0].selected)

    def test_weight_rules_do_not_select(self):
        title = sample_title()
        apply(title, "-sel:all,=100:all", "")
        self.assertTrue(any(s.selected for s in title.streams))
        # =100:all must not resurrect tracks deselected by -sel:all
        self.assertEqual([s.selected for s in title.streams[1:]],
                         [False, False, False, False])


if __name__ == "__main__":
    unittest.main()
