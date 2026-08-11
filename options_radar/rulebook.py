from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional

from .db import Database
from .models import RawMessage


DEFAULT_FAMILIES = {
    "pa": "price_action",
    "mr": "momentum_reversal",
    "qmr": "momentum_reversal",
    "fpd": "flow_positioning",
    "fqd": "flow_positioning",
}

TERM_PATTERNS = {
    "bull": ("BULL", "看多方向"),
    "bear": ("BEAR", "看空方向"),
    "neutral": ("NEUTRAL", "中性或等待"),
    "confidence": ("CONFIDENCE", "分析师信心"),
    "win rate": ("WIN_RATE", "历史样本胜率"),
    "胜率": ("WIN_RATE", "历史样本胜率"),
    "风险": ("RISK", "风险等级或风险说明"),
    "入场": ("ENTRY", "标的触发或入场条件"),
    "止损": ("STOP", "信号失效或止损条件"),
    "目标": ("TARGET", "目标价格或退出条件"),
}


@dataclass
class CompiledRulebook:
    version: str
    source_hash: str
    rules: List[Dict[str, str]] = field(default_factory=list)
    analyst_profiles: Dict[str, Dict[str, str]] = field(default_factory=dict)
    terminology: Dict[str, Dict[str, str]] = field(default_factory=dict)


class RulebookCompiler:
    """Compile Discord guide/subscription messages into an auditable rule version."""

    def __init__(self, database: Database, family_map: Optional[Mapping[str, str]] = None):
        self.database = database
        self.family_map = {**DEFAULT_FAMILIES, **dict(family_map or {})}

    @staticmethod
    def normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text or "")
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())

    def compile(self, messages: Iterable[RawMessage]) -> CompiledRulebook:
        normalized = []
        for message in messages:
            content = self.normalize(message.content)
            if content:
                normalized.append((message, content))
        canonical = "\n---\n".join(
            f"{item.channel}|{item.analyst}|{content}"
            for item, content in sorted(normalized, key=lambda pair: (pair[0].channel, pair[0].observed_at))
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        stamp = max((item.observed_at for item, _ in normalized), default=datetime.utcnow())
        version = f"{stamp:%Y%m%d}-{digest[:10]}"
        compiled = CompiledRulebook(version=version, source_hash=digest)

        for message, content in normalized:
            source_key = f"{message.channel}:{message.analyst}"
            self.database.save_source_rule(
                source_key=source_key,
                rule_version=version,
                title=message.channel,
                content=content,
                screenshot_path=message.screenshot_path,
                observed_at=message.observed_at,
            )
            compiled.rules.append({"source": source_key, "content": content})
            analyst = message.analyst.lower().strip()
            profile_names = [analyst] if analyst in self.family_map else [
                name for name in self.family_map
                if re.search(rf"(?<![a-z]){re.escape(name)}(?:分析师)?(?![a-z])", content, re.IGNORECASE)
            ]
            for profile_name in profile_names:
                profile = {
                    "analyst": profile_name,
                    "family": self.family_map[profile_name],
                    "summary": self._summary(content),
                }
                compiled.analyst_profiles[profile_name] = profile
                self.database.save_analyst_profile(
                    profile_name, profile["family"], profile, version
                )
            lowered = content.casefold()
            for term, (normalized_value, explanation) in TERM_PATTERNS.items():
                if term.casefold() in lowered:
                    compiled.terminology[term] = {
                        "normalized_value": normalized_value,
                        "explanation": explanation,
                    }

        with self.database.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO execution_rule_versions
                (version, rules_json, source_hash, status, created_at)
                VALUES (?, ?, ?, 'active', ?)""",
                (version, json.dumps({
                    "rules": compiled.rules,
                    "profiles": compiled.analyst_profiles,
                    "terminology": compiled.terminology,
                }, ensure_ascii=False), digest, datetime.utcnow().isoformat()),
            )
            for term, payload in compiled.terminology.items():
                connection.execute(
                    """INSERT OR REPLACE INTO terminology_mappings
                    (term, rule_version, normalized_value, explanation, updated_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (term, version, payload["normalized_value"], payload["explanation"],
                     datetime.utcnow().isoformat()),
                )
        return compiled

    @staticmethod
    def _summary(text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        return text[:240]
