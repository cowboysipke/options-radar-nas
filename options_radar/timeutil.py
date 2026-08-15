from datetime import date, datetime, timedelta

try:
    from pytz import timezone
    ET = timezone("America/New_York")
except Exception:  # pragma: no cover - pytz is a hard dependency in this project
    ET = None


def us_session_date_from_china_time(value: datetime) -> date:
    """Map Asia/Shanghai display time to the US cash-session trade date.

    US sessions and their early after-hours finish before noon China time, so the
    local calendar date is one day ahead during that portion of the session.
    """
    return value.date() - timedelta(days=1) if value.hour < 12 else value.date()


def _in_et(now: datetime = None) -> datetime:
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is not None:
        return now.astimezone(ET)
    return ET.localize(now)


def is_us_cash_session(now: datetime = None) -> bool:
    """Return True while the US cash session is open (Mon-Fri 09:30-16:00 ET).

    Holidays are not modelled; the trading calendar is intentionally simple.
    """
    current = _in_et(now)
    if current.weekday() >= 5:
        return False
    seconds = current.hour * 3600 + current.minute * 60 + current.second
    return 9 * 3600 + 30 * 60 <= seconds < 16 * 3600


def us_cash_session_label(now: datetime = None) -> str:
    """Human label used on the dashboard: 交易中 / 休市 / 周末休市."""
    current = _in_et(now)
    if current.weekday() >= 5:
        return "周末休市"
    seconds = current.hour * 3600 + current.minute * 60 + current.second
    if seconds < 9 * 3600 + 30 * 60:
        return "休市（未开盘）"
    if seconds < 16 * 3600:
        return "交易中"
    return "休市（已收盘）"
