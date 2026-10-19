#!/usr/bin/env python3
"""Playwright MCP config for the validator's preprod stage: basic-auth credentials and, if present, a signed-in session.

Usage: python3 mcp_config.py write <out.json> --netrc <file> --host <host> [--storage-state <file>]

Writes <out.json> (the `claude --mcp-config` file) and <out>.playwright.json (Playwright's own config, which holds the
basic-auth password), both mode 0600, and prints `signed_in=true|false`. The caller deletes both after the run. Never logs
the credentials. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import netrc
import os
import sys
from pathlib import Path

PLAYWRIGHT_MCP = ["npx", "-y", "@playwright/mcp@0.0.83", "--headless", "--isolated", "--browser", "chromium"]


def write_private(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)  # in case the file already existed with wider permissions


def build(out: Path, netrc_file: Path | None, host: str, storage_state: Path | None) -> bool:
    """Write both files; True when the browser will start signed in."""
    context: dict = {}
    if netrc_file and netrc_file.is_file():
        auth = netrc.netrc(str(netrc_file)).authenticators(host)
        if auth:
            context["httpCredentials"] = {"username": auth[0], "password": auth[2], "origin": f"https://{host}"}
    signed_in = bool(storage_state and storage_state.is_file())
    if signed_in:
        context["storageState"] = str(storage_state)
    pw_config = out.with_suffix(".playwright.json")
    write_private(pw_config, {"browser": {"browserName": "chromium", "isolated": True, "contextOptions": context}})
    write_private(out, {"mcpServers": {"playwright": {"command": PLAYWRIGHT_MCP[0],
                                                      "args": [*PLAYWRIGHT_MCP[1:], "--config", str(pw_config)]}}})
    return signed_in


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("write")
    p.add_argument("out", type=Path)
    p.add_argument("--netrc", type=Path)
    p.add_argument("--host", required=True)
    p.add_argument("--storage-state", type=Path)
    args = parser.parse_args()
    signed_in = build(args.out, args.netrc, args.host, args.storage_state)
    print(f"signed_in={'true' if signed_in else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
