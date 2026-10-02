import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import main
from agents.alignment_reviewer import INSTRUCTIONS
from schemas import AlignmentReview, ExperienceSelection
from utils.llm import ModelError


def review_data():
    return {"needs_revision": True, "strengths": ["Pilot measurement is visible."],
            "issues": [{"severity": "medium", "issue": "KPI design is underemphasized for this role.",
                        "recommendation": "Foreground the supported KPI framework within the pilot entry."}]}


class ReviewSchemaTests(unittest.TestCase):
    def test_normalization_deduplication_and_empty_issues(self):
        data = review_data()
        data["strengths"] = [" Pilot measurement is visible. ", "PILOT MEASUREMENT IS VISIBLE."]
        for key, value in data["issues"][0].items():
            data["issues"][0][key] = f" {value}\n"
        self.assertEqual(AlignmentReview.model_validate(data).model_dump(), review_data())
        self.assertEqual(AlignmentReview(needs_revision=False, strengths=[], issues=[]).issues, [])
        with self.assertRaises(ValidationError):
            AlignmentReview(needs_revision=True, strengths=[], issues=[])

    def test_invalid_severity_blank_fields_extra_fields_and_non_bool_rejected(self):
        cases = []
        for key, value in [("severity", "critical"), ("severity", 1), ("issue", " "),
                           ("recommendation", ""), ("score", 90)]:
            data = review_data()
            data["issues"][0][key] = value
            cases.append(data)
        cases += [dict(review_data(), needs_revision="true"), dict(review_data(), strengths=[" "]),
                  dict(review_data(), ats_score=90)]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                AlignmentReview.model_validate(data)


