"""Create a local .env from the legacy bot.py inside bot.zip.

The generated .env is intentionally git-ignored.  It may contain live bot/API
secrets from the old deployment, so do not commit it or paste it into chat.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZIP_PATH = ROOT / "bot.zip"
ENV_PATH = ROOT / ".env"


def _find_string(source: str, name: str) -> str:
    match = re.search(rf'^{name}\s*=\s*["\']([^"\']*)["\']', source, re.MULTILINE)
    return match.group(1) if match else ""


def _find_admin_ids(source: str) -> str:
    match = re.search(r"^ADMIN_IDS\s*=\s*\{([^}]*)\}", source, re.MULTILINE)
    if not match:
        return ""
    ids = re.findall(r"\d+", match.group(1))
    return ",".join(ids)


def main() -> None:
    if not ZIP_PATH.exists():
        raise SystemExit(f"Missing {ZIP_PATH}")
    with zipfile.ZipFile(ZIP_PATH) as archive:
        source = archive.read("bot.py").decode("utf-8", errors="replace")

    values = {
        "BOT_TOKEN": _find_string(source, "BOT_TOKEN"),
        "ADMIN_IDS": _find_admin_ids(source),
        "API_BASE_URL": _find_string(source, "API_BASE_URL"),
        "API_KEY": _find_string(source, "API_KEY"),
        "TOPUP_API_URL": _find_string(source, "TOPUP_API_URL"),
        "DATA_DIR": "data",
    }

    lines = [
        "# Generated from bot.zip by scripts/extract_legacy_env.py",
        "# Do not commit this file. Rotate old exposed secrets when possible.",
    ]
    lines.extend(f"{key}={value}" for key, value in values.items())
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {ENV_PATH} with legacy deployment values. Do not commit it.")


if __name__ == "__main__":
    main()
