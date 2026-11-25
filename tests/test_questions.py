"""AI questions (AskUserQuestion): reported with their questions, answered by position, never answered by the runner."""
import threading
import unittest

from timetrace import questions
from timetrace.claude_stream import permission_response
from timetrace.remote_control import NO_PHONE_DENY

from tests.test_claude_stream import FakeClaudeCase, Recorder
from tests.test_remote_control import Base

ASK = {"questions": [{"question": "Which word goes first?", "header": "Word", "multiSelect": False,
                      "options": [{"label": "apple", "description": "a fruit"}, {"label": "banana", "description": ""}]}]}
MULTI = {"questions": [
    {"question": "Which word goes first?", "header": "Word", "multiSelect": False,
     "options": [{"label": "apple", "description": ""}, {"label": "banana", "description": ""}]},
    {"question": "Which files?", "header": "Files", "multiSelect": True,
     "options": [{"label": "a.txt", "description": ""}, {"label": "b.txt", "description": ""}, {"label": "c.txt", "description": ""}]}]}


class ShapeTest(unittest.TestCase):
    def test_answers_are_rebuilt_from_claudes_own_text(self):
        self.assertEqual(questions.updated_input(ASK, [[1]]), dict(ASK, answers={"Which word goes first?": "banana"}))
        self.assertEqual(questions.updated_input(MULTI, [[0], [2, 0]])["answers"],
                         {"Which word goes first?": "apple", "Which files?": "c.txt, a.txt"})

    def test_an_answer_must_fit_every_question(self):
        for picked in (None, [], [[]], [[2]], [[-1]], [[0, 1]], [[True]], [["0"]], [[0], [0]], [[0, 0]]):
            with self.subTest(picked=picked):
                self.assertIsNone(questions.updated_input(ASK, picked))
        self.assertIsNone(questions.updated_input(MULTI, [[0], [1, 1]]))
        self.assertIsNone(questions.updated_input(MULTI, [[0], []]))

    def test_malformed_questions_are_not_questions(self):
        bad = [
            {}, {"questions": []}, {"questions": "x"},
            {"questions": [{"question": "  ", "options": ASK["questions"][0]["options"]}]},
            {"questions": [{"question": "Q", "options": [{"label": "only"}]}]},
            {"questions": [{"question": "Q", "options": [{"label": "a"}, {"label": "a"}]}]},
            {"questions": [{"question": "Q", "options": [{"label": "a"}, {"label": ""}]}]},
            {"questions": [ASK["questions"][0], ASK["questions"][0]]},
            {"questions": [ASK["questions"][0]] * 5},
        ]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(questions.parse(value))

    def test_the_wire_form_is_redacted_but_keeps_its_shape(self):
        secret = {"questions": [{"question": "Use token ghp_" + "a" * 36 + "?", "header": "", "multiSelect": False,
                                 "options": [{"label": "yes", "description": ""}, {"label": "no", "description": ""}]}]}
        wire = questions.wire_input(questions.parse(secret), 3500)
        self.assertNotIn("ghp_" + "a" * 36, str(wire))
        self.assertEqual([len(q["options"]) for q in wire["questions"]], [2])
        # A question too long to send whole is not sent cut.
        long = {"questions": [{"question": "Q" * 900, "header": "", "multiSelect": False,
                               "options": [{"label": "长" * 190, "description": ""}, {"label": "短" * 190, "description": ""}]}]}
        self.assertIsNone(questions.wire_input(questions.parse(long), 1000))


class MaskedAgreementTest(unittest.TestCase):
    """What the runner accepted on the masked copy is exactly what Claude gets from its original input."""

    def test_a_label_masking_shortened_is_still_answered(self):
        from timetrace.redact import mask_structure
        original = {"questions": [{"question": "Which?", "header": "", "multiSelect": False,
                                   "options": [{"label": "ttm_" + "a" * 210, "description": ""}, {"label": "no", "description": ""}]}]}
        masked = mask_structure(original)
        asked = questions.parse(masked)
        self.assertIsNotNone(asked)
        self.assertTrue(questions.valid_answers(asked, [[0]]))
        rebuilt = questions.updated_input(original, [[0]])
        self.assertEqual(rebuilt["answers"], {"Which?": "ttm_" + "a" * 210})
        reply = permission_response("r1", "toolu_1", {"behavior": "allow", "answers": [[0]]}, original)["response"]["response"]
        self.assertEqual(reply["behavior"], "allow")

    def test_labels_that_masking_makes_alike_are_refused_before_asking(self):
        from timetrace.redact import mask_structure
        original = {"questions": [{"question": "Which?", "header": "", "multiSelect": False,
                                   "options": [{"label": "ttm_" + "a" * 30, "description": ""}, {"label": "ttm_" + "b" * 30, "description": ""}]}]}
        self.assertIsNone(questions.parse(mask_structure(original)))

    def test_whatever_the_masked_copy_accepts_the_original_accepts(self):
        from timetrace.redact import mask_structure
        token = "ttm_" + "c" * 400
        cases = [
            {"questions": [{"question": token, "options": [{"label": "a"}, {"label": "b"}]}]},
            {"questions": [{"question": "Q " + token, "multiSelect": True, "options": [{"label": token + "x"}, {"label": "b"}, {"label": " c "}]}]},
            {"questions": [{"question": "A", "options": [{"label": "x"}, {"label": "y"}]}, {"question": "B", "options": [{"label": token}, {"label": "z"}]}]},
        ]
        for original in cases:
            asked = questions.parse(mask_structure(original))
            if asked is None:
                continue
            for picked in ([[0]] * len(asked), [[1]] * len(asked)):
                with self.subTest(original=original, picked=picked):
                    self.assertEqual(questions.valid_answers(asked, picked), questions.updated_input(original, picked) is not None)


