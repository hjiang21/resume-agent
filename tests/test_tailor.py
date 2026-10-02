"""Exercise orchestration with real stage persistence and mocked model responses."""

import contextlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
from schemas import JDAnalysis, ExperienceSelection, AlignmentReview, RecruiterReview
from utils.llm import ModelError

STAGES = ['analyze_job_description', 'select_run_experiences', 'write_run_resume',
          'review_run_alignment', 'review_run_recruiter', 'finalize_run_resume']
AGENTS = ['analyze_jd', 'select_experiences', 'write_resume',
          'review_alignment', 'review_recruiter', 'finalize_resume']
LABELS = ['JD Analyst', 'Experience Selector', 'Resume Writer',
          'Alignment Reviewer', 'Recruiter Reviewer', 'Final Editor']
ARTIFACTS = ['jd_analysis.json', 'selected_experiences.json', 'draft_resume.md',
             'alignment_review.json', 'recruiter_review.json', 'final_resume.md']
STATUSES = ['initialized', 'jd_analyzed', 'experiences_selected', 'draft_written',
            'alignment_reviewed', 'recruiter_reviewed', 'finalized']
SNAPSHOTS = {'run.json', 'source_resume.md', 'source_job_description.md', 'experience_bank_snapshot.json'}


class TailorTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'inputs').mkdir()
        (self.root / 'data').mkdir()
        self.resume = self.root / 'inputs/resume.md'
        self.jd = self.root / 'inputs/job_description.md'
        self.bank = self.root / 'data/experience_bank.json'
        self.resume.write_text('# Example Candidate\n- Designed pilot KPIs.\n')
        self.jd.write_text('Analyst: design pilot KPIs.')
        self.bank_data = {'experiences': [{'organization': 'Example', 'title': 'Pilot', 'content': 'Designed KPIs.'}]}
        self.bank.write_text(json.dumps(self.bank_data))
        self.models = {
            JDAnalysis: {'role': 'Analyst', **{key: [] for key in [
                'core_responsibilities', 'required_qualifications', 'preferred_qualifications',
                'technical_skills', 'business_skills', 'important_language', 'hiring_themes']}},
            ExperienceSelection: {'selected_experiences': [{'organization': 'Example', 'title': 'Pilot',
                'relevance': ['Measurement'], 'priority': 'high'}],
                'resume_strategy': {'emphasize': ['Measurement'], 'deemphasize': []}},
            AlignmentReview: {'needs_revision': False, 'strengths': [], 'issues': []},
            RecruiterReview: {'needs_revision': False, 'strengths': [], 'issues': []},
        }
        self.sdk = self.start(patch('utils.llm.OpenAI'))
        self.sdk.return_value.responses.parse.side_effect = lambda **kwargs: SimpleNamespace(
            status='completed', output_parsed=kwargs['text_format'].model_validate(self.models[kwargs['text_format']]))
        self.sdk.return_value.responses.create.return_value = SimpleNamespace(
            status='completed', output_text='# Example Candidate\n- Designed KPIs.\n', output=[])
        self.start(patch('main.ROOT', self.root))
        self.start(patch('main.load_dotenv'))
        self.start(patch.dict(os.environ, {'OPENAI_API_KEY': 'test', 'OPENAI_MODEL': 'test-model'}, clear=True))
        self.ingest = self.start(patch('main.ingest', side_effect=AssertionError('Ingest must stay explicit')))
        self.background = self.start(patch('main.read_background', side_effect=AssertionError('No background inspection')))
        self.order = Mock()
        self.spies = []
        for name in STAGES:
            spy = self.start(patch('main.' + name, wraps=getattr(main, name)))
            self.spies.append(spy)
            self.order.attach_mock(spy, name)

    def start(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def cli(self, args=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main.main(args or ['tailor', '--name', 'example_analyst'])
        self.stdout, self.stderr = output.getvalue(), errors.getvalue()
        return code

    def run_path(self):
        paths = list((self.root / 'outputs').iterdir())
        self.assertEqual(len(paths), 1)
        return paths[0]

    def test_success_one_run_stage_order_artifacts_and_final_status(self):
        self.assertEqual(self.cli(), 0, self.stderr)
        run = self.run_path()
        self.assertEqual({p.name for p in run.iterdir()}, SNAPSHOTS | set(ARTIFACTS))
        manifest = json.loads((run / 'run.json').read_text())
        self.assertEqual(manifest['status'], 'finalized')
        self.assertEqual(manifest['model'], 'test-model')
        self.assertEqual([call[0] for call in self.order.mock_calls], STAGES)
        self.spies[0].assert_called_once_with('example_analyst', self.resume, self.jd, self.bank)
        for spy in self.spies[1:]:
            spy.assert_called_once_with(run)
        self.assertEqual(self.sdk.return_value.responses.parse.call_count, 4)
        self.assertEqual(self.sdk.return_value.responses.create.call_count, 2)
        self.ingest.assert_not_called()
        self.background.assert_not_called()
        for number in range(1, 7):
            self.assertIn(f'[{number}/6]', self.stdout)
        self.assertIn(f'Final resume: {run / "final_resume.md"}', self.stdout)

    def test_custom_inputs_are_snapshotted_and_globals_not_reread(self):
        args = ['tailor', '--name', 'custom_role']
        for flag, path in [('--resume', self.resume), ('--job-description', self.jd), ('--bank', self.bank)]:
            custom = self.root / ('custom-' + path.name)
            path.rename(custom)
            args += [flag, str(custom)]
        initialize = main.initialize_run
        def snapshot_then_remove_sources(*args):
            result = initialize(*args)
            for path in self.root.glob('custom-*'):
                path.unlink()
            return result
        with patch('main.initialize_run', side_effect=snapshot_then_remove_sources):
            self.assertEqual(self.cli(args), 0, self.stderr)
        self.assertEqual((self.run_path() / 'source_resume.md').read_text(), '# Example Candidate\n- Designed pilot KPIs.\n')

    def test_missing_and_malformed_bank_never_ingests_or_creates_run(self):
        for bad in [None, 'bad JSON', '{}']:
            with self.subTest(bad=bad):
                if bad is None:
                    self.bank.unlink()
                else:
                    self.bank.write_text(bad)
                self.assertEqual(self.cli(), 1)
                self.assertIn('experience bank', self.stderr.lower())
                self.assertIn('ingest', self.stderr)
                self.assertIn('JD Analyst', self.stderr)
                self.assertFalse((self.root / 'outputs').exists())
        self.sdk.assert_not_called()
        self.ingest.assert_not_called()
        for spy in self.spies[1:]:
            spy.assert_not_called()

    def assert_stage_failure(self, index):
        with patch('main.' + AGENTS[index], side_effect=ModelError('Synthetic API failure')):
            self.assertEqual(self.cli(), 1)
        run = self.run_path()
        self.assertEqual({p.name for p in run.iterdir()}, SNAPSHOTS | set(ARTIFACTS[:index]))
        self.assertEqual(json.loads((run / 'run.json').read_text())['status'], STATUSES[index])
        self.assertEqual([call[0] for call in self.order.mock_calls], STAGES[:index + 1])
        self.assertIn(f'Tailor failed during {LABELS[index]}', self.stderr)
        self.assertIn(str(run), self.stderr)
        self.assertIn(f'Current status: {STATUSES[index]}', self.stderr)
        self.assertIn('Completed artifacts were preserved', self.stderr)
        self.assertIn('Synthetic API failure', self.stderr)
        return run

    def test_jd_failure_stops_and_preserves_snapshots(self):
        self.assert_stage_failure(0)

    def test_selector_failure_preserves_analysis(self):
        self.assert_stage_failure(1)

    def test_writer_failure_preserves_selection(self):
        self.assert_stage_failure(2)

    def test_alignment_failure_preserves_draft(self):
        self.assert_stage_failure(3)

    def test_recruiter_failure_preserves_alignment_and_can_resume_manually(self):
        run = self.assert_stage_failure(4)
        before = {p.name: p.read_bytes() for p in run.iterdir() if p.name != 'run.json'}
        self.assertEqual(self.cli(['review-recruiter', '--run', str(run)]), 0)
        self.assertEqual(self.cli(['finalize-resume', '--run', str(run)]), 0)
        self.assertEqual(json.loads((run / 'run.json').read_text())['status'], 'finalized')
        for name, content in before.items():
            self.assertEqual((run / name).read_bytes(), content)

    def test_final_failure_preserves_both_reviews(self):
        self.assert_stage_failure(5)

    def test_downstream_model_mismatch_stops_before_model_call(self):
        select = self.spies[1]._mock_wraps
        def changed_model(run):
            os.environ['OPENAI_MODEL'] = 'different-model'
            return select(run)
        self.spies[1].side_effect = changed_model
        self.assertEqual(self.cli(), 1)
        self.assertIn('Experience Selector', self.stderr)
        self.assertIn('OPENAI_MODEL must match', self.stderr)
        self.assertEqual(json.loads((self.run_path() / 'run.json').read_text())['status'], 'jd_analyzed')
        self.assertEqual(self.sdk.return_value.responses.parse.call_count, 1)
        for spy in self.spies[2:]:
            spy.assert_not_called()

    def test_initial_snapshot_failure_reports_partial_path(self):
        with patch('utils.runs.write_bytes_atomic', side_effect=OSError('Snapshot write failed')):
            self.assertEqual(self.cli(), 1)
        run = self.run_path()
        self.assertIn(str(run), self.stderr)
        self.assertIn('Current status: initializing', self.stderr)
        self.assertIn('Snapshot write failed', self.stderr)
        for spy in self.spies[1:]:
            spy.assert_not_called()

    def test_first_manifest_failure_reports_path_without_masking_error(self):
        with patch('utils.runs.save_manifest', side_effect=OSError('Manifest write failed')):
            self.assertEqual(self.cli(), 1)
        self.assertIn(str(self.run_path()), self.stderr)
        self.assertIn('status: unavailable', self.stderr)
        self.assertIn('Manifest write failed', self.stderr)

    def test_analysis_programmatic_return_is_path_and_standalone_exit_is_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            run = main.analyze_job_description('example_analyst', self.resume, self.jd, self.bank)
        self.assertIsInstance(run, Path)
        self.assertEqual(run, self.run_path())
        self.assertEqual(self.cli(['analyze-jd', '--name', 'second_role']), 0)


if __name__ == '__main__':
    unittest.main()
