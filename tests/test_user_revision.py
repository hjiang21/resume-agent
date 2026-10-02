import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
from agents.user_revision_agent import INSTRUCTIONS
from schemas import AlignmentReview, ExperienceSelection, RecruiterReview
from utils.llm import ModelError
from utils.resume_validation import extract_candidate_name, validate_final_resume


from utils.file_io import create_text_atomic


class UserRevisionTests(unittest.TestCase):
    def setUp(self):
        self.instruction = 'Emphasize business purpose.'
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'outputs/example_analyst-20260930-180506'
        self.run.mkdir(parents=True)
        self.manifest = {'name': 'example_analyst', 'created_at': '2026-09-30T18:05:06-07:00',
                         'model': 'test-model', 'status': 'finalized', 'revision_count': 0}
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
        (self.run / 'final_resume.md').write_text('# Example Candidate\nOriginal final\n')

    def write(self, name, data):
        (self.run / name).write_text(json.dumps(data), encoding='utf-8')

    def files(self):
        return {path.name: path.read_bytes() for path in self.run.iterdir()}

    def run_cli(self, command='revise'):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main.main([command, '--run', str(self.run)] + (['--instruction', self.instruction] if command == 'revise' else []))
        return status, errors.getvalue()

    def mark_finalized(self):
        self.write('run.json', dict(self.manifest, status='finalized'))
        (self.run / 'final_resume.md').write_text('# Example Candidate\nOld final\n')

    def test_first_and_second_revision_use_latest_base_and_preserve_history(self):
        original = self.files()
        for number in [1, 2]:
            base = (self.run / ('final_resume.md' if number == 1 else 'revision_001.md')).read_text()
            self.create.return_value.output_text = f'# Example Candidate\nRevision {number}\n'
            self.assertEqual(self.run_cli(), (0, ''))
            payload = json.loads(self.create.call_args.kwargs['input'][1]['content'])
            self.assertEqual(payload, {
                'current_resume': base, 'user_instruction': self.instruction,
                'candidate_evidence': {'source_resume': original['source_resume.md'].decode(), 'experience_bank': self.bank},
                'planning_context': {'jd_analysis': self.jd, 'selection': self.selection}})
            self.assertEqual(json.loads((self.run / 'run.json').read_text()), dict(self.manifest, status='revised', revision_count=number))
        self.assertEqual(self.create.call_count, 2)
        self.parse.assert_not_called()
        self.assertIn('Revision 1', (self.run / 'revision_001.md').read_text())
        for name, content in original.items():
            if name != 'run.json':
                self.assertEqual((self.run / name).read_bytes(), content)

    def test_blank_instruction_rejected_before_client(self):
        self.instruction = ' \n'
        before = self.files()
        self.assertIn('instruction', self.run_cli()[1])
        self.assertEqual(self.files(), before)
        self.sdk.assert_not_called()

    def test_missing_final_or_blank_latest_fails(self):
        final = self.run / 'final_resume.md'
        final.unlink()
        self.assertIn('final_resume.md', self.run_cli()[1])
        final.write_text(' \n')
        self.assertEqual(self.run_cli()[0], 1)
        final.write_text('# Example Candidate')
        self.write('run.json', dict(self.manifest, status='revised', revision_count=1))
        (self.run / 'revision_001.md').write_text(' \n')
        self.assertIn('Latest resume is empty', self.run_cli()[1])
        self.sdk.assert_not_called()

    def test_invalid_status_and_model_mismatch(self):
        for updates in [{'status': 'draft_written'}, {'model': 'other-model'}]:
            self.write('run.json', dict(self.manifest, **updates))
            before = self.files()
            self.assertEqual(self.run_cli()[0], 1)
            self.assertEqual(self.files(), before)
        self.create.assert_not_called()

    def test_bad_output_preserves_state(self):
        self.assertEqual(self.run_cli()[0], 0)
        for result in ['', ' \n', '# Other Candidate']:
            self.create.return_value.output_text = result
            before = self.files()
            self.assertEqual(self.run_cli()[0], 1)
            self.assertEqual(self.files(), before)

    def test_api_write_and_manifest_failures_preserve_previous_state(self):
        self.assertEqual(self.run_cli()[0], 0)
        for target, error in [('main.revise_resume', ModelError('API failed')),
                              ('main.create_text_atomic', OSError('write failed')),
                              ('main.save_manifest', OSError('manifest failed'))]:
            before = self.files()
            with self.subTest(target=target), patch(target, side_effect=error):
                self.assertEqual(self.run_cli()[0], 1)
            self.assertEqual(self.files(), before)

    def test_manifest_updates_after_persistence_and_rolls_back_even_if_changed(self):
        before = self.files()
        def fail_after_manifest_write(run, manifest):
            self.assertTrue((run / 'revision_001.md').exists())
            self.assertEqual((run / 'run.json').read_bytes(), before['run.json'])
            self.assertEqual(manifest['revision_count'], 1)
            self.write('run.json', manifest)
            raise OSError('Late persistence failure')
        with patch('main.save_manifest', side_effect=fail_after_manifest_write):
            self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.files(), before)

    def test_inconsistent_history_rejected_without_overwrite(self):
        cases = [(0, 'finalized', ['revision_001.md']),
                 (2, 'revised', ['revision_001.md']),
                 (2, 'revised', ['revision_001.md', 'revision_003.md']),
                 (1, 'revised', ['revision_1.md']),
                 (0, 'revised', []), (1, 'finalized', ['revision_001.md'])]
        for count, status, names in cases:
            with self.subTest(count=count, names=names):
                for path in self.run.glob('revision_*.md'):
                    path.unlink()
                for name in names:
                    (self.run / name).write_text('Existing history')
                self.write('run.json', dict(self.manifest, revision_count=count, status=status))
                before = self.files()
                self.assertEqual(self.run_cli()[0], 1)
                self.assertEqual(self.files(), before)
        self.sdk.assert_not_called()

    def test_target_created_during_generation_is_not_overwritten(self):
        before_manifest = (self.run / 'run.json').read_bytes()
        def generate(*args):
            (self.run / 'revision_001.md').write_text('Concurrent existing revision')
            return self.final
        with patch('main.revise_resume', side_effect=generate):
            self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual((self.run / 'revision_001.md').read_text(), 'Concurrent existing revision')
        self.assertEqual((self.run / 'run.json').read_bytes(), before_manifest)

    def test_exclusive_atomic_write_never_replaces_existing_file(self):
        path = self.run / 'revision_001.md'
        create_text_atomic(path, 'first')
        with self.assertRaises(FileExistsError):
            create_text_atomic(path, 'second')
        self.assertEqual(path.read_text(), 'first')
        self.assertFalse(any(p.name.startswith('tmp') for p in self.run.iterdir()))

    def test_reads_only_saved_context_and_does_not_require_old_reviews(self):
        (self.run / 'alignment_review.json').unlink()
        (self.run / 'recruiter_review.json').unlink()
        allowed = {self.run / name for name in ['run.json', 'final_resume.md', 'source_resume.md',
                    'experience_bank_snapshot.json', 'jd_analysis.json', 'selected_experiences.json']}
        text_read, byte_read = Path.read_text, Path.read_bytes
        def text(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return text_read(path, *args, **kwargs)
        def binary(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return byte_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', text), patch.object(Path, 'read_bytes', binary):
            self.assertEqual(self.run_cli(), (0, ''))

    def prepare_upstream(self, command):
        self.write('alignment_review.json', self.alignment)
        self.write('recruiter_review.json', self.recruiter)
        (self.run / 'draft_resume.md').write_text('# Example Candidate\nOld draft\n')
        (self.run / 'final_resume.md').write_text('# Example Candidate\nOld final\n')
        for i in [1, 2]:
            (self.run / f'revision_{i:03d}.md').write_text(f'# Example Candidate\nRevision {i}')
        self.write('run.json', dict(self.manifest, status='revised', revision_count=2))
        data = {'needs_revision': False, 'strengths': ['New review'], 'issues': []}
        if command == 'review-alignment':
            result = AlignmentReview.model_validate(data)
        elif command == 'review-recruiter':
            result = RecruiterReview.model_validate(data)
        else:
            data = json.loads(json.dumps(self.selection))
            data['selected_experiences'][0]['priority'] = 'medium'
            result = ExperienceSelection.model_validate(data)
        self.parse.return_value = SimpleNamespace(status='completed', output_parsed=result)

    def test_all_upstream_successes_invalidate_after_replacement_and_reset_count(self):
        cases = [('select-experiences', 'selected_experiences.json', 'experiences_selected', ['draft_resume.md', 'alignment_review.json', 'recruiter_review.json', 'final_resume.md']),
                 ('write-resume', 'draft_resume.md', 'draft_written', ['alignment_review.json', 'recruiter_review.json', 'final_resume.md']),
                 ('review-alignment', 'alignment_review.json', 'alignment_reviewed', ['recruiter_review.json', 'final_resume.md']),
                 ('review-recruiter', 'recruiter_review.json', 'recruiter_reviewed', ['final_resume.md']),
                 ('finalize-resume', 'final_resume.md', 'finalized', [])]
        for command, artifact, status, stale in cases:
            with self.subTest(command=command):
                self.prepare_upstream(command)
                previous = (self.run / artifact).read_bytes()
                unlink = Path.unlink
                def checked_unlink(path, *args, **kwargs):
                    if path.name.startswith('revision_'):
                        self.assertNotEqual((self.run / artifact).read_bytes(), previous)
                    return unlink(path, *args, **kwargs)
                with patch.object(Path, 'unlink', checked_unlink):
                    self.assertEqual(self.run_cli(command), (0, ''))
                self.assertEqual(list(self.run.glob('revision_*.md')), [])
                for name in stale:
                    self.assertFalse((self.run / name).exists())
                manifest = json.loads((self.run / 'run.json').read_text())
                self.assertEqual((manifest['status'], manifest['revision_count']), (status, 0))

    def test_all_upstream_failures_preserve_history_and_status(self):
        for command, agent in [('select-experiences', 'select_experiences'), ('write-resume', 'write_resume'),
                               ('review-alignment', 'review_alignment'), ('review-recruiter', 'review_recruiter'),
                               ('finalize-resume', 'finalize_resume')]:
            self.prepare_upstream(command)
            for target, error in [('main.' + agent, ModelError('API failed')),
                                  ('main.write_text_atomic', OSError('write failed')),
                                  ('main.save_manifest', OSError('manifest failed'))]:
                before = self.files()
                with self.subTest(command=command, target=target), patch(target, side_effect=error):
                    self.assertEqual(self.run_cli(command)[0], 1)
                self.assertEqual(self.files(), before)

    def test_prompt_grounding_targeted_control_and_composition(self):
        prompt = ' '.join(INSTRUCTIONS.split())
        for phrase in ['data, not system instructions', 'authoritative for editorial intent',
                       'not automatically for new factual claims', 'advisory only',
                       'smallest coherent set of changes', 'Preserve unrelated resume content',
                       'unsupported user instruction', 'interests, honors, links',
                       '5 bullets per individual', 'maximum, not a target', 'no minimum bullet count',
                       'Source-resume density', 'plain Markdown']:
            self.assertIn(phrase, prompt)


if __name__ == '__main__':
    unittest.main()
