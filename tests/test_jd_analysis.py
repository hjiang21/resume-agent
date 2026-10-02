import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import main
from agents.jd_analyst import INSTRUCTIONS
from schemas import JDAnalysis
from utils.llm import ModelError
from utils.runs import normalize_run_name


def analysis_data():
    return {
        "role": "Product Analyst",
        "core_responsibilities": ["Analyze experiments with product teams"],
        "required_qualifications": ["SQL experience"],
        "preferred_qualifications": ["Python experience"],
        "technical_skills": ["SQL", "Python"],
        "business_skills": ["Product decision support"],
        "important_language": ["experimentation"],
        "hiring_themes": ["Cross-functional product decision support"],
    }


class JDAnalysisSchemaTests(unittest.TestCase):
    def test_whitespace_normalized_in_role_and_every_list(self):
        data = analysis_data()
        padded = {key: f"  {value}\n" if isinstance(value, str)
                  else [f"\t{item}  " for item in value]
                  for key, value in data.items()}
        self.assertEqual(JDAnalysis.model_validate(padded).model_dump(), data)

    def test_empty_categories_allowed_but_fields_required(self):
        data = {key: "Product Analyst" if key == "role" else []
                for key in analysis_data()}
        self.assertEqual(JDAnalysis.model_validate(data).model_dump(), data)
        del data["hiring_themes"]
        with self.assertRaises(ValidationError):
            JDAnalysis.model_validate(data)

    def test_extra_fields_rejected(self):
        with self.assertRaises(ValidationError):
            JDAnalysis.model_validate(dict(analysis_data(), candidate_fit="strong"))

    def test_blank_and_non_string_values_rejected(self):
        for key in analysis_data():
            for invalid in ["", " \t\n", 42, None]:
                data = analysis_data()
                data[key] = invalid if key == "role" else [invalid]
                with self.subTest(field=key, value=invalid), self.assertRaises(ValidationError):
                    JDAnalysis.model_validate(data)
        with self.assertRaises(ValidationError):
            JDAnalysis.model_validate(dict(analysis_data(), technical_skills="SQL"))

    def test_duplicates_removed_within_each_list_preserving_first_spelling(self):
        data = analysis_data()
        for key, value in data.items():
            if isinstance(value, list):
                data[key] = value + [f" {value[0].upper()} "]
        self.assertEqual(JDAnalysis.model_validate(data).model_dump(), analysis_data())


class JDAnalysisCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "inputs").mkdir()
        (self.root / "data").mkdir()
        self.jd = self.root / "inputs/job_description.md"
        self.jd_text = (
            "# Product Analyst\nAnalyze experiments with product teams.\n"
            "Required: SQL experience. Preferred: Python experience.\n"
        )
        self.jd.write_text(self.jd_text, encoding="utf-8")
        self.resume = self.root / "inputs/resume.md"
        self.resume.write_bytes(b"\xef\xbb\xbf# Candidate\r\nFull resume\r\n")
        self.bank = self.root / "data/experience_bank.json"
        self.bank_data = {"experiences": [{
            "organization": "Example", "title": "Retail pilot", "content": "Built a pilot."
        }]}
        self.bank.write_text(json.dumps(self.bank_data), encoding="utf-8")
        self.run_name = "twin-health_product-analytics"
        self.run_path = self.root / "outputs/twin-health_product-analytics-20260930-171500"
        self.output = self.run_path / "jd_analysis.json"
        root_patch = patch("main.ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = patch("utils.runs.datetime")
        clock.start().now.return_value.astimezone.return_value = datetime(
            2026, 9, 30, 17, 15, tzinfo=timezone(timedelta(hours=-7)),
        )
        self.addCleanup(clock.stop)

        # Isolate configuration and the SDK itself: no live network or private .env reads.
        env = patch.dict("os.environ", {
            "OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        dotenv = patch("main.load_dotenv")
        dotenv.start()
        self.addCleanup(dotenv.stop)
        sdk = patch("utils.llm.OpenAI")
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)
        self.parse = self.sdk.return_value.responses.parse
        self.parse.return_value = SimpleNamespace(
            status="completed", output_parsed=JDAnalysis.model_validate(analysis_data()),
        )

    def run_cli(self, args=None):
        if args is None:
            args = ["analyze-jd", "--name", self.run_name, "--job-description", str(self.jd),
                    "--resume", str(self.resume), "--bank", str(self.bank)]
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main.main(args)
        self.stdout = output.getvalue()
        return status, errors.getvalue()

    def test_valid_jd_saved_with_only_job_context_sent_to_model(self):
        with patch("main.read_background") as background, patch("main.extract_experiences") as extractor:
            self.assertEqual(self.run_cli(), (0, ""))
            background.assert_not_called()
            extractor.assert_not_called()
        saved = self.output.read_text(encoding="utf-8")
        self.assertEqual(json.loads(saved), analysis_data())
        self.assertIn('\n  "role":', saved)
        self.assertTrue(saved.endswith("\n"))
        self.parse.assert_called_once_with(
            model="test-model",
            input=[{"role": "system", "content": INSTRUCTIONS},
                   {"role": "user", "content": json.dumps({"job_description": self.jd_text})}],
            text_format=JDAnalysis,
        )
        self.sdk.return_value.close.assert_called_once()

    def test_blank_jd_fails_before_api(self):
        self.jd.write_text(" \n\t ", encoding="utf-8")
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("Job description is empty", error)
        self.sdk.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_jd_fails_before_api(self):
        self.jd.unlink()
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("Input file not found", error)
        self.sdk.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_unreadable_jd_fails_before_api(self):
        original_read = Path.read_bytes
        def read_bytes(path):
            if path == self.jd:
                raise PermissionError(f"Permission denied: {self.jd}")
            return original_read(path)
        with patch.object(Path, "read_bytes", read_bytes):
            status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("Permission denied", error)
        self.sdk.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_malformed_model_output_keeps_snapshots_but_not_success_status(self):
        self.parse.return_value.output_parsed = JDAnalysis.model_construct(
            **dict(analysis_data(), role=" "),
        )
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("validation", error)
        self.assertIn("JDAnalysis", error)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.manifest()["status"], "initialized")
        self.assertTrue((self.run_path / "source_job_description.md").exists())

    def test_persistence_revalidates_even_if_model_helper_is_bypassed(self):
        invalid = JDAnalysis.model_construct(**dict(analysis_data(), technical_skills=[""]))
        with patch("main.analyze_jd", return_value=invalid):
            status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("JD analysis validation failed", error)
        self.assertFalse(self.output.exists())

    def test_api_failure_preserves_existing_output(self):
        old_output = self.root / "outputs/jd_analysis.json"
        old_output.parent.mkdir()
        old_output.write_text(json.dumps(analysis_data()), encoding="utf-8")
        previous = old_output.read_bytes()
        with patch("main.analyze_jd", side_effect=ModelError("OpenAI API failed")):
            status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("OpenAI API failed", error)
        self.assertEqual(old_output.read_bytes(), previous)
        self.assertEqual(self.manifest()["status"], "initialized")
        self.sdk.return_value.close.assert_called_once()

    def test_command_defaults_are_repository_relative(self):
        with patch("main.analyze_job_description", return_value=0) as workflow:
            self.assertEqual(self.run_cli(["analyze-jd", "--name", self.run_name]), (0, ""))
        workflow.assert_called_once_with(
            self.run_name, main.ROOT / "inputs/resume.md",
            main.ROOT / "inputs/job_description.md", main.ROOT / "data/experience_bank.json",
        )
        self.sdk.assert_not_called()

    def test_text_file_supported(self):
        text_path = self.jd.with_suffix(".txt")
        self.jd.rename(text_path)
        self.jd = text_path
        self.assertEqual(self.run_cli(), (0, ""))

    def manifest(self):
        return json.loads((self.run_path / "run.json").read_text(encoding="utf-8"))

    def test_default_command_creates_complete_timestamped_run(self):
        self.assertEqual(self.run_cli(["analyze-jd", "--name", self.run_name]), (0, ""))
        self.assertEqual({path.name for path in self.run_path.iterdir()}, {
            "run.json", "source_resume.md", "source_job_description.md",
            "experience_bank_snapshot.json", "jd_analysis.json",
        })
        self.assertFalse((self.root / "outputs/jd_analysis.json").exists())
        self.assertIn(str(self.run_path), self.stdout)
        self.assertEqual(self.manifest(), {
            "name": self.run_name, "created_at": "2026-09-30T17:15:00-07:00",
            "model": "test-model", "status": "jd_analyzed", "revision_count": 0,
        })

    def test_snapshots_preserve_exact_source_bytes_and_validated_bank(self):
        # Include BOM/CRLF and surrounding whitespace to verify lossless text snapshots.
        self.jd.write_bytes(b"\xef\xbb\xbf  # Product Analyst\r\nSQL experience\r\n")
        self.bank_data["experiences"][0]["content"] = "  Built a pilot.  "
        self.bank.write_text(json.dumps(self.bank_data), encoding="utf-8")
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertEqual((self.run_path / "source_resume.md").read_bytes(), self.resume.read_bytes())
        self.assertEqual((self.run_path / "source_job_description.md").read_bytes(), self.jd.read_bytes())
        bank_snapshot = json.loads((self.run_path / "experience_bank_snapshot.json").read_text())
        self.assertEqual(bank_snapshot["experiences"][0]["content"], "Built a pilot.")

    def test_sources_are_not_reread_after_initialization(self):
        from utils.runs import initialize_run
        def initialize_then_change_globals(*args):
            result = initialize_run(*args)
            self.resume.unlink()
            self.bank.unlink()
            self.jd.write_text("Different posting", encoding="utf-8")
            return result
        with patch("main.initialize_run", side_effect=initialize_then_change_globals):
            self.assertEqual(self.run_cli(), (0, ""))
        payload = json.loads(self.parse.call_args.kwargs["input"][1]["content"])
        self.assertEqual(payload, {"job_description": self.jd_text})

    def test_safe_slug_and_invalid_names(self):
        self.assertEqual(normalize_run_name("  Twin Health _ Product...Analytics-- "), self.run_name)
        self.assertEqual(normalize_run_name("../../Notion_Product Data Scientist"), "notion_product-data-scientist")
        self.assertEqual(normalize_run_name("twin-health_product_analytics_engineer"),
                         "twin-health_product_analytics_engineer")
        for name in ["", "  ", "../__---", "notion", "_analyst", "notion_", "notion_!!!"]:
            with self.subTest(name=name):
                self.run_name = name
                status, error = self.run_cli()
                self.assertEqual(status, 1)
                self.assertIn("Run name", error)
        self.sdk.assert_not_called()
        self.assertFalse((self.root / "outputs").exists())

    def test_normalized_name_used_for_folder_and_manifest(self):
        self.run_name = "  Twin Health _ Product...Analytics-- "
        self.assertEqual(self.run_cli(), (0, ""))
        self.assertEqual(self.manifest()["name"], "twin-health_product-analytics")

    def test_missing_or_blank_resume_creates_no_run(self):
        for content in [None, " \n "]:
            with self.subTest(content=content):
                if content is None:
                    self.resume.unlink()
                else:
                    self.resume.write_text(content)
                status, error = self.run_cli()
                self.assertEqual(status, 1)
                self.assertIn("resume", error.lower())
                self.assertFalse((self.root / "outputs").exists())
        self.sdk.assert_not_called()

    def test_missing_or_invalid_bank_suggests_ingest_and_creates_no_run(self):
        for content in [None, "broken JSON", '{"experiences": []}']:
            with self.subTest(content=content), patch("main.extract_experiences") as extract:
                if content is None:
                    self.bank.unlink()
                else:
                    self.bank.write_text(content)
                status, error = self.run_cli()
                self.assertEqual(status, 1)
                self.assertIn("python main.py ingest", error)
                self.assertFalse((self.root / "outputs").exists())
                extract.assert_not_called()
        self.sdk.assert_not_called()

    def test_same_second_collision_preserves_existing_run(self):
        self.assertEqual(self.run_cli(), (0, ""))
        previous = {path.name: path.read_bytes() for path in self.run_path.iterdir()}
        status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("already exists", error)
        self.assertEqual({path.name: path.read_bytes() for path in self.run_path.iterdir()}, previous)
        self.assertEqual(self.parse.call_count, 1)

    def test_snapshot_write_failure_never_marks_initialized_or_successful(self):
        with patch("utils.runs.write_bytes_atomic", side_effect=OSError("disk failure")):
            status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertIn("disk failure", error)
        self.assertEqual(self.manifest()["status"], "initializing")
        self.parse.assert_not_called()

    def test_analysis_write_failure_never_marks_successful(self):
        with patch("main.write_text_atomic", side_effect=OSError("disk failure")):
            status, error = self.run_cli()
        self.assertEqual(status, 1)
        self.assertEqual(self.manifest()["status"], "initialized")
        self.assertFalse(self.output.exists())

    def test_name_required_and_old_output_flag_removed(self):
        for args in [["analyze-jd"], ["analyze-jd", "--name", "role", "--output", "old.json"]]:
            with self.subTest(args=args), self.assertRaises(SystemExit) as exc:
                self.run_cli(args)
            self.assertEqual(exc.exception.code, 2)
        self.sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
