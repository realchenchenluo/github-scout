import contextlib
import copy
from datetime import date
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_search_repos import scout, repo, DEFAULTS

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))
ANIMAL = CONFIG["relevance_profiles"]["animal-modeling"]
UI = CONFIG["relevance_profiles"]["game-ui"]
TODAY = date(2026, 11, 20)


def normalized(number=1, **changes):
    return scout.normalize(repo(number, **changes))


def batch(*repositories):
    return {"status": "ok", "warnings": [], "repositories": list(repositories)}


def readme(text):
    return {"text": text, "url": "https://github.com/example/project/blob/main/README.md", "sha": "fixture"}


class RelevanceTests(unittest.TestCase):
    def setUp(self):
        self.settings = copy.deepcopy(DEFAULTS)

    def test_default_admits_mature_projects_with_old_push(self):
        old = repo(pushed_at="2020-01-01T00:00:00Z")
        self.assertTrue(scout.eligible(old, self.settings, TODAY))
        self.assertFalse(any("pushed:" in q for q in scout.make_queries(self.settings, ["topic:game-ui"], TODAY)))
        self.settings["active_days"] = 365
        self.assertFalse(scout.eligible(old, self.settings, TODAY))

    def test_specialist_category_expands_synonyms_without_single_animal_keyword(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scout.main(["--category", "animal-modeling", "--dry-run"])
        data = json.loads(output.getvalue())
        self.assertEqual(data["settings"]["profile"], "animal-modeling")
        self.assertTrue(any("quadruped reconstruction" in q for q in data["queries"]))
        self.assertTrue(all('animal in:name' not in q for q in data["queries"]))

    def test_health_and_robot_projects_rejected_even_with_3d_examples(self):
        for description in ("Open-source operating system for animal health", "A quadruped robot pet framework for robotics"):
            result = scout.classify_relevance(normalized(description=description), "This includes animal 3D meshes.", ANIMAL)
            self.assertEqual(result["status"], "rejected")

    def test_high_star_book_list_never_enters_shortlist_or_consumes_readme_budget(self):
        book = normalized(full_name="someone/Some-Many-Books", description="个人收藏书籍列表", stargazers_count=90000)
        with patch.object(scout, "fetch_readme") as fetch:
            result = scout.review_candidates(batch(book), self.settings, UI, None, TODAY)
        self.assertEqual(result["shortlist"], [])
        self.assertEqual(len(result["rejected"]), 1)
        fetch.assert_not_called()

    def test_readme_required_even_for_very_relevant_title(self):
        project = normalized(description="3D animal reconstruction")
        result = scout.classify_relevance(project, "Welcome to the project.", ANIMAL)
        self.assertEqual(result["status"], "needs_review")

    def test_random_readme_example_cannot_establish_primary_purpose(self):
        project = normalized(description="A general documentation portal")
        text = "Documentation for many topics. " * 100 + "Example: 3D animal mesh reconstruction."
        self.assertEqual(scout.classify_relevance(project, text, ANIMAL)["status"], "needs_review")

    def test_category_word_is_not_mistaken_for_cat(self):
        project = normalized(description="3D category reconstruction")
        text = "A 3D category mesh reconstruction toolkit."
        self.assertNotEqual(scout.classify_relevance(project, text, ANIMAL)["status"], "direct")

    def test_direct_animal_project_retains_readme_evidence(self):
        result = scout.classify_relevance(normalized(description="3D animal reconstruction"), "Reconstruct 3D animal meshes from images.", ANIMAL)
        self.assertEqual(result["status"], "direct")
        self.assertTrue(result["evidence"])

    def test_general_3d_tool_labeled_adjacent(self):
        result = scout.classify_relevance(normalized(description="Official Blender mirror"), "Blender is a free 3D creation suite for modeling and rigging.", ANIMAL)
        self.assertEqual(result["status"], "adjacent")

    def test_relevance_precedes_stars_and_older_direct_project_survives(self):
        generic = normalized(1, description="A 3D creation suite", stargazers_count=90000)
        specific = normalized(2, description="3D animal reconstruction", stargazers_count=1001, pushed_at="2020-01-01T00:00:00Z")
        texts = {generic["full_name"]: readme("This is a 3D creation suite."), specific["full_name"]: readme("This library reconstructs 3D animal meshes.")}
        self.settings["sort"] = "stars"
        with patch.object(scout, "fetch_readme", side_effect=lambda name, token: texts[name]):
            result = scout.review_candidates(batch(generic, specific), self.settings, ANIMAL, None, TODAY)
        self.assertEqual([r["id"] for r in result["shortlist"]], [2, 1])
        self.assertEqual(result["shortlist"][0]["activity"]["label"], "older-push-review-compatibility")

    def test_readme_budget_prioritizes_scoped_purpose_not_raw_star_order(self):
        broad = normalized(1, description="A general developer collection", stargazers_count=100000)
        scoped = normalized(2, description="Unity UI framework", stargazers_count=1200)
        self.settings["readme_budget"] = 1
        with patch.object(scout, "fetch_readme", return_value=readme("A Unity UI framework for games.")) as fetch:
            result = scout.review_candidates(batch(broad, scoped), self.settings, UI, None, TODAY)
        self.assertEqual(fetch.call_args.args[0], scoped["full_name"])
        self.assertEqual([r["id"] for r in result["shortlist"]], [2])
        self.assertEqual([r["id"] for r in result["review_queue"]], [1])

    def test_no_profile_needs_semantic_review_instead_of_auto_recommendation(self):
        with patch.object(scout, "fetch_readme", return_value=readme("A useful project.")):
            result = scout.review_candidates(batch(normalized()), self.settings, None, None, TODAY)
        self.assertEqual(result["shortlist"], [])
        self.assertEqual(len(result["review_queue"]), 1)

    def test_readme_404_is_unknown_rather_than_irrelevant_or_successfully_reviewed(self):
        with patch.object(scout, "fetch_readme", side_effect=scout.ApiError("GitHub HTTP 404", status=404)):
            result = scout.review_candidates(batch(normalized(description="Unity UI framework")), self.settings, UI, None, TODAY)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["repositories"], [])
        self.assertEqual(len(result["review_queue"]), 1)

    def test_readme_rate_limit_preserves_checked_results_and_stops(self):
        items = [normalized(i, description="Unity UI framework") for i in range(3)]
        with patch.object(scout, "fetch_readme", side_effect=[readme("A Unity UI framework for games."), scout.ApiError("GitHub HTTP 429", stop=True, status=429)]) as fetch:
            result = scout.review_candidates(batch(*items), self.settings, UI, None, TODAY)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["repositories"]), 1)
        self.assertEqual(len(result["review_queue"]), 2)
        self.assertEqual(fetch.call_count, 2)

    def test_code_example_and_badge_do_not_supply_relevance_evidence(self):
        text = "A repository.\n```\n3D animal reconstruction\n```\n![3D animal reconstruction](https://example.com/x)"
        result = scout.classify_relevance(normalized(description="3D animal reconstruction"), text, ANIMAL)
        self.assertEqual(result["status"], "needs_review")

    def test_custom_queries_do_not_disable_selected_relevance_profile(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scout.main(["--category", "game-ui", "--query", "repo:fairygui/FairyGUI-unity", "--dry-run"])
        result = json.loads(output.getvalue())
        self.assertEqual(result["settings"]["profile"], "game-ui")
        self.assertEqual(len(result["queries"]), 1)

    def test_broad_collection_engine_or_desktop_framework_is_not_a_game_ui_tool(self):
        descriptions = ["A categorized collection of awesome opensource unity3d repos", "An open source C# 3D game engine inspired by Unity", "Develop Desktop and Mobile apps with .NET UI"]
        for description in descriptions:
            with self.subTest(description=description):
                result = scout.classify_relevance(normalized(description=description), "Unity UI library for games with layouts and widgets.", UI)
                self.assertEqual(result["status"], "needs_review")

    def test_blender_resources_with_animal_section_is_not_an_animal_modeling_tool(self):
        project = normalized(description="A curated list of Blender add-ons, tutorials and 3D resources")
        result = scout.classify_relevance(project, "3D animal reconstruction and creature modeling. Links to useful resources.", ANIMAL)
        self.assertEqual(result["status"], "needs_review")

    def test_partial_cli_saves_checked_results_and_returns_two(self):
        import tempfile
        from test_search_repos import response
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            with patch.object(scout, "existing_token", return_value=None), patch.object(scout, "fetch", side_effect=scout.ApiError("GitHub HTTP 429", stop=True)), contextlib.redirect_stdout(io.StringIO()):
                code = scout.main(["--category", "game-ui", "--output", str(path)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "partial")


if __name__ == "__main__":
    unittest.main()
