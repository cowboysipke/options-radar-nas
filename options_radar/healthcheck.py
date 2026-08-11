"""Docker health probe for the private NAS runtime."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib.request import urlopen


def main() -> None:
    port = int(os.getenv("SETUP_PORT", "8787"))
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        heartbeat = datetime.fromisoformat(str(data["last_heartbeat"]).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds()
        if response.status != 200 or age > 90 or data.get("status") not in {"ok", "setup", "degraded"}:
            raise RuntimeError("stale runtime")
    except Exception as exc:
        print(f"unhealthy:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
    print("healthy")


if __name__ == "__main__":
    main()

