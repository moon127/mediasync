import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from mediasync.state import State


class StaleRunTests(unittest.TestCase):
    def test_clear_stale_runs_cancels_old_runs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            with State(tmp) as state:
                state.start_run("stale-run", "TV", "apply", None)
                state.start_run("active-run", "TV", "apply", None)
                old = (
                    datetime.now(UTC) - timedelta(hours=2)
                ).isoformat(timespec="seconds")
                state.conn.execute(
                    "UPDATE runs SET started=? WHERE id=?", (old, "stale-run")
                )
                state.conn.commit()

                cleared = state.clear_stale_runs(max_age_minutes=60)
                self.assertEqual(cleared, 1)

                stale = state.get_run_progress("stale-run")
                active = state.get_run_progress("active-run")
                self.assertEqual(stale["status"], "cancelled")
                self.assertIsNotNone(stale["finished"])
                self.assertEqual(active["status"], "running")

    def test_active_run_progress_filters_by_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            with State(tmp) as state:
                state.start_run("tv-run", "TV", "apply", None)
                state.start_run("movie-run", "Movies", "apply", None)

                tv = state.active_run_progress("TV")
                movies = state.active_run_progress("Movies")
                any_run = state.active_run_progress()

                self.assertEqual(tv["id"], "tv-run")
                self.assertEqual(movies["id"], "movie-run")
                self.assertIsNotNone(any_run)


class TrashListingTests(unittest.TestCase):
    def test_list_trash_filters_by_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            with State(tmp) as state:
                state.add_trash("TV", "run", "Show/a.mkv", "trash/a.mkv", 4, "file", "test")
                state.add_trash("Movies", "run", "Film/b.mkv", "trash/b.mkv", 5, "file", "test")

                all_entries = state.list_trash()
                tv_entries = state.list_trash("TV")
                movie_entries = state.list_trash("Movies")

                self.assertEqual(len(all_entries), 2)
                self.assertEqual([e["library"] for e in tv_entries], ["TV"])
                self.assertEqual([e["library"] for e in movie_entries], ["Movies"])

    def test_list_trash_excludes_completed_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with State(tmp) as state:
                trash_id = state.add_trash(
                    "TV", "run", "Show/a.mkv", "trash/a.mkv", 4, "file", "test"
                )
                state.mark_trash_restored(trash_id)

                self.assertEqual(state.list_trash("TV"), [])
                all_entries = state.list_trash("TV", include_done=True)
                self.assertEqual(len(all_entries), 1)
                self.assertIsNotNone(all_entries[0]["restored_at"])


    def test_update_run_progress_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with State(tmp) as state:
                state.update_run_progress(
                    "apply-1",
                    library="TV",
                    plan_id="plan-1",
                    files_total=10,
                    files_done=3,
                    bytes_total=1024,
                    bytes_done=256,
                    current="Show/ep.mkv",
                    status="running",
                    mode="apply",
                )
                run = state.get_run_progress("apply-1")
                self.assertEqual(run["library"], "TV")
                self.assertEqual(run["plan_id"], "plan-1")
                self.assertEqual(run["files_total"], 10)
                self.assertEqual(run["files_done"], 3)
                self.assertEqual(run["bytes_total"], 1024)
                self.assertEqual(run["bytes_done"], 256)
                self.assertEqual(run["current"], "Show/ep.mkv")
                self.assertEqual(run["status"], "running")
                state.update_run_progress(
                    "apply-1",
                    files_done=10,
                    bytes_done=1024,
                    current="done",
                )
                run = state.get_run_progress("apply-1")
                self.assertEqual(run["files_done"], 10)
                self.assertEqual(run["bytes_done"], 1024)




if __name__ == "__main__":
    unittest.main()
