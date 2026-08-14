from secagent.domain import ModelStage
from secagent.providers.deepseek import _StructuredProvider


class GLMProvider(_StructuredProvider):
    name = "glm"
    allowed_stages = frozenset({ModelStage.TASK_PARSE, ModelStage.REPORT})
    max_tokens = {ModelStage.TASK_PARSE: 2048, ModelStage.REPORT: 4096}
