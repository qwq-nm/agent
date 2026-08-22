import asyncio

from secagent.domain import ToolResult
from secagent.tools.base import BaseTool, ToolContext


SCENE_COMPATIBILITY: dict[str, set[str]] = {
    "ctf_web": {"web_analysis"},
}


class ToolRegistry:
    def __init__(self, tools: list[BaseTool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"tool not registered: {name}")
        return self._tools[name]

    async def execute(
        self,
        name: str,
        params: dict,
        context: ToolContext,
    ) -> ToolResult:
        tool = self.get(name)
        if not self._scene_allowed(tool.scene, context.scene):
            raise PermissionError(f"tool {name} not allowed for {context.scene}")
        return await asyncio.wait_for(
            tool.run(params, context), timeout=tool.timeout_seconds
        )

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def describe(self) -> list[dict[str, str]]:
        return [
            tool.spec().__dict__
            for tool in self._tools.values()
        ]

    @staticmethod
    def _scene_allowed(tool_scene: str, context_scene: str) -> bool:
        if tool_scene == context_scene:
            return True
        return tool_scene in SCENE_COMPATIBILITY.get(context_scene, set())
