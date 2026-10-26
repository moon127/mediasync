import os
import tempfile
import unittest

from mediasync.config import (
    ConfigError,
    config_from_dict,
    config_summary,
    init_config,
    load_config,
)
def base_data(tmp: str) -> dict:
    return {
        "state_dir": os.path.join(tmp, "state"),
        "require_mount": False,
        "libraries": [
            {
                "name": "TV",
                "kind": "tv",
                "source": os.path.join(tmp, "source"),
                "target": os.path.join(tmp, "target"),
            }
        ],
    }


class ConfigParsingTests(unittest.TestCase):
    def test_defaults_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_from_dict(
                base_data(tmp), os.path.join(tmp, "config.toml"), tmp
            )
            self.assertEqual(config.concurrency, 8)
            self.assertEqual(config.rsync_for(config.libraries[0]).modify_window, 2)
            self.assertTrue(config.rsync_for(config.libraries[0]).fsync)
            self.assertIn("add_title", config.apply.auto_approve)
            self.assertNotIn("delete_title", config.apply.auto_approve)
            summary = config_summary(config)
            self.assertEqual(summary["libraries"][0]["name"], "TV")

    def test_library_rsync_inherits_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = base_data(tmp)
            data["rsync"] = {
                "modify_window": 3,
                "fsync": False,
                "extra_args": ["--bwlimit=10m"],
                "excludes": ["custom"],
            }
            data["libraries"][0]["rsync"] = {"modify_window": 1}
            config = config_from_dict(data, os.path.join(tmp, "config.toml"), tmp)
            options = config.rsync_for(config.libraries[0])
            self.assertEqual(options.modify_window, 1)
            self.assertFalse(options.fsync)
            self.assertIn("--bwlimit=10m", options.extra_args)
            self.assertIn("custom", options.excludes)

    def test_invalid_kind_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = base_data(tmp)
            data["libraries"][0]["kind"] = "documentary"
            with self.assertRaises(ConfigError):
                config_from_dict(data, os.path.join(tmp, "config.toml"), tmp)

    def test_dangerous_rsync_arg_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = base_data(tmp)
            data["rsync"] = {"extra_args": ["--remove-source-files"]}
            with self.assertRaises(ConfigError):
                config_from_dict(data, os.path.join(tmp, "config.toml"), tmp)

    def test_invalid_delete_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = base_data(tmp)
            data["delete_mode"] = "shred"
            with self.assertRaises(ConfigError):
                config_from_dict(data, os.path.join(tmp, "config.toml"), tmp)

    def test_load_config_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            state_dir = os.path.join(tmp, "state")
            source = os.path.join(tmp, "movies-src")
            target = os.path.join(tmp, "movies-dst")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    f"""
state_dir = "{state_dir}"
require_mount = false
scan_mode = "deep"

[rsync]
modify_window = 4
fsync = false

[[libraries]]
name = "Movies"
kind = "movies"
source = "{source}"
target = "{target}"
"""
                )
            config = load_config(path)
            self.assertEqual(config.scan_mode, "deep")
            self.assertEqual(config.libraries[0].kind, "movies")
            self.assertEqual(config.rsync_for(config.libraries[0]).modify_window, 4)
            self.assertFalse(config.rsync_for(config.libraries[0]).fsync)


class InitTests(unittest.TestCase):
    def test_init_creates_config_and_relative_policy_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mediasync.toml")
            config_path, created = init_config(path)
            self.assertEqual(config_path, os.path.abspath(path))
            self.assertTrue(os.path.exists(config_path))
            self.assertTrue(
                os.path.exists(os.path.join(tmp, "policy", "tv-whitelist.txt"))
            )
            self.assertTrue(
                os.path.exists(os.path.join(tmp, "policy", "movies-blacklist.txt"))
            )
            self.assertEqual(len(created), 5)
            with open(config_path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn('whitelist = "policy/tv-whitelist.txt"', text)

    def test_init_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mediasync.toml")
            init_config(path)
            with self.assertRaises(ConfigError):
                init_config(path)
            init_config(path, force=True)


if __name__ == "__main__":
    unittest.main()
