from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import dataclass

from .util import ensure_dir

LOG = logging.getLogger(__name__)


@dataclass
class Rule:
    raw: str
    kind: str
    pattern: str
    regex: re.Pattern[str] | None = None

    @property
    def specificity(self) -> int:
        if self.kind == "regex":
            literal = re.sub(r"[^A-Za-z0-9 _./-]", "", self.raw[3:])
            return len(literal) + self.raw.count("/") * 100
        return len(self.raw) + self.raw.count("/") * 100

    def matches(self, path: str) -> bool:
        path = path.strip("/")
        if self.kind == "regex":
            assert self.regex is not None
            return bool(self.regex.search(path))
        if self.kind == "glob":
            return fnmatch.fnmatchcase(path, self.pattern)
        if "/" in self.pattern:
            return path == self.pattern or path.startswith(self.pattern + "/")
        return path == self.pattern or path.startswith(self.pattern + "/")


@dataclass
class PolicyMatch:
    kind: str = "default"
    pattern: str | None = None
    specificity: int = 0


class PolicySet:
    def __init__(self, whitelist: list[Rule], blacklist: list[Rule]):
        self.whitelist = whitelist
        self.blacklist = blacklist

    @classmethod
    def load(
        cls,
        whitelist_path: str | None,
        blacklist_path: str | None,
    ) -> PolicySet:
        whitelist: list[Rule] = []
        blacklist: list[Rule] = []
        if whitelist_path:
            whitelist = load_rules(whitelist_path)
        if blacklist_path:
            blacklist = load_rules(blacklist_path)
        return cls(whitelist, blacklist)

    def evaluate(self, path: str) -> PolicyMatch:
        white = best_match(self.whitelist, path)
        black = best_match(self.blacklist, path)
        if black and white:
            if black.specificity >= white.specificity:
                return PolicyMatch("blacklist", black.raw, black.specificity)
            return PolicyMatch("whitelist", white.raw, white.specificity)
        if black:
            return PolicyMatch("blacklist", black.raw, black.specificity)
        if white:
            return PolicyMatch("whitelist", white.raw, white.specificity)
        return PolicyMatch()

    def all_rules(self) -> list[tuple[str, Rule]]:
        return [("whitelist", rule) for rule in self.whitelist] + [
            ("blacklist", rule) for rule in self.blacklist
        ]


def best_match(rules: list[Rule], path: str) -> Rule | None:
    found = [rule for rule in rules if rule.matches(path)]
    if not found:
        return None
    return max(found, key=lambda rule: (rule.specificity, rule.raw))


def parse_rule(line: str) -> Rule | None:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if raw.startswith("re:"):
        expression = raw[3:]
        try:
            regex = re.compile(expression)
        except re.error as exc:
            raise ValueError(f"invalid regex rule {raw!r}: {exc}") from exc
        return Rule(raw=raw, kind="regex", pattern=expression, regex=regex)
    if any(ch in raw for ch in "*?["):
        return Rule(raw=raw, kind="glob", pattern=raw)
    return Rule(raw=raw, kind="exact", pattern=raw.strip("/"))


def load_rules(path: str) -> list[Rule]:
    if not os.path.exists(path):
        LOG.warning("policy file does not exist: %s", path)
        return []
    rules: list[Rule] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            try:
                rule = parse_rule(line)
            except ValueError as exc:
                LOG.warning("%s:%s: %s", path, lineno, exc)
                continue
            if rule:
                rules.append(rule)
    return rules


def validate_pattern(pattern: str) -> Rule:
    rule = parse_rule(pattern)
    if rule is None:
        raise ValueError("pattern is empty or a comment")
    return rule


def add_pattern(path: str, pattern: str) -> bool:
    rule = validate_pattern(pattern)
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    existing = load_rules(path)
    if any(rule.raw == item.raw for item in existing):
        return False
    with open(path, "a", encoding="utf-8") as fh:
        existing_text = ""
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "r", encoding="utf-8") as read_fh:
                existing_text = read_fh.read()
            if not existing_text.endswith("\n"):
                fh.write("\n")
        fh.write(rule.raw + "\n")
    return True


def remove_pattern(path: str, pattern: str) -> bool:
    if not os.path.exists(path):
        return False
    target = pattern.strip()
    removed = False
    lines: list[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip() == target:
                removed = True
                continue
            lines.append(line)
    if removed:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    return removed
