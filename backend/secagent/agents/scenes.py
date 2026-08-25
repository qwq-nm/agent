from dataclasses import dataclass

from secagent.domain import TaskScene
from secagent.tools.http_request import HttpRequest


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
    TaskScene.VULNERABILITY_HUNTING: ScenePolicy(
        TaskScene.VULNERABILITY_HUNTING,
        ("vulnerability_scanner",),
        ("vulnerability_finding",),
    ),
    TaskScene.REVERSE_ANALYSIS: ScenePolicy(
        TaskScene.REVERSE_ANALYSIS,
        ("reverse_artifact_analyzer",),
        ("reverse_artifact",),
    ),
    TaskScene.WEB_ANALYSIS: ScenePolicy(
        TaskScene.WEB_ANALYSIS,
        (
            "url_guard",
            HttpRequest.name,
            "header_check",
            "form_extract",
            "link_extract",
            "browser_snapshot",
            "dirsearch_scan",
            "login_probe",
            "sqlmap_probe",
            "robots_analyzer",
            "js_analyzer",
            "path_normalizer",
            "flag_pattern_detector",
            "cookie_analyzer",
            "sensitive_file_checker",
        ),
        ("http_observation",),
    ),
    TaskScene.CTF_WEB: ScenePolicy(
        TaskScene.CTF_WEB,
        (
            "url_guard",
            HttpRequest.name,
            "header_check",
            "form_extract",
            "link_extract",
            "browser_snapshot",
            "dirsearch_scan",
            "login_probe",
            "sqlmap_probe",
            "robots_analyzer",
            "js_analyzer",
            "path_normalizer",
            "flag_pattern_detector",
            "cookie_analyzer",
            "sensitive_file_checker",
        ),
        ("http_observation",),
    ),
}
