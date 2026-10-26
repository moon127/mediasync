import os
import tempfile
import unittest

from mediasync.classifier import classify_title, episode_key, logical_key
from mediasync.models import Action
from mediasync.rsync_backend import RawChange


def write(path: str, content: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)


class EpisodeKeyTests(unittest.TestCase):
    def test_common_episode_formats(self):
        self.assertEqual(episode_key("Show.S01E02.1080p.mkv"), "S01E02")
        self.assertEqual(episode_key("Show 1x03.mkv"), "S01E03")
        self.assertEqual(episode_key("Show Season 4 Episode 5.mkv"), "S04E05")
        self.assertIsNone(episode_key("Movie (2020).mkv"))

    def test_logical_key_ignores_quality_tags(self):
        first = logical_key("movies", "Movie", "Some.Movie.2020.1080p.BluRay.mkv")
        second = logical_key("movies", "Movie", "Some Movie 2020 720p WEB-DL.mkv")
        self.assertEqual(first, second)


class ClassifyTests(unittest.TestCase):
    def test_media_update_is_reencode_and_sidecar_update_is_plain(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            write(os.path.join(source, "Show", "Episode.mkv"), b"new media")
            write(os.path.join(target, "Show", "Episode.mkv"), b"old")
            write(os.path.join(source, "Show", "Episode.nfo"), b"new nfo")
            write(os.path.join(target, "Show", "Episode.nfo"), b"old nfo")
            result = classify_title(
                "tv",
                "Show",
                [
                    RawChange("update", "Episode.mkv", size=9),
                    RawChange("update", "Episode.nfo", size=7),
                ],
                os.path.join(source, "Show"),
                os.path.join(target, "Show"),
            )
            by_path = {change.path: change for change in result.changes}
            self.assertEqual(
                by_path["Show/Episode.mkv"].action, Action.REENCODE.value
            )
            self.assertEqual(
                by_path["Show/Episode.nfo"].action, Action.UPDATE.value
            )

    def test_added_and_deleted_media_merge_into_reencode(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            new_file = "Show.S01E02.1080p.mkv"
            old_file = "Show.S01E02.720p.mkv"
            write(os.path.join(source, "Show", new_file), b"new" * 10)
            write(os.path.join(target, "Show", old_file), b"old" * 20)
            result = classify_title(
                "tv",
                "Show",
                [
                    RawChange("add", new_file, size=30),
                    RawChange("delete", old_file, size=60),
                ],
                os.path.join(source, "Show"),
                os.path.join(target, "Show"),
            )
            self.assertEqual(len(result.changes), 1)
            change = result.changes[0]
            self.assertEqual(change.action, Action.REENCODE.value)
            self.assertEqual(change.path, f"Show/{new_file}")
            self.assertEqual(change.old_path, f"Show/{old_file}")
            self.assertEqual(change.logical_key, "tv:S01E02")

    def test_junk_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            write(os.path.join(source, "Show", ".DS_Store"))
            write(os.path.join(source, "Show", "._Episode.mkv"))
            write(os.path.join(source, "Show", "Episode.mkv.part"))
            result = classify_title(
                "tv",
                "Show",
                [
                    RawChange("add", ".DS_Store"),
                    RawChange("add", "._Episode.mkv"),
                    RawChange("add", "Episode.mkv.part"),
                ],
                os.path.join(source, "Show"),
                os.path.join(target, "Show"),
            )
            self.assertEqual(result.changes, [])


if __name__ == "__main__":
    unittest.main()
