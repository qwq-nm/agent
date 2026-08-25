import json

from secagent.domain import (
    CriticDecision,
    MissingEvidenceKind,
    MissingEvidenceItem,
    ModelRequest,
    ModelResponse,
    ModelStage,
    ParsedTask,
    TaskScene,
)
from secagent.providers.router import ModelRouter
from secagent.security.redaction import redact_mapping
from secagent.services.ledger import LedgerService

MAX_OBSERVATIONS = 8
MAX_OBSERVATION_CONTENT_CHARS = 360
MAX_OBSERVATION_SOURCE_CHARS = 160


class Critic:
    def __init__(self, router: ModelRouter, ledger: LedgerService) -> None:
        self.router = router
        self.ledger = ledger

    async def review(
        self, task_id: str, parsed: ParsedTask
    ) -> tuple[CriticDecision, ModelResponse]:
        observations = self.observation_summary(task_id)
        scene_assessment = self.scene_evidence_assessment(parsed.scene, observations)
        response = await self.router.complete(
            ModelStage.CRITIC,
            ModelRequest(
                system=(
                    "你正在报告生成前复核证据。仅判断现有事实证据是否足以回答"
                    "用户目标并生成报告，而不是判断报告是否已经存在。"
                    "报告本身不得列为缺失证据；如需表达尚待生成报告，"
                    "只能将其标记为 report_generation 流程项。"
                    "missing_evidence 的每一项必须包含 kind 和 description。"
                    "可通过白名单工具补充的事实或观察使用 kind=factual；"
                    "仅表示尚需生成报告的流程项使用 kind=report_generation。"
                    "同时给出 goal_completed、should_continue、should_report、"
                    "next_focus 和 stop_reason：证据足以输出时 should_report=true；"
                    "还缺事实证据且仍有合理白名单工具方向时 should_continue=true；"
                    "无法继续或触及授权/安全边界时 should_continue=false 并说明 stop_reason。"
                ),
                user=json.dumps(
                    {
                        "goal": parsed.goal,
                        "scene": parsed.scene.value,
                        "evidence_count": observations["evidence_count"],
                        "observations": observations["items"],
                        "scene_evidence_assessment": scene_assessment,
                    },
                    ensure_ascii=False,
                ),
                response_schema=CriticDecision.model_json_schema(),
            ),
        )
        decision = self.normalize_decision(
            CriticDecision.model_validate(response.data),
            scene_assessment,
        )
        return decision, response

    @staticmethod
    def normalize_decision(
        decision: CriticDecision, scene_assessment: dict
    ) -> CriticDecision:
        factual_missing = [
            item
            for item in decision.missing_evidence
            if item.kind is MissingEvidenceKind.FACTUAL
        ]
        hard_gaps = [
            MissingEvidenceItem(kind=MissingEvidenceKind.FACTUAL, description=item)
            for item in scene_assessment.get("required_missing", [])
        ]
        seen = {item.description for item in factual_missing}
        factual_missing.extend(item for item in hard_gaps if item.description not in seen)
        is_complete = not factual_missing
        next_focus = list(decision.next_focus)
        for item in scene_assessment.get("recommended_next_focus", []):
            if item not in next_focus:
                next_focus.append(item)
        if not next_focus:
            next_focus = [item.description for item in factual_missing[:5]]
        should_report = bool(decision.should_report or is_complete)
        should_continue = bool(not should_report and factual_missing)
        stop_reason = decision.stop_reason
        if should_report and stop_reason is None:
            stop_reason = "evidence_sufficient"
        elif should_continue and stop_reason == "evidence_sufficient":
            stop_reason = None
        return decision.model_copy(
            update={
                "is_complete": is_complete,
                "goal_completed": bool(decision.goal_completed or is_complete),
                "missing_evidence": factual_missing,
                "should_report": should_report,
                "should_continue": should_continue,
                "next_focus": next_focus[:8],
                "stop_reason": stop_reason,
            }
        )

    @staticmethod
    def scene_evidence_assessment(scene: TaskScene, observations: dict) -> dict:
        items = observations["items"]
        contents = "\n".join(str(item.get("content", "")) for item in items).lower()
        sources = "\n".join(str(item.get("source", "")) for item in items).lower()
        evidence_count = observations["evidence_count"]
        required_missing: list[str] = []
        recommended_next_focus: list[str] = []
        if evidence_count == 0:
            required_missing.append("at least one tool-backed factual observation")
        if scene in {TaskScene.WEB_ANALYSIS, TaskScene.CTF_WEB}:
            if "http" not in contents and "http" not in sources:
                required_missing.append("authorized HTTP observation")
            if "header" not in contents:
                recommended_next_focus.append("response header observation")
            if "public link" not in contents and "client-side route" not in contents:
                recommended_next_focus.append("public link or client-side route hints")
            if "form" not in contents:
                recommended_next_focus.append("public form observation")
            if "robots.txt" not in contents and "robots.txt" not in sources:
                recommended_next_focus.append("robots.txt observation")
            if "flag-like pattern" not in contents:
                recommended_next_focus.append("flag-like pattern detection")
            if scene is TaskScene.CTF_WEB:
                if not any(
                    marker in contents or marker in sources
                    for marker in (
                        "public link",
                        "client-side route",
                        "robots.txt",
                        "flag-like pattern",
                        "sensitive file",
                        "backup",
                        "cookie",
                        "form",
                    )
                ):
                    required_missing.append("CTF Web public clue observation")
                for item in (
                    "front-end route or JavaScript clue review",
                    "hidden path or backup file clue review",
                    "candidate flag evidence review",
                ):
                    if item not in recommended_next_focus:
                        recommended_next_focus.append(item)
        elif scene is TaskScene.INCIDENT_RESPONSE:
            if not any(marker in contents for marker in ("raw_line", "timeline", "rule")):
                required_missing.append("parsed log evidence")
            if "timeline" not in contents:
                recommended_next_focus.append("incident timeline")
            if "rule" not in contents and "attack" not in contents:
                recommended_next_focus.append("attack pattern evidence")
        elif scene is TaskScene.SOURCE_AUDIT:
            if "source" not in contents and "rule" not in contents:
                required_missing.append("source audit finding or explicit no-finding evidence")
            if "secret" not in contents:
                recommended_next_focus.append("secret scanning evidence")
            if "config" not in contents and "cfg-" not in contents:
                recommended_next_focus.append("configuration risk evidence")
        elif scene is TaskScene.VULNERABILITY_HUNTING:
            if "vulnerability" not in contents and "vuln-" not in contents:
                required_missing.append("vulnerability finding or explicit no-finding evidence")
            if "vuln-" not in contents:
                recommended_next_focus.append("static vulnerability pattern evidence")
        elif scene is TaskScene.REVERSE_ANALYSIS:
            if "reverse" not in contents and "artifact=" not in contents:
                required_missing.append("reverse artifact triage evidence")
            if "artifact=" not in contents:
                recommended_next_focus.append("artifact metadata and printable string evidence")
        return {
            "required_missing": required_missing,
            "recommended_next_focus": recommended_next_focus[:8],
        }

    def observation_summary(
        self,
        task_id: str,
        decision: CriticDecision | None = None,
    ) -> dict:
        evidences = self.ledger.snapshot(task_id)["evidences"]
        items = []
        for evidence in evidences[-MAX_OBSERVATIONS:]:
            safe = redact_mapping(
                {
                    "evidence_type": evidence.get("evidence_type", "observation"),
                    "source": evidence.get("source", "unknown"),
                    "content": evidence.get("content", ""),
                }
            )
            items.append(
                {
                    "evidence_type": str(safe["evidence_type"])[:64],
                    "source": str(safe["source"])[
                        :MAX_OBSERVATION_SOURCE_CHARS
                    ],
                    "content": str(safe["content"])[
                        :MAX_OBSERVATION_CONTENT_CHARS
                    ],
                }
            )
        missing = decision.missing_evidence if decision is not None else []
        return {
            "evidence_count": len(evidences),
            "items": items,
            "missing_evidence": [item.description[:256] for item in missing[:12]],
        }
