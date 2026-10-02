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
from agents.recruiter_reviewer import INSTRUCTIONS
from schemas import AlignmentReview, ExperienceSelection, RecruiterReview
from utils.llm import ModelError


def review_data():
    return {"needs_revision": True, "strengths": ["The current role is immediately clear."],
            "issues": [{"severity": "medium", "issue": "The pilot bullet buries business purpose.",
                        "recommendation": "Lead with the decision supported while retaining the technical evidence."}]}


class RecruiterSchemaTests(unittest.TestCase):
    def test_normalization_deduplication_and_empty_issue_rule(self):
        data = review_data()
        data["strengths"] += [" THE CURRENT ROLE IS IMMEDIATELY CLEAR. "]
        for key, value in data["issues"][0].items():
            data["issues"][0][key] = f" {value}\n"
        self.assertEqual(RecruiterReview.model_validate(data).model_dump(), review_data())
        self.assertEqual(RecruiterReview(needs_revision=False, strengths=[], issues=[]).issues, [])
        with self.assertRaises(ValidationError):
            RecruiterReview(needs_revision=True, strengths=[], issues=[])

    def test_strict_fields_and_severity(self):
        cases = [dict(review_data(), needs_revision=1), dict(review_data(), score=80),
                 dict(review_data(), strengths=[" "])]
        for key, value in [("severity", "critical"), ("severity", 2), ("issue", " "),
                           ("recommendation", ""), ("score", 80)]:
            data = review_data()
            data["issues"][0][key] = value
            cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                RecruiterReview.model_validate(data)


