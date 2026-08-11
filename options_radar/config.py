from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml


ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


@dataclass
class AppConfig:
    root: Path
    raw: Dict[str, Any]

    @property
    def database_path(self) -> Path:
        return self.resolve(self.raw.get("database_path", "data/options_radar.db"))

    @property
    def evidence_dir(self) -> Path:
        return self.resolve(self.raw.get("evidence_dir", "evidence"))

    def resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def section(self, name: str) -> Dict[str, Any]:
        return dict(self.raw.get(name, {}))


def load_config(path: str) -> AppConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return AppConfig(root=config_path.parent, raw=_expand(raw))
