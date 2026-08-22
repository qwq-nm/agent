from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from secagent.domain import RiskLevel, ToolResult


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    scene: str
    workspace: Path


@dataclass(frozen=True)
class ToolSpec:
    name: str
    scene: str
    risk_level: str
    description: str
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    requires_human_confirm: bool = False
    idempotent: bool = True
    timeout_seconds: float = 30.0


class BaseTool(ABC):
    name: str
    scene: str
    risk_level: RiskLevel
    idempotent: bool
    timeout_seconds: float = 30.0
    description: str = "安全分析工具"
    input_schema: dict = {}
    output_schema: dict = {}
    requires_human_confirm: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            scene=self.scene,
            risk_level=self.risk_level.value,
            description=self.description,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            requires_human_confirm=self.requires_human_confirm,
            idempotent=self.idempotent,
            timeout_seconds=self.timeout_seconds,
        )

    @abstractmethod
    async def run(self, params: dict, context: ToolContext) -> ToolResult: ...
