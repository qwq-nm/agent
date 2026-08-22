import pytest
from pydantic import ValidationError

from secagent.agents.critic import Critic
from secagent.agents.runner import AgentRunner
from secagent.domain import CriticDecision, MissingEvidenceKind, TaskScene


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
    assert decision.should_continue is True
    assert decision.should_report is False


def test_critic_decision_defaults_are_backward_compatible() -> None:
    decision = CriticDecision.model_validate(
        {
            "is_complete": True,
            "confidence": 0.8,
            "reason": "old provider response",
            "missing_evidence": [],
        }
    )

    assert decision.goal_completed is False
    assert decision.should_continue is True
    assert decision.should_report is False
    assert decision.next_focus == []
    assert decision.stop_reason is None


def test_normalized_complete_critic_decision_reports_with_stop_reason() -> None:
    decision = Critic.normalize_decision(
        CriticDecision.model_validate(
            {
                "is_complete": True,
                "confidence": 0.9,
                "reason": "enough evidence",
                "missing_evidence": [
                    {
                        "kind": "report_generation",
                        "description": "Need final report",
                    }
                ],
            }
        ),
        {"required_missing": [], "recommended_next_focus": ["report from evidence"]},
    )

    assert decision.is_complete is True
    assert decision.goal_completed is True
    assert decision.should_report is True
    assert decision.should_continue is False
    assert decision.stop_reason == "evidence_sufficient"
    assert decision.missing_evidence == []
    assert decision.next_focus == ["report from evidence"]


def test_scene_assessment_requires_web_http_observation_when_absent() -> None:
    assessment = Critic.scene_evidence_assessment(
        TaskScene.WEB_ANALYSIS,
        {
            "evidence_count": 1,
            "items": [
                {
                    "evidence_type": "note",
                    "source": "manual",
                    "content": "operator note without network observation",
                }
            ],
        },
    )

    assert "authorized HTTP observation" in assessment["required_missing"]
    assert "robots.txt observation" in assessment["recommended_next_focus"]
