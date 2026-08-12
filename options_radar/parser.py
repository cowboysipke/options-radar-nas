from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Protocol

from .models import FlowEvent, ParsedSignal, RawMessage
from .timeutil import us_session_date_from_china_time


CONTRACT_HEADER = re.compile(
    r"\b([A-Z][A-Z0-9.\-]{0,9})\s+"
    r"(\d{4}-\d{2}-\d{2})\s+"
    r"(\d+(?:\.\d+)?)\s*([CP])\b",
    re.IGNORECASE,
)
RAW_FLOW = re.compile(
    r"\b([A-Z][A-Z0-9.\-]{0,9})\s+"
    r"(\d+(?:\.\d+)?)\s+([CP])\s+"
    r"(\d{4}-\d{2}-\d{2})\s+"
    r"\$([\d,.]+)\s*([KMB])?\s+"
    r"AVG\$([\d,.]+)\s+(\d+)DTE\b",
    re.IGNORECASE,
)
MONEY = re.compile(r"Premium\s*\$([\d,.]+)\s*([KMB])?", re.IGNORECASE)

ANALYST_FAMILIES = {
    "pa": "price_action",
    "mr": "momentum_reversal",
    "qmr": "momentum_reversal",
    "fpd": "flow_positioning",
    "fqd": "flow_positioning",
}


class TextRefiner(Protocol):
    def refine_signal(self, text: str, base: Dict[str, Any]) -> Dict[str, Any]:
        ...


def parse_scaled_number(number: str, suffix: Optional[str] = None) -> float:
    value = float(number.replace(",", ""))
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(
        (suffix or "").upper(), 1
    )
    return value * multiplier


def contract_key(symbol: str, expiry: date, strike: float, option_type: str) -> str:
    strike_text = f"{strike:.6f}".rstrip("0").rstrip(".")
    return f"US.{symbol.upper()}|{expiry.isoformat()}|{strike_text}|{option_type.upper()}"


def flow_event_key(key: str, premium: Optional[float], trade_date: date) -> str:
    # Analyst cards show exact premium while the raw feed abbreviates it (for example
    # $1.5M versus $1,478,877). Daily contract identity therefore omits premium.
    payload = f"{trade_date.isoformat()}|{key}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def parse_flow_message(message: RawMessage) -> Optional[FlowEvent]:
    match = RAW_FLOW.search(message.content)
    if not match:
        return None
    symbol, strike_text, option_type, expiry_text, amount, suffix, avg, dte = match.groups()
    expiry = date.fromisoformat(expiry_text)
    strike = float(strike_text)
    premium = parse_scaled_number(amount, suffix)
    key = contract_key(symbol, expiry, strike, option_type)
    observed = message.source_timestamp or message.observed_at
    session_date = us_session_date_from_china_time(observed)
    return FlowEvent(
        event_key=flow_event_key(key, premium, session_date),
        contract_key=key,
        symbol=symbol.upper(),
        expiry=expiry,
        strike=strike,
        option_type=option_type.upper(),
        premium=premium,
        average_price=float(avg.replace(",", "")),
        dte=int(dte),
        observed_at=observed,
        session_date=session_date,
        raw_message_id=message.id,
    )


def split_contract_messages(text: str) -> List[str]:
    """Split OCR/accessibility text containing multiple Discord cards."""
    matches = sorted(
        list(CONTRACT_HEADER.finditer(text)) + list(RAW_FLOW.finditer(text)),
        key=lambda item: item.start(),
    )
    # One raw-flow line also contains tokens that may partially match another pattern.
    unique_matches = []
    for match in matches:
        if unique_matches and match.start() < unique_matches[-1].end():
            continue
        unique_matches.append(match)
    matches = unique_matches
    if not matches:
        return [text.strip()] if text.strip() else []
    blocks: List[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end].strip(" ,\n")
        if block:
            blocks.append(block)
    return blocks


