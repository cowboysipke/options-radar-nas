from datetime import date, datetime, timedelta


def us_session_date_from_china_time(value: datetime) -> date:
    """Map Asia/Shanghai display time to the US cash-session trade date.

    US sessions and their early after-hours finish before noon China time, so the
    local calendar date is one day ahead during that portion of the session.
    """
    return value.date() - timedelta(days=1) if value.hour < 12 else value.date()
