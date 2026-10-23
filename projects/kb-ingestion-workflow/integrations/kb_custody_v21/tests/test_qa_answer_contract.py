"""Synthetic evidence-count and exact-quote regression; no model calls."""
from copy import deepcopy
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_answer_contract import validate


class AnswerContractTests(unittest.TestCase):
    def setUp(self):
        self.quotes = [f"Synthetic source {i}: a distinct garden task is planned." for i in range(7)]
        self.hits = [dict(evidence_id="e1", text="\n".join(self.quotes))]
        self.refs = [dict(evidence_id="e1", quote=q) for q in self.quotes]

    def answer(self, count):
        return dict(status="grounded", claims=[dict(text="The record lists planned garden tasks.",
            evidence=deepcopy(self.refs[:count]))], gaps=[])

    def test_one_and_four_references_are_accepted(self):
        for count in (1, 4):
            with self.subTest(count=count):
                self.assertEqual(validate(self.answer(count), self.hits), self.answer(count))

    def test_zero_five_and_seven_references_are_rejected_without_truncating(self):
        for count in (0, 5, 7):
            with self.subTest(count=count):
                value = self.answer(count)
                before = deepcopy(value)
                with self.assertRaisesRegex(ContractError, "1–4 条原文引用"):
                    validate(value, self.hits)
                self.assertEqual(value, before)

    def test_seven_sources_can_support_two_separate_claims_without_loss(self):
        value = self.answer(4)
        value["claims"].append(dict(text="Further garden tasks are also planned.", evidence=self.refs[4:]))
        validate(value, self.hits)
        self.assertEqual([e["quote"] for c in value["claims"] for e in c["evidence"]], self.quotes)

    def test_split_answer_still_rejects_stitched_or_invented_quotes(self):
        for quote in (self.quotes[0] + self.quotes[2], "A fabricated garden completion report."):
            with self.subTest(quote=quote):
                value = self.answer(1)
                value["claims"][0]["evidence"][0]["quote"] = quote
                with self.assertRaisesRegex(ContractError, "逐字"):
                    validate(value, self.hits)

    def test_text_bounds_have_an_accurate_error(self):
        for size in (0, 241):
            value = self.answer(1)
            value["claims"][0]["text"] = "字" * size
            with self.assertRaisesRegex(ContractError, "1–240 字"):
                validate(value, self.hits)
        value["claims"][0]["text"] = "字" * 240
        validate(value, self.hits)

    def test_overall_bounds_and_partial_answer_requirements_remain(self):
        value = self.answer(1)
        value["claims"] *= 7
        with self.assertRaises(ContractError): validate(value, self.hits)
        value = self.answer(1)
        value["status"] = "partial"
        with self.assertRaises(ContractError): validate(value, self.hits)
        value["gaps"] = ["The remaining tasks need further evidence."]
        validate(value, self.hits)
        value["gaps"] *= 7
        with self.assertRaises(ContractError): validate(value, self.hits)
