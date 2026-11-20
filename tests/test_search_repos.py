"""Behavioral checks for the installed collector, without GitHub/network access."""

import contextlib
import copy
from datetime import date, timedelta
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scout", SKILL / "scripts/search_repos.py")
scout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scout)
DEFAULTS = json.loads((SKILL / "config.json").read_text(encoding="utf-8"))["defaults"]
TODAY = date(2026, 11, 20)


def repo(number=1, **changes):
    item = dict(id=number, full_name=f"owner/project-{number}",
                html_url=f"https://github.com/owner/project-{number}",
                stargazers_count=1000, language="Python", topics=["automation"],
                license={"key": "mit", "spdx_id": "MIT"}, private=False,
                archived=False, fork=False, disabled=False,
                created_at="2026-01-01T00:00:00Z", pushed_at="2026-11-20T00:00:00Z",
                updated_at="2026-11-20T00:00:00Z")
    item.update(changes)
    return item


def response(items, total=None, incomplete=False):
    return dict(items=items, total_count=len(items) if total is None else total,
                incomplete_results=incomplete)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.settings = copy.deepcopy(DEFAULTS)

    def test_filters_keep_inclusive_boundaries_and_reject_outside(self):
        self.settings.update(max_stars=2000, active_days=30, language="python", license="MIT")
        boundary = (TODAY - timedelta(days=30)).isoformat() + "T00:00:00Z"
        self.assertTrue(scout.eligible(repo(pushed_at=boundary), self.settings, TODAY))
        self.assertTrue(scout.eligible(repo(stargazers_count=2000), self.settings, TODAY))
        for changes in [dict(stargazers_count=999), dict(stargazers_count=2001),
                        dict(pushed_at="2026-10-20T00:00:00Z"), dict(language="Go"),
                        dict(license=None), dict(archived=True), dict(fork=True),
                        dict(private=True), dict(disabled=True), dict(pushed_at=None)]:
            with self.subTest(changes=changes):
                self.assertFalse(scout.eligible(repo(**changes), self.settings, TODAY))

    def test_zero_disables_activity_filter_and_topics_are_conjunctive(self):
        self.settings.update(active_days=0, topics=["automation", "cli"])
        self.assertFalse(scout.eligible(repo(), self.settings, TODAY))
        self.assertTrue(scout.eligible(repo(topics=["automation", "CLI"], pushed_at=None), self.settings, TODAY))

    def test_new_project_creation_boundary(self):
        self.settings.update(created_days=365)
        self.assertFalse(scout.eligible(repo(created_at="2025-11-19T00:00:00Z"), self.settings, TODAY))
        self.assertTrue(scout.eligible(repo(created_at="2025-11-20T00:00:00Z"), self.settings, TODAY))

    def test_query_union_deduplicates_and_preserves_evidence(self):
        with patch.object(scout, "fetch", side_effect=[response([repo(1), repo(2)]), response([repo(1), repo(3)])]):
            result = scout.collect(self.settings, ["a", "b"], TODAY, None, set())
        self.assertEqual(len(result["repositories"]), 3)
        self.assertEqual(result["repositories"][0]["matched_queries"], ["a", "b"])

    def test_seen_removes_only_known_repository(self):
        with patch.object(scout, "fetch", return_value=response([repo(1), repo(2)])):
            result = scout.collect(self.settings, ["a"], TODAY, None, {"owner/project-1"})
        self.assertEqual([r["id"] for r in result["repositories"]], [2])

    def test_pagination_keeps_fixed_page_size_and_budget(self):
        self.settings["per_query"] = 150
        with patch.object(scout, "fetch", side_effect=[response([repo(i) for i in range(100)], 250),
                                                       response([repo(i) for i in range(100, 200)], 250)]) as fetch:
            result = scout.collect(self.settings, ["a"], TODAY, None, set())
        self.assertEqual(len(result["repositories"]), 150)
        self.assertEqual([c.args[1:3] for c in fetch.call_args_list], [(1, 100), (2, 100)])
        self.assertTrue(result["queries"][0]["truncated"])
        self.assertTrue(result["warnings"])

    def test_rate_limit_preserves_results_and_stops_remaining_requests(self):
        with patch.object(scout, "fetch", side_effect=[response([repo()]), scout.ApiError("GitHub HTTP 429", stop=True)]) as fetch:
            result = scout.collect(self.settings, ["a", "b", "c"], TODAY, None, set())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["repositories"]), 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertIsNotNone(result["queries"][2]["error"])

    def test_incomplete_and_empty_results_have_different_status(self):
        for incomplete, status in [(False, "ok"), (True, "partial")]:
            with patch.object(scout, "fetch", return_value=response([], incomplete=incomplete)):
                result = scout.collect(self.settings, ["a"], TODAY, None, set())
            self.assertEqual(result["status"], status)

    def test_cli_dry_run_preset_override_and_custom_queries(self):
        output = io.StringIO()
        with patch.object(scout, "existing_token", side_effect=AssertionError("dry-run accessed auth")), contextlib.redirect_stdout(output):
            exit_code = scout.main(["--dry-run", "--preset", "emerging", "--min-stars", "300", "--query", "topic:rag", "--as-of", "2026-11-20"])
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["settings"]["min_stars"], 300)
        self.assertEqual(result["settings"]["max_stars"], 5000)
        self.assertEqual(len(result["queries"]), 1)
        self.assertIn("created:>=2025-11-20", result["queries"][0])

    def test_invalid_star_range_fails_before_network(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            scout.main(["--min-stars", "1000", "--max-stars", "999", "--dry-run"])
        self.assertEqual(error.exception.code, 2)

    def test_saved_json_can_be_reused_for_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.json"
            with patch.object(scout, "existing_token", return_value=None), patch.object(scout, "fetch", return_value=response([repo()])), patch.object(scout, "fetch_readme", return_value={"text": "Example repository", "url": None, "sha": "test"}), contextlib.redirect_stdout(io.StringIO()):
                exit_code = scout.main(["--output", str(output), "--as-of", "2026-11-20"])
            self.assertEqual(exit_code, 0)
            self.assertEqual(scout.seen_names(output), {"owner/project-1"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
