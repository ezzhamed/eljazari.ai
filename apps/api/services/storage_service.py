"""eljazari.ai — Storage Service (local filesystem / S3)"""

import json
import logging
from pathlib import Path
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)


class StorageService:
    def __init__(self) -> None:
        self.base_path = Path(settings.storage_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def project_path(self, project_id: str) -> Path:
        path = self.base_path / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_design(self, project_id: str, design: dict) -> Path:
        path = self.project_path(project_id) / "design.json"
        path.write_text(json.dumps(design, indent=2))
        return path

    def save_code(self, project_id: str, code: str, iteration: int = 0) -> Path:
        path = self.project_path(project_id) / f"mission_v{iteration}.py"
        path.write_text(code)
        return path

    def save_report(self, project_id: str, report: dict) -> Path:
        path = self.project_path(project_id) / "performance_report.json"
        path.write_text(json.dumps(report, indent=2))
        return path

    def load_json(self, project_id: str, filename: str) -> dict[str, Any] | None:
        path = self.project_path(project_id) / filename
        if not path.exists():
            return None
        return json.loads(path.read_text())
