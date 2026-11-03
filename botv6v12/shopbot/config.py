"""Environment-driven configuration for the Telegram shop bot.

Production deployments only need this layout::

    bot.py
    .env
    shopbot/
    data/

The loader reads ``.env`` next to ``bot.py``/the project root first, then normal
environment variables.  ``bot.zip`` support is only a local upgrade fallback and
is not required on the real server.
"""

from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BotConfig:
    bot_token: str
    admin_ids: set[int]
    api_base_url: str
    api_key: str
    topup_api_url: str
    data_dir: Path
    fishy_api_base_url: str
    fishy_api_key: str
    fishy_low_balance_threshold: int
    sachtai_api_base_url: str
    sachtai_api_key: str


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FISHYMARKET_DEFAULT_BASE_URL = "https://footbath-onyx-obtain.ngrok-free.dev/api/docs"
_REQUIRED_ENV = ("BOT_TOKEN", "ADMIN_IDS", "API_BASE_URL", "API_KEY", "TOPUP_API_URL")
_LAST_ENV_FILES_CHECKED: list[Path] = []
_LAST_ENV_FILES_LOADED: list[Path] = []


def _set_env_if_missing(key: str, value: str) -> None:
    if key and value and key not in os.environ:
        os.environ[key] = value


def env_file_candidates(path: Path | None = None) -> list[Path]:
    """Return .env candidates in the order used by the config loader."""

    if path is not None:
        return [path]

    candidates = [
        Path.cwd() / ".env",
        PROJECT_ROOT / ".env",
        Path.cwd() / ".env.example",
        PROJECT_ROOT / ".env.example",
    ]
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            unique.append(candidate)
            seen.add(resolved)
    return unique


def load_dotenv(path: Path | None = None) -> bool:
    """Load KEY=VALUE pairs from .env without overriding real environment vars."""

    global _LAST_ENV_FILES_CHECKED, _LAST_ENV_FILES_LOADED
    candidates = env_file_candidates(path)
    _LAST_ENV_FILES_CHECKED = candidates
    _LAST_ENV_FILES_LOADED = []

    for candidate in candidates:
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            _set_env_if_missing(key, value)
        _LAST_ENV_FILES_LOADED.append(candidate)
    return bool(_LAST_ENV_FILES_LOADED)


def _find_string(source: str, name: str) -> str:
    match = re.search(rf'^{name}\s*=\s*["\']([^"\']*)["\']', source, re.MULTILINE)
    return match.group(1) if match else ""


def _find_admin_ids(source: str) -> str:
    match = re.search(r"^ADMIN_IDS\s*=\s*\{([^}]*)\}", source, re.MULTILINE)
    if not match:
        return ""
    return ",".join(re.findall(r"\d+", match.group(1)))


def load_legacy_zip_env(zip_path: Path | None = None) -> bool:
    """Local upgrade fallback: read config from bot.zip/bot.py if it exists."""

    candidate = zip_path or PROJECT_ROOT / "bot.zip"
    if not candidate.exists():
        return False
    with zipfile.ZipFile(candidate) as archive:
        source = archive.read("bot.py").decode("utf-8", errors="replace")
    values = {
        "BOT_TOKEN": _find_string(source, "BOT_TOKEN"),
        "ADMIN_IDS": _find_admin_ids(source),
        "API_BASE_URL": _find_string(source, "API_BASE_URL"),
        "API_KEY": _find_string(source, "API_KEY"),
        "TOPUP_API_URL": _find_string(source, "TOPUP_API_URL"),
        "DATA_DIR": "data",
    }
    for key, value in values.items():
        _set_env_if_missing(key, value)
    return True


def missing_required_env() -> list[str]:
    return [name for name in _REQUIRED_ENV if not os.getenv(name, "").strip()]


def _format_paths(paths: list[Path]) -> str:
    if not paths:
        return "không có"
    return ", ".join(str(path) for path in paths)


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        checked = _format_paths(_LAST_ENV_FILES_CHECKED)
        loaded = _format_paths(_LAST_ENV_FILES_LOADED)
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Cấu trúc chạy đúng là: bot.py, .env, shopbot/, data/ cùng một thư mục. "
            f"Các file env đã kiểm tra: {checked}. File env đã đọc được: {loaded}. "
            "Hãy kiểm tra file phải tên chính xác là .env (không phải .env.txt) và có dòng dạng BOT_TOKEN=..."
        )
    return value


def parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if part.isdigit():
            ids.add(int(part))
    if not ids:
        raise RuntimeError("ADMIN_IDS must contain at least one numeric Telegram user id")
    return ids


def load_config() -> BotConfig:
    load_dotenv()
    if missing_required_env():
        load_legacy_zip_env()
    return BotConfig(
        bot_token=env_required("BOT_TOKEN"),
        admin_ids=parse_admin_ids(env_required("ADMIN_IDS")),
        api_base_url=env_required("API_BASE_URL"),
        api_key=env_required("API_KEY"),
        topup_api_url=env_required("TOPUP_API_URL"),
        data_dir=Path(os.getenv("DATA_DIR", "data").strip() or "data"),
        # FishyMarket is independent of the legacy Shop API.  Its documented
        # deployment includes the /api/docs prefix before buyer endpoints.
        fishy_api_base_url=os.getenv("FISHY_API_BASE_URL", FISHYMARKET_DEFAULT_BASE_URL).strip(),
        fishy_api_key=os.getenv("FISHY_API_KEY", os.getenv("API_KEY", "")).strip(),
        fishy_low_balance_threshold=int(os.getenv("FISHY_LOW_BALANCE_THRESHOLD", "100000") or 100000),
        sachtai_api_base_url=os.getenv("SACHTAI_API_BASE_URL", "https://bot.sachtai.com").strip(),
        sachtai_api_key=os.getenv("SACHTAI_API_KEY", "").strip(),
    )
