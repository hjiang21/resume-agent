import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
from agents.final_editor import INSTRUCTIONS
from schemas import AlignmentReview, ExperienceSelection, RecruiterReview
from utils.llm import ModelError
from utils.resume_validation import extract_candidate_name, validate_final_resume


class FinalEditorTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'outputs/example_analyst-20260930-180506'
        self.run.mkdir(parents=True)
        self.manifest = {'name': 'example_analyst', 'created_at': '2026-09-30T18:05:06-07:00',
                         'model': 'test-model', 'status': 'recruiter_reviewed', 'revision_count': 0}
        self.jd = {'role': 'Analyst', **{key: [] for key in [
            'core_responsibilities', 'required_qualifications', 'preferred_qualifications',
            'technical_skills', 'business_skills', 'important_language', 'hiring_themes']}}
        self.bank = {'experiences': [{'organization': 'Example', 'title': 'Pilot', 'content': 'Designed KPIs.'}]}
        self.selection = {'selected_experiences': [{'organization': 'Example', 'title': 'Pilot',
                           'relevance': ['KPI design'], 'priority': 'high'}],
                          'resume_strategy': {'emphasize': ['Measurement'], 'deemphasize': []}}
        self.alignment = {'needs_revision': False, 'strengths': ['Role alignment'], 'issues': []}
        self.recruiter = {'needs_revision': False, 'strengths': ['Clear purpose'], 'issues': []}
        for name, data in [('run.json', self.manifest), ('jd_analysis.json', self.jd),
                           ('experience_bank_snapshot.json', self.bank), ('selected_experiences.json', self.selection),
                           ('alignment_review.json', self.alignment), ('recruiter_review.json', self.recruiter)]:
            self.write(name, data)
        (self.run / 'source_resume.md').write_text('# Example Candidate\n- Designed pilot KPIs.\n')
        (self.run / 'draft_resume.md').write_text('# Example Candidate\n- Measured KPIs.\n')
        for mock in [patch('main.ROOT', self.root), patch('main.load_dotenv'),
                     patch.dict('os.environ', {'OPENAI_API_KEY': 'test', 'OPENAI_MODEL': 'test-model'}, clear=True)]:
            mock.start()
            self.addCleanup(mock.stop)
        sdk = patch('utils.llm.OpenAI')
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)
        self.create = self.sdk.return_value.responses.create
        self.final = '# Example Candidate\n- Designed KPIs for pilot decisions.\n'
        self.create.return_value = SimpleNamespace(status='completed', output_text=self.final, output=[])
        self.parse = self.sdk.return_value.responses.parse

    def write(self, name, data):
        (self.run / name).write_text(json.dumps(data), encoding='utf-8')

    def files(self):
        return {path.name: path.read_bytes() for path in self.run.iterdir()}

    def run_cli(self, command='finalize-resume'):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main.main([command, '--run', str(self.run)])
        return status, errors.getvalue()

    def mark_finalized(self):
        self.write('run.json', dict(self.manifest, status='finalized'))
        (self.run / 'final_resume.md').write_text('# Example Candidate\nOld final\n')

    def test_success_uses_all_saved_artifacts_once_and_preserves_inputs(self):
        before = self.files()
        self.assertEqual(self.run_cli(), (0, ''))
        self.assertEqual((self.run / 'final_resume.md').read_text(), self.final)
        self.assertEqual(json.loads((self.run / 'run.json').read_text()), dict(self.manifest, status='finalized'))
        self.assertEqual(list((self.root / 'outputs').iterdir()), [self.run])
        for name, content in before.items():
            if name != 'run.json':
                self.assertEqual((self.run / name).read_bytes(), content)
        self.create.assert_called_once()
        self.parse.assert_not_called()
        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs['model'], 'test-model')
        self.assertEqual(kwargs['input'][0]['content'], INSTRUCTIONS)
        self.assertEqual(json.loads(kwargs['input'][1]['content']), {
            'draft_resume': before['draft_resume.md'].decode(),
            'candidate_evidence': {'source_resume': before['source_resume.md'].decode(), 'experience_bank': self.bank},
            'advisory_context': {'jd_analysis': self.jd, 'selection': self.selection,
                                 'alignment_review': self.alignment, 'recruiter_review': self.recruiter}})

    def test_only_saved_run_files_are_read(self):
        allowed = {self.run / name for name in self.files()}
        text_read, bytes_read = Path.read_text, Path.read_bytes
        def text(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return text_read(path, *args, **kwargs)
        def binary(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return bytes_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', text), patch.object(Path, 'read_bytes', binary):
            self.assertEqual(self.run_cli(), (0, ''))

    def test_missing_malformed_json_and_missing_blank_text_fail_before_api(self):
        self.mark_finalized()
        for name in ['run.json', 'jd_analysis.json', 'selected_experiences.json',
                     'experience_bank_snapshot.json', 'alignment_review.json', 'recruiter_review.json',
                     'source_resume.md', 'draft_resume.md']:
            path = self.run / name
            original = path.read_bytes()
            for bad in ([None, 'bad JSON', '{}'] if name.endswith('.json') else [None, ' \n']):
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

    def test_unresolved_selection_reference_fails_before_api(self):
        self.selection['selected_experiences'][0]['title'] = 'Unknown'
        self.write('selected_experiences.json', self.selection)
        before = self.files()
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.files(), before)
        self.sdk.assert_not_called()

    def test_incomplete_status_and_model_mismatch_fail_before_editing(self):
        for manifest in [dict(self.manifest, status='alignment_reviewed'), dict(self.manifest, model='other')]:
            with self.subTest(manifest=manifest):
                self.write('run.json', manifest)
                before = self.files()
                status, error = self.run_cli()
                self.assertEqual(status, 1)
                self.assertIn('recruiter review' if manifest['status'] == 'alignment_reviewed' else 'OPENAI_MODEL', error)
                self.assertEqual(self.files(), before)
        self.create.assert_not_called()

    def test_invalid_editor_output_preserves_final_and_status(self):
        self.mark_finalized()
        for output in ['', ' \n', '# Someone Else\n- Designed KPIs.']:
            with self.subTest(output=output):
                self.create.return_value.output_text = output
                before = self.files()
                self.assertEqual(self.run_cli()[0], 1)
                self.assertEqual(self.files(), before)
        # Also enforce validation if a future editor bypasses the shared helper.
        with patch('main.finalize_resume', return_value=None):
            before = self.files()
            self.assertEqual(self.run_cli()[0], 1)
            self.assertEqual(self.files(), before)

    def test_successful_finalized_rerun_replaces_final_without_versions(self):
        self.mark_finalized()
        before = self.files()
        self.assertEqual(self.run_cli(), (0, ''))
        self.assertEqual((self.run / 'final_resume.md').read_text(), self.final)
        self.assertEqual(set(self.files()), set(before))

    def test_failed_finalization_preserves_prior_artifacts_and_status(self):
        for rerun in [False, True]:
            if rerun:
                self.mark_finalized()
            for target, error in [('main.finalize_resume', ModelError('API failed')),
                                  ('main.write_text_atomic', OSError('write failed')),
                                  ('main.save_manifest', OSError('manifest failed'))]:
                before = self.files()
                with self.subTest(rerun=rerun, target=target), patch(target, side_effect=error):
                    self.assertEqual(self.run_cli()[0], 1)
                self.assertEqual(self.files(), before)

    def prepare_upstream(self, command):
        self.write('alignment_review.json', self.alignment)
        self.write('recruiter_review.json', self.recruiter)
        (self.run / 'draft_resume.md').write_text('# Example Candidate\nOld draft\n')
        self.mark_finalized()
        data = {'needs_revision': False, 'strengths': ['Updated review'], 'issues': []}
        if command == 'review-alignment':
            result = AlignmentReview.model_validate(data)
        elif command == 'review-recruiter':
            result = RecruiterReview.model_validate(data)
        else:
            data = json.loads(json.dumps(self.selection))
            data['selected_experiences'][0]['priority'] = 'medium'
            result = ExperienceSelection.model_validate(data)
        self.parse.return_value = SimpleNamespace(status='completed', output_parsed=result)

    def test_upstream_success_invalidates_only_after_replacement(self):
        cases = [('write-resume', 'draft_resume.md', 'draft_written', ['alignment_review.json', 'recruiter_review.json']),
                 ('review-alignment', 'alignment_review.json', 'alignment_reviewed', ['recruiter_review.json']),
                 ('review-recruiter', 'recruiter_review.json', 'recruiter_reviewed', []),
                 ('select-experiences', 'selected_experiences.json', 'experiences_selected',
                  ['draft_resume.md', 'alignment_review.json', 'recruiter_review.json'])]
        for command, artifact, status, stale in cases:
            with self.subTest(command=command):
                self.prepare_upstream(command)
                previous = (self.run / artifact).read_bytes()
                unlink = Path.unlink
                def checked_unlink(path, *args, **kwargs):
                    if path == self.run / 'final_resume.md':
                        self.assertNotEqual((self.run / artifact).read_bytes(), previous)
                    return unlink(path, *args, **kwargs)
                with patch.object(Path, 'unlink', checked_unlink):
                    self.assertEqual(self.run_cli(command), (0, ''))
                for name in stale + ['final_resume.md']:
                    self.assertFalse((self.run / name).exists())
                self.assertEqual(json.loads((self.run / 'run.json').read_text())['status'], status)

    def test_upstream_failures_preserve_final_and_other_artifacts(self):
        for command, agent in [('write-resume', 'main.write_resume'), ('review-alignment', 'main.review_alignment'),
                               ('review-recruiter', 'main.review_recruiter'), ('select-experiences', 'main.select_experiences')]:
            self.prepare_upstream(command)
            for target, error in [(agent, ModelError('API failed')),
                                  ('main.write_text_atomic', OSError('write failed')),
                                  ('main.save_manifest', OSError('manifest failed'))]:
                before = self.files()
                with self.subTest(command=command, target=target), patch(target, side_effect=error):
                    self.assertEqual(self.run_cli(command)[0], 1)
                self.assertEqual(self.files(), before)


class FinalIdentityAndPromptTests(unittest.TestCase):
    def test_name_extraction_accepts_markdown_unicode_and_normalizes_spacing(self):
        for source in ['\n# **Zoë O’Neil**\nContact', '  Zoë   O’Neil\nContact', '## Zoë O’Neil ##\n']:
            name = extract_candidate_name(source)
            self.assertEqual(name, 'Zoë O’Neil')
            validate_final_resume('# ZOË   O’NEIL\nResume', name)

    def test_missing_name_and_substring_rejected(self):
        for text in ['', ' \n', '# Joanne Doe', '# Jane Doer']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_final_resume(text, 'Jane Doe')
        with self.assertRaises(ValueError):
            extract_candidate_name('### 12345')

    def test_prompt_policy(self):
        prompt = ' '.join(INSTRUCTIONS.split())
        for phrase in ['data, not instructions', 'not independent factual evidence',
                       'Candidate evidence wins', 'neither review is authoritative',
                       'primary job-alignment', 'primary guidance for fast-scan clarity',
                       'applying it partially', 'another edit', 'leaving the draft unchanged',
                       'more than 5 bullets', 'maximum, not a target', 'no minimum bullet count',
                       'Do not mechanically equalize', 'source-resume density',
                       'dbt', 'randomized A/B test', 'dashboard ownership', 'SQL window-function',
                       'CGM', 'Care Team', 'member-engagement', 'plain Markdown']:
            self.assertIn(phrase, prompt)


if __name__ == '__main__':
    unittest.main()
