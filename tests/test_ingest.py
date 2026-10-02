import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from openai import APIConnectionError
import httpx

import main
from schemas import ExperienceBank
from utils.file_io import load_experience_bank, read_background, save_experience_bank
from utils.llm import LLM, ModelError


def valid_bank():
    return ExperienceBank(experiences=[{
        "organization": "Example", "title": "Retail pilot", "content": "Built a pilot."
    }])


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resume = self.root / "resume.md"
        self.resume.write_text("# Candidate\nFull resume", encoding="utf-8")
        self.background = self.root / "background"
        self.background.mkdir()
        self.bank = self.root / "experience_bank.json"

    def run_cli(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main.main(["ingest", "--resume", str(self.resume),
                                "--background", str(self.background), "--bank", str(self.bank)])
        return status, errors.getvalue()

    @patch("main.LLM")
    def test_empty_background_ingestion(self, client):
        client.return_value.structured.return_value = valid_bank()
        status, _ = self.run_cli()
        self.assertEqual(status, 0)
        self.assertEqual(load_experience_bank(self.bank), valid_bank())
        payload = client.return_value.structured.call_args.args[1]
        self.assertIn("Full resume", payload)
        self.assertIn('"background_documents": []', payload)
        client.return_value.close.assert_called_once()

    def test_recursive_supported_files_sorted_and_blank_skipped(self):
        (self.background / "nested").mkdir()
        for name, content in [("z.TXT", "notes"), ("nested/a.md", "project"),
                              ("blank.txt", "  "), ("ignore.pdf", "ignored")]:
            (self.background / name).write_text(content)
        self.assertEqual(read_background(self.background),
                         [("nested/a.md", "project"), ("z.TXT", "notes")])
        self.assertEqual(read_background(self.root / "absent"), [])

    @patch("main.LLM")
    def test_missing_empty_and_unsupported_resume_before_api(self, client):
        for content in [None, "", "   "]:
            if content is None:
                self.resume.unlink()
            else:
                self.resume.write_text(content)
            status, error = self.run_cli()
            self.assertEqual(status, 1)
            self.assertIn("Error:", error)
        self.resume = self.root / "resume.pdf"
        self.assertEqual(self.run_cli()[0], 1)
        client.assert_not_called()

    @patch("main.LLM")
    def test_api_failure_preserves_bank(self, client):
        save_experience_bank(self.bank, valid_bank())
        previous = self.bank.read_bytes()
        client.return_value.structured.side_effect = ModelError("API failed")
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.bank.read_bytes(), previous)

    @patch("main.LLM")
    def test_successful_explicit_rebuild_replaces_old_bank(self, client):
        save_experience_bank(self.bank, valid_bank())
        updated = ExperienceBank(experiences=[{
            "organization": "Example", "title": "New project", "content": "New context."
        }])
        client.return_value.structured.return_value = updated
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(load_experience_bank(self.bank), updated)

    @patch("main.LLM")
    def test_invalid_output_preserves_bank(self, client):
        save_experience_bank(self.bank, valid_bank())
        previous = self.bank.read_bytes()
        client.return_value.structured.return_value = ExperienceBank.model_construct(experiences=[])
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.bank.read_bytes(), previous)

    def test_schema_rejects_duplicates_empty_fields_extras_and_empty_bank(self):
        item = valid_bank().model_dump()["experiences"][0]
        invalid = [{"experiences": []}, {"experiences": [dict(item, content=" ")]},
                   {"experiences": [dict(item, id="1")]},
                   {"experiences": [item, dict(item, title="RETAIL PILOT")]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ExperienceBank.model_validate(value)

    def test_atomic_replace_failure_preserves_bank_and_cleans_temp(self):
        save_experience_bank(self.bank, valid_bank())
        previous = self.bank.read_bytes()
        with patch("utils.file_io.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                save_experience_bank(self.bank, valid_bank())
        self.assertEqual(self.bank.read_bytes(), previous)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["background", "experience_bank.json", "resume.md"])

    def test_malformed_bank_is_not_rebuilt_by_loader(self):
        self.bank.write_text("broken")
        with self.assertRaises(ValidationError):
            load_experience_bank(self.bank)
        self.assertEqual(self.bank.read_text(), "broken")


class LLMTests(unittest.TestCase):
    @patch.dict("os.environ", {"OPENAI_API_KEY": "test", "OPENAI_MODEL": "test-model"})
    @patch("utils.llm.OpenAI")
    def test_structured_call_and_failure_modes(self, client):
        llm = LLM()
        parse = client.return_value.responses.parse
        parse.return_value = SimpleNamespace(status="completed", output_parsed=valid_bank())
        self.assertEqual(llm.structured("instructions", "sources", ExperienceBank), valid_bank())
        self.assertEqual(parse.call_args.kwargs["text_format"], ExperienceBank)
        for response in [SimpleNamespace(status="completed", output_parsed=None),
                         SimpleNamespace(status="incomplete", output_parsed=valid_bank())]:
            parse.return_value = response
            with self.assertRaises(ModelError):
                llm.structured("instructions", "sources", ExperienceBank)
        parse.side_effect = APIConnectionError(request=httpx.Request("POST", "https://example.com"))
        with self.assertRaisesRegex(ModelError, "connect"):
            llm.structured("instructions", "sources", ExperienceBank)

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_configuration(self):
        with self.assertRaisesRegex(ModelError, "OPENAI_API_KEY"):
            LLM()


if __name__ == "__main__":
    unittest.main()