class RunControlQuestionTest(Base):
    def test_a_question_goes_to_the_person_and_their_pick_comes_back(self):
        box, thread, _ = self.ask_async({"tool_name": "AskUserQuestion", "input": ASK})
        asked, _ = self.wait_event("approval_requested")
        self.assertEqual(asked["tool"], "AskUserQuestion")
        self.assertEqual(asked["summary"], "AI 提问：Which word goes first?")
        self.assertEqual(asked["input"]["questions"][0]["options"][1]["label"], "banana")
        self.control.apply_lease({"approvals": [{"request_id": asked["request_id"], "decision": "approve", "remember": True, "answers": [[1]]}]})
        thread.join(5)
        self.assertEqual(box["answer"], {"behavior": "allow", "answers": [[1]]})
        resolved, _ = self.wait_event("approval_resolved")
        self.assertEqual(resolved["outcome"], "approved")
        # Never remembered: the next question is asked again.
        box2, thread2, _ = self.ask_async({"tool_name": "AskUserQuestion", "input": ASK})
        again, _ = self.wait_event("approval_requested")
        self.control.apply_lease({"approvals": [{"request_id": again["request_id"], "decision": "deny"}]})
        thread2.join(5)
        self.assertEqual(box2["answer"], {"behavior": "deny", "message": questions.NO_ANSWER_DENY})

    def test_an_approval_without_a_valid_answer_is_a_refusal(self):
        for decision in ({"decision": "approve"}, {"decision": "approve", "answers": [[5]]}, {"decision": "approve", "answers": [[0, 1]]}):
            with self.subTest(decision=decision):
                box, thread, _ = self.ask_async({"tool_name": "AskUserQuestion", "input": ASK})
                asked, _ = self.wait_event("approval_requested")
                self.control.apply_lease({"approvals": [dict(decision, request_id=asked["request_id"])]})
                thread.join(5)
                self.assertEqual(box["answer"], {"behavior": "deny", "message": questions.NO_ANSWER_DENY})
                resolved, _ = self.wait_event("approval_resolved")
                self.assertEqual(resolved["outcome"], "denied")

    def test_a_malformed_question_is_refused_here(self):
        answer = self.control.permission({"tool_name": "AskUserQuestion", "input": {"questions": []}}, threading.Event())
        self.assertEqual(answer, {"behavior": "deny", "message": questions.INVALID_DENY})
        self.assertEqual(self.control.take_events(), [])

    def test_without_approvals_nobody_can_answer(self):
        self.control.approvals_enabled = False
        answer = self.control.permission({"tool_name": "AskUserQuestion", "input": ASK}, threading.Event())
        self.assertEqual(answer, {"behavior": "deny", "message": NO_PHONE_DENY})


class StreamAnswerTest(FakeClaudeCase):
    def test_the_response_carries_claudes_question_with_the_answer(self):
        reply = permission_response("r1", "toolu_1", {"behavior": "allow", "answers": [[0]]}, ASK)["response"]["response"]
        self.assertEqual(reply["behavior"], "allow")
        self.assertEqual(reply["updatedInput"]["answers"], {"Which word goes first?": "apple"})
        self.assertEqual(reply["updatedInput"]["questions"], ASK["questions"])
        broken = permission_response("r1", "toolu_1", {"behavior": "allow", "answers": [[9]]}, ASK)["response"]["response"]
        self.assertEqual((broken["behavior"], broken["message"]), ("deny", questions.NO_ANSWER_DENY))

    def test_a_masked_request_is_answered_with_the_original_text(self):
        original = {"questions": [{"question": "Token ttm_" + "b" * 36 + " ok?", "header": "", "multiSelect": False,
                                   "options": [{"label": "yes", "description": ""}, {"label": "no", "description": ""}]}]}
        host = Recorder(lambda request: {"behavior": "allow", "answers": [[1]]})
        code, _ = self.run_session([[{"permission": {"tool": "AskUserQuestion", "input": original}}, {"result": "done"}]], host=host)
        self.assertEqual(code, 0)
        self.assertNotIn("ttm_" + "b" * 36, str(host.requests[0]["input"]))
        reply = [r["decision"] for r in self.records() if "decision" in r][0]["reply"]["response"]
        self.assertEqual(reply["behavior"], "allow")
        self.assertEqual(reply["updatedInput"]["answers"], {original["questions"][0]["question"]: "no"})


if __name__ == "__main__":
    unittest.main()
