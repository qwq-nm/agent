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
        (
            "log_type_detector",
            "log_analyzer",
            "attack_pattern_detector",
            "timeline_builder",
        ),
        ("raw_line", "rule_id", "timeline"),
    ),
    TaskScene.SOURCE_AUDIT: ScenePolicy(
        TaskScene.SOURCE_AUDIT,
        (
            "project_detector",
            "source_scanner",
            "secret_scanner",
            "config_checker",
        ),
        ("source_location",),
    ),
    TaskScene.WEB_ANALYSIS: ScenePolicy(
        TaskScene.WEB_ANALYSIS,
        (),
        ("http_observation",),
    ),
}
