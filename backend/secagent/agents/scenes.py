from dataclasses import dataclass

from secagent.domain import TaskScene


@dataclass(frozen=True)
class ScenePolicy:
    scene: TaskScene
    allowed_tools: tuple[str, ...]
    completion_evidence: tuple[str, ...]


SCENES = {
    TaskScene.INCIDENT_RESPONSE: ScenePolicy(
        TaskScene.INCIDENT_RESPONSE,
        ("demo_evidence",),
        ("raw_line",),
    ),
    TaskScene.SOURCE_AUDIT: ScenePolicy(
        TaskScene.SOURCE_AUDIT,
        (),
        ("source_location",),
    ),
    TaskScene.WEB_ANALYSIS: ScenePolicy(
        TaskScene.WEB_ANALYSIS,
        (),
        ("http_observation",),
    ),
}
