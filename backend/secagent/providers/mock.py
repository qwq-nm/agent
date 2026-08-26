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
            successful_tools = {
                call.get("tool_name")
                for call in payload.get("execution_memory", {}).get(
                    "successful_tool_calls", []
                )
                if isinstance(call, dict) and isinstance(call.get("tool_name"), str)
            }
            planned_tools = [
                name
                for name in payload["allowed_tools"]
                if name not in successful_tools
            ][: payload["max_plan_steps"]]
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
                    for name in planned_tools
                ]
            }
        elif title == "CriticDecision":
            has_evidence = bool(payload["evidence_count"])
            assessment = payload.get("scene_evidence_assessment", {})
            observations = payload.get("observations", [])
            has_demo_evidence = any(
                isinstance(item, dict)
                and (
                    item.get("source") == "demo_evidence"
                    or item.get("evidence_type") == "demo_evidence"
                    or str(item.get("source", "")).startswith("demo:")
                )
                for item in observations
            )
            recommended = assessment.get("recommended_next_focus", [])
            next_focus = [
                item
                for item in [*assessment.get("required_missing", []), *recommended]
                if isinstance(item, str) and item
            ]
            blocking_recommended = {
                "response header observation",
                "public form observation",
                "attack pattern evidence",
                "secret scanning evidence",
                "configuration risk evidence",
            }
            gaps = []
            if not has_demo_evidence:
                gaps = [
                    item
                    for item in next_focus
                    if item in assessment.get("required_missing", [])
                    or item in blocking_recommended
                ]
            if not has_evidence and "工具证据" not in gaps:
                gaps.insert(0, "工具证据")
            is_complete = has_evidence and not gaps
            data = {
                "is_complete": is_complete,
                "confidence": 0.9 if is_complete else 0.0,
                "reason": "存在可追溯工具证据" if is_complete else "缺少工具证据",
                "goal_completed": is_complete,
                "should_continue": bool(gaps),
                "should_report": is_complete,
                "next_focus": list(dict.fromkeys(next_focus)),
                "stop_reason": "evidence_sufficient" if is_complete else None,
                "missing_evidence": (
                    [{"kind": "factual", "description": item} for item in gaps]
                ),
            }
        elif title == "ReportSections":
            data = {
                "summary": payload["goal"],
                "findings": [
                    f"证据 {finding['id']}：{finding['content']}"
                    for finding in payload["findings"]
                ],
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
