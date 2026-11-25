"""AI questions: Claude's own AskUserQuestion tool, answered from the phone or web.

Claude Code asks the host before AskUserQuestion runs (`can_use_tool`, in
every permission mode the runner uses, `bypassPermissions` included). The
runner reports the question as an ordinary approval request whose tool is
AskUserQuestion and whose input holds the questions; the person answers by
picking options, and the lease brings the picked options back by position
(`answers`: one list of option indexes per question, in order). The runner
then allows the tool with the answers filled in, the shape the Agent SDK
documents: `updatedInput = {questions, answers: {<question>: <label>}}`,
several labels of a multiple choice joined with ", ".

Positions, not text: the request the runner reports is redacted, so the text
the person saw may differ from what Claude asked; the answer is rebuilt from
Claude's own question and option labels. A question that does not have the
shape the tool allows (1–4 questions, 2–4 distinct options each) is refused
locally, and an approval without a valid answer is a refusal: Claude never
receives an empty or made-up answer.
"""
import json
from typing import Any, Dict, List, Optional

from timetrace.redact import redact

TOOL = "AskUserQuestion"
# What the inventory reports as questions_version: Valley sends answers only to a computer that hands them back.
VERSION = 1
MIN_QUESTIONS, MAX_QUESTIONS, MIN_OPTIONS, MAX_OPTIONS = 1, 4, 2, 4
TEXT_CHARS, LABEL_CHARS = 1000, 200
SUMMARY_CHARS = 300

INVALID_DENY = ("AskUserQuestion 的问题格式不对：需要 1–4 个不重复的问题，每题 2–4 个不重复、非空的选项。"
                "请按这个格式重新提问，或说明需要用户决定什么后结束")
TOO_LONG_DENY = "这个问题太长，无法转给用户。请缩短问题和选项后重新提问，或说明需要用户决定什么后结束"
NO_ANSWER_DENY = "用户没有回答这个问题。不要替用户做选择；请说明需要用户决定什么，然后结束或换一种不需要这个回答的做法"


def parse(tool_input: Any) -> Optional[List[Dict[str, Any]]]:
    """The questions of an AskUserQuestion input, or None when it is not a
    question anybody can answer."""
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("questions")
    if not isinstance(raw, list) or not MIN_QUESTIONS <= len(raw) <= MAX_QUESTIONS:
        return None
    out, asked = [], set()
    for q in raw:
        if not isinstance(q, dict):
            return None
        text = q.get("question")
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > TEXT_CHARS or text.strip() in asked:
            return None
        asked.add(text.strip())
        options = q.get("options")
        if not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            return None
        labels, seen = [], set()
        for o in options:
            label = o.get("label") if isinstance(o, dict) else None
            if not isinstance(label, str) or not label.strip() or len(label.strip()) > LABEL_CHARS or label.strip() in seen:
                return None
            seen.add(label.strip())
            description = o.get("description") if isinstance(o.get("description"), str) else ""
            labels.append({"label": label, "description": description})
        header = q.get("header") if isinstance(q.get("header"), str) else ""
        out.append({"question": text, "header": header, "multiSelect": q.get("multiSelect") is True, "options": labels})
    return out



def wire_input(questions: List[Dict[str, Any]], limit: int) -> Optional[Dict[str, Any]]:
    """The questions as they may leave this computer: every string redacted,
    the structure unchanged, at most `limit` bytes of JSON; None when they do
    not fit without cutting (a cut question could not be answered)."""
    def clean(text: str) -> str:
        return " ".join(redact(text).split())
    value = {"questions": [{"question": clean(q["question"]), "header": clean(q["header"]), "multiSelect": q["multiSelect"],
                            "options": [{"label": clean(o["label"]), "description": clean(o["description"])} for o in q["options"]]}
                           for q in questions]}
    if len(json.dumps(value)) <= limit:
        return value
    for q in value["questions"]:
        for o in q["options"]:
            o["description"] = ""
    return value if len(json.dumps(value)) <= limit else None


def summary(questions: List[Dict[str, Any]]) -> str:
    """One line for the phone: the first question."""
    text = "AI 提问：" + " ".join(redact(questions[0]["question"]).split())
    return (text[:SUMMARY_CHARS - 1] + "…") if len(text) > SUMMARY_CHARS else text


def valid_answers(questions: List[Dict[str, Any]], picked: Any) -> bool:
    """Whether `picked` (one list of option indexes per question) answers
    every question: one option for a single choice, at least one and none
    twice for a multiple choice."""
    if not isinstance(picked, list) or len(picked) != len(questions):
        return False
    for q, chosen in zip(questions, picked):
        if not isinstance(chosen, list) or not chosen or (not q["multiSelect"] and len(chosen) != 1):
            return False
        if len(set(chosen)) != len(chosen):
            return False
        for index in chosen:
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(q["options"]):
                return False
    return True


def updated_input(original: Any, picked: Any) -> Optional[Dict[str, Any]]:
    """Claude's own input with the answers filled in, built from its own
    question text and labels; None when the answer does not fit it."""
    questions = parse(original)
    if questions is None or not valid_answers(questions, picked):
        return None
    answers = {q["question"]: ", ".join(q["options"][i]["label"] for i in chosen) for q, chosen in zip(questions, picked)}
    return dict(original, answers=answers)