def _first_number(text: str, labels: Iterable[str]) -> Optional[float]:
    joined = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{joined})\s*[:：]?\s*(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _extract_section(text: str, start_labels: Iterable[str], end_labels: Iterable[str]) -> str:
    starts = "|".join(re.escape(label) for label in start_labels)
    ends = "|".join(re.escape(label) for label in end_labels)
    match = re.search(rf"(?:{starts})\s*(.*?)(?=(?:{ends})|$)", text, re.IGNORECASE | re.DOTALL)
    return " ".join(match.group(1).split()) if match else ""


def _direction(text: str, option_type: str, decision: str, analyst: str) -> (str, str):
    lowered = text.lower()
    if re.search(r"方向\s*[:：]\s*(?:偏空|空)|偏向\s*bear|\bbear\b", lowered):
        return "BEAR", "explicit"
    if re.search(r"方向\s*[:：]\s*(?:偏多|多)|偏向\s*bull|\bbull\b", lowered):
        return "BULL", "explicit"
    if re.search(r"方向\s*[:：]\s*中性|\bneutral\b", lowered):
        return "NEUTRAL", "explicit"
    if analyst == "mr" and decision == "TRADE":
        return ("BULL" if option_type == "C" else "BEAR"), "inferred_from_contract"
    return "UNKNOWN", "missing"


def _decision(text: str) -> str:
    if re.search(r"\bno_trade\b|不交易|结论\s*[:：]\s*观望", text, re.IGNORECASE):
        return "NO_TRADE"
    if re.search(r"\btrade\b|执行观点\s*交易|结论\s*[:：]\s*交易", text, re.IGNORECASE):
        return "TRADE"
    return "WATCH"


def _confidence(text: str) -> (Optional[float], Optional[str]):
    numeric = re.search(r"(?:confidence_score|置信)\s*[:：]?\s*([1-5])\b", text, re.IGNORECASE)
    if numeric:
        raw = numeric.group(1)
        return int(raw) / 5.0, raw
    categorical = re.search(r"信心\s*[:：]\s*(高|中|低)", text)
    if categorical:
        raw = categorical.group(1)
        return {"高": 0.8, "中": 0.6, "低": 0.3}[raw], raw
    return None, None


def _analyst_from(message: RawMessage) -> str:
    value = (message.analyst or message.channel).lower()
    for name in ("qmr", "fqd", "fpd", "mr", "pa"):
        if name in value:
            return name
    return value.strip("#@ ") or "unknown"


def parse_analyst_message(message: RawMessage, refiner: Optional[TextRefiner] = None) -> Optional[ParsedSignal]:
    match = CONTRACT_HEADER.search(message.content)
    if not match:
        return None
    symbol, expiry_text, strike_text, option_type = match.groups()
    expiry = date.fromisoformat(expiry_text)
    strike = float(strike_text)
    option_type = option_type.upper()
    key = contract_key(symbol, expiry, strike, option_type)
    analyst = _analyst_from(message)
    decision = _decision(message.content)
    direction, direction_source = _direction(message.content, option_type, decision, analyst)
    confidence, confidence_raw = _confidence(message.content)

    premium_match = MONEY.search(message.content)
    premium = parse_scaled_number(*premium_match.groups()) if premium_match else None
    dte_value = _first_number(message.content, ["DTE"])
    if "DTE -" in message.content:
        dte_value = None

    entry = _first_number(message.content, ["入场"])
    target = _first_number(message.content, ["目标"])
    stop = _first_number(message.content, ["止损", "破位"])
    win_rate = _first_number(message.content, ["胜率"])
    if win_rate is not None:
        win_rate /= 100.0
    risk_value = _first_number(message.content, ["风险"])
    risk_score = int(risk_value) if risk_value is not None and 1 <= risk_value <= 5 else None
    rationale_text = _extract_section(
        message.content,
        ["价格结构", "执行要点"],
        ["期权结构", "执行计划", "风险提示", "Informational purposes"],
    )
    risk_text = _extract_section(
        message.content,
        ["风险提示"],
        ["Informational purposes"],
    )
    rationale = [rationale_text] if rationale_text else []
    risk_notes = [risk_text] if risk_text else []

    base: Dict[str, Any] = {
        "decision": decision,
        "direction": direction,
        "confidence": confidence,
        "rationale": rationale,
        "risk_notes": risk_notes,
    }
    needs_refinement = bool(
        decision == "WATCH" or direction == "UNKNOWN" or confidence is None or not rationale
        or (analyst in {"pa", "fpd", "fqd"} and any(value is None for value in (entry, target, stop)))
    )
    if refiner and needs_refinement:
        try:
            refined = refiner.refine_signal(message.content, base)
            if isinstance(refined, dict):
                rationale_source = refined.get("rationale", refined.get("reason_summary", rationale)) or rationale
                risk_source = refined.get("risk_notes", refined.get("risk_summary", risk_notes)) or risk_notes
                rationale = [str(x) for x in rationale_source if str(x).strip()]
                risk_notes = [str(x) for x in risk_source if str(x).strip()]
                if decision == "WATCH" and refined.get("decision") in {"TRADE", "NO_TRADE", "WATCH"}:
                    decision = str(refined["decision"])
                if direction == "UNKNOWN" and refined.get("direction") in {"BULL", "BEAR", "NEUTRAL", "UNKNOWN"}:
                    direction = str(refined["direction"])
                    direction_source = "ai_from_text"
                if confidence is None and isinstance(refined.get("confidence"), (int, float)):
                    confidence = max(0.0, min(1.0, float(refined["confidence"])))
                    confidence_raw = "ai"
                if entry is None and isinstance(refined.get("underlying_entry"), (int, float)):
                    entry = float(refined["underlying_entry"])
                if target is None and isinstance(refined.get("underlying_target"), (int, float)):
                    target = float(refined["underlying_target"])
                if stop is None and isinstance(refined.get("underlying_stop"), (int, float)):
                    stop = float(refined["underlying_stop"])
        except Exception as exc:
            risk_notes.append(f"LLM refinement skipped: {type(exc).__name__}")

    known = [decision != "WATCH", direction != "UNKNOWN", confidence is not None, bool(rationale)]
    if analyst in {"pa", "fpd", "fqd"}:
        known.extend([entry is not None, target is not None, stop is not None])
    completeness = sum(1 for item in known if item) / len(known)

    observed = message.source_timestamp or message.observed_at
    session_date = us_session_date_from_china_time(observed)
    return ParsedSignal(
        flow_event_key=flow_event_key(key, premium, session_date),
        contract_key=key,
        symbol=symbol.upper(),
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        decision=decision,
        direction=direction,
        direction_source=direction_source,
        confidence=confidence,
        confidence_raw=confidence_raw,
        analyst_family=ANALYST_FAMILIES.get(analyst, analyst),
        analyst=analyst,
        channel=message.channel,
        observed_at=observed,
        rationale=rationale,
        underlying_entry=entry,
        underlying_target=target,
        underlying_stop=stop,
        premium=premium,
        average_price=None,
        dte=int(dte_value) if dte_value is not None else None,
        win_rate=win_rate,
        risk_score=risk_score,
        risk_notes=risk_notes,
        completeness=completeness,
        raw_message_id=message.id,
    )


def parse_accessibility_messages(tree: str, channel: str, analyst: str, observed_at: datetime) -> List[RawMessage]:
    """Extract top-level Discord message control names from a UI Automation tree."""
    results: List[RawMessage] = []
    pattern = re.compile(r"^\s*\d+\s+消息\s+(.+)$")
    for line in tree.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        content = match.group(1).strip()
        # UIA message names usually end in a localized display timestamp. Keeping it is
        # harmless for parsing and provides dedupe evidence when source time is unavailable.
        results.append(RawMessage(channel=channel, analyst=analyst, observed_at=observed_at, content=content))
    return results


def parse_llm_json(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    return json.loads(cleaned)
