import pytest
from pydantic import ValidationError

from secagent.agents.runner import AgentRunner
from secagent.domain import CriticDecision, MissingEvidenceKind


def test_critic_missing_evidence_requires_an_explicit_kind() -> None:
    with pytest.raises(ValidationError):
        CriticDecision.model_validate(
            {
                "is_complete": False,
                "confidence": 0.3,
                "reason": "missing evidence",
                "missing_evidence": ["second observation"],
            }
        )


def test_critic_missing_evidence_accepts_structured_items() -> None:
    decision = CriticDecision.model_validate(
        {
            "is_complete": False,
            "confidence": 0.3,
            "reason": "missing evidence",
            "missing_evidence": [
                {"kind": "factual", "description": "second observation"},
                {
                    "kind": "report_generation",
                    "description": "Need final report",
                },
            ],
        }
    )

    assert [item.kind for item in decision.missing_evidence] == [
        MissingEvidenceKind.FACTUAL,
        MissingEvidenceKind.REPORT_GENERATION,
    ]


def test_legacy_critic_checkpoint_strings_restore_as_factual() -> None:
    decision = AgentRunner._critic_from_checkpoint(
        {
            "is_complete": False,
            "confidence": 0.3,
            "reason": "legacy checkpoint",
            "missing_evidence": ["second observation"],
            "replan_round": 0,
        }
    )

    assert len(decision.missing_evidence) == 1
    assert decision.missing_evidence[0].kind is MissingEvidenceKind.FACTUAL
    assert decision.missing_evidence[0].description == "second observation"
