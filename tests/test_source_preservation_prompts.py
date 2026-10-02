"""Policy regressions; these check instructions, not live model compliance."""

import unittest

from agents.final_editor import INSTRUCTIONS as EDITOR
from agents.resume_writer import INSTRUCTIONS as WRITER


class SourcePreservationPromptTests(unittest.TestCase):
    def assert_guidance(self, instructions, *phrases):
        prompt = " ".join(instructions.split())
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_writer_discourages_silent_deletion(self):
        self.assert_guidance(WRITER,
            "editorial baseline as well as factual evidence",
            "Do not silently remove brief, supported source-resume content",
            "merely because it is not central to the target JD",
            "Omission should be intentional, not automatic")

    def test_both_distinguish_reallocatable_strategy_from_baseline_content(self):
        for prompt in [WRITER, EDITOR]:
            self.assert_guidance(prompt,
                "Strategic content such as experience and project bullets",
                "coursework, technical skills, methods, and detailed accomplishments",
                "may be freely reallocated based on relevance, recency, distinctiveness, and space",
                "candidate name and contact information, existing profile/portfolio links",
                "honors or awards, leadership labels/context, interests or personal-interest lines",
                "brief non-bullet", "by default")

    def test_preservation_allows_reasoned_removal_and_does_not_mandate_interests(self):
        for prompt in [WRITER, EDITOR]:
            self.assert_guidance(prompt,
                "not an immutable template", "affirmative reason",
                "space pressure, redundancy, weak relevance relative to stronger supported evidence",
                "inconsistency with the target resume strategy",
                "materially stronger evidence", "Interests are an example, not mandatory",
                "do not invent them when absent from the source",
                "remove them when they materially harm space or focus")
            self.assertNotRegex(prompt.lower(), r"(?:always|must) (?:include|retain|preserve) (?:an? |the )?interests")

    def test_editor_compares_source_and_draft_for_accidental_omissions(self):
        self.assert_guidance(EDITOR,
            "The draft may contain accidental omissions",
            "do not assume that all draft omissions were intentional",
            "compare the draft against the source resume for low-cost supported content that disappeared",
            "Do not automatically restore every source element")

    def test_editor_considers_restoration_before_structural_deletion(self):
        self.assert_guidance(EDITOR,
            "If a reviewer flags a structural consequence of an omission",
            "do not automatically choose the easiest deletion-based fix",
            'if "Leadership & Interests" has lost its interests line',
            "consider restoring the supported interests rather than simply renaming",
            "respecting space, clarity, and target relevance")

    def test_grounding_density_and_bullet_cap_remain(self):
        for prompt in [WRITER, EDITOR]:
            self.assert_guidance(prompt,
                "Only candidate_evidence.source_resume and candidate_evidence.experience_bank establish candidate facts",
                "more than 5 bullets", "maximum, not a target", "no minimum bullet count")
        self.assert_guidance(WRITER, "source resume's overall content density as the primary V1 proxy")
        self.assert_guidance(EDITOR, "source-resume density as the qualitative capacity proxy",
            "Only restore information supported by the source resume or experience bank",
            "do not invent omitted content")

    def test_reviewer_reconciliation_remains(self):
        self.assert_guidance(EDITOR,
            "Candidate evidence wins over reviewer suggestions",
            "Alignment review is the primary job-alignment optimization guidance",
            "Reconcile conflicting recommendations rather than mechanically applying every issue",
            "applying it directly, applying it partially, resolving it through another edit",
            "leaving the draft unchanged when the advice conflicts with evidence or reduces quality")


if __name__ == "__main__":
    unittest.main()
