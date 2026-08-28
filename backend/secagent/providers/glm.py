from secagent.domain import ModelStage
from secagent.providers.deepseek import _StructuredProvider


class GLMProvider(_StructuredProvider):
    name = "glm"
    allowed_stages = frozenset(
        {ModelStage.TASK_PARSE, ModelStage.REPORT, ModelStage.SUBTASK_EXECUTE}
    )
    max_tokens = {
        ModelStage.TASK_PARSE: 4096,
        ModelStage.REPORT: 4096,
        ModelStage.SUBTASK_EXECUTE: 4096,
    }
