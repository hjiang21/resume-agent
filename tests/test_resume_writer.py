import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError, APIStatusError

import main
from agents.resume_writer import INSTRUCTIONS


class WriterPromptTests(unittest.TestCase):
    def test_five_bullet_cap_applies_to_individual_entries_not_sections(self):
        prompt = " ".join(INSTRUCTIONS.split())
        for guidance in ["Do not use more than 5 bullets for any single experience or project entry",
                         "individual employment roles and leadership entries",
                         "The cap is a maximum, not a target",
                         "Use fewer when the experience can be represented effectively with less",
                         "each individual entry, not an entire resume section"]:
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, prompt)

    def test_section_balance_considers_diminishing_returns_without_equalizing(self):
        prompt = " ".join(INSTRUCTIONS.split())
        for guidance in ["SECTION BALANCE", "additional bullets provide diminishing value",
                         "compare its incremental value", "distinct technical, domain, product, or analytical evidence",
                         "Do not enforce equal bullet counts", "Do not add filler to make sections look balanced"]:
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, prompt)

    def test_prompt_has_no_minimum_bullet_quota(self):
        prompt = " ".join(INSTRUCTIONS.split()).lower()
        self.assertIn("there is no minimum bullet count", prompt)
        self.assertNotRegex(prompt, r"(?:at least|minimum(?: of)?)\s+(?:\d+|one|two|three|four|five)\s+bullets?")

    def test_density_is_qualitative_and_balanced_with_distinct_older_evidence(self):
        prompt = " ".join(INSTRUCTIONS.split())
        for guidance in ["COMPOSITION AND DENSITY",
                         "source resume's overall content density as the primary V1 proxy",
                         "roughly comparable total content density",
                         "approximate total bullet count", "relative section depth",
                         "older experience may still deserve inclusion",
                         "Do not use recency mechanically",
                         "If the draft is materially sparser",
                         "Do not add filler merely to match length",
                         "qualitative capacity signal, not a page-fit guarantee",
                         "Do not attempt exact page rendering"]:
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, prompt)


class ResumeWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "outputs/twin-health_product-analytics-20260930-180506"
        self.run.mkdir(parents=True)
        self.manifest = {
            "name": "twin-health_product-analytics", "created_at": "2026-09-30T18:05:06-07:00",
            "model": "test-model", "status": "experiences_selected", "revision_count": 0,
        }
        self.resume = "# Example Candidate\nexample@example.com\n\n## EXPERIENCE\nExample Corp | Analyst | 2025–Present\n- Measured pilot performance.\n"
        self.bank = {"experiences": [{"organization": "Example Corp", "title": "Retail pilot",
                                    "content": "Designed a KPI framework and matched controls for a pilot."}]}
        self.selection = {
            "selected_experiences": [{"organization": "Example Corp", "title": "Retail pilot",
                                      "relevance": ["Supports experiment measurement"], "priority": "high"}],
            "resume_strategy": {"emphasize": ["Measurement"], "deemphasize": []},
        }
        self.jd = {"role": "Product Analyst", "core_responsibilities": ["Analyze experiments"],
                   "required_qualifications": [], "preferred_qualifications": [],
                   "technical_skills": ["dbt"], "business_skills": [],
                   "important_language": [], "hiring_themes": ["Measurement"]}
        for name, data in [("run.json", self.manifest), ("jd_analysis.json", self.jd),
                           ("selected_experiences.json", self.selection),
                           ("experience_bank_snapshot.json", self.bank)]:
            (self.run / name).write_text(json.dumps(data), encoding="utf-8")
        (self.run / "source_resume.md").write_text(self.resume, encoding="utf-8")
        (self.run / "source_job_description.md").write_text("Unused original posting", encoding="utf-8")
        self.draft = self.resume.replace("Measured pilot performance.", "Designed KPI frameworks to measure pilot performance.")
        for mock in [patch("main.ROOT", self.root), patch("main.load_dotenv"),
                     patch.dict("os.environ", {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model"}, clear=True)]:
            mock.start()
            self.addCleanup(mock.stop)
        sdk = patch("utils.llm.OpenAI")
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)
        self.create = self.sdk.return_value.responses.create
        self.create.return_value = SimpleNamespace(status="completed", output_text=self.draft, output=[])

    def run_cli(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error:
            status = main.main(["write-resume", "--run", str(self.run)])
        return status, error.getvalue()

    def files(self):
        return {path.name: path.read_bytes() for path in self.run.iterdir()}

    def test_success_writes_plain_draft_in_existing_run_and_updates_status(self):
        before = self.files()
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertEqual((self.run / "draft_resume.md").read_text(), self.draft)
        self.assertEqual(json.loads((self.run / "run.json").read_text()),
                         dict(self.manifest, status="draft_written"))
        for name, content in before.items():
            if name != "run.json":
                self.assertEqual((self.run / name).read_bytes(), content)
        self.assertEqual(list((self.root / "outputs").iterdir()), [self.run])
        self.create.assert_called_once()
        self.sdk.return_value.responses.parse.assert_not_called()
        self.sdk.return_value.close.assert_called_once()
        kwargs = self.create.call_args.kwargs
        self.assertEqual(set(kwargs), {"model", "input"})
        self.assertEqual(kwargs["model"], "test-model")
        self.assertEqual(kwargs["input"][0], {"role": "system", "content": INSTRUCTIONS})
        payload = json.loads(kwargs["input"][1]["content"])
        self.assertEqual(payload, {
            "candidate_evidence": {"source_resume": self.resume, "experience_bank": self.bank},
            "planning_guidance": {"jd_analysis": self.jd, "selected_experiences": self.selection},
        })

    def test_only_reads_required_run_artifacts(self):
        allowed = {self.run / name for name in ["run.json", "source_resume.md", "jd_analysis.json",
                                                "selected_experiences.json", "experience_bank_snapshot.json"]}
        original_text, original_bytes = Path.read_text, Path.read_bytes
        def read_text(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return original_text(path, *args, **kwargs)
        def read_bytes(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return original_bytes(path, *args, **kwargs)
        with patch.object(Path, "read_text", read_text), patch.object(Path, "read_bytes", read_bytes):
            self.assertEqual(self.run_cli(), (0, ""))

    def test_missing_and_malformed_json_artifacts_fail_before_api(self):
        for name in ["run.json", "selected_experiences.json", "jd_analysis.json", "experience_bank_snapshot.json"]:
            path = self.run / name
            original = path.read_bytes()
            for invalid in [None, "bad JSON", "{}"]:
                with self.subTest(name=name, invalid=invalid):
                    if invalid is None:
                        path.unlink()
                    else:
                        path.write_text(invalid)
                    before = self.files()
                    status, error = self.run_cli()
                    self.assertEqual(status, 1)
                    self.assertIn(name, error)
                    self.assertEqual(self.files(), before)
                    path.write_bytes(original)
        self.sdk.assert_not_called()

    def test_missing_or_blank_source_resume_fails_before_api(self):
        path = self.run / "source_resume.md"
        for value in [None, " \n\t"]:
            with self.subTest(value=value):
                if value is None:
                    path.unlink()
                else:
                    path.write_text(value)
                before = self.files()
                status, error = self.run_cli()
                self.assertEqual(status, 1)
                self.assertIn("resume", error.lower())
                self.assertEqual(self.files(), before)
        self.sdk.assert_not_called()

    def test_unknown_selection_reference_fails_before_generation(self):
        self.selection["selected_experiences"][0]["title"] = "Nonexistent work"
        (self.run / "selected_experiences.json").write_text(json.dumps(self.selection))
        before = self.files()
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("not found", error)
        self.assertEqual(self.files(), before)
        self.sdk.assert_not_called()

    def test_incomplete_selection_status_rejected(self):
        for state in ["initialized", "jd_analyzed"]:
            with self.subTest(state=state):
                (self.run / "run.json").write_text(json.dumps(dict(self.manifest, status=state)))
                before = self.files()
                self.assertEqual(self.run_cli()[0], 1)
                self.assertEqual(self.files(), before)
        self.sdk.assert_not_called()

    def test_model_mismatch_fails_before_generation(self):
        (self.run / "run.json").write_text(json.dumps(dict(self.manifest, model="another-model")))
        before = self.files()
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("OPENAI_MODEL must match", error)
        self.assertEqual(self.files(), before)
        self.create.assert_not_called()

    def test_blank_incomplete_and_refused_responses_preserve_existing_draft(self):
        (self.run / "draft_resume.md").write_text("Previous valid draft")
        (self.run / "run.json").write_text(json.dumps(dict(self.manifest, status="draft_written")))
        refusal = SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal")])
        for response in [SimpleNamespace(status="completed", output_text="", output=[]),
                         SimpleNamespace(status="completed", output_text=" \n\t", output=[]),
                         SimpleNamespace(status="incomplete", output_text="Partial draft", output=[]),
                         SimpleNamespace(status="failed", output_text="Partial draft", output=[]),
                         SimpleNamespace(status="completed", output_text="Some text", output=[refusal])]:
            with self.subTest(response=response):
                self.create.return_value = response
                before = self.files()
                self.assertEqual(self.run_cli()[0], 1)
                self.assertEqual(self.files(), before)

    def test_persistence_checks_blank_output_even_if_helper_bypassed(self):
        before = self.files()
        with patch("main.write_resume", return_value=" \n"):
            status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("blank", error)
        self.assertEqual(self.files(), before)

    def test_api_failures_preserve_existing_draft_and_status(self):
        (self.run / "draft_resume.md").write_text("Previous valid draft")
        request = httpx.Request("POST", "https://example.com")
        for failure in [APIConnectionError(request=request), APIStatusError(
                "failed", response=httpx.Response(429, request=request), body=None)]:
            with self.subTest(failure=type(failure).__name__):
                self.create.side_effect = failure
                before = self.files()
                status, error = self.run_cli()
                self.assertEqual(status, 1)
                self.assertIn("OpenAI", error)
                self.assertEqual(self.files(), before)

    def test_atomic_write_failure_preserves_previous_draft_and_status(self):
        (self.run / "draft_resume.md").write_text("Previous valid draft")
        before = self.files()
        with patch("utils.file_io.os.replace", side_effect=OSError("disk failure")):
            self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.files(), before)

    def test_rerun_replaces_draft_without_versioning(self):
        (self.run / "run.json").write_text(json.dumps(dict(self.manifest, status="draft_written")))
        (self.run / "draft_resume.md").write_text("Previous valid draft")
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertEqual((self.run / "draft_resume.md").read_text(), self.draft)
        self.assertEqual(len(list(self.run.glob("draft*"))), 1)

    def test_manifest_update_happens_after_draft_persistence(self):
        original = (self.run / "run.json").read_bytes()
        def fail_manifest(*args):
            self.assertEqual((self.run / "draft_resume.md").read_text(), self.draft)
            raise OSError("manifest write failed")
        with patch("main.save_manifest", side_effect=fail_manifest):
            self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual((self.run / "run.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