class RecruiterCLITests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "outputs/example_analyst-20260930-180506"
        self.run.mkdir(parents=True)
        self.manifest = {"name": "example_analyst", "created_at": "2026-09-30T18:05:06-07:00",
                         "model": "test-model", "status": "alignment_reviewed", "revision_count": 0}
        self.jd = {"role": "Analyst", "core_responsibilities": ["Design KPIs"],
                   "required_qualifications": [], "preferred_qualifications": [],
                   "technical_skills": [], "business_skills": [], "important_language": [], "hiring_themes": []}
        self.bank = {"experiences": [{"organization": "Example", "title": "Pilot", "content": "Designed KPIs."}]}
        self.selection = {"selected_experiences": [{"organization": "Example", "title": "Pilot",
                                                   "relevance": ["KPI design"], "priority": "high"}],
                          "resume_strategy": {"emphasize": ["Measurement"], "deemphasize": []}}
        self.alignment = {"needs_revision": False, "strengths": ["Measurement is clear."], "issues": []}
        for name, data in [("run.json", self.manifest), ("jd_analysis.json", self.jd),
                           ("experience_bank_snapshot.json", self.bank),
                           ("selected_experiences.json", self.selection), ("alignment_review.json", self.alignment)]:
            self.write(name, data)
        (self.run / "source_resume.md").write_text("# Candidate\n- Measured pilot results.")
        (self.run / "draft_resume.md").write_text("# Candidate\n- Measured pilot KPIs.")
        for mock in [patch("main.ROOT", self.root), patch("main.load_dotenv"),
                     patch.dict("os.environ", {"OPENAI_API_KEY": "test", "OPENAI_MODEL": "test-model"}, clear=True)]:
            mock.start()
            self.addCleanup(mock.stop)
        sdk = patch("utils.llm.OpenAI")
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)
        self.parse = self.sdk.return_value.responses.parse
        self.parse.return_value = SimpleNamespace(status="completed", output_parsed=RecruiterReview.model_validate(review_data()))
        self.sdk.return_value.responses.create.return_value = SimpleNamespace(status="completed", output_text="New draft", output=[])

    def write(self, name, data):
        (self.run / name).write_text(json.dumps(data), encoding="utf-8")

    def files(self):
        return {path.name: path.read_bytes() for path in self.run.iterdir()}

    def run_cli(self, command="review-recruiter"):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main.main([command, "--run", str(self.run)])
        return status, errors.getvalue()

    def mark_reviewed(self):
        self.write("recruiter_review.json", review_data())
        self.write("run.json", dict(self.manifest, status="recruiter_reviewed"))

    def test_success_payload_persistence_and_status(self):
        before = self.files()
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertEqual(json.loads((self.run / "recruiter_review.json").read_text()), review_data())
        self.assertEqual(json.loads((self.run / "run.json").read_text()), dict(self.manifest, status="recruiter_reviewed"))
        for name, content in before.items():
            if name != "run.json":
                self.assertEqual((self.run / name).read_bytes(), content)
        self.assertEqual(list((self.root / "outputs").iterdir()), [self.run])
        self.parse.assert_called_once()
        kwargs = self.parse.call_args.kwargs
        self.assertIs(kwargs["text_format"], RecruiterReview)
        self.assertEqual(kwargs["input"][0]["content"], INSTRUCTIONS)
        self.assertEqual(json.loads(kwargs["input"][1]["content"]), {
            "draft_resume": before["draft_resume.md"].decode(),
            "candidate_evidence": {"source_resume": before["source_resume.md"].decode(), "experience_bank": self.bank},
            "advisory_context": {"jd_analysis": self.jd, "alignment_review": self.alignment},
        })

    def test_only_required_run_artifacts_are_read(self):
        allowed = {self.run / name for name in ["run.json", "jd_analysis.json", "experience_bank_snapshot.json",
                                                "alignment_review.json", "source_resume.md", "draft_resume.md"]}
        text_read, bytes_read = Path.read_text, Path.read_bytes
        def text(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return text_read(path, *args, **kwargs)
        def binary(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return bytes_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", text), patch.object(Path, "read_bytes", binary):
            self.assertEqual(self.run_cli(), (0, ""))

    def test_missing_malformed_json_and_missing_blank_text(self):
        for name in ["run.json", "alignment_review.json", "jd_analysis.json", "experience_bank_snapshot.json",
                     "source_resume.md", "draft_resume.md"]:
            path = self.run / name
            original = path.read_bytes()
            for bad in ([None, "bad JSON", "{}"] if name.endswith(".json") else [None, " \n"]):
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

    def test_bad_state_and_model_fail_before_generation(self):
        for manifest in [dict(self.manifest, status="draft_written"), dict(self.manifest, model="other")]:
            self.write("run.json", manifest)
            before = self.files()
            self.assertEqual(self.run_cli()[0], 1)
            self.assertEqual(self.files(), before)
        self.parse.assert_not_called()

    def test_malformed_reviewer_output_preserves_existing_review(self):
        self.mark_reviewed()
        self.parse.return_value.output_parsed = RecruiterReview.model_construct(needs_revision=True, strengths=[], issues=[])
        before = self.files()
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.files(), before)

    def test_successful_rerun_replaces_review(self):
        self.mark_reviewed()
        self.parse.return_value.output_parsed = RecruiterReview(needs_revision=False, strengths=[], issues=[])
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertFalse(json.loads((self.run / "recruiter_review.json").read_text())["needs_revision"])

    def test_review_failures_preserve_artifacts_and_status(self):
        self.mark_reviewed()
        for target, error in [("main.review_recruiter", ModelError("API failed")),
                              ("main.write_text_atomic", OSError("write failed")),
                              ("main.save_manifest", OSError("manifest failed"))]:
            before = self.files()
            with self.subTest(target=target), patch(target, side_effect=error):
                self.assertEqual(self.run_cli()[0], 1)
            self.assertEqual(self.files(), before)

    def test_upstream_success_invalidates_only_after_replacement_saved(self):
        for command, artifact, target_status in [("write-resume", "draft_resume.md", "draft_written"),
                                                 ("review-alignment", "alignment_review.json", "alignment_reviewed"),
                                                 ("select-experiences", "selected_experiences.json", "experiences_selected")]:
            with self.subTest(command=command):
                self.write("alignment_review.json", self.alignment)
                self.mark_reviewed()
                if command == "review-alignment":
                    result = AlignmentReview.model_validate(review_data())
                else:
                    data = json.loads(json.dumps(self.selection))
                    data["selected_experiences"][0]["priority"] = "medium"
                    result = ExperienceSelection.model_validate(data)
                self.parse.return_value.output_parsed = result
                previous = (self.run / artifact).read_bytes()
                original_unlink = Path.unlink
                def checked_unlink(path, *args, **kwargs):
                    if path == self.run / "recruiter_review.json":
                        self.assertNotEqual((self.run / artifact).read_bytes(), previous)
                    return original_unlink(path, *args, **kwargs)
                with patch.object(Path, "unlink", checked_unlink):
                    self.assertEqual(self.run_cli(command), (0, ""))
                self.assertFalse((self.run / "recruiter_review.json").exists())
                self.assertEqual(json.loads((self.run / "run.json").read_text())["status"], target_status)
                if command == "write-resume":
                    self.assertFalse((self.run / "alignment_review.json").exists())

    def test_upstream_failures_preserve_both_reviews(self):
        self.mark_reviewed()
        for command, agent in [("write-resume", "main.write_resume"),
                                ("review-alignment", "main.review_alignment"),
                                ("select-experiences", "main.select_experiences")]:
            if command == "review-alignment":
                self.parse.return_value.output_parsed = AlignmentReview.model_validate(self.alignment)
            else:
                self.parse.return_value.output_parsed = ExperienceSelection.model_validate(self.selection)
            for target, error in [(agent, ModelError("API failed")),
                                  ("main.write_text_atomic", OSError("write failed")),
                                  ("main.save_manifest", OSError("manifest failed"))]:
                before = self.files()
                with self.subTest(command=command, target=target), patch(target, side_effect=error):
                    self.assertEqual(self.run_cli(command)[0], 1)
                self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