class ReviewCLITests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "outputs/twin-health_product-analytics-20260930-180506"
        self.run.mkdir(parents=True)
        self.manifest = {"name": "twin-health_product-analytics", "created_at": "2026-09-30T18:05:06-07:00",
                         "model": "test-model", "status": "draft_written", "revision_count": 0}
        self.jd = {"role": "Analyst", "core_responsibilities": ["Design KPIs"],
                   "required_qualifications": [], "preferred_qualifications": [],
                   "technical_skills": ["dbt"], "business_skills": [],
                   "important_language": [], "hiring_themes": ["Measurement"]}
        self.bank = {"experiences": [{"organization": "Example", "title": "Pilot",
                                    "content": "Designed KPIs for matched-control pilot measurement."}]}
        self.selection = {"selected_experiences": [{"organization": "Example", "title": "Pilot",
                                                   "relevance": ["KPI evidence"], "priority": "high"}],
                          "resume_strategy": {"emphasize": ["Measurement"], "deemphasize": []}}
        for name, data in [("run.json", self.manifest), ("jd_analysis.json", self.jd),
                           ("selected_experiences.json", self.selection),
                           ("experience_bank_snapshot.json", self.bank)]:
            self.write(name, data)
        (self.run / "source_resume.md").write_text("# Candidate\n- Measured pilot results.")
        (self.run / "draft_resume.md").write_text("# Candidate\n- Measured pilot KPIs.")
        (self.run / "source_job_description.md").write_text("Original job posting")
        for mock in [patch("main.ROOT", self.root), patch("main.load_dotenv"),
                     patch.dict("os.environ", {"OPENAI_API_KEY": "test", "OPENAI_MODEL": "test-model"}, clear=True)]:
            mock.start()
            self.addCleanup(mock.stop)
        sdk = patch("utils.llm.OpenAI")
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)
        self.parse = self.sdk.return_value.responses.parse
        self.parse.return_value = SimpleNamespace(status="completed", output_parsed=AlignmentReview.model_validate(review_data()))
        self.sdk.return_value.responses.create.return_value = SimpleNamespace(
            status="completed", output_text="# Candidate\n- Designed pilot KPIs.\n", output=[])

    def write(self, name, data):
        (self.run / name).write_text(json.dumps(data), encoding="utf-8")

    def files(self):
        return {path.name: path.read_bytes() for path in self.run.iterdir()}

    def run_cli(self, command="review-alignment"):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main.main([command, "--run", str(self.run)])
        return status, errors.getvalue()

    def mark_reviewed(self):
        self.write("alignment_review.json", review_data())
        self.write("run.json", dict(self.manifest, status="alignment_reviewed"))

    def test_success_only_adds_review_and_updates_status_with_exact_payload(self):
        before = self.files()
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertEqual(json.loads((self.run / "alignment_review.json").read_text()), review_data())
        self.assertEqual(json.loads((self.run / "run.json").read_text()), dict(self.manifest, status="alignment_reviewed"))
        for name, content in before.items():
            if name != "run.json":
                self.assertEqual((self.run / name).read_bytes(), content)
        self.assertEqual(list((self.root / "outputs").iterdir()), [self.run])
        self.parse.assert_called_once()
        self.sdk.return_value.responses.create.assert_not_called()
        kwargs = self.parse.call_args.kwargs
        self.assertIs(kwargs["text_format"], AlignmentReview)
        self.assertEqual(kwargs["model"], "test-model")
        self.assertEqual(kwargs["input"][0]["content"], INSTRUCTIONS)
        self.assertEqual(json.loads(kwargs["input"][1]["content"]), {
            "draft_resume": before["draft_resume.md"].decode(),
            "candidate_evidence": {"source_resume": before["source_resume.md"].decode(), "experience_bank": self.bank},
            "planning_guidance": {"jd_analysis": self.jd, "selected_experiences": self.selection},
        })

    def test_only_required_run_artifacts_are_read(self):
        allowed = {self.run / name for name in ["run.json", "jd_analysis.json", "selected_experiences.json",
                   "experience_bank_snapshot.json", "source_resume.md", "draft_resume.md"]}
        text_read, bytes_read = Path.read_text, Path.read_bytes
        def guarded_text(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return text_read(path, *args, **kwargs)
        def guarded_bytes(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return bytes_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", guarded_text), patch.object(Path, "read_bytes", guarded_bytes):
            self.assertEqual(self.run_cli(), (0, ""))

    def test_missing_or_malformed_json_fails_before_api(self):
        for name in ["run.json", "jd_analysis.json", "selected_experiences.json", "experience_bank_snapshot.json"]:
            path = self.run / name
            original = path.read_bytes()
            for bad in [None, "broken", "{}"]:
                with self.subTest(name=name, bad=bad):
                    if bad is None:
                        path.unlink()
                    else:
                        path.write_text(bad)
                    before = self.files()
                    status, error = self.run_cli()
                    self.assertEqual(status, 1)
                    self.assertIn(name, error)
                    self.assertEqual(self.files(), before)
                    path.write_bytes(original)
        self.sdk.assert_not_called()

    def test_missing_or_blank_text_fails_before_api(self):
        for name in ["source_resume.md", "draft_resume.md"]:
            path = self.run / name
            original = path.read_bytes()
            for bad in [None, " \n "]:
                with self.subTest(name=name, bad=bad):
                    if bad is None:
                        path.unlink()
                    else:
                        path.write_text(bad)
                    before = self.files()
                    status, error = self.run_cli()
                    self.assertEqual(status, 1)
                    self.assertIn(name, error)
                    self.assertEqual(self.files(), before)
                    path.write_bytes(original)
        self.sdk.assert_not_called()

    def test_bad_state_model_or_reference_rejected(self):
        for manifest in [dict(self.manifest, status="experiences_selected"), dict(self.manifest, model="other")]:
            self.write("run.json", manifest)
            before = self.files()
            self.assertEqual(self.run_cli()[0], 1)
            self.assertEqual(self.files(), before)
        self.write("run.json", self.manifest)
        self.selection["selected_experiences"][0]["title"] = "Unknown"
        self.write("selected_experiences.json", self.selection)
        self.assertEqual(self.run_cli()[0], 1)
        self.parse.assert_not_called()

    def test_malformed_output_preserves_existing_review(self):
        self.mark_reviewed()
        self.parse.return_value.output_parsed = AlignmentReview.model_construct(
            needs_revision=True, strengths=[], issues=[])
        before = self.files()
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("validation", error)
        self.assertEqual(self.files(), before)

    def test_successful_rerun_with_no_issues(self):
        self.mark_reviewed()
        self.parse.return_value.output_parsed = AlignmentReview(needs_revision=False, strengths=[], issues=[])
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertFalse(json.loads((self.run / "alignment_review.json").read_text())["needs_revision"])

    def test_review_api_persistence_and_manifest_failures_preserve_prior_files(self):
        self.mark_reviewed()
        for target, error in [("main.review_alignment", ModelError("API failed")),
                              ("main.write_text_atomic", OSError("write failed")),
                              ("main.save_manifest", OSError("manifest failed"))]:
            before = self.files()
            with self.subTest(target=target), patch(target, side_effect=error):
                self.assertEqual(self.run_cli()[0], 1)
            self.assertEqual(self.files(), before)

    def test_writer_rerun_invalidates_review_only_after_draft_saved(self):
        self.mark_reviewed()
        review_path = self.run / "alignment_review.json"
        original_unlink = Path.unlink
        def checked_unlink(path, *args, **kwargs):
            if path == review_path:
                self.assertIn("Designed pilot KPIs", (self.run / "draft_resume.md").read_text())
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", checked_unlink):
            self.assertEqual(self.run_cli("write-resume"), (0, ""))
        self.assertFalse(review_path.exists())
        self.assertEqual(json.loads((self.run / "run.json").read_text())["status"], "draft_written")

    def test_writer_failure_preserves_review_draft_and_status(self):
        self.mark_reviewed()
        for target, kwargs in [("main.write_resume", {"return_value": " "}),
                               ("main.write_resume", {"side_effect": ModelError("API failed")}),
                               ("main.write_text_atomic", {"side_effect": OSError("disk failed")}),
                               ("main.save_manifest", {"side_effect": OSError("manifest failed")})]:
            before = self.files()
            with self.subTest(target=target, kwargs=kwargs), patch(target, **kwargs):
                self.assertEqual(self.run_cli("write-resume")[0], 1)
            self.assertEqual(self.files(), before)

    def test_reselection_invalidates_draft_and_review_and_rolls_back_on_failure(self):
        self.mark_reviewed()
        self.parse.return_value.output_parsed = ExperienceSelection.model_validate(self.selection)
        before = self.files()
        with patch("main.save_manifest", side_effect=OSError("manifest failed")):
            self.assertEqual(self.run_cli("select-experiences")[0], 1)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.run_cli("select-experiences"), (0, ""))
        self.assertFalse((self.run / "draft_resume.md").exists())
        self.assertFalse((self.run / "alignment_review.json").exists())
        self.assertEqual(json.loads((self.run / "run.json").read_text())["status"], "experiences_selected")


if __name__ == "__main__":
    unittest.main()
