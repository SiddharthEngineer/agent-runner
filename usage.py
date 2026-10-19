#!/usr/bin/env python3
"""Session-usage gate for unattended runs: is the Claude "Current session" (5-hour window) below the limit?

The runner's token (`claude setup-token`) is inference-only, so it can't read /usage. Instead this sends one tiny
Haiku prompt and reads the `rate_limit_event` that `claude -p --output-format stream-json` emits
(`unifiedWindows.five_hour.utilization`, 0-1). run.sh calls it only when a queue item is due.

Usage:
  usage.py check     exit 0: below AGENT_MAX_SESSION_USAGE (percent, default 70; 0 disables the check)
                     exit 10: at or above it; stdout's last line is the hours until the window resets
                     exit 11: usage unknown (probe failed); skip this tick
Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time

PROBE_MODEL = os.environ.get("AGENT_USAGE_PROBE_MODEL", "claude-haiku-5-5")


def five_hour_window(lines: list[str]) -> tuple[float, int | None] | None:
    """(utilization 0-1, resetsAt epoch seconds) from stream-json lines, or None if no rate_limit_event has it."""
    found = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info") or {}
        window = (info.get("unifiedWindows") or {}).get("five_hour")
        if window and window.get("utilization") is not None:
            found = (float(window["utilization"]), window.get("resetsAt"))
        elif info.get("rateLimitType") == "five_hour" and info.get("utilization") is not None:
            found = (float(info["utilization"]), info.get("resetsAt"))
    return found


def probe() -> list[str]:
    from buildqueue import STATE as state  # same directory

    state.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(
        ["claude", "-p", "Reply with just: ok", "--model", PROBE_MODEL, "--max-turns", "1",
         "--output-format", "stream-json", "--verbose"],
        cwd=state, capture_output=True, text=True, timeout=120,
    )
    return out.stdout.splitlines()


def cmd_check() -> int:
    limit = float(os.environ.get("AGENT_MAX_SESSION_USAGE", "70") or 0)
    if limit <= 0:
        print("session usage check disabled")
        return 0
    try:
        window = five_hour_window(probe())
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"session usage unknown: {exc}")
        return 11
    if window is None:
        print("session usage unknown: no rate_limit_event in the probe output")
        return 11
    used = window[0] * 100
    resets = window[1]
    when = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(resets)) if resets else "unknown"
    print(f"session usage {used:.0f}% (limit {limit:.0f}%), resets {when}")
    if used < limit:
        return 0
    # Pause until the window resets (at least 5 minutes, one cron tick), or an hour if the reset time is missing.
    hours = max((resets - time.time()) / 3600, 1 / 12) if resets else 1.0
    print(f"{math.ceil(hours * 100) / 100}")
    return 10


def main() -> int:
    if sys.argv[1:] != ["check"]:
        print(__doc__.split("Usage:")[1].split("Standard")[0].rstrip(), file=sys.stderr)
        return 2
    return cmd_check()


if __name__ == "__main__":
    sys.exit(main())
