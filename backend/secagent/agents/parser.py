import json

from secagent.domain import (
    ModelRequest,
    ModelResponse,
    ModelStage,
    ParsedTask,
    RouteMode,
    TaskRead,
)
from secagent.providers.router import ModelRouter


class TaskParser:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def parse(self, task: TaskRead) -> tuple[ParsedTask, ModelResponse]:
        payload = {
            "goal": task.goal,
            "authorization_scope": task.authorization_scope,
            "scene_hint": task.scene_hint.value if task.scene_hint else None,
            "target_url": task.target_url,
            "inputs": [],
        }
        response = await self.router.complete(
            ModelStage.TASK_PARSE,
            ModelRequest(
                system=(
                    "将授权的网络安全任务解析为严格 JSON。"
                    "如果 scene_hint 已给出，优先采用 scene_hint。"
                    "如果用户目标包含 CTF、靶场、flag、NSSCTF、BUU、题目、Web 题等语义，"
                    "且目标是 URL 或 Web 页面，应归类为 ctf_web；"
                    "普通站点被动观察归类为 web_analysis；日志归类为 incident_response；源码或 ZIP 审计归类为 source_audit。"
                    "如果目标是发现漏洞、验证代码或配置风险，归类为 vulnerability_hunting；"
                    "如果输入是二进制、脚本、固件或需要提取字符串和元数据，归类为 reverse_analysis。"
                    "解析结果必须保留授权范围、限制条件和预期输出。"
                ),
                user=json.dumps(payload, ensure_ascii=False),
                response_schema=ParsedTask.model_json_schema(),
            ),
            preferred=(
                task.preferred_model if task.route_mode is RouteMode.MANUAL else None
            ),
        )
        return ParsedTask.model_validate(response.data), response
