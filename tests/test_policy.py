import os
import tempfile
import unittest

from mediasync.policy import (
    PolicySet,
    add_pattern,
    load_rules,
    parse_rule,
    remove_pattern,
)


class PolicyRuleTests(unittest.TestCase):
    def test_parse_exact_glob_and_regex(self):
        self.assertEqual(parse_rule("Andor").kind, "exact")
        self.assertEqual(parse_rule("*/Extras").kind, "glob")
        regex = parse_rule(r"re:^Doctor Who/.*S0[1-9].*$")
        self.assertEqual(regex.kind, "regex")
        self.assertTrue(regex.matches("Doctor Who/S01/x.mkv"))

    def test_blank_and_comment_lines_are_ignored(self):
        self.assertIsNone(parse_rule(""))
        self.assertIsNone(parse_rule("   # comment"))

    def test_invalid_regex_raises(self):
        with self.assertRaises(ValueError):
            parse_rule("re:[")

    def test_exact_rule_matches_children(self):
        rule = parse_rule("Babylon 5")
        self.assertTrue(rule.matches("Babylon 5"))
        self.assertTrue(rule.matches("Babylon 5/Season 1"))
        self.assertFalse(rule.matches("Babylon 5 and 6"))


class PolicyPrecedenceTests(unittest.TestCase):
    def test_blacklist_wins_when_specificity_is_equal(self):
        policy = PolicySet([parse_rule("Show")], [parse_rule("Show")])
        match = policy.evaluate("Show")
        self.assertEqual(match.kind, "blacklist")

    def test_more_specific_whitelist_beats_blacklist(self):
        policy = PolicySet(
            [parse_rule("Show/Season 1/Episode 1")],
            [parse_rule("Show")],
        )
        match = policy.evaluate("Show/Season 1/Episode 1")
        self.assertEqual(match.kind, "whitelist")

    def test_more_specific_blacklist_beats_whitelist(self):
        policy = PolicySet(
            [parse_rule("Show")],
            [parse_rule("Show/Season 1")],
        )
        match = policy.evaluate("Show/Season 1/Episode 1")
        self.assertEqual(match.kind, "blacklist")

    def test_regex_specificity_is_used(self):
        policy = PolicySet(
            [parse_rule(r"re:^Show/.*$")],
            [parse_rule("Show")],
        )
        match = policy.evaluate("Show/Season 1/Episode 1")
        self.assertEqual(match.kind, "whitelist")

    def test_default_when_nothing_matches(self):
        policy = PolicySet([parse_rule("A")], [parse_rule("B")])
        self.assertEqual(policy.evaluate("C").kind, "default")


class PolicyFileTests(unittest.TestCase):
    def test_load_add_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "whitelist.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# comment\n\nExisting\n")
            self.assertEqual([rule.raw for rule in load_rules(path)], ["Existing"])
            self.assertTrue(add_pattern(path, "New Show"))
            self.assertFalse(add_pattern(path, "New Show"))
            rules = load_rules(path)
            self.assertEqual([rule.raw for rule in rules], ["Existing", "New Show"])
            self.assertTrue(remove_pattern(path, "Existing"))
            self.assertFalse(remove_pattern(path, "Missing"))
            self.assertEqual([rule.raw for rule in load_rules(path)], ["New Show"])

    def test_missing_file_is_empty(self):
        self.assertEqual(load_rules("/does/not/exist.txt"), [])


if __name__ == "__main__":
    unittest.main()
