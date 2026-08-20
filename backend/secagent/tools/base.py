from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from secagent.domain import RiskLevel, ToolResult


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    scene: str
    workspace: Path


class BaseTool(ABC):
    name: str
    scene: str
    risk_level: RiskLevel
    idempotent: bool
    timeout_seconds: float = 30.0

    @abstractmethod
    async def run(self, params: dict, context: ToolContext) -> ToolResult: ...
