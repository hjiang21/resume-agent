import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import main
from agents.experience_selector import INSTRUCTIONS
from schemas import ExperienceBank, ExperienceSelection
from utils.llm import ModelError


def bank_data():
    return {"experiences": [{
        "organization": "Example Corp", "title": "Retail pilot",
        "content": "Designed a KPI framework and matched controls for a retail pilot.",
    }]}


def selection_data():
    return {
        "selected_experiences": [{
            "organization": "Example Corp", "title": "Retail pilot",
            "relevance": ["Designed a KPI framework supporting the job's measurement responsibilities."],
            "priority": "high",
        }],
        "resume_strategy": {"emphasize": ["Experiment measurement"], "deemphasize": []},
    }


class SelectionSchemaTests(unittest.TestCase):
    def test_prompt_balances_recency_with_distinct_older_evidence(self):
        prompt = " ".join(INSTRUCTIONS.split())
        for guidance in ["consider both relevance and recency",
                         "older experience may deserve meaningful consideration",
                         "Do not use recency mechanically",
                         "overall evidence portfolio",
                         "Use only chronology supported by the bank",
                         "Do not use numeric recency scores or date-weight formulas"]:
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, prompt)

    def test_strings_trimmed_lists_deduplicated_and_empty_deemphasize_allowed(self):
        data = selection_data()
        item = data["selected_experiences"][0]
        for field in ["organization", "title", "priority"]:
            item[field] = f"  {item[field]}\n"
        item["relevance"] = ["  Pilot measurement ", "PILOT MEASUREMENT"]
        data["resume_strategy"]["emphasize"] = ["  Measurement ", "MEASUREMENT"]
        result = ExperienceSelection.model_validate(data)
        self.assertEqual(result.selected_experiences[0].organization, "Example Corp")
        self.assertEqual(result.selected_experiences[0].title, "Retail pilot")
        self.assertEqual(result.selected_experiences[0].priority, "high")
        self.assertEqual(result.selected_experiences[0].relevance, ["Pilot measurement"])
        self.assertEqual(result.resume_strategy.emphasize, ["Measurement"])
        self.assertEqual(result.resume_strategy.deemphasize, [])

    def test_invalid_shapes_priorities_and_blank_strings_rejected(self):
        cases = []
        for field, value in [("organization", " "), ("title", ""), ("relevance", []),
                             ("relevance", [" "]), ("priority", "urgent"),
                             ("priority", 1), ("title", 42)]:
            data = selection_data()
            data["selected_experiences"][0][field] = value
            cases.append(data)
        for field, value in [("emphasize", []), ("emphasize", [""]),
                             ("deemphasize", [" "]), ("deemphasize", "none")]:
            data = selection_data()
            data["resume_strategy"][field] = value
            cases.append(data)
        cases.append(dict(selection_data(), selected_experiences=[]))
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                ExperienceSelection.model_validate(data)

    def test_extra_fields_rejected_at_every_level(self):
        for level in ["root", "experience", "strategy"]:
            data = selection_data()
            target = data if level == "root" else (
                data["selected_experiences"][0] if level == "experience" else data["resume_strategy"]
            )
            target["score"] = 99
            with self.subTest(level=level), self.assertRaises(ValidationError):
                ExperienceSelection.model_validate(data)

    def test_duplicate_references_rejected_even_with_conflicting_priorities(self):
        data = selection_data()
        duplicate = deepcopy(data["selected_experiences"][0])
        duplicate.update(title="RETAIL PILOT", priority="low")
        data["selected_experiences"].append(duplicate)
        with self.assertRaises(ValidationError):
            ExperienceSelection.model_validate(data)

    def test_exact_case_insensitive_reference_matching(self):
        bank = ExperienceBank.model_validate(bank_data())
        data = selection_data()
        data["selected_experiences"][0].update(organization=" example corp ", title="RETAIL PILOT")
        ExperienceSelection.model_validate(data).validate_references(bank)
        for field, value in [("title", "Retail pilots"), ("organization", "Example Corporation")]:
            invalid = deepcopy(data)
            invalid["selected_experiences"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "not found"):
                ExperienceSelection.model_validate(invalid).validate_references(bank)


