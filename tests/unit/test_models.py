import pytest
from pydantic import ValidationError

from jobpilot.core.enums import ATS, AnswerSource, FieldType, Severity, Verdict
from jobpilot.core.models import (
    FieldAnswer,
    FillPlan,
    FormField,
    MatchResult,
    VerifyIssue,
    VerifyResult,
)


def _field(fid: str, required: bool) -> FormField:
    return FormField(
        field_id=fid, selector=f"#{fid}", type=FieldType.TEXT, label=fid, required=required
    )


def _answer(fid: str, value: str | list[str] | None) -> FieldAnswer:
    return FieldAnswer(
        field_id=fid, value=value, source=AnswerSource.ALIAS_PROFILE, confidence=1.0
    )


def _plan(answers: list[FieldAnswer]) -> FillPlan:
    return FillPlan(job_id=1, adapter=ATS.GREENHOUSE, url="https://x", resume_key="backend",
                    answers=answers)


def test_unanswered_required_lists_only_missing_required() -> None:
    fields = [_field("f0", True), _field("f1", True), _field("f2", False)]
    plan = _plan([_answer("f0", "Asha")])
    assert [f.field_id for f in plan.unanswered_required(fields)] == ["f1"]


@pytest.mark.parametrize("empty", [None, "", []])
def test_empty_values_do_not_count_as_answered(empty: str | list[str] | None) -> None:
    fields = [_field("f0", True)]
    plan = _plan([_answer("f0", empty)])
    assert [f.field_id for f in plan.unanswered_required(fields)] == ["f0"]


def test_all_answered_returns_empty() -> None:
    fields = [_field("f0", True)]
    assert _plan([_answer("f0", "x")]).unanswered_required(fields) == []


def test_confidence_bounds_enforced() -> None:
    with pytest.raises(ValidationError):
        FieldAnswer(field_id="f0", source=AnswerSource.NONE, confidence=1.5)


def test_match_score_bounds_and_reason_limit() -> None:
    with pytest.raises(ValidationError):
        MatchResult(score=101, decision="apply")
    with pytest.raises(ValidationError):
        MatchResult(score=80, decision="apply", reasons=list("abcde"))
    with pytest.raises(ValidationError):
        MatchResult(score=80, decision="maybe")  # type: ignore[arg-type]


def test_verify_result_roundtrip() -> None:
    r = VerifyResult(
        verdict=Verdict.NEEDS_INPUT,
        confidence=0.6,
        issues=[VerifyIssue(field_id="f1", severity=Severity.ASK, message="low confidence")],
    )
    assert VerifyResult.model_validate(r.model_dump()) == r