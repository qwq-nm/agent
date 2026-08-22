import json

from secagent.domain import ModelRequest, ModelResponse


class MockProvider:
    name = "mock"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = json.loads(request.user)
        title = request.response_schema.get("title")
        if title == "ParsedTask":
            hint = payload.get("scene_hint")
            goal = payload["goal"]
            scene = hint or (
                "source_audit"
                if "源码" in goal
                else "ctf_web"
                if any(item in goal.lower() for item in ("ctf", "flag", "nssctf", "web题", "web 题", "靶场", "题目"))
                else "web_analysis"
                if "Web" in goal or "网站" in goal or payload.get("target_url")
                else "incident_response"
            )
            data = {
                "scene": scene,
                "goal": goal,
                "inputs": payload.get("inputs", []),
                "constraints": [payload["authorization_scope"]],
                "authorization_scope": payload["authorization_scope"],
                "risk_level": "medium" if scene in {"web_analysis", "ctf_web"} else "low",
                "expected_outputs": ["证据链", "处置建议", "报告"],
            }
        elif title == "PlanDocument":
            data = {
                "steps": [
                    {
                        "name": f"执行 {name}",
                        "purpose": f"获取 {name} 的结构化证据",
                        "tool_name": name,
                        "params": payload["params_by_tool"].get(name, {}),
                        "risk_level": payload["risk_by_tool"][name],
                        "need_human_confirm": payload["risk_by_tool"][name]
                        == "medium",
                    }
                    for name in payload["allowed_tools"]
                ]
            }
        elif title == "CriticDecision":
            has_evidence = bool(payload["evidence_count"])
            data = {
                "is_complete": has_evidence,
                "confidence": 0.9 if has_evidence else 0.0,
                "reason": "存在可追溯工具证据" if has_evidence else "缺少工具证据",
                "goal_completed": has_evidence,
                "should_continue": not has_evidence,
                "should_report": has_evidence,
                "next_focus": [] if has_evidence else ["补充白名单工具证据"],
                "stop_reason": "evidence_sufficient" if has_evidence else None,
                "missing_evidence": (
                    []
                    if has_evidence
                    else [{"kind": "factual", "description": "工具证据"}]
                ),
            }
        elif title == "ReportSections":
            data = {
                "summary": payload["goal"],
                "findings": payload["findings"],
                "recommendations": payload["recommendations"],
                "uncertainties": payload["errors"],
                "evidence_ids": [
                    finding["id"] for finding in payload["findings"]
                ],
            }
        else:
            raise ValueError(f"unsupported mock schema: {title}")
        return ModelResponse(
            provider="mock",
            model="deterministic-mock",
            data=data,
            latency_ms=0,
            is_demo=True,
        )
