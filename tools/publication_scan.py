#!/usr/bin/env python3
"""Fail closed if private host evidence enters the public AAG - InputLock tree."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = {
    "engineering",
    "platform",
    "PRODUCTION-ACCEPTANCE-20260911.md",
    "PROMPT_FOR_PRO.md",
    "reports",
    "artifacts",
}
local_user = "aag" + "-linux"
data_root = "/mnt" + "/data"
private_home = "/home/" + local_user
known_serial = "24451B" + "401863"
remote_id = "f7c3c1b4" + "-fa31-40dc-bae8-f6bb327fa324"
PATTERNS = {
    "local-user": re.compile(r"\\b" + re.escape(local_user) + r"\\b"),
    "private-data-path": re.compile(re.escape(data_root) + r"(?:/|\\b)"),
    "private-home-path": re.compile(re.escape(private_home) + r"(?:/|\\b)"),
    "known-device-serial": re.compile(re.escape(known_serial)),
    "dated-private-backup": re.compile(r"/var/backups/input-lock/\\d{8}T\\d{6}Z"),
    "remote-device-id": re.compile(re.escape(remote_id)),
}

errors = []
for rel in FORBIDDEN_PATHS:
    if (ROOT / rel).exists():
        errors.append(f"forbidden public path: {rel}")

skip_parts = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build"}
for path in ROOT.rglob("*"):
    if not path.is_file() or any(part in skip_parts for part in path.parts):
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    rel = path.relative_to(ROOT)
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            errors.append(f"{label}: {rel}")

if errors:
    print("PUBLICATION_SCAN=FAIL")
    for item in sorted(set(errors)):
        print(item)
    raise SystemExit(1)

print("PUBLICATION_SCAN=PASS")