class SelectionCLITests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "outputs/twin-health_product-analytics-20260930-180506"
        self.run.mkdir(parents=True)
        self.manifest = {
            "name": "twin-health_product-analytics", "created_at": "2026-09-30T18:05:06-07:00",
            "model": "test-model", "status": "jd_analyzed", "revision_count": 0,
        }
        self.jd = {"role": "Product Analyst", "core_responsibilities": ["Design KPI frameworks"],
                   "required_qualifications": [], "preferred_qualifications": [],
                   "technical_skills": [], "business_skills": [],
                   "important_language": ["measurement"], "hiring_themes": ["Experiment measurement"]}
        self.write("run.json", self.manifest)
        self.write("jd_analysis.json", self.jd)
        self.write("experience_bank_snapshot.json", bank_data())
        (self.run / "source_resume.md").write_text("Original resume", encoding="utf-8")
        (self.run / "source_job_description.md").write_text("Original JD", encoding="utf-8")
        for target, value in [("main.ROOT", self.root), ("main.load_dotenv", None)]:
            mock = patch(target, value) if value is not None else patch(target)
            mock.start()
            self.addCleanup(mock.stop)
        env = patch.dict("os.environ", {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        sdk = patch("utils.llm.OpenAI")
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)
        self.parse = self.sdk.return_value.responses.parse
        self.parse.return_value = SimpleNamespace(
            status="completed", output_parsed=ExperienceSelection.model_validate(selection_data()),
        )

    def write(self, filename, data):
        (self.run / filename).write_text(json.dumps(data), encoding="utf-8")

    def run_cli(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error:
            status = main.main(["select-experiences", "--run", str(self.run)])
        return status, error.getvalue()

    def files(self):
        return {path.name: path.read_bytes() for path in self.run.iterdir()}

    def test_valid_run_writes_selection_and_preserves_metadata_and_sources(self):
        before = self.files()
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertEqual(json.loads((self.run / "selected_experiences.json").read_text()), selection_data())
        expected = dict(self.manifest, status="experiences_selected")
        self.assertEqual(json.loads((self.run / "run.json").read_text()), expected)
        for name, content in before.items():
            if name != "run.json":
                self.assertEqual((self.run / name).read_bytes(), content)
        self.assertEqual(list((self.root / "outputs").iterdir()), [self.run])
        self.parse.assert_called_once()
        kwargs = self.parse.call_args.kwargs
        self.assertEqual(kwargs["model"], "test-model")
        self.assertIs(kwargs["text_format"], ExperienceSelection)
        self.assertEqual(kwargs["input"][0]["content"], INSTRUCTIONS)
        self.assertEqual(json.loads(kwargs["input"][1]["content"]), {
            "jd_analysis": self.jd, "experience_bank": bank_data(),
        })

    def test_reads_only_required_run_artifacts_not_global_or_resume_files(self):
        allowed = {self.run / filename for filename in (
            "run.json", "jd_analysis.json", "experience_bank_snapshot.json",
        )}
        original = Path.read_text
        def guarded_read(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", guarded_read), patch("main.read_text") as text_reader:
            self.assertEqual(self.run_cli(), (0, ""))
            text_reader.assert_not_called()

    def test_missing_and_malformed_required_artifacts_fail_before_api(self):
        original = self.files()
        for name in ["run.json", "jd_analysis.json", "experience_bank_snapshot.json"]:
            for malformed in [None, "broken JSON", "{}"]:
                with self.subTest(name=name, malformed=malformed):
                    path = self.run / name
                    if malformed is None:
                        path.unlink()
                    else:
                        path.write_text(malformed)
                    before = self.files()
                    status, error = self.run_cli()
                    self.assertEqual(status, 1)
                    self.assertIn(name, error)
                    self.assertEqual(self.files(), before)
                    path.write_bytes(original[name])
        self.sdk.assert_not_called()

    def test_invalid_manifest_metadata_or_incomplete_stage_rejected(self):
        for field, value in [("created_at", "2026-09-30T18:00:00"), ("revision_count", -1),
                             ("revision_count", True), ("model", ""), ("status", "unknown"),
                             ("status", "initializing"), ("status", "initialized")]:
            with self.subTest(field=field, value=value):
                self.write("run.json", dict(self.manifest, **{field: value}))
                self.assertEqual(self.run_cli()[0], 1)
        self.sdk.assert_not_called()

    def test_nonexistent_reference_preserves_existing_selection_and_status(self):
        self.write("selected_experiences.json", selection_data())
        invalid = selection_data()
        invalid["selected_experiences"][0]["title"] = "Retail pilots"
        self.parse.return_value.output_parsed = ExperienceSelection.model_validate(invalid)
        before = self.files()
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("not found in run's bank snapshot", error)
        self.assertEqual(self.files(), before)

    def test_case_insensitive_references_succeed(self):
        data = selection_data()
        data["selected_experiences"][0].update(organization="example corp", title="RETAIL PILOT")
        self.parse.return_value.output_parsed = ExperienceSelection.model_validate(data)
        self.assertEqual(self.run_cli(), (0, ""))

    def test_malformed_model_response_preserves_previous_artifacts_on_rerun(self):
        self.write("selected_experiences.json", selection_data())
        self.write("run.json", dict(self.manifest, status="experiences_selected"))
        self.parse.return_value.output_parsed = ExperienceSelection.model_construct(**dict(
            selection_data(), selected_experiences=[],
        ))
        before = self.files()
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("validation", error)
        self.assertEqual(self.files(), before)

    def test_rerun_successfully_replaces_selection(self):
        self.write("selected_experiences.json", selection_data())
        self.write("run.json", dict(self.manifest, status="experiences_selected"))
        updated = selection_data()
        updated["selected_experiences"][0]["priority"] = "medium"
        self.parse.return_value.output_parsed = ExperienceSelection.model_validate(updated)
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertEqual(json.loads((self.run / "selected_experiences.json").read_text()), updated)

    def test_reselection_from_draft_written_invalidates_only_after_persistence(self):
        self.write("selected_experiences.json", selection_data())
        self.write("run.json", dict(self.manifest, status="draft_written"))
        draft = self.run / "draft_resume.md"
        draft.write_text("Existing draft")
        updated = selection_data()
        updated["selected_experiences"][0]["priority"] = "medium"
        self.parse.return_value.output_parsed = ExperienceSelection.model_validate(updated)
        original_unlink = Path.unlink
        def checked_unlink(path, *args, **kwargs):
            if path == draft:
                self.assertEqual(json.loads((self.run / "selected_experiences.json").read_text()), updated)
                self.assertEqual(path.read_text(), "Existing draft")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", checked_unlink):
            self.assertEqual(self.run_cli(), (0, ""))
        self.assertFalse(draft.exists())
        self.assertEqual(json.loads((self.run / "run.json").read_text())["status"], "experiences_selected")

    def test_failed_reselection_preserves_selection_draft_and_status(self):
        self.write("selected_experiences.json", selection_data())
        self.write("run.json", dict(self.manifest, status="draft_written"))
        (self.run / "draft_resume.md").write_text("Existing draft")
        invalid = ExperienceSelection.model_construct(**dict(selection_data(), selected_experiences=[]))
        for failure in ["api", "validation", "persistence", "invalidation", "manifest"]:
            with self.subTest(failure=failure):
                before = self.files()
                if failure == "api":
                    mocked = patch("main.select_experiences", side_effect=ModelError("API failed"))
                elif failure == "validation":
                    mocked = patch("main.select_experiences", return_value=invalid)
                elif failure == "persistence":
                    mocked = patch("main.write_text_atomic", side_effect=OSError("write failed"))
                elif failure == "manifest":
                    mocked = patch("main.save_manifest", side_effect=OSError("manifest failed"))
                else:
                    original_unlink = Path.unlink
                    def fail_draft_unlink(path, *args, **kwargs):
                        if path == self.run / "draft_resume.md":
                            raise PermissionError("Cannot remove draft")
                        return original_unlink(path, *args, **kwargs)
                    mocked = patch.object(Path, "unlink", fail_draft_unlink)
                with mocked:
                    self.assertEqual(self.run_cli()[0], 1)
                self.assertEqual(self.files(), before)

    def test_model_mismatch_fails_without_changing_run(self):
        self.write("run.json", dict(self.manifest, model="different-model"))
        before = self.files()
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("OPENAI_MODEL must match", error)
        self.assertEqual(self.files(), before)
        self.parse.assert_not_called()

    def test_api_and_output_write_failures_preserve_run(self):
        self.write("selected_experiences.json", selection_data())
        for target, failure in [("main.select_experiences", ModelError("API failed")),
                                ("utils.file_io.os.replace", OSError("disk failure"))]:
            with self.subTest(target=target):
                before = self.files()
                with patch(target, side_effect=failure):
                    self.assertEqual(self.run_cli()[0], 1)
                self.assertEqual(self.files(), before)

    def test_status_updated_only_after_selection_has_been_saved(self):
        def fail_manifest(*args):
            saved = json.loads((self.run / "selected_experiences.json").read_text())
            self.assertEqual(saved, selection_data())
            raise OSError("manifest write failed")
        original_manifest = (self.run / "run.json").read_bytes()
        with patch("main.save_manifest", side_effect=fail_manifest):
            self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual((self.run / "run.json").read_bytes(), original_manifest)


if __name__ == "__main__":
    unittest.main()
