import asyncio
import os
import json
import time
import re
import secrets
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    BotCommand,
    InlineKeyboardMarkup,
    URLInputFile,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shopbot.admin_panel import admin_panel_kb, admin_panel_text
from shopbot.config import FISHYMARKET_DEFAULT_BASE_URL, load_config
from shopbot.paths import LegacyPaths


# ================== CONFIG ==================
APP_CONFIG = load_config()
DATA_PATHS = LegacyPaths(APP_CONFIG.data_dir)
DATA_PATHS.ensure()

BOT_TOKEN = APP_CONFIG.bot_token
ADMIN_IDS = APP_CONFIG.admin_ids

# ===== SHOP BOT API CONFIG =====
API_BASE_URL = APP_CONFIG.api_base_url
API_KEY = APP_CONFIG.api_key
FISHY_API_BASE_URL = APP_CONFIG.fishy_api_base_url
FISHY_API_KEY = APP_CONFIG.fishy_api_key
FISHY_LOW_BALANCE_THRESHOLD = APP_CONFIG.fishy_low_balance_threshold
SACHTAI_API_BASE_URL = APP_CONFIG.sachtai_api_base_url
SACHTAI_API_KEY = APP_CONFIG.sachtai_api_key

DATA_DIR = str(DATA_PATHS.data_dir)
PRODUCT_DATA_DIR = str(DATA_PATHS.product_data_dir)
RESERVE_DIR = str(DATA_PATHS.reserve_dir)
ORDERS_DATA_DIR = str(DATA_PATHS.orders_data_dir)
WARRANTY_DIR = str(DATA_PATHS.warranty_dir)
WARRANTY_ARCHIVE_DIR = str(DATA_PATHS.warranty_archive_dir)
DIRECT_PAY_DIR = str(DATA_PATHS.direct_pay_dir)

# ===== AUTO TOPUP CONFIG =====
MA_NAP = "NAP"
TOPUP_API_URL = APP_CONFIG.topup_api_url
TOPUP_POLL_SECONDS = 5
TOPUP_CONTROL_PATH = os.path.join(DATA_DIR, "topup_control.json")

PROCESSED_TX_PATH = os.path.join(DATA_DIR, "processed_topup_tx.json")
PROCESSED_TX_LOCK = asyncio.Lock()

# ===== ORDER CODE CONFIG =====
ORDER_PREFIX = "ORDER"

# ===== PRODUCT DETAIL CONFIG =====
PRODUCT_DETAILS_PATH = os.path.join(DATA_DIR, "product_details.json")

# ===== RESERVE CONFIG =====
RESERVE_HOLD_SECONDS = 600  # 10 phút
BOT_INSTANCE_LOCK_PATH = os.path.join(DATA_DIR, "bot.instance.lock")


# ================== GLOBALS ==================
USER_LOCKS: dict[int, asyncio.Lock] = {}
PRODUCTS_LOCK = asyncio.Lock()
USERS_LOCK = asyncio.Lock()
PRODUCT_FILE_LOCKS: dict[str, asyncio.Lock] = {}
RESERVE_TASKS: dict[str, asyncio.Task] = {}
DETAILS_LOCK = asyncio.Lock()
MANUAL_ORDERS_LOCK = asyncio.Lock()
CATEGORIES_LOCK = asyncio.Lock()
AUTO_ORDERS_LOCK = asyncio.Lock()
EMAIL_CHANGE_LOCK = asyncio.Lock()
API_SETTINGS_LOCK = asyncio.Lock()
API_PURCHASE_LOCKS: dict[str, asyncio.Lock] = {}
WALLET_PAY_LOCKS: dict[int, asyncio.Lock] = {}
ORDER_POLL_TASKS: dict[str, asyncio.Task] = {}
ORDER_LOCKS: dict[str, asyncio.Lock] = {}

MANUAL_ORDERS_PATH = os.path.join(DATA_DIR, "manual_orders.json")
CATEGORIES_PATH = os.path.join(DATA_DIR, "categories.json")
AUTO_ORDERS_PATH = os.path.join(DATA_DIR, "auto_orders.json")
EMAIL_CHANGE_REQUESTS_PATH = os.path.join(DATA_DIR, "email_change_requests.json")
API_SETTINGS_PATH = os.path.join(DATA_DIR, "api_settings.json")


TOPUP_ENABLED = True

ADMIN_STATE: dict[int, dict[str, Any]] = {}
ADMIN_REPLY_STATE: dict[int, dict[str, Any]] = {}
ADMIN_LAST_ADDED: dict[int, dict[str, Any]] = {}
BUY_STATE: dict[int, dict[str, Any]] = {}
WARRANTY_STATE: dict[int, dict[str, Any]] = {}
API_FAIL_STATE: dict[str, dict[str, Any]] = {}  # key=f"{uid}:{pid}" -> fail info

# map tin bot gửi cho khách để khách reply tiếp admin
USER_WARRANTY_REPLY_MAP: dict[str, str] = {}  # key=f"{uid}:{bot_msg_id}" -> order_code

BOT_INSTANCE_LOCK_FD: Optional[int] = None


def acquire_bot_instance_lock() -> None:
    """Ngăn chạy trùng nhiều process bot trên cùng máy (giảm Conflict polling)."""
    global BOT_INSTANCE_LOCK_FD
    try:
        fd = os.open(BOT_INSTANCE_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        BOT_INSTANCE_LOCK_FD = fd
    except FileExistsError:
        raise RuntimeError(
            "Phát hiện bot instance khác đang chạy (lock file tồn tại). "
            "Hãy tắt process cũ hoặc xóa file lock nếu là lock rác."
        )


def release_bot_instance_lock() -> None:
    global BOT_INSTANCE_LOCK_FD
    try:
        if BOT_INSTANCE_LOCK_FD is not None:
            os.close(BOT_INSTANCE_LOCK_FD)
            BOT_INSTANCE_LOCK_FD = None
    except Exception:
        pass
    try:
        if os.path.exists(BOT_INSTANCE_LOCK_PATH):
            os.remove(BOT_INSTANCE_LOCK_PATH)
    except Exception:
        pass

PRODUCTS_PATH = os.path.join(DATA_DIR, "products.json")
USERS_PATH = os.path.join(DATA_DIR, "users.json")


# ================== HELPERS ==================
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def now_date() -> str:
    return datetime.now().strftime("%d/%m/%Y")

def now_datetime_full() -> str:
    return datetime.now().strftime("%H:%M:%S %d/%m/%Y")

def alert_day_key() -> str:
    # Fixed UTC+7: VN has no DST, so this avoids a tzdata dependency.
    return datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")

def utc_iso_z() -> str:
    return datetime.utcnow().isoformat() + "Z"

def vnd(n: int) -> str:
    return f"{n:,}".replace(",", ".")

def _atomic_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in USER_LOCKS:
        USER_LOCKS[user_id] = asyncio.Lock()
    return USER_LOCKS[user_id]

def _prod_lock(product_id: str) -> asyncio.Lock:
    if product_id not in PRODUCT_FILE_LOCKS:
        PRODUCT_FILE_LOCKS[product_id] = asyncio.Lock()
    return PRODUCT_FILE_LOCKS[product_id]

async def notify_admin(bot: Bot, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if not ADMIN_IDS:
        return
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=reply_markup)
        except Exception:
            pass

async def notify_admin_photo(bot: Bot, photo: str, caption: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if not ADMIN_IDS:
        return
    for aid in ADMIN_IDS:
        try:
            await bot.send_photo(aid, photo=photo, caption=caption, reply_markup=reply_markup)
        except Exception:
            pass

async def safe_cb_answer(cb: CallbackQuery) -> None:
    """Trả lời callback an toàn, tránh crash khi query đã quá hạn/invalid."""
    try:
        await cb.answer()
    except Exception:
        pass


def nap_code(user_id: int) -> str:
    return f"{MA_NAP}{user_id}"

def extract_user_id_from_description(desc: str) -> Optional[int]:
    if not desc:
        return None
    m = re.search(rf"{re.escape(MA_NAP)}(\d{{5,20}})", desc, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

def extract_order_code_from_description(desc: str) -> Optional[str]:
    if not desc:
        return None
    d = desc.upper()
    # Ưu tiên đúng format mã đơn do bot tạo: ORDER + 8 HEX + 10 số timestamp.
    m = re.search(rf"({re.escape(ORDER_PREFIX)}[0-9A-F]{{8}}\d{{10}})", d)
    if m:
        return m.group(1).strip()

    # Fallback nếu có format cũ/khác: bắt từ ORDER tới khi gặp ký tự không phải [A-Z0-9].
    m2 = re.search(rf"({re.escape(ORDER_PREFIX)}[A-Z0-9]+)", d)
    if m2:
        return m2.group(1).strip()
    return None

def reserve_path(uid: int, pid: str) -> str:
    return os.path.join(RESERVE_DIR, f"{uid}_{pid}.json")

def warranty_path(order_code: str) -> str:
    return os.path.join(WARRANTY_DIR, f"{order_code}.json")

def warranty_archive_path(order_code: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(WARRANTY_ARCHIVE_DIR, f"{order_code}_{ts}.txt")

def direct_pay_path(order_code: str) -> str:
    return os.path.join(DIRECT_PAY_DIR, f"{order_code}.json")


# ================== TOPUP CONTROL ==================
async def load_topup_enabled() -> bool:
    global TOPUP_ENABLED
    if not os.path.exists(TOPUP_CONTROL_PATH):
        _atomic_write_json(TOPUP_CONTROL_PATH, {"enabled": True})
        TOPUP_ENABLED = True
        return True
    try:
        with open(TOPUP_CONTROL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        TOPUP_ENABLED = bool(data.get("enabled", True))
        return TOPUP_ENABLED
    except Exception:
        _atomic_write_json(TOPUP_CONTROL_PATH, {"enabled": True})
        TOPUP_ENABLED = True
        return True

async def save_topup_enabled(enabled: bool) -> None:
    global TOPUP_ENABLED
    TOPUP_ENABLED = bool(enabled)
    _atomic_write_json(TOPUP_CONTROL_PATH, {"enabled": TOPUP_ENABLED})



# ================== PRODUCT DETAILS ==================
async def load_product_details() -> dict[str, str]:
    async with DETAILS_LOCK:
        if not os.path.exists(PRODUCT_DETAILS_PATH):
            _atomic_write_json(PRODUCT_DETAILS_PATH, {})
            return {}
        try:
            with open(PRODUCT_DETAILS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError()
            return {str(k): str(v) for k, v in data.items()}
        except Exception:
            _atomic_write_json(PRODUCT_DETAILS_PATH, {})
            return {}

async def save_product_details(details: dict[str, str]) -> None:
    async with DETAILS_LOCK:
        _atomic_write_json(PRODUCT_DETAILS_PATH, details)

async def get_product_detail(product_id: str) -> str:
    details = await load_product_details()
    return str(details.get(product_id, "")).strip()

async def set_product_detail(product_id: str, text: str) -> None:
    details = await load_product_details()
    details[product_id] = text.strip()
    await save_product_details(details)

async def remove_product_detail(product_id: str) -> None:
    details = await load_product_details()
    if product_id in details:
        details.pop(product_id, None)
        await save_product_details(details)


# ================== USERS LIST ==================
async def load_all_users() -> list[int]:
    if not os.path.exists(USERS_PATH):
        _atomic_write_json(USERS_PATH, [])
        return []
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError()
        return [int(x) for x in data if str(x).isdigit()]
    except Exception:
        _atomic_write_json(USERS_PATH, [])
        return []

async def add_user_global(user_id: int) -> None:
    async with USERS_LOCK:
        users = await load_all_users()
        if user_id not in users:
            users.append(user_id)
            _atomic_write_json(USERS_PATH, users)

async def total_members() -> int:
    users = await load_all_users()
    return len(users)


# ================== USER STORAGE ==================
def _user_path(user_id: int) -> str:
    return os.path.join(DATA_DIR, f"{user_id}.json")

def default_user_data(user_id: int) -> dict[str, Any]:
    return {
        "id": user_id,
        "username": None,
        "full_name": None,
        "created_at": now_date(),
        "last_seen": now_datetime_full(),
        "balance": 0,
        "stats": {"total_orders": 0, "done_orders": 0, "total_spent": 0},
        "orders": [],
        "wallet_logs": [],
        "lang": "vi"
    }

async def load_user(user_id: int) -> dict[str, Any]:
    path = _user_path(user_id)
    lock = _get_user_lock(user_id)
    async with lock:
        if not os.path.exists(path):
            data = default_user_data(user_id)
            _atomic_write_json(path, data)
            return data
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = default_user_data(user_id)
            _atomic_write_json(path, data)
            return data

        base = default_user_data(user_id)
        for k, v in base.items():
            data.setdefault(k, v)
        data["id"] = user_id

        if "stats" not in data or not isinstance(data["stats"], dict):
            data["stats"] = base["stats"]
        for sk, sv in base["stats"].items():
            data["stats"].setdefault(sk, sv)

        if "orders" not in data or not isinstance(data["orders"], list):
            data["orders"] = []
        if "wallet_logs" not in data or not isinstance(data["wallet_logs"], list):
            data["wallet_logs"] = []
        return data

async def save_user(user_id: int, data: dict[str, Any]) -> None:
    path = _user_path(user_id)
    lock = _get_user_lock(user_id)
    async with lock:
        _atomic_write_json(path, data)

async def touch_user_profile(user_id: int, username: Optional[str], full_name: Optional[str]) -> None:
    data = await load_user(user_id)
    if username is not None:
        data["username"] = username
    if full_name is not None:
        data["full_name"] = full_name
    data["last_seen"] = now_datetime_full()
    await save_user(user_id, data)



# ================== CATEGORIES / API / AUTO ORDERS ==================
PRODUCT_TYPE_NORMAL = "local"
PRODUCT_TYPE_AUTO_API = "api"
API_PROVIDER_FISHYMARKET = "FISHYMARKET"
API_PROVIDER_SACHTAI = "SACHTAI"
API_PROVIDER_LEGACY = "LEGACY"
DEPRECATED_FISHYMARKET_BASE_URL = "https://untruthful-kimi-nonidyllic.ngrok-free.dev"
SACHTAI_CAPCUT_ADD_TEAM_KEY = "CAPCUT_PRO_30D_ADD_TEAM_API"
PRODUCT_TYPE_MANUAL = "dontay"
ORDER_STATUS_PROCESSING = "PROCESSING"
ORDER_STATUS_COMPLETED = "COMPLETED"
ORDER_STATUS_FAILED = "FAILED"
ORDER_STATUS_REFUNDED = "REFUNDED"
ORDER_STATUS_CANCELLED = "CANCELLED"
EMAIL_CHANGE_WINDOW_SECONDS = 24 * 3600
EMAIL_CHANGE_MAX_APPROVED = 2
API_SYNC_INTERVAL_SECONDS = 300
AUTO_ORDER_POLL_INTERVAL_SECONDS = 30
AUTO_ORDER_MAX_POLLS = 15

# Luôn chừa lại 1 đơn vị ở nguồn: nguồn 10 -> bot max 9, nguồn 1 -> bot 0
API_STOCK_SAFETY_BUFFER = 1


def normalize_api_base_url(raw: str) -> str:
    # /api/docs là phần thật của base URL FishyMarket, không được cắt bỏ.
    return str(raw or "").strip().rstrip("/")


def usable_provider_stock(raw_stock: Any) -> int:
    try:
        value = int(raw_stock or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value - API_STOCK_SAFETY_BUFFER)


def now_unix() -> int:
    return int(time.time())


def is_valid_email(value: str) -> bool:
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", value.strip()))


def _default_categories() -> list[dict[str, Any]]:
    ts = now_datetime_full()
    return [
        {"id": "cat_youtube", "name": "YouTube", "icon": "📺", "description": "", "sort_order": 10, "is_active": True, "created_at": ts, "updated_at": ts},
        {"id": "cat_canva", "name": "Canva", "icon": "🎨", "description": "", "sort_order": 20, "is_active": True, "created_at": ts, "updated_at": ts},
        {"id": "cat_capcut", "name": "CapCut", "icon": "🎬", "description": "", "sort_order": 30, "is_active": True, "created_at": ts, "updated_at": ts},
        {"id": "cat_vpn", "name": "VPN", "icon": "🔐", "description": "", "sort_order": 40, "is_active": True, "created_at": ts, "updated_at": ts},
        {"id": "cat_other", "name": "Khác", "icon": "🛍", "description": "", "sort_order": 999, "is_active": True, "created_at": ts, "updated_at": ts},
    ]


async def load_categories() -> list[dict[str, Any]]:
    async with CATEGORIES_LOCK:
        if not os.path.exists(CATEGORIES_PATH):
            cats = _default_categories()
            _atomic_write_json(CATEGORIES_PATH, cats)
            return cats
        try:
            with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError()
        except Exception:
            data = _default_categories()
            _atomic_write_json(CATEGORIES_PATH, data)
        out = []
        for i, c in enumerate(data):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id", "")).strip() or f"cat_{i}_{int(time.time())}"
            out.append({
                "id": cid,
                "name": str(c.get("name", cid)).strip() or cid,
                "icon": str(c.get("icon", "📁")).strip() or "📁",
                "description": str(c.get("description", "")),
                "sort_order": int(c.get("sort_order", i * 10) or 0),
                "is_active": bool(c.get("is_active", True)),
                "created_at": str(c.get("created_at", now_datetime_full())),
                "updated_at": str(c.get("updated_at", now_datetime_full())),
            })
        # Always ensure cat_other exists — products default to it and the
        # user-facing "Khác" fallback button sends cat:cat_other callbacks.
        if not any(c.get("id") == "cat_other" for c in out):
            ts = now_datetime_full()
            out.append({
                "id": "cat_other",
                "name": "Khác",
                "icon": "🛍",
                "description": "",
                "sort_order": 999,
                "is_active": True,
                "created_at": ts,
                "updated_at": ts,
            })
        return sorted(out, key=lambda x: (int(x.get("sort_order", 999)), str(x.get("name", ""))))


async def save_categories(categories: list[dict[str, Any]]) -> None:
    async with CATEGORIES_LOCK:
        _atomic_write_json(CATEGORIES_PATH, categories)


def gen_category_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower()).strip("_") or "category"
    return f"cat_{slug}_{int(time.time()*1000)}"


async def ensure_category(name: str, icon: str = "📁") -> str:
    cats = await load_categories()
    for c in cats:
        if str(c.get("name", "")).strip().lower() == name.strip().lower():
            return str(c.get("id"))
    now = now_datetime_full()
    cid = gen_category_id(name)
    cats.append({"id": cid, "name": name.strip(), "icon": icon, "description": "", "sort_order": max([int(c.get("sort_order", 0)) for c in cats] + [0]) + 10, "is_active": True, "created_at": now, "updated_at": now})
    await save_categories(cats)
    return cid


async def load_api_settings() -> dict[str, Any]:
    async with API_SETTINGS_LOCK:
        if not os.path.exists(API_SETTINGS_PATH):
            data = {"base_url": FISHY_API_BASE_URL, "api_key": FISHY_API_KEY, "low_balance_threshold": FISHY_LOW_BALANCE_THRESHOLD, "updated_at": now_datetime_full()}
            _atomic_write_json(API_SETTINGS_PATH, data)
            return data
        try:
            with open(API_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError()
        except Exception:
            data = {}
        data.setdefault("base_url", FISHY_API_BASE_URL)
        data.setdefault("api_key", FISHY_API_KEY)
        data.setdefault("low_balance_threshold", FISHY_LOW_BALANCE_THRESHOLD)
        data.setdefault("providers", {})
        providers = data["providers"] if isinstance(data.get("providers"), dict) else {}
        providers.setdefault("FISHYMARKET", {"base_url": FISHY_API_BASE_URL, "api_key": FISHY_API_KEY})
        providers.setdefault("SACHTAI", {"base_url": SACHTAI_API_BASE_URL, "api_key": SACHTAI_API_KEY})
        data["providers"] = providers
        return data


async def save_api_settings(data: dict[str, Any]) -> None:
    async with API_SETTINGS_LOCK:
        data["updated_at"] = now_datetime_full()
        _atomic_write_json(API_SETTINGS_PATH, data)


async def get_provider_config(provider: str) -> tuple[str, str]:
    st = await load_api_settings()
    pkey = str(provider or "").upper()
    providers = st.get("providers") if isinstance(st.get("providers"), dict) else {}
    cfg = providers.get(pkey) if isinstance(providers.get(pkey), dict) else {}
    if pkey == API_PROVIDER_SACHTAI:
        return str(cfg.get("base_url") or SACHTAI_API_BASE_URL).rstrip("/"), str(cfg.get("api_key") or SACHTAI_API_KEY).strip()
    # Do not fall back to the legacy API's ``base_url``: that endpoint has a
    # different contract and responds 404 for FishyMarket buyer routes.
    base_url = str(cfg.get("base_url") or FISHY_API_BASE_URL or FISHYMARKET_DEFAULT_BASE_URL).rstrip("/")
    if base_url == DEPRECATED_FISHYMARKET_BASE_URL:
        base_url = FISHYMARKET_DEFAULT_BASE_URL
    return base_url, str(cfg.get("api_key") or FISHY_API_KEY).strip()


async def get_fishy_config() -> tuple[str, str]:
    return await get_provider_config(API_PROVIDER_FISHYMARKET)


async def get_sachtai_config() -> tuple[str, str]:
    return await get_provider_config(API_PROVIDER_SACHTAI)


def fishy_error_message(status: int, body: Any = None) -> str:
    if status == 401:
        return "API Key sai hoặc hết hạn."
    if status == 400:
        return "Thiếu dữ liệu hoặc số dư API không đủ."
    if status == 404:
        return "Product hoặc Order không tồn tại."
    if status == 409:
        return "Sản phẩm API đã hết stock."
    if status >= 500:
        return "API server đang lỗi."
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or f"HTTP {status}")
    return f"HTTP {status}"


async def fishy_request(method: str, path: str, payload: Optional[dict[str, Any]] = None) -> tuple[bool, dict[str, Any], str, int]:
    base_url, api_key = await get_fishy_config()
    base_url = normalize_api_base_url(base_url)
    if not base_url or not api_key:
        return False, {}, "Thiếu FISHY_API_BASE_URL hoặc FISHY_API_KEY.", 0
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Required by ngrok-hosted FishyMarket deployments to return the API
        # response instead of the browser warning page.
        "ngrok-skip-browser-warning": "true",
        "User-Agent": "ShopBotApiClient/1.0",
    }
    url = f"{base_url}{path}"
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            kwargs: dict[str, Any] = {"headers": headers}
            if method.upper() == "GET":
                # FishyMarket documents the key query parameter for every GET
                # endpoint; retaining Bearer auth keeps both supported methods.
                kwargs["params"] = {"key": api_key}
            if payload is not None:
                body = dict(payload)
                body.setdefault("key", api_key)
                kwargs["json"] = body
            async with session.request(method.upper(), url, **kwargs) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"raw": (await resp.text())[:500]}
                if resp.status >= 400:
                    return False, data if isinstance(data, dict) else {}, fishy_error_message(resp.status, data), resp.status
                if not isinstance(data, dict):
                    return False, {}, "API trả dữ liệu không hợp lệ.", resp.status
                if data.get("success") is False:
                    return False, data, str(data.get("message") or data.get("error") or "API trả success=false"), resp.status
                return True, data, "", resp.status
    except Exception as e:
        return False, {}, f"NETWORK_ERROR: {e}", 0


class FishyMarketApiService:
    async def get_products(self) -> tuple[bool, list[dict[str, Any]], str]:
        ok, data, err, _ = await fishy_request("GET", "/api/telegram-buyer/products")
        if not ok:
            return False, [], err
        items = data.get("products", [])
        if not isinstance(items, list):
            return False, [], "Response products không hợp lệ."
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            stats = it.get("stats", {}) if isinstance(it.get("stats"), dict) else {}
            pid = str(it.get("_id") or it.get("id") or "").strip()
            if not pid:
                continue
            out.append({
                "external_product_id": pid,
                "name": str(it.get("product_name") or pid).strip(),
                "api_price": max(0, int(it.get("pricing", 0) or 0)),
                "api_stock": max(0, int(stats.get("available", 0) or 0)),
                "requires_customer_email": bool(it.get("requiresCustomerEmail", False)),
            })
        return True, out, ""

    async def get_balance(self) -> tuple[bool, dict[str, Any], str]:
        ok, data, err, _ = await fishy_request("GET", "/api/telegram-buyer/balance")
        if not ok:
            return False, {}, err
        return True, {
            "balance": int(data.get("balance", 0) or 0),
            "balance_text": str(data.get("balanceText") or vnd(int(data.get("balance", 0) or 0)) + " ₫"),
            "wallet_currency": str(data.get("walletCurrency") or "VND"),
        }, ""

    async def purchase(self, product_id: str, quantity: int, customer_email: str = "") -> tuple[bool, dict[str, Any], str]:
        payload = {"product_id": product_id, "quantity": max(1, int(quantity))}
        if customer_email:
            payload["customer_email"] = customer_email.strip()
        ok, data, err, _ = await fishy_request("POST", "/api/telegram-buyer/purchase", payload)
        if not ok:
            return False, {}, err
        return True, {
            "order_code": str(data.get("orderCode") or ""),
            "status": str(data.get("workspaceInviteStatus") or data.get("status") or "processing").lower(),
            "customer_email": str(data.get("workspaceOwnerEmail") or customer_email),
            "balance": int(data.get("balance", 0) or 0),
            "note": str(data.get("note") or ""),
        }, ""

    async def get_order_status(self, order_code: str) -> tuple[bool, dict[str, Any], str]:
        ok, data, err, _ = await fishy_request("GET", f"/api/telegram-buyer/order/{order_code}/status")
        if not ok:
            return False, {}, err
        return True, {
            "order_code": str(data.get("orderCode") or order_code),
            "product_type": str(data.get("productType") or ""),
            "customer_email": str(data.get("customerEmail") or ""),
            "status": str(data.get("status") or data.get("inviteStatus") or "pending").lower(),
            "invite_status": str(data.get("inviteStatus") or data.get("status") or "pending").lower(),
            "error_message": data.get("errorMessage"),
            "created_at": str(data.get("createdAt") or ""),
        }, ""


FISHY_API = FishyMarketApiService()


def sachtai_error_message(status: int, body: Any = None) -> str:
    error = str(body.get("error") or body.get("code") or "") if isinstance(body, dict) else ""
    http_mapping = {
        401: ("invalid_api_key", "API Key không hợp lệ hoặc đã bị thu hồi."),
        403: ("missing_scope", "API Key không có scope cần thiết."),
        404: ("product_not_found", "Sản phẩm hoặc order Sachtai không tồn tại."),
        409: ("duplicate_idempotency_key", "Mã chống trùng đã dùng cho sản phẩm/số lượng khác."),
        422: ("invalid_request", "Thiếu mã chống trùng hoặc dữ liệu không hợp lệ."),
        429: ("rate_limited", "API đang rate limit. Vui lòng thử lại sau."),
        500: ("internal_error", "Sachtai API đang lỗi nội bộ."),
        503: ("service_unavailable", "Sachtai tạm thời không xử lý được."),
    }
    body_error_messages = {
        "out_of_stock": "Hết hàng",
        "insufficient_balance": "Số dư API không đủ",
        "product_not_found": "Sản phẩm hoặc order Sachtai không tồn tại.",
        "invalid_quantity": "Số lượng không hợp lệ.",
        "invalid_api_key": "API Key không hợp lệ hoặc đã bị thu hồi.",
        "missing_scope": "API Key không có scope cần thiết.",
        "invalid_request": "Thiếu dữ liệu bắt buộc (kiểm tra lại idempotency_key / order_code).",
        "duplicate_idempotency_key": "Mã chống trùng đã dùng cho đơn khác.",
        "request_in_progress": "Đơn đang xử lý, vui lòng chờ và retry cùng mã.",
        "service_unavailable": "Sachtai tạm thời không xử lý được, retry cùng mã.",
        "api_access_blocked": "Quyền API của bạn đã bị khóa.",
        "rate_limited": "API đang rate limit. Vui lòng thử lại sau.",
        "internal_error": "Sachtai API đang lỗi nội bộ.",
    }
    # Check body error code first — Sachtai returns HTTP 200 with success:false
    # for business errors like out_of_stock, insufficient_balance, etc.
    if error in body_error_messages:
        return body_error_messages[error]
    # Then check HTTP status mapping for non-200 errors
    if status in http_mapping:
        return http_mapping[status][1]
    # Fall back to body message or generic
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or f"HTTP {status}")
    return f"HTTP {status}"


async def sachtai_request(method: str, path: str, payload: Optional[dict[str, Any]] = None, idempotency_key: str = "") -> tuple[bool, dict[str, Any], str, int, Optional[str]]:
    base_url, api_key = await get_sachtai_config()
    base_url = normalize_api_base_url(base_url)
    if not base_url or not api_key:
        return False, {}, "Thiếu SACHTAI_API_BASE_URL hoặc SACHTAI_API_KEY.", 0, None
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            kwargs: dict[str, Any] = {"headers": headers}
            if payload is not None:
                kwargs["json"] = payload
            async with session.request(method.upper(), f"{base_url}{path}", **kwargs) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"raw": (await resp.text())[:500]}
                remaining = resp.headers.get("X-RateLimit-Remaining")
                if resp.status >= 400:
                    return False, data if isinstance(data, dict) else {}, sachtai_error_message(resp.status, data), resp.status, remaining
                if not isinstance(data, dict):
                    return False, {}, "Sachtai trả dữ liệu không hợp lệ.", resp.status, remaining
                if data.get("success") is not True:
                    return False, data, sachtai_error_message(resp.status, data), resp.status, remaining
                return True, data, "", resp.status, remaining
    except asyncio.TimeoutError:
        return False, {}, "NETWORK_TIMEOUT", 0, None
    except Exception as e:
        return False, {}, f"NETWORK_ERROR: {e}", 0, None


class SachtaiDealerApiService:
    provider = API_PROVIDER_SACHTAI

    async def get_products(self) -> tuple[bool, list[dict[str, Any]], str]:
        ok, data, err, _, _ = await sachtai_request("GET", "/api/dealer/stock")
        if not ok:
            return False, [], err
        raw_products = data.get("products", {})
        if not isinstance(raw_products, dict):
            return False, [], "Response products phải là object products[product_key]."
        out = []
        for key, it in raw_products.items():
            if not isinstance(it, dict):
                continue
            fields = it.get("required_fields", []) if isinstance(it.get("required_fields", []), list) else []
            pkey = str(key).strip()
            out.append({
                "external_product_id": pkey,
                "name": str(it.get("name") or pkey).strip(),
                "description": str(it.get("description") or ""),
                "api_price": max(0, int(it.get("price", 0) or 0)),
                "api_stock": max(0, int(it.get("stock", 0) or 0)),
                "type": str(it.get("type") or "stock"),
                "fulfillment": str(it.get("fulfillment") or "instant"),
                "required_fields": [str(x) for x in fields],
                "requires_customer_email": "email" in {str(x).lower() for x in fields} or pkey == SACHTAI_CAPCUT_ADD_TEAM_KEY,
                "warranty_hours": max(0, int(it.get("warranty_hours", 0) or 0)),
                "api_purchase_enabled": bool(it.get("api_purchase_enabled", True)),
            })
        return True, out, ""

    async def get_balance(self) -> tuple[bool, dict[str, Any], str]:
        ok, data, err, _, _ = await sachtai_request("GET", "/api/dealer/balance")
        if not ok:
            return False, {}, err
        bal = int(data.get("balance", 0) or 0)
        return True, {"success": True, "balance": bal, "currency": "VND", "balance_text": f"{vnd(bal)} ₫"}, ""

    async def purchase(self, product_key: str, quantity: int, customer_email: str = "", order_code: str = "") -> tuple[bool, dict[str, Any], str]:
        qty = max(1, int(quantity))
        eff_order_code = (order_code or "").strip() or gen_sachtai_order_code()
        payload: dict[str, Any] = {
            "product_key": product_key,
            "qty": qty,
            "order_code": eff_order_code,
            "idempotency_key": eff_order_code,
        }
        if product_key == SACHTAI_CAPCUT_ADD_TEAM_KEY or customer_email.strip():
            payload["emails"] = [customer_email.strip()] if customer_email.strip() else []
        ok, data, err, _, _ = await sachtai_request("POST", "/api/dealer/buy", payload, idempotency_key=eff_order_code)
        if not ok:
            return False, {}, err
        items = [str(x) for x in data.get("items", [])] if isinstance(data.get("items", []), list) else []
        emails = data.get("emails", []) if isinstance(data.get("emails", []), list) else []
        status = str(data.get("status") or ("completed" if items else "processing")).lower()
        return True, {
            "provider": API_PROVIDER_SACHTAI,
            "external_order_code": str(data.get("order_code") or data.get("reference") or order_code),
            "reference": str(data.get("reference") or order_code),
            "status": "completed" if status in {"completed", "done"} or items else "processing",
            "items": items,
            "email_statuses": emails,
            "total_charged": int(data.get("total_charged", 0) or 0),
            "balance_remaining": int(data.get("balance_remaining", 0) or 0),
        }, ""

    async def get_order_status(self, order_code: str) -> tuple[bool, dict[str, Any], str]:
        ok, data, err, _, _ = await sachtai_request("GET", f"/api/dealer/orders/{order_code}")
        if not ok:
            return False, {}, err
        emails = data.get("emails", []) if isinstance(data.get("emails", []), list) else []
        statuses = [str(e.get("status", "pending")).lower() for e in emails if isinstance(e, dict)]
        if statuses and all(x == "in_team" for x in statuses):
            status = "completed"
        elif any(x == "failed" for x in statuses):
            status = "failed"
        else:
            status = str(data.get("status") or "pending").lower()
        return True, {"order_code": str(data.get("order_code") or order_code), "status": status, "email_statuses": emails, "error_message": str(data.get("message") or "")}, ""


SACHTAI_API = SachtaiDealerApiService()

def gen_sachtai_order_code() -> str:
    return f"BSS-{datetime.utcnow().strftime('%Y%m%d')}-{int(time.time()*1000)}-{secrets.token_hex(2).upper()}"

def get_api_provider_for_product(product: dict[str, Any]):
    provider = str(product.get("api_provider") or (API_PROVIDER_FISHYMARKET if product.get("fishy_managed") else "")).upper()
    if provider == API_PROVIDER_SACHTAI:
        return SACHTAI_API
    if provider == API_PROVIDER_FISHYMARKET:
        return FISHY_API
    return None


def api_external_product_id(product: dict[str, Any]) -> str:
    return str(product.get("api_product_id") or product.get("external_product_id") or "").strip()


def api_provider_key(product: dict[str, Any]) -> str:
    provider = str(product.get("api_provider") or "").strip().upper()
    if provider in {API_PROVIDER_FISHYMARKET, API_PROVIDER_SACHTAI, API_PROVIDER_LEGACY}:
        return provider
    return API_PROVIDER_FISHYMARKET if bool(product.get("fishy_managed", False)) else API_PROVIDER_LEGACY


async def load_auto_orders() -> dict[str, Any]:
    async with AUTO_ORDERS_LOCK:
        if not os.path.exists(AUTO_ORDERS_PATH):
            _atomic_write_json(AUTO_ORDERS_PATH, {})
            return {}
        try:
            with open(AUTO_ORDERS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


async def save_auto_orders(data: dict[str, Any]) -> None:
    async with AUTO_ORDERS_LOCK:
        _atomic_write_json(AUTO_ORDERS_PATH, data)


async def get_auto_order(order_code: str) -> Optional[dict[str, Any]]:
    return (await load_auto_orders()).get(order_code)


async def upsert_auto_order(order: dict[str, Any]) -> None:
    orders = await load_auto_orders()
    orders[str(order["order_code"])] = order
    await save_auto_orders(orders)

async def find_user_open_auto_order(user_id: int, product_id: str) -> Optional[dict[str, Any]]:
    for order in (await load_auto_orders()).values():
        if int(order.get("user_id", 0)) == user_id and str(order.get("product_id")) == product_id and str(order.get("status")) in {"PENDING_EMAIL", "PROCESSING"}:
            return order
    return None


async def refund_auto_order_once(user_id: int, order: dict[str, Any], reason: str) -> bool:
    if str(order.get("refund_status", "")) == ORDER_STATUS_REFUNDED:
        return False
    user = await load_user(user_id)
    before = int(user.get("balance", 0))
    amount = int(order.get("selling_price", 0))
    user["balance"] = before + amount
    user.setdefault("wallet_logs", []).insert(0, {"time": now_datetime_full(), "type": "credit", "source": "auto_order_refund", "amount": amount, "before": before, "after": before + amount, "note": reason[:300]})
    await save_user(user_id, user)
    order["refund_status"] = ORDER_STATUS_REFUNDED
    order["refund_amount"] = amount
    order["updated_at"] = now_datetime_full()
    await upsert_auto_order(order)
    return True


async def load_email_change_requests() -> dict[str, Any]:
    async with EMAIL_CHANGE_LOCK:
        if not os.path.exists(EMAIL_CHANGE_REQUESTS_PATH):
            _atomic_write_json(EMAIL_CHANGE_REQUESTS_PATH, {})
            return {}
        try:
            with open(EMAIL_CHANGE_REQUESTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


async def save_email_change_requests(data: dict[str, Any]) -> None:
    async with EMAIL_CHANGE_LOCK:
        _atomic_write_json(EMAIL_CHANGE_REQUESTS_PATH, data)


def gen_change_request_id(order_code: str) -> str:
    return f"chg_{order_code}_{int(time.time()*1000)}"


async def upsert_email_change_request(req: dict[str, Any]) -> None:
    data = await load_email_change_requests()
    data[str(req["id"])] = req
    await save_email_change_requests(data)


async def get_email_change_request(req_id: str) -> Optional[dict[str, Any]]:
    return (await load_email_change_requests()).get(req_id)


async def can_request_email_change(order: dict[str, Any]) -> tuple[bool, str]:
    if str(order.get("status")) != ORDER_STATUS_COMPLETED:
        return False, "Đơn chưa hoàn thành."
    if int(order.get("email_change_deadline_unix", 0) or 0) < now_unix():
        return False, "Đã quá thời gian đổi email 24 giờ."
    if int(order.get("change_email_count", 0) or 0) >= EMAIL_CHANGE_MAX_APPROVED:
        return False, "Bạn đã sử dụng tối đa 2 lần đổi email cho đơn hàng này."
    return True, ""


def auto_order_user_kb(order: dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    ok, _ = (False, "")
    if str(order.get("status")) == ORDER_STATUS_COMPLETED and int(order.get("email_change_deadline_unix", 0) or 0) >= now_unix() and int(order.get("change_email_count", 0) or 0) < EMAIL_CHANGE_MAX_APPROVED:
        kb.button(text="🔄 Yêu cầu đổi Email", callback_data=f"auto:change_email:{order['order_code']}")
    kb.button(text="💬 Hỗ trợ đơn hàng", callback_data=f"mo:chat:{order['order_code']}")
    kb.adjust(1)
    return kb.as_markup()


def email_change_admin_kb(req_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Đã đổi email", callback_data=f"adm:email_ok:{req_id}")
    kb.button(text="❌ Từ chối", callback_data=f"adm:email_reject:{req_id}")
    kb.button(text="💬 Nhắn khách", callback_data=f"adm:email_msg:{req_id}")
    kb.adjust(1)
    return kb.as_markup()

def auto_order_failed_admin_kb(order_code: str, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Hoàn tiền khách", callback_data=f"adm:auto_refund:{order_code}")
    kb.button(text="📧 Gửi thông tin tài khoản", callback_data=f"adm:auto_send_account:{order_code}")
    kb.button(text="💬 Trả lời khách", callback_data=f"adm:mo_reply:{user_id}:{order_code}")
    kb.adjust(1)
    return kb.as_markup()


def auto_admin_reply_kb(order_code: str, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Trả lời khách", callback_data=f"adm:mo_reply:{user_id}:{order_code}")
    kb.adjust(1)
    return kb.as_markup()

def category_name(categories: list[dict[str, Any]], category_id: str) -> str:
    c = next((x for x in categories if x.get("id") == category_id), None)
    if not c:
        return "Chưa phân nhóm"
    return f"{c.get('icon', '📁')} {c.get('name')}"


def auto_admin_text(order: dict[str, Any], categories: list[dict[str, Any]]) -> str:
    username = order.get("username") or "Không có"
    return (
        "📡 ĐƠN AUTO API\n\n"
        f"🆔 Order ID: #{order.get('order_code')}\n"
        f"👤 Tên: {order.get('full_name')}\n"
        f"🆔 User ID: {order.get('user_id')}\n"
        f"🔗 Username: {username}\n"
        f"📦 Sản phẩm: {order.get('product_name')}\n"
        f"📁 Nhóm: {category_name(categories, str(order.get('category_id', '')))}\n"
        f"💰 Giá: {vnd(int(order.get('selling_price', 0)))}đ\n"
        f"📊 Loại sản phẩm: AUTO_API\n"
        f"📡 API Order Code: {order.get('external_order_code') or 'N/A'}\n"
        f"📧 Email: {order.get('customer_email') or 'N/A'}\n"
        f"📌 Status: {order.get('status')}\n"
        f"⏰ Thời gian: {order.get('created_at')}"
    )


async def sync_fishy_products() -> tuple[bool, str, int, int, list[dict[str, Any]]]:
    ok, items, err = await FISHY_API.get_products()
    if not ok:
        return False, err, 0, 0, []
    products = await load_products()
    updated = 0
    price_changes: list[dict[str, Any]] = []
    existing_by_external = {
        str(p.get("external_product_id") or p.get("api_product_id") or "").lower(): p
        for p in products
        if str(p.get("type")) == PRODUCT_TYPE_AUTO_API
        and str(p.get("api_provider") or ("FISHYMARKET" if p.get("fishy_managed") else "")).upper() in {"", API_PROVIDER_FISHYMARKET}
        and str(p.get("external_product_id") or p.get("api_product_id") or "").strip()
    }
    for item in items:
        ext = str(item["external_product_id"]).strip()
        target = existing_by_external.get(ext.lower())
        if not target:
            continue
        target["api_product_id"] = ext
        target["external_product_id"] = ext
        old_api_price = int(target.get("api_price", 0))
        new_api_price = item["api_price"]
        if old_api_price != new_api_price and old_api_price > 0:
            price_changes.append({
                "product_id": target["id"],
                "name": target.get("name", ""),
                "old_api_price": old_api_price,
                "new_api_price": new_api_price,
                "sale_price": int(target.get("price_vnd", 0)),
            })
        target["api_price"] = new_api_price
        threshold = max(int(target.get("fishy_stock_threshold", 0) or 0), API_STOCK_SAFETY_BUFFER)
        cap = max(0, int(item["api_stock"]) - threshold)
        target["stock"] = min(max(0, int(target.get("stock", 0))), cap)
        target["requires_customer_email"] = item["requires_customer_email"]
        target["last_synced_at"] = utc_iso_z()
        target["fishy_managed"] = True
        target["api_provider"] = API_PROVIDER_FISHYMARKET
        target["api_stock"] = item["api_stock"]
        target["required_fields"] = ["email"] if item["requires_customer_email"] else []
        updated += 1
    if updated:
        await save_products(products)
    return True, "ok", 0, updated, price_changes


async def sync_sachtai_products() -> tuple[bool, str, int, int, list[dict[str, Any]]]:
    ok, items, err = await SACHTAI_API.get_products()
    if not ok:
        return False, err, 0, 0, []
    products = await load_products()
    existing = {
        (str(p.get("api_provider") or "").upper(), str(p.get("external_product_id") or p.get("api_product_id") or "")): p
        for p in products
        if str(p.get("type")) == PRODUCT_TYPE_AUTO_API
    }
    updated = 0
    price_changes: list[dict[str, Any]] = []
    for item in items:
        ext = str(item["external_product_id"]).strip()
        target = existing.get((API_PROVIDER_SACHTAI, ext))
        if not target:
            continue
        target["api_product_id"] = ext
        target["external_product_id"] = ext
        target["api_provider"] = API_PROVIDER_SACHTAI
        old_api_price = int(target.get("api_price", 0))
        new_api_price = int(item["api_price"])
        if old_api_price != new_api_price and old_api_price > 0:
            price_changes.append({
                "product_id": target["id"],
                "name": target.get("name", ""),
                "old_api_price": old_api_price,
                "new_api_price": new_api_price,
                "sale_price": int(target.get("price_vnd", 0)),
            })
        target["api_price"] = new_api_price
        target["api_stock"] = int(item["api_stock"])
        cap = usable_provider_stock(item["api_stock"])
        target["stock"] = min(max(0, int(target.get("stock", 0))), cap)
        target["requires_customer_email"] = bool(item["requires_customer_email"])
        target["required_fields"] = item.get("required_fields", [])
        target["fulfillment"] = item.get("fulfillment", "instant")
        target["warranty_hours"] = int(item.get("warranty_hours", 0))
        target["is_active"] = bool(item.get("api_purchase_enabled", True)) and cap > 0
        target["last_synced_at"] = utc_iso_z()
        updated += 1
        if item.get("description"):
            await set_product_detail(str(target["id"]), str(item.get("description", "")))
    if updated:
        await save_products(products)
    return True, "ok", 0, updated, price_changes


# ================== PRODUCTS ==================
def _default_products() -> list[dict[str, Any]]:
    return [
        {"id": "gemini_1y", "name": "Gemini Pro 1 Năm", "price_vnd": 50000, "stock": 0, "out_of_stock_notified": False, "type": "local", "api_product_id": "", "api_price": 0, "api_reserved_units": 0},
        {"id": "netflix_1m", "name": "Netflix 1 Tháng", "price_vnd": 39000, "stock": 0, "out_of_stock_notified": False, "type": "local", "api_product_id": "", "api_price": 0, "api_reserved_units": 0},
        {"id": "spotify_1m", "name": "Spotify 1 Tháng", "price_vnd": 29000, "stock": 0, "out_of_stock_notified": False, "type": "local", "api_product_id": "", "api_price": 0, "api_reserved_units": 0},
    ]

async def load_products() -> list[dict[str, Any]]:
    async with PRODUCTS_LOCK:
        if not os.path.exists(PRODUCTS_PATH):
            prods = _default_products()
            _atomic_write_json(PRODUCTS_PATH, prods)
            return prods
        try:
            with open(PRODUCTS_PATH, "r", encoding="utf-8") as f:
                prods = json.load(f)
            if not isinstance(prods, list):
                raise ValueError()

            out = []
            changed = False
            for p in prods:
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("id", "")).strip()
                name = str(p.get("name", "")).strip()
                price = int(p.get("price_vnd", 0))
                stock = int(p.get("stock", 0))
                raw_notified = p.get("out_of_stock_notified", "")
                if isinstance(raw_notified, bool):
                    notified = "" if not raw_notified else now_date()
                else:
                    notified = str(raw_notified).strip()
                if pid and name and price >= 0 and stock >= 0:
                    np = {
                        "id": pid,
                        "name": name,
                        "price_vnd": price,
                        "stock": stock,
                        "out_of_stock_notified": notified,
                        "type": str(p.get("type", "local") or "local").strip().lower(),
                        "api_product_id": str(p.get("api_product_id", "")).strip(),
                        "api_price": int(p.get("api_price", 0) or 0),
                        "api_reserved_units": int(p.get("api_reserved_units", 0) or 0),
                        "sale_percent": max(0, min(99, int(p.get("sale_percent", 0) or 0))),
                        "original_price_vnd": int(p.get("original_price_vnd", 0) or 0),
                        "sale_ends_at": int(p.get("sale_ends_at", 0) or 0),
                        "category_id": str(p.get("category_id", "cat_other") or "cat_other"),
                        "is_active": bool(p.get("is_active", True)),
                        "external_product_id": str(p.get("external_product_id", p.get("api_product_id", "")) or "").strip(),
                        "requires_customer_email": bool(p.get("requires_customer_email", False)),
                        "required_fields": p.get("required_fields", []) if isinstance(p.get("required_fields", []), list) else [],
                        "api_provider": str(p.get("api_provider") or ("FISHYMARKET" if bool(p.get("fishy_managed", False)) else "")).strip().upper() or None,
                        "api_stock": int(p.get("api_stock", p.get("stock", 0)) or 0),
                        "fulfillment": str(p.get("fulfillment", "instant") or "instant"),
                        "warranty_hours": int(p.get("warranty_hours", 0) or 0),
                        "delivery_status": str(p.get("delivery_status", "") or ""),
                        "last_synced_at": str(p.get("last_synced_at", "")),
                        "fishy_managed": bool(p.get("fishy_managed", False)),
                        "is_hot": bool(p.get("is_hot", False)),
                    }
                    if p.get("type") is None or "api_product_id" not in p or "api_price" not in p or "api_reserved_units" not in p or "sale_percent" not in p or "original_price_vnd" not in p or "sale_ends_at" not in p:
                        changed = True
                    np["manual_fields"] = p.get("manual_fields", []) if isinstance(p.get("manual_fields", []), list) else []
                    if np["type"] not in {"local", "api", "dontay"}:
                        np["type"] = "local"
                        changed = True
                    if np["sale_percent"] > 0 and np["sale_ends_at"] > 0 and np["sale_ends_at"] <= int(time.time()):
                        original = int(np.get("original_price_vnd", 0) or 0)
                        if original > 0:
                            np["price_vnd"] = original
                        np["sale_percent"] = 0
                        np["original_price_vnd"] = 0
                        np["sale_ends_at"] = 0
                        changed = True
                    out.append(np)
            if changed:
                _atomic_write_json(PRODUCTS_PATH, out)
            return out
        except Exception:
            prods = _default_products()
            _atomic_write_json(PRODUCTS_PATH, prods)
            return prods

async def save_products(products: list[dict[str, Any]]) -> None:
    async with PRODUCTS_LOCK:
        _atomic_write_json(PRODUCTS_PATH, products)

def gen_product_id() -> str:
    return f"sp_{int(time.time()*1000)}"

async def add_product(name: str, price_vnd: int) -> dict[str, Any]:
    products = await load_products()
    newp = {
        "id": gen_product_id(),
        "name": name.strip(),
        "price_vnd": int(price_vnd),
        "stock": 0,
        "out_of_stock_notified": False,
        "type": "local",
        "api_product_id": "",
        "api_price": 0,
        "api_reserved_units": 0,
        "category_id": "cat_other",
        "is_active": True,
        "is_hot": False,
        "external_product_id": "",
        "requires_customer_email": False,
        "required_fields": [],
        "api_provider": None,
        "api_stock": 0,
        "fulfillment": "local",
        "warranty_hours": 0,
    }
    products.insert(0, newp)
    await save_products(products)
    return newp


async def add_manual_product(name: str, price_vnd: int, fields: list[dict[str, Any]]) -> dict[str, Any]:
    products = await load_products()
    newp = {
        "id": gen_product_id(),
        "name": name.strip(),
        "price_vnd": int(price_vnd),
        "stock": 0,
        "out_of_stock_notified": False,
        "type": "dontay",
        "api_product_id": "",
        "api_price": 0,
        "api_reserved_units": 0,
        "manual_fields": fields,
        "category_id": "cat_other",
        "is_active": True,
        "is_hot": False,
        "external_product_id": "",
        "requires_customer_email": False,
        "required_fields": fields,
        "api_provider": None,
        "api_stock": 0,
        "fulfillment": "manual",
        "warranty_hours": 0,
    }
    products.insert(0, newp)
    await save_products(products)
    return newp


async def add_api_product(name: str, sale_price_vnd: int, api_product_id: str, api_price: int, api_provider: str = API_PROVIDER_FISHYMARKET, required_fields: Optional[list[str]] = None, fulfillment: str = "instant", warranty_hours: int = 0, api_stock: int = 0) -> dict[str, Any]:
    products = await load_products()
    newp = {
        "id": gen_product_id(),
        "name": name.strip(),
        "price_vnd": int(sale_price_vnd),
        "stock": 0,
        "out_of_stock_notified": False,
        "type": "api",
        "api_product_id": str(api_product_id).strip(),
        "api_price": max(0, int(api_price)),
        "api_reserved_units": 0,
        "category_id": "cat_other",
        "is_active": True,
        "is_hot": False,
        "external_product_id": str(api_product_id).strip(),
        "requires_customer_email": "email" in (required_fields or []),
        "required_fields": required_fields or [],
        "api_provider": str(api_provider).upper(),
        "api_stock": max(0, int(api_stock)),
        "fulfillment": fulfillment,
        "warranty_hours": max(0, int(warranty_hours)),
        "fishy_stock_threshold": 0,
    }
    products.insert(0, newp)
    await save_products(products)
    return newp

async def delete_product(product_id: str) -> bool:
    products = await load_products()
    before = len(products)
    products = [p for p in products if p.get("id") != product_id]
    if len(products) == before:
        return False
    await save_products(products)
    await remove_product_detail(product_id)
    return True


# ================== PRODUCT DATA (keys) ==================
async def append_product_lines(product_id: str, lines: list[str]) -> None:
    path = os.path.join(PRODUCT_DATA_DIR, f"{product_id}.txt")
    lock = _prod_lock(product_id)
    async with lock:
        with open(path, "a", encoding="utf-8") as f:
            for l in lines:
                l = str(l).strip()
                if l:
                    f.write(l + "\n")

async def pop_product_lines(product_id: str, n: int) -> list[str]:
    if n <= 0:
        return []
    path = os.path.join(PRODUCT_DATA_DIR, f"{product_id}.txt")
    lock = _prod_lock(product_id)
    async with lock:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if len(lines) < n:
            return []
        picked = lines[:n]
        rest = lines[n:]
        with open(path, "w", encoding="utf-8") as f:
            for l in rest:
                f.write(l + "\n")
        return picked

async def remove_last_product_lines(product_id: str, n: int) -> list[str]:
    if n <= 0:
        return []
    path = os.path.join(PRODUCT_DATA_DIR, f"{product_id}.txt")
    lock = _prod_lock(product_id)
    async with lock:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            raw = [l.rstrip("\n") for l in f.readlines()]

        non_empty_count = sum(1 for l in raw if l.strip())
        if non_empty_count < n:
            return []

        removed = []
        need = n
        i = len(raw) - 1
        while i >= 0 and need > 0:
            if raw[i].strip():
                removed.append(raw[i].strip())
                raw.pop(i)
                need -= 1
            i -= 1

        with open(path, "w", encoding="utf-8") as f:
            for l in raw:
                if l.strip():
                    f.write(l.strip() + "\n")

        removed.reverse()
        return removed


# ================== RESERVE ==================
async def reserve_items(uid: int, pid: str, qty: int, hold_seconds: int = RESERVE_HOLD_SECONDS) -> tuple[bool, str]:
    products = await load_products()
    product = next((p for p in products if p.get("id") == pid), None)
    if not product:
        return False, "Sản phẩm không tồn tại."

    stock = int(product.get("stock", 0))
    if qty <= 0:
        return False, "Số lượng không hợp lệ."
    if stock < qty:
        return False, f"Tồn kho không đủ. Hiện còn: {stock}"

    ptype = str(product.get("type", "local"))
    is_api = ptype == "api"
    is_manual = ptype == "dontay"
    if is_api or is_manual:
        keys: list[str] = []
    else:
        keys = await pop_product_lines(pid, qty)
        if len(keys) != qty:
            return False, "Dữ liệu sản phẩm không đủ để giữ hàng."

    product["stock"] = stock - qty
    if int(product["stock"]) > 0:
        product["out_of_stock_notified"] = ""
    await save_products(products)

    if str(product.get("type", "local")) == "api" and int(product.get("stock", 0)) <= 0:
        pass

    rp = reserve_path(uid, pid)
    payload = {
        "user_id": uid,
        "product_id": pid,
        "is_api": is_api,
        "is_manual": is_manual,
        "qty": qty,
        "keys": keys,
        "reserved_at": utc_iso_z(),
        "expires_at_unix": int(time.time()) + hold_seconds
    }
    _atomic_write_json(rp, payload)

    k = f"{uid}:{pid}"
    old = RESERVE_TASKS.get(k)
    if old and not old.done():
        old.cancel()

    async def _rollback_later():
        try:
            await asyncio.sleep(hold_seconds)
            await rollback_reserve(uid, pid)
        except asyncio.CancelledError:
            pass

    RESERVE_TASKS[k] = asyncio.create_task(_rollback_later())
    return True, "Đã giữ hàng."

async def rollback_reserve(uid: int, pid: str) -> None:
    rp = reserve_path(uid, pid)
    if not os.path.exists(rp):
        return

    try:
        with open(rp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        try:
            os.remove(rp)
        except Exception:
            pass
        return

    qty = int(data.get("qty", 0))
    is_api = bool(data.get("is_api", False))
    keys = data.get("keys", [])
    if not isinstance(keys, list):
        keys = []

    if keys and not is_api and not bool(data.get("is_manual", False)):
        await append_product_lines(pid, [str(k).strip() for k in keys if str(k).strip()])

    products = await load_products()
    for p in products:
        if p.get("id") == pid:
            p["stock"] = int(p.get("stock", 0)) + max(0, qty)
            p["out_of_stock_notified"] = ""
            break
    await save_products(products)

    try:
        os.remove(rp)
    except Exception:
        pass

    # xóa pending direct pay nếu còn
    for fn in os.listdir(DIRECT_PAY_DIR):
        fp = os.path.join(DIRECT_PAY_DIR, fn)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            if int(d.get("user_id", 0)) == uid and str(d.get("product_id", "")) == pid:
                os.remove(fp)
        except Exception:
            pass

async def cancel_reserve_now(uid: int, pid: str) -> None:
    k = f"{uid}:{pid}"
    t = RESERVE_TASKS.pop(k, None)
    if t and not t.done():
        t.cancel()
    await rollback_reserve(uid, pid)

async def finalize_reserve(uid: int, pid: str) -> None:
    k = f"{uid}:{pid}"
    t = RESERVE_TASKS.pop(k, None)
    if t and not t.done():
        t.cancel()
    rp = reserve_path(uid, pid)
    if os.path.exists(rp):
        try:
            os.remove(rp)
        except Exception:
            pass


# ================== ORDERS ==================
def gen_order_code() -> str:
    rnd = secrets.token_hex(4).upper()
    return f"{ORDER_PREFIX}{rnd}{int(time.time())}"

async def add_order(user_id: int, product_name: str, quantity: int, total_money: int, status: str, order_code: Optional[str] = None) -> dict[str, Any]:
    user = await load_user(user_id)
    order = {
        "order_code": order_code or gen_order_code(),
        "product_name": product_name,
        "quantity": int(quantity),
        "total_money": int(total_money),
        "created_time": now_datetime_full(),
        "status": status
    }
    user["orders"].insert(0, order)

    stats = user.get("stats", {"total_orders": 0, "done_orders": 0, "total_spent": 0})
    stats["total_orders"] = int(stats.get("total_orders", 0)) + 1
    if str(status).startswith("✅"):
        stats["done_orders"] = int(stats.get("done_orders", 0)) + 1
    stats["total_spent"] = int(stats.get("total_spent", 0)) + int(total_money)
    user["stats"] = stats
    user["last_seen"] = now_datetime_full()
    await save_user(user_id, user)
    return order


async def update_user_order_status(user_id: int, order_code: str, status: str) -> None:
    user = await load_user(user_id)
    changed = False
    for od in user.get("orders", []):
        if od.get("order_code") == order_code:
            od["status"] = status
            changed = True
            break
    if changed:
        await save_user(user_id, user)

def format_orders_text(orders: list[dict[str, Any]], page: int, page_size: int = 5) -> tuple[str, int, int]:
    total = len(orders)
    total_pages = max(1, ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start_i = page * page_size
    end_i = start_i + page_size
    chunk = orders[start_i:end_i]

    header = (
        "TỔNG ĐƠN HÀNG ĐÃ MUA ⏱\n\n"
        "Hiển thị 5 đơn gần nhất\n"
        f"Tổng: {total} đơn\n\n"
    )

    if total == 0:
        return header + "Bạn chưa có đơn hàng nào.", total_pages, page

    lines = [header]
    for od in chunk:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"{od.get('order_code', 'N/A')}")
        lines.append(f"SP: {od.get('product_name', 'N/A')}")
        lines.append(f"SL: {od.get('quantity', 'N/A')}")
        lines.append(f"Tổng: {vnd(int(od.get('total_money', 0)))} đ")
        lines.append(f"📅 Ngày: ({od.get('created_time', 'N/A')})")
        lines.append(f"📊 Trạng thái: {od.get('status', 'N/A')}")
        lines.append("━━━━━━━━━━━━━━━━━━\n")

    if total_pages > 1:
        lines.append(f"Trang {page+1}/{total_pages}")

    return "\n".join(lines).strip(), total_pages, page

def orders_nav_kb(page: int, total_pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if total_pages > 1:
        if page > 0:
            kb.button(text="⬅️ Trang trước", callback_data=f"orders:{page-1}")
        if page < total_pages - 1:
            kb.button(text="Trang sau ➡️", callback_data=f"orders:{page+1}")
        kb.adjust(2)
    kb.button(text="⬅️ Quay lại Menu", callback_data="back_home")
    kb.adjust(2, 1) if total_pages > 1 else kb.adjust(1)
    return kb.as_markup()


# ================== ORDER FILE ==================


def customer_order_success_caption(order_code: str, product_name: str, qty: int, total: int, wallet_after: int) -> str:
    """Caption cho khách: ẩn thông tin nội bộ và số dư hệ thống."""
    _ = wallet_after
    return (
        "🎉 MUA HÀNG THÀNH CÔNG\n\n"
        f"🧾 Mã đơn: {order_code}\n"
        f"📦 Sản phẩm: {product_name}\n"
        f"🔢 Số lượng: {qty}\n"
        f"💰 Tổng tiền: {vnd(total)} đ\n\n"
        "📎 File chi tiết đã được gửi kèm.\n"
        "Nếu cần hỗ trợ sau mua, bấm nút bảo hành bên dưới.\n"
        "Cảm ơn quý khách!"
    )

def create_order_file_text(
    order_code: str,
    iso_date: str,
    product_name: str,
    price_vnd: int,
    qty: int,
    total_vnd: int,
    keys: list[str],
) -> str:
    path = os.path.join(ORDERS_DATA_DIR, f"{order_code}.txt")
    body_keys = "\n".join(keys)

    content = (
        f"- MÃ ĐƠN: {order_code}\n"
        f"- NGÀY  : {iso_date}\n"
        f"- SẢN PHẨM: {product_name} - {vnd(price_vnd)}Đ\n"
        f"- SỐ LƯỢNG: {qty}\n"
        f"- TỔNG TIỀN: {vnd(total_vnd)}\n"
        "----------------------------------------\n"
        f"{body_keys}\n"
        "----------------------------------------\n"
        "Cảm ơn quý khách!"
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return path


# ================== DIRECT PAY ==================
async def create_direct_payment(uid: int, pid: str, qty: int, product_name: str, unit_price: int, total: int, customer_email: str = "") -> dict[str, Any]:
    order_code = gen_order_code()
    payload = {
        "order_code": order_code,
        "user_id": uid,
        "product_id": pid,
        "product_name": product_name,
        "qty": qty,
        "unit_price": unit_price,
        "total": total,
        "created_at": now_datetime_full(),
        "expires_at_unix": int(time.time()) + RESERVE_HOLD_SECONDS,
        "payment_desc": f"{nap_code(uid)} {order_code}",
        "customer_email": customer_email.strip(),
    }
    _atomic_write_json(direct_pay_path(order_code), payload)
    return payload

async def load_direct_payment(order_code: str) -> Optional[dict[str, Any]]:
    path = direct_pay_path(order_code)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


async def find_direct_payment_by_desc(uid: int, desc: str) -> Optional[dict[str, Any]]:
    """Fallback tìm pending direct pay theo nội dung CK khi load theo order_code thất bại."""
    if not desc:
        return None
    d_up = desc.upper()
    try:
        for fn in os.listdir(DIRECT_PAY_DIR):
            if not fn.lower().endswith(".json"):
                continue
            fp = os.path.join(DIRECT_PAY_DIR, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    p = json.load(f)
                if not isinstance(p, dict):
                    continue
                if int(p.get("user_id", 0)) != int(uid):
                    continue
                oc = str(p.get("order_code", "")).strip().upper()
                pd = str(p.get("payment_desc", "")).strip().upper()
                if (oc and oc in d_up) or (pd and pd in d_up):
                    return p
            except Exception:
                continue
    except Exception:
        return None
    return None


async def find_direct_payment_by_uid_amount(uid: int, amount: int) -> Optional[dict[str, Any]]:
    """Fallback cuối: tìm pending theo user + đúng số tiền + còn hiệu lực.
    Chỉ trả về khi có duy nhất 1 đơn khớp để tránh chốt nhầm.
    """
    matches: list[dict[str, Any]] = []
    now_ts = int(time.time())
    try:
        for fn in os.listdir(DIRECT_PAY_DIR):
            if not fn.lower().endswith(".json"):
                continue
            fp = os.path.join(DIRECT_PAY_DIR, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    p = json.load(f)
                if not isinstance(p, dict):
                    continue
                if int(p.get("user_id", 0)) != int(uid):
                    continue
                if int(p.get("total", 0)) != int(amount):
                    continue
                # Chỉ nhận đơn còn hiệu lực giữ hàng
                if int(p.get("expires_at_unix", 0)) <= now_ts:
                    continue
                matches.append(p)
            except Exception:
                continue
    except Exception:
        return None

    if len(matches) == 1:
        return matches[0]
    return None

async def delete_direct_payment(order_code: str) -> None:
    path = direct_pay_path(order_code)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

def api_fail_admin_kb(user_id: int, pid: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Hoàn tiền khách", callback_data=f"adm:apirefund:{user_id}:{pid}")
    kb.button(text="📤 Gửi thông tin tài khoản", callback_data=f"adm:apisendinfo:{user_id}:{pid}")
    kb.adjust(1, 1)
    return kb.as_markup()

def direct_pay_kb(order_code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Kiểm tra lại thanh toán", callback_data=f"directcheck:{order_code}")
    kb.button(text="❌ Hủy đơn chuyển khoản", callback_data=f"directcancel:{order_code}")
    kb.adjust(1, 1)
    return kb.as_markup()

async def finalize_direct_order(bot: Bot, txid: str, tdate: str, desc: str, pending: dict[str, Any]) -> None:
    uid = int(pending["user_id"])
    pid = str(pending["product_id"])
    qty = int(pending["qty"])
    unit_price = int(pending["unit_price"])
    total = int(pending["total"])
    product_name = str(pending["product_name"])
    order_code = str(pending["order_code"])

    rp = reserve_path(uid, pid)
    if not os.path.exists(rp):
        await delete_direct_payment(order_code)
        try:
            await bot.send_message(uid, f"❌ Đơn {order_code} đã hết thời gian giữ hàng, tiền vào sẽ được cộng ví thủ công nếu cần.")
        except Exception:
            pass
        await notify_admin(
            bot,
            "⚠️ THANH TOÁN TRỰC TIẾP TỚI SAU KHI HẾT GIỮ HÀNG\n\n"
            f"🧾 Mã đơn: {order_code}\n"
            f"🆔 User: {uid}\n"
            f"💰 Số tiền vào: {desc}\n"
            "👉 Kiểm tra thủ công."
        )
        return

    try:
        with open(rp, "r", encoding="utf-8") as f:
            rdata = json.load(f)
    except Exception:
        await delete_direct_payment(order_code)
        return

    keys = rdata.get("keys", [])
    is_api = bool(rdata.get("is_api", False))
    is_manual = bool(rdata.get("is_manual", False))
    if not isinstance(keys, list):
        await delete_direct_payment(order_code)
        return
    if (not is_api) and (not is_manual) and len(keys) != qty:
        await delete_direct_payment(order_code)
        return

    products = await load_products()
    product_obj = next((p for p in products if p.get("id") == pid), None)
    if not product_obj:
        await delete_direct_payment(order_code)
        await cancel_reserve_now(uid, pid)
        return

    if str(product_obj.get("type", "local")) == "dontay":
        await finalize_reserve(uid, pid)
        await delete_direct_payment(order_code)
        order = await add_order(uid, product_name, qty, total, "⏳ Chờ khách gửi dữ liệu", order_code=order_code)
        user = await load_user(uid)
        manual_order = {
            "order_code": order_code, "user_id": uid, "full_name": user.get("full_name") or str(uid),
            "username": f"@{user.get('username')}" if user.get("username") else "Không có",
            "product_id": pid, "product_name": product_name, "quantity": qty, "unit_price": unit_price, "total": total,
            "status": "WAITING_CUSTOMER_DATA", "created_time": order.get("created_time"),
            "manual_fields": product_obj.get("manual_fields", []), "customer_data": {}, "stock_after": product_obj.get("stock", 0),
            "refunded": False, "stock_refunded": False, "paid_by": "bank", "txid": txid
        }
        add_order_history(manual_order, f"user:{uid}", "Thanh toán thành công", "PAID", "WAITING_CUSTOMER_DATA", "Bot yêu cầu dữ liệu")
        await upsert_manual_order(manual_order)
        try:
            await bot.send_message(uid, "✅ Đơn hàng của bạn đã được tạo thành công.\nVui lòng gửi các thông tin cần thiết để Admin xử lý đơn hàng.\n\n" + f"🧾 Mã đơn: {order_code}\n📦 Sản phẩm: {product_name}\n\n" + manual_fields_text(product_obj.get("manual_fields", [])), reply_markup=manual_customer_kb(order_code))
        except Exception:
            pass
        await notify_admin(bot, manual_admin_text(manual_order), reply_markup=manual_admin_kb(order_code, uid))
        return

    if str(product_obj.get("type", "local")) == "api" and bool(product_obj.get("fishy_managed", False)) and str(product_obj.get("external_product_id") or product_obj.get("api_product_id") or "").strip():
        customer_email = str(pending.get("customer_email", "")).strip()
        if bool(product_obj.get("requires_customer_email", False)) and not is_valid_email(customer_email):
            await cancel_reserve_now(uid, pid)
            await delete_direct_payment(order_code)
            await notify_admin(bot, f"❌ Đơn API chuyển khoản thiếu email hợp lệ.\n🧾 {order_code}\n🆔 User ID: {uid}\n📦 {product_name}\n👉 Cần xử lý hoàn tiền thủ công nếu tiền đã vào.")
            try:
                await bot.send_message(uid, f"❌ Đơn {order_code} thiếu email hợp lệ nên không thể tạo đơn API.\n\n⏳ Vui lòng liên hệ Admin để được hỗ trợ.")
            except Exception:
                pass
            return
        ok_buy, buy_data, err = await FISHY_API.purchase(str(product_obj.get("external_product_id") or product_obj.get("api_product_id")), qty, customer_email)
        if not ok_buy or not buy_data.get("order_code"):
            await cancel_reserve_now(uid, pid)
            await delete_direct_payment(order_code)
            await notify_admin(bot, f"❌ FISHY PURCHASE THẤT BẠI (chuyển khoản)\n\n🧾 {order_code}\n🆔 User ID: {uid}\n📦 {product_name}\n📧 {customer_email}\n📝 Lỗi: {err}\n\n👉 Chọn hành động:", reply_markup=auto_order_failed_admin_kb(order_code, uid))
            try:
                await bot.send_message(uid, f"⏳ Đơn hàng {order_code} đang gặp sự cố. Admin sẽ kiểm tra và phản hồi cho bạn. Vui lòng chờ.")
            except Exception:
                pass
            return
        await finalize_reserve(uid, pid)
        await delete_direct_payment(order_code)
        order = await add_order(uid, product_name, qty, total, "🔄 Đang xử lý API", order_code=order_code)
        user = await load_user(uid)
        cats = await load_categories()
        auto_order = {
            "order_code": order_code, "external_order_code": buy_data["order_code"], "user_id": uid,
            "full_name": user.get("full_name") or str(uid), "username": f"@{user.get('username')}" if user.get("username") else "Không có",
            "product_id": pid, "category_id": product_obj.get("category_id", "cat_other"), "product_name": product_name,
            "product_type": "AUTO_API", "customer_email": customer_email or buy_data.get("customer_email", ""),
            "quantity": qty, "selling_price": total, "api_price": int(product_obj.get("api_price", 0)), "status": ORDER_STATUS_PROCESSING,
            "api_status": buy_data.get("status", "processing"), "refund_status": "NONE", "refund_amount": 0,
            "change_email_count": 0, "email_change_deadline_unix": 0, "poll_count": 0,
            "created_at": order.get("created_time"), "created_at_unix": now_unix(), "updated_at": now_datetime_full(), "history": []
        }
        add_order_history(auto_order, f"user:{uid}", "Thanh toán chuyển khoản + tạo API", "PENDING_PAYMENT", ORDER_STATUS_PROCESSING, f"external={buy_data['order_code']}")
        await upsert_auto_order(auto_order)
        try:
            await bot.send_message(uid, f"🔄 Đơn hàng #{order_code} đang được xử lý.\n\n📦 {product_name}\n📧 {auto_order['customer_email']}\n\nTrạng thái: PROCESSING", reply_markup=auto_order_user_kb(auto_order))
        except Exception:
            pass
        await notify_admin(bot, auto_admin_text(auto_order, cats))
        start_auto_order_poll_task(bot, order_code)
        return

    # Sachtai API products: purchase via SACHTAI_API when bank transfer is confirmed.
    if str(product_obj.get("type", "local")) == "api" and str(product_obj.get("api_provider") or "").upper() == API_PROVIDER_SACHTAI:
        customer_email = str(pending.get("customer_email", "")).strip()
        ext = str(product_obj.get("external_product_id") or product_obj.get("api_product_id") or "").strip()
        fields_set = {str(x).lower() for x in (product_obj.get("required_fields") or []) if isinstance(product_obj.get("required_fields"), list)}
        needs_email = bool(product_obj.get("requires_customer_email")) or "email" in fields_set or ext == SACHTAI_CAPCUT_ADD_TEAM_KEY
        if needs_email and not is_valid_email(customer_email):
            await cancel_reserve_now(uid, pid)
            await delete_direct_payment(order_code)
            await notify_admin(bot, f"❌ Đơn Sachtai chuyển khoản thiếu email hợp lệ.\n🧾 {order_code}\n🆔 User ID: {uid}\n📦 {product_name}\n👉 Cần xử lý hoàn tiền thủ công nếu tiền đã vào.")
            try:
                await bot.send_message(uid, f"❌ Đơn {order_code} thiếu email hợp lệ nên không thể tạo đơn Sachtai.\n\n⏳ Vui lòng liên hệ Admin để được hỗ trợ.")
            except Exception:
                pass
            return
        internal_ref = gen_sachtai_order_code()
        ok_buy, buy_data, err = await SACHTAI_API.purchase(ext, qty, customer_email, internal_ref)
        if not ok_buy or not buy_data.get("external_order_code"):
            await cancel_reserve_now(uid, pid)
            await delete_direct_payment(order_code)
            await notify_admin(bot, f"❌ SACHTAI PURCHASE THẤT BẠI (chuyển khoản)\n\n🧾 {order_code}\n🆔 User ID: {uid}\n📦 {product_name}\n📧 {customer_email}\n📝 Lỗi: {err}\n\n👉 Chọn hành động:", reply_markup=auto_order_failed_admin_kb(order_code, uid))
            try:
                await bot.send_message(uid, f"⏳ Đơn hàng {order_code} đang gặp sự cố. Admin sẽ kiểm tra và phản hồi cho bạn. Vui lòng chờ.")
            except Exception:
                pass
            return
        await finalize_reserve(uid, pid)
        await delete_direct_payment(order_code)
        if buy_data.get("status") == "completed" and buy_data.get("items"):
            order = await add_order(uid, product_name, qty, total, "✅ Đã hoàn thành", order_code=order_code)
            order_file = create_order_file_text(order["order_code"], utc_iso_z(), product_name, unit_price, qty, total, [str(x).strip() for x in buy_data.get("items", [])])
            try:
                await bot.send_document(uid, document=FSInputFile(order_file), caption=f"🎉 THANH TOÁN CHUYỂN KHOẢN THÀNH CÔNG\n\n🧾 Mã đơn: {order_code}\n📦 Sản phẩm: {product_name}\n🔢 Số lượng: {qty}\n💰 Tổng tiền: {vnd(total)} đ\n\n📎 File chi tiết đã được gửi kèm.", reply_markup=warranty_request_kb(order_code))
            except Exception:
                pass
            await notify_admin(bot, f"🔌 SACHTAI ORDER COMPLETED (chuyển khoản)\n\n🧾 {order_code}\n📡 API Order: {buy_data['external_order_code']}\n📦 {product_name}")
            return
        order = await add_order(uid, product_name, qty, total, "🔄 Đang xử lý API", order_code=order_code)
        user = await load_user(uid)
        cats = await load_categories()
        auto_order = {
            "order_code": order_code, "external_order_code": buy_data["external_order_code"], "internal_reference": internal_ref,
            "user_id": uid, "full_name": user.get("full_name") or str(uid), "username": f"@{user.get('username')}" if user.get("username") else "Không có",
            "product_id": pid, "category_id": product_obj.get("category_id", "cat_other"), "product_name": product_name,
            "product_type": "API", "api_provider": API_PROVIDER_SACHTAI, "external_product_id": ext,
            "customer_email": customer_email, "quantity": qty, "selling_price": total,
            "api_price": int(product_obj.get("api_price", 0)), "status": ORDER_STATUS_PROCESSING,
            "api_status": buy_data.get("status", "processing"), "email_statuses": buy_data.get("email_statuses", []),
            "delivery_status": "PROCESSING", "delivery_data": [], "refund_status": "NONE", "refund_amount": 0,
            "poll_count": 0, "created_at": order.get("created_time"), "created_at_unix": now_unix(),
            "updated_at": now_datetime_full(), "history": []
        }
        add_order_history(auto_order, f"user:{uid}", "Thanh toán chuyển khoản + tạo đơn Sachtai", "PENDING_API_PURCHASE", ORDER_STATUS_PROCESSING, f"external={buy_data['external_order_code']}")
        await upsert_auto_order(auto_order)
        try:
            await bot.send_message(uid, f"🔄 Đơn hàng #{order_code} đang được xử lý.\n\n📦 {product_name}\n📧 {customer_email}\n\nTrạng thái: PROCESSING", reply_markup=auto_order_user_kb(auto_order))
        except Exception:
            pass
        await notify_admin(bot, auto_admin_text(auto_order, cats))
        start_auto_order_poll_task(bot, order_code)
        return

    # Với sản phẩm API legacy: chỉ lấy account khi thanh toán thành công bằng cách gọi /buy.
    if str(product_obj.get("type", "local")) == "api":
        ok_buy, buy_data, err_code, err_msg = await shop_api_buy(str(product_obj.get("api_product_id", "")).strip(), qty)
        if not ok_buy:
            await cancel_reserve_now(uid, pid)
            await delete_direct_payment(order_code)
            await notify_admin(
                bot,
                "❌ API BUY THẤT BẠI (đơn chuyển khoản)\n\n"
                f"🧾 Mã đơn: {order_code}\n"
                f"🆔 User: {uid}\n"
                f"📦 SP: {product_name}\n"
                f"🔢 SL: {qty}\n"
                f"🔎 Mã lỗi: {err_code or 'N/A'}\n"
                f"📝 Chi tiết: {err_msg or 'N/A'}"
            )
            try:
                await bot.send_message(uid, f"⏳ Đơn hàng {order_code} đang gặp sự cố. Admin sẽ kiểm tra và phản hồi cho bạn. Vui lòng chờ.")
            except Exception:
                pass
            return
        buy_accounts = buy_data.get("accounts", [])
        keys = [str(x).strip() for x in buy_accounts if str(x).strip()]
        if not keys:
            await cancel_reserve_now(uid, pid)
            await delete_direct_payment(order_code)
            await notify_admin(bot, f"❌ API /buy trả về rỗng accounts cho đơn {order_code}. Đã rollback giữ hàng.")
            return

    await finalize_reserve(uid, pid)
    await delete_direct_payment(order_code)

    order = await add_order(
        user_id=uid,
        product_name=product_name,
        quantity=qty,
        total_money=total,
        status="✅ Đã hoàn thành",
        order_code=order_code
    )

    iso = utc_iso_z()
    order_file = create_order_file_text(
        order_code=order["order_code"],
        iso_date=iso,
        product_name=product_name,
        price_vnd=unit_price,
        qty=qty,
        total_vnd=total,
        keys=[str(x).strip() for x in keys]
    )

    try:
        await bot.send_document(
            uid,
            document=FSInputFile(order_file),
            caption=(
                "🎉 THANH TOÁN CHUYỂN KHOẢN THÀNH CÔNG\n\n"
                f"🧾 Mã đơn: {order_code}\n"
                f"📦 Sản phẩm: {product_name}\n"
                f"🔢 Số lượng: {qty}\n"
                f"💰 Tổng tiền: {vnd(total)} đ\n"
                f"🧾 Mã GD: {txid}\n"
                f"🕒 Thời gian: {tdate or now_datetime_full()}\n\n"
                "📎 File chi tiết đã được gửi kèm.\n"
                "Nếu cần hỗ trợ sau mua, bấm nút bảo hành bên dưới."
            ),
            reply_markup=warranty_request_kb(order_code)
        )
    except Exception:
        pass

    await notify_admin(
        bot,
        "🏦 ĐƠN HÀNG THANH TOÁN TRỰC TIẾP THÀNH CÔNG\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Mã đơn: {order_code}\n"
        f"🆔 User ID: {uid}\n"
        f"📦 SP: {product_name}\n"
        f"🔢 SL: {qty}\n"
        f"💰 Tổng: {vnd(total)} đ\n"
        f"🧾 Mã GD: {txid}\n"
        f"🕒 Thời gian: {tdate or now_datetime_full()}\n"
        "━━━━━━━━━━━━━━━━━━"
    )



# ================== MANUAL ORDERS ==================
MANUAL_STATUSES = {"PENDING_PAYMENT", "PAID", "WAITING_CUSTOMER_DATA", "CUSTOMER_SUBMITTED", "PROCESSING", "COMPLETED", "CANCELLED", "REFUNDED"}

def _order_lock(order_code: str) -> asyncio.Lock:
    if order_code not in ORDER_LOCKS:
        ORDER_LOCKS[order_code] = asyncio.Lock()
    return ORDER_LOCKS[order_code]

def parse_manual_fields(raw: str) -> list[dict[str, Any]]:
    fields = []
    for part in re.split(r"[,\n]", raw):
        label = part.strip()
        if not label:
            continue
        required = True
        if label.endswith("?"):
            label = label[:-1].strip()
            required = False
        if label.lower().endswith("(optional)"):
            label = label[:-10].strip()
            required = False
        key = re.sub(r"[^a-zA-Z0-9_]+", "_", label.lower()).strip("_") or f"field_{len(fields)+1}"
        fields.append({"key": key[:40], "label": label[:80], "required": required})
    return fields

def manual_fields_text(fields: list[dict[str, Any]]) -> str:
    icon = {"email": "📧", "sdt": "📱", "phone": "📱", "ghi_chu": "📝", "note": "📝", "link": "🔗", "uid": "🆔"}
    lines = []
    for f in fields:
        key = str(f.get("key", ""))
        req = "*" if f.get("required", True) else "(không bắt buộc)"
        lines.append(f"{icon.get(key, '▫️')} {f.get('label', key)} {req}:")
    return "\n".join(lines)

def parse_customer_manual_data(text: str, fields: list[dict[str, Any]]) -> tuple[bool, dict[str, str], str]:
    data: dict[str, str] = {}
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    kv = {}
    for line in lines:
        if ":" in line:
            k, v = line.split(":", 1)
            kv[k.strip().lower()] = v.strip()
    for i, f in enumerate(fields):
        label = str(f.get("label", "")).strip()
        key = str(f.get("key", label)).strip()
        val = kv.get(label.lower()) or kv.get(key.lower()) or (lines[i] if i < len(lines) and ":" not in lines[i] else "")
        if f.get("required", True) and not val:
            return False, {}, f"Thiếu trường bắt buộc: {label}"
        data[label] = val
    return True, data, ""

def manual_customer_kb(order_code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Gửi thông tin", callback_data=f"mo:send:{order_code}")
    kb.button(text="❌ Hủy đơn", callback_data=f"mo:ucancel:{order_code}")
    kb.button(text="💬 Liên hệ Admin", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()

def manual_chat_kb(order_code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Nhắn tin về đơn hàng", callback_data=f"mo:chat:{order_code}")
    return kb.as_markup()

def manual_admin_kb(order_code: str, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Hoàn thành đơn", callback_data=f"adm:mo_done:{user_id}:{order_code}")
    kb.button(text="❌ Hủy đơn", callback_data=f"adm:mo_cancel:{user_id}:{order_code}")
    kb.button(text="💬 Trả lời khách", callback_data=f"adm:mo_reply:{user_id}:{order_code}")
    kb.adjust(1)
    return kb.as_markup()

async def load_manual_orders() -> dict[str, Any]:
    async with MANUAL_ORDERS_LOCK:
        if not os.path.exists(MANUAL_ORDERS_PATH):
            _atomic_write_json(MANUAL_ORDERS_PATH, {})
            return {}
        try:
            with open(MANUAL_ORDERS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

async def save_manual_orders(data: dict[str, Any]) -> None:
    async with MANUAL_ORDERS_LOCK:
        _atomic_write_json(MANUAL_ORDERS_PATH, data)

async def get_manual_order(order_code: str) -> Optional[dict[str, Any]]:
    return (await load_manual_orders()).get(order_code)

async def upsert_manual_order(order: dict[str, Any]) -> None:
    all_orders = await load_manual_orders()
    all_orders[str(order["order_code"])] = order
    await save_manual_orders(all_orders)

def add_order_history(order: dict[str, Any], actor: str, action: str, before: str, after: str, note: str = "") -> None:
    order.setdefault("history", []).append({"time": now_datetime_full(), "actor": actor, "action": action, "before": before, "after": after, "note": note[:1000]})

def manual_admin_text(order: dict[str, Any]) -> str:
    username = order.get("username") or "Không có"
    data = order.get("customer_data", {}) if isinstance(order.get("customer_data"), dict) else {}
    block = "\n".join(f"{k}: {v}" for k, v in data.items()) or "(chưa có)"
    return ("📦 ĐƠN HÀNG ĐƠN TAY\n\n"
        f"🆔 Order ID: #{order.get('order_code')}\n"
        f"👤 Tên: {order.get('full_name')}\n"
        f"🆔 User ID: {order.get('user_id')}\n"
        f"🔗 Username: {username}\n"
        f"🛒 Sản phẩm: {order.get('product_name')}\n"
        f"💰 Giá: {vnd(int(order.get('total',0)))}đ\n"
        f"📊 Trạng thái: {order.get('status')}\n"
        f"📦 Stock: {order.get('stock_after','N/A')}\n"
        f"🕒 Thời gian: {order.get('created_time')}\n\n"
        "📥 DỮ LIỆU KHÁCH GỬI\n```\n" + block + "\n```")

# ================== WARRANTY ==================
def warranty_request_kb(order_code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛡 Yêu cầu bảo hành", callback_data=f"warranty:{order_code}")
    kb.button(text="⬅️ Quay lại Menu", callback_data="back_home")
    kb.adjust(1, 1)
    return kb.as_markup()

def admin_warranty_chat_kb(user_id: int, order_code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Gửi tin nhắn cho khách", callback_data=f"adm:wchat:{user_id}:{order_code}")
    kb.button(text="🔒 Đóng mã bảo hành", callback_data=f"adm:wclose:{user_id}:{order_code}")
    kb.button(text="🗑 Xóa chat reply", callback_data=f"adm:wclear:{user_id}:{order_code}")
    kb.adjust(1, 1, 1)
    return kb.as_markup()

async def load_warranty(order_code: str) -> Optional[dict[str, Any]]:
    path = warranty_path(order_code)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        data.setdefault("chat_messages", [])
        data.setdefault("closed", False)
        data.setdefault("close_reason", "")
        return data
    except Exception:
        return None

async def save_warranty(data: dict[str, Any]) -> None:
    order_code = str(data.get("order_code", "")).strip()
    if not order_code:
        return
    data.setdefault("chat_messages", [])
    data.setdefault("closed", False)
    data.setdefault("close_reason", "")
    _atomic_write_json(warranty_path(order_code), data)

async def append_warranty_chat(order_code: str, item: dict[str, Any]) -> None:
    data = await load_warranty(order_code)
    if not data:
        return
    chats = data.get("chat_messages", [])
    if not isinstance(chats, list):
        chats = []
    chats.append(item)
    data["chat_messages"] = chats
    await save_warranty(data)

async def clear_warranty_chat_and_archive(order_code: str) -> tuple[bool, Optional[str], int]:
    data = await load_warranty(order_code)
    if not data:
        return False, None, 0

    chats = data.get("chat_messages", [])
    if not isinstance(chats, list):
        chats = []

    count = len(chats)
    archive_file = warranty_archive_path(order_code)

    with open(archive_file, "w", encoding="utf-8") as f:
        f.write(f"MÃ BẢO HÀNH / ĐƠN: {order_code}\n")
        f.write(f"THỜI GIAN LƯU: {now_datetime_full()}\n")
        f.write("=" * 60 + "\n")
        for i, c in enumerate(chats, 1):
            f.write(f"[{i}] {c.get('time','N/A')} | {c.get('sender','N/A')}\n")
            f.write(f"{c.get('text','')}\n")
            f.write("-" * 60 + "\n")

    data["chat_messages"] = []
    await save_warranty(data)
    return True, archive_file, count


# ================== WALLET (admin edit) ==================
async def admin_edit_balance(user_id: int, mode: str, amount: int, admin_id: int, reason: str = "") -> dict:
    if amount <= 0:
        raise ValueError("amount must be > 0")

    data = await load_user(user_id)
    before = int(data.get("balance", 0))
    mode = mode.lower().strip()

    if mode == "cong":
        delta = int(amount)
        after = before + delta
    elif mode == "tru":
        delta = -int(amount)
        after = before + delta
        if after < 0:
            raise ValueError(f"insufficient balance (current={before}, need={amount})")
    else:
        raise ValueError("mode must be cong or tru")

    data["balance"] = after
    data["last_seen"] = now_datetime_full()
    data.setdefault("wallet_logs", [])
    log_item = {
        "time": now_datetime_full(),
        "admin_id": admin_id,
        "type": mode,
        "amount": int(amount),
        "delta": int(delta),
        "before": int(before),
        "after": int(after),
        "reason": reason.strip()[:200]
    }
    data["wallet_logs"].insert(0, log_item)
    await save_user(user_id, data)

    return {
        "before": before,
        "after": after,
        "delta": delta,
        "log": log_item,
        "user_full_name": data.get("full_name"),
        "user_username": data.get("username"),
    }


# ================== AUTO TOPUP ==================
async def load_processed_tx() -> set[str]:
    async with PROCESSED_TX_LOCK:
        if not os.path.exists(PROCESSED_TX_PATH):
            _atomic_write_json(PROCESSED_TX_PATH, [])
            return set()
        try:
            with open(PROCESSED_TX_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError()
            return set(str(x) for x in data)
        except Exception:
            _atomic_write_json(PROCESSED_TX_PATH, [])
            return set()

async def save_processed_tx(tx_set: set[str]) -> None:
    async with PROCESSED_TX_LOCK:
        _atomic_write_json(PROCESSED_TX_PATH, list(tx_set))

async def credit_wallet(user_id: int, amount: int, txid: str, desc: str) -> tuple[int, int]:
    u = await load_user(user_id)
    before = int(u.get("balance", 0))
    after = before + int(amount)

    u["balance"] = after
    u["last_seen"] = now_datetime_full()
    u.setdefault("wallet_logs", [])
    u["wallet_logs"].insert(0, {
        "time": now_datetime_full(),
        "type": "credit",
        "source": "mbbank_api",
        "amount": int(amount),
        "before": before,
        "after": after,
        "txid": txid,
        "note": (desc or "")[:300],
    })
    await save_user(user_id, u)
    return before, after

async def poll_topups_forever(bot: Bot) -> None:
    processed = await load_processed_tx()
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                if not TOPUP_ENABLED:
                    await asyncio.sleep(TOPUP_POLL_SECONDS)
                    continue

                async with session.get(TOPUP_API_URL) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(TOPUP_POLL_SECONDS)
                        continue
                    data = await resp.json(content_type=None)

                if not isinstance(data, dict) or data.get("status") != "success":
                    await asyncio.sleep(TOPUP_POLL_SECONDS)
                    continue

                txs = data.get("transactions", [])
                if not isinstance(txs, list):
                    await asyncio.sleep(TOPUP_POLL_SECONDS)
                    continue

                changed = False

                for tx in txs:
                    if not isinstance(tx, dict):
                        continue

                    txid = str(tx.get("transactionID", "")).strip()
                    ttype = str(tx.get("type", "")).upper().strip()
                    amount_s = str(tx.get("amount", "")).strip()
                    desc = str(tx.get("description", "")).strip()
                    tdate = str(tx.get("transactionDate", "")).strip()

                    if not txid or txid in processed:
                        continue

                    if ttype != "IN":
                        processed.add(txid)
                        changed = True
                        continue

                    if not amount_s.isdigit():
                        processed.add(txid)
                        changed = True
                        continue

                    amount = int(amount_s)
                    if amount <= 0:
                        processed.add(txid)
                        changed = True
                        continue

                    uid = extract_user_id_from_description(desc)
                    if not uid:
                        processed.add(txid)
                        changed = True
                        continue

                    order_code = extract_order_code_from_description(desc)

                    # ===== direct order payment =====
                    if order_code:
                        pending = await load_direct_payment(order_code)
                        if not pending:
                            pending = await find_direct_payment_by_desc(uid, desc)
                        if not pending:
                            pending = await find_direct_payment_by_uid_amount(uid, amount)
                        if pending:
                            if int(pending.get("user_id", 0)) == uid:
                                need = int(pending.get("total", 0))
                                if amount == need and need > 0:
                                    await finalize_direct_order(bot, txid, tdate, desc, pending)
                                    processed.add(txid)
                                    changed = True
                                    continue
                                else:
                                    # direct pay yêu cầu đúng số tiền đơn, sai amount thì báo khách + admin.
                                    try:
                                        await bot.send_message(
                                            uid,
                                            "❌ THANH TOÁN ĐƠN CHUYỂN KHOẢN KHÔNG HỢP LỆ\n\n"
                                            f"🧾 Mã đơn: {order_code}\n"
                                            f"💰 Cần đúng: {vnd(need)} đ\n"
                                            f"💸 Đã chuyển: {vnd(amount)} đ\n"
                                            "👉 Đơn direct pay yêu cầu chuyển đúng số tiền. Vui lòng liên hệ admin để hỗ trợ."
                                        )
                                    except Exception:
                                        pass
                                    await notify_admin(
                                        bot,
                                        "⚠️ SAI SỐ TIỀN ĐƠN CHUYỂN KHOẢN TRỰC TIẾP\n\n"
                                        f"🧾 Mã đơn: {order_code}\n"
                                        f"🆔 User: {uid}\n"
                                        f"💰 Cần đúng: {vnd(need)} đ\n"
                                        f"💸 Đã chuyển: {vnd(amount)} đ\n"
                                        f"🧾 Mã GD: {txid}\n"
                                        "👉 Không tự hoàn tất đơn, cần xử lý thủ công."
                                    )
                                    processed.add(txid)
                                    changed = True
                                    continue
                            # có mã đơn nhưng sai user -> không cộng ví tự động
                            await notify_admin(
                                bot,
                                "⚠️ GIAO DỊCH CÓ MÃ ĐƠN CHUYỂN KHOẢN NHƯNG SAI USER\n\n"
                                f"🧾 Mã đơn: {order_code}\n"
                                f"🆔 User trong nội dung: {uid}\n"
                                f"🆔 User của đơn: {int(pending.get('user_id', 0))}\n"
                                f"💰 Số tiền: {vnd(amount)} đ\n"
                                f"🧾 Mã GD: {txid}\n"
                                "👉 Không tự cộng ví, cần kiểm tra thủ công."
                            )
                            processed.add(txid)
                            changed = True
                            continue

                        # Có mã đơn nhưng không còn pending -> KHÔNG fallback cộng ví để tránh sai luồng direct pay
                        await notify_admin(
                            bot,
                            "⚠️ GIAO DỊCH CÓ MÃ ĐƠN CHUYỂN KHOẢN KHÔNG TÌM THẤY PENDING\n\n"
                            f"🧾 Mã đơn: {order_code}\n"
                            f"🆔 User: {uid}\n"
                            f"💰 Số tiền: {vnd(amount)} đ\n"
                            f"🧾 Mã GD: {txid}\n"
                            "👉 Không tự cộng ví, cần kiểm tra thủ công."
                        )
                        processed.add(txid)
                        changed = True
                        continue

                    # ===== fallback: cộng ví =====
                    before, after = await credit_wallet(uid, amount, txid, desc)

                    try:
                        await bot.send_message(
                            uid,
                            "✅ NẠP TIỀN THÀNH CÔNG\n\n"
                            f"🧾 Mã GD: {txid}\n"
                            f"💰 Số tiền: {vnd(amount)} đ\n"
                            f"💼 Số dư trước: {vnd(before)} đ\n"
                            f"💼 Số dư sau: {vnd(after)} đ\n"
                            f"🕒 Thời gian: {tdate or now_datetime_full()}\n"
                            f"📝 Nội dung: {desc}"
                        )
                    except Exception:
                        pass

                    await notify_admin(
                        bot,
                        "💳 NẠP TIỀN TỰ ĐỘNG\n\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"🧾 Mã GD: {txid}\n"
                        f"👤 User ID: {uid}\n"
                        f"💰 Số tiền: {vnd(amount)} đ\n"
                        f"🕒 Thời gian: {tdate or now_datetime_full()}\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"📝 Nội dung: {desc}"
                    )

                    processed.add(txid)
                    changed = True

                if changed:
                    await save_processed_tx(processed)

            except Exception:
                pass

            await asyncio.sleep(TOPUP_POLL_SECONDS)


# ================== SHOP API HELPERS ==================
async def fetch_shop_api_products() -> tuple[bool, list[dict[str, Any]], str]:
    headers = {"X-API-Key": API_KEY} if API_KEY.strip() else {}
    url = f"{API_BASE_URL.rstrip('/')}/products"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params={"api_key": API_KEY} if API_KEY.strip() else None) as resp:
                if resp.status >= 400:
                    txt = await resp.text()
                    return False, [], f"HTTP {resp.status}: {txt[:160]}"
                data = await resp.json(content_type=None)
    except Exception as e:
        return False, [], str(e)

    if isinstance(data, dict) and data.get("success") is False:
        return False, [], str(data.get("message") or data.get("error") or "API /products trả về lỗi")

    items = data if isinstance(data, list) else data.get("products") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return False, [], "Dữ liệu API không hợp lệ"

    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or it.get("product_id") or "").strip()
        if not pid:
            continue
        try:
            price = int(it.get("price", 0))
        except Exception:
            price = 0
        try:
            stock = int(it.get("stock", 0))
        except Exception:
            stock = 0
        out.append({
            "id": pid,
            "name": str(it.get("name") or pid),
            "price": max(0, price),
            "stock": max(0, stock),
            "description": str(it.get("description") or ""),
        })
    return True, out, ""


async def fetch_api_products_for_admin(provider: str) -> tuple[bool, list[dict[str, Any]], str]:
    """Return provider catalog entries in the format used by the add-product flow."""
    if provider == "legacy":
        return await fetch_shop_api_products()

    api = FISHY_API if provider == "fishy" else SACHTAI_API
    ok, items, err = await api.get_products()
    if not ok:
        return False, [], err
    return True, [{
        "id": str(item.get("external_product_id", "")),
        "name": str(item.get("name", "")),
        "price": max(0, int(item.get("api_price", 0))),
        "stock": max(0, int(item.get("api_stock", 0))),
        "description": str(item.get("description", "")),
        "required_fields": item.get("required_fields", []),
        "fulfillment": str(item.get("fulfillment", "instant")),
        "warranty_hours": max(0, int(item.get("warranty_hours", 0))),
        "requires_customer_email": bool(item.get("requires_customer_email", False)),
    } for item in items], ""



async def fetch_shop_api_balance() -> tuple[bool, int, str]:
    headers = {"X-API-Key": API_KEY} if API_KEY.strip() else {}
    url = f"{API_BASE_URL.rstrip('/')}/balance"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params={"api_key": API_KEY} if API_KEY.strip() else None) as resp:
                if resp.status >= 400:
                    txt = await resp.text()
                    return False, 0, f"HTTP {resp.status}: {txt[:160]}"
                data = await resp.json(content_type=None)
    except Exception as e:
        return False, 0, str(e)

    if isinstance(data, dict) and data.get("success") is False:
        return False, 0, str(data.get("message") or data.get("error") or "API /balance trả về lỗi")

    try:
        if isinstance(data, dict):
            if isinstance(data.get("data"), dict):
                bal = int(data["data"].get("balance", 0))
            else:
                bal = int(data.get("balance", 0))
        else:
            bal = 0
    except Exception:
        bal = 0
    return True, max(0, bal), ""


async def shop_api_buy(product_id: str, quantity: int) -> tuple[bool, dict[str, Any], str, str]:
    """Mua hàng qua Shop API.
    Returns: (ok, order_data, err_code, err_msg)
    """
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"} if API_KEY.strip() else {"Content-Type": "application/json"}
    url = f"{API_BASE_URL.rstrip('/')}/buy"
    payload = {"product_id": str(product_id).strip(), "quantity": max(1, int(quantity))}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, params={"api_key": API_KEY} if API_KEY.strip() else None, json=payload) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    if isinstance(data, dict):
                        code = str(data.get("error") or data.get("code") or "")
                        msg = str(data.get("message") or f"HTTP {resp.status}")
                    else:
                        code = ""
                        msg = f"HTTP {resp.status}"
                    return False, {}, code, msg
    except Exception as e:
        return False, {}, "NETWORK_ERROR", str(e)

    if not isinstance(data, dict):
        return False, {}, "BAD_RESPONSE", "Phản hồi API không hợp lệ"

    if data.get("success") is False:
        return False, {}, str(data.get("error") or data.get("code") or "UNKNOWN_ERROR"), str(data.get("message") or "Mua API thất bại")

    order = data.get("order") if isinstance(data.get("order"), dict) else data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(order, dict):
        return False, {}, "BAD_RESPONSE", "Thiếu dữ liệu order từ API"
    return True, order, "", ""


async def fetch_provider_budget(provider: str) -> tuple[bool, int, dict[str, int], str]:
    """(ok, balance, {external_id: raw stock}, err) theo đúng provider."""
    if provider == API_PROVIDER_LEGACY:
        ok_b, balance, err_b = await fetch_shop_api_balance()
        if not ok_b:
            return False, 0, {}, f"Lỗi lấy balance API cũ: {err_b}"
        ok_p, items, err_p = await fetch_shop_api_products()
        if not ok_p:
            return False, 0, {}, f"Lỗi lấy products API cũ: {err_p}"
        stock_map = {
            str(x.get("id") or "").strip(): max(0, int(x.get("stock", 0) or 0))
            for x in items
        }
        return True, max(0, int(balance)), stock_map, ""

    api = FISHY_API if provider == API_PROVIDER_FISHYMARKET else SACHTAI_API
    ok_b, bal, err_b = await api.get_balance()
    if not ok_b:
        return False, 0, {}, f"Lỗi lấy balance {provider}: {err_b}"
    ok_p, items, err_p = await api.get_products()
    if not ok_p:
        return False, 0, {}, f"Lỗi lấy products {provider}: {err_p}"
    stock_map = {
        str(it.get("external_product_id") or "").strip(): max(0, int(it.get("api_stock", 0) or 0))
        for it in items
    }
    return True, max(0, int(bal.get("balance", 0) or 0)), stock_map, ""


def _lookup_stock(stock_map: dict[str, int], external_id: str) -> tuple[int, bool]:
    """Tra không phân biệt hoa/thường."""
    if external_id in stock_map:
        return max(0, int(stock_map[external_id] or 0)), True
    wanted = external_id.strip().lower()
    for key, value in stock_map.items():
        if str(key).strip().lower() == wanted:
            return max(0, int(value or 0)), True
    return 0, False


async def fetch_provider_stock(provider: str) -> tuple[bool, dict[str, int], str]:
    """(ok, {external_id: raw_stock}, err) theo đúng provider."""
    if provider == API_PROVIDER_LEGACY:
        ok_p, items, err_p = await fetch_shop_api_products()
        if not ok_p:
            return False, {}, f"Lỗi lấy products API cũ: {err_p}"
        stock_map = {
            str(x.get("id") or "").strip(): max(0, int(x.get("stock", 0) or 0))
            for x in items
        }
        return True, stock_map, ""

    api = FISHY_API if provider == API_PROVIDER_FISHYMARKET else SACHTAI_API
    ok_p, items, err_p = await api.get_products()
    if not ok_p:
        return False, {}, f"Lỗi lấy products {provider}: {err_p}"
    stock_map = {
        str(it.get("external_product_id") or "").strip(): max(0, int(it.get("api_stock", 0) or 0))
        for it in items
    }
    return True, stock_map, ""


async def get_api_stock_budget_snapshot(provider: str = API_PROVIDER_LEGACY) -> tuple[bool, dict[str, Any], str]:
    """Snapshot ngân sách 1 provider (không auto cộng stock)."""
    products = await load_products()
    api_products = [
        p for p in products
        if str(p.get("type", "local")) == "api"
        and api_external_product_id(p)
        and api_provider_key(p) == provider
    ]
    ok, balance, raw_stock, err = await fetch_provider_budget(provider)
    if not ok:
        return False, {}, err

    money_allocated = sum(
        max(0, int(p.get("stock", 0))) * max(0, int(p.get("api_price", 0)))
        for p in api_products
    )
    return True, {
        "provider": provider,
        "balance": max(0, int(balance)),
        "provider_stock": raw_stock,
        "money_allocated": max(0, int(money_allocated)),
        "money_available": max(0, int(balance) - int(money_allocated)),
    }, ""


async def enforce_api_stock_constraints(bot: Optional[Bot] = None) -> tuple[bool, str]:
    """Chỉ đồng bộ GIẢM stock API khi không còn hợp lệ; tuyệt đối không tự tăng stock."""
    products = await load_products()
    api_products = [
        p for p in products
        if str(p.get("type", "local")) == "api"
        and api_external_product_id(p)
    ]
    if not api_products:
        return True, "Không có sản phẩm API"

    by_provider: dict[str, list[dict[str, Any]]] = {}
    for p in api_products:
        by_provider.setdefault(api_provider_key(p), []).append(p)

    changed = False
    alerts: list[str] = []
    today = now_date()

    for provider, prov_products in by_provider.items():
        ok_b, balance, _stock_map_unused, err_b = await fetch_provider_budget(provider)
        if not ok_b:
            return False, f"Lỗi lấy balance {provider}: {err_b}"

        ok_p, raw_stock, err_p = await fetch_provider_stock(provider)
        if not ok_p:
            return False, f"Lỗi lấy products {provider}: {err_p}"

        usable_stock = {k: usable_provider_stock(v) for k, v in raw_stock.items()}

        # Anti-false-zero: re-check 2 lần nếu có sản phẩm đang có stock mà nguồn báo 0.
        suspect_ids: set[str] = set()
        for p in prov_products:
            ext_id = api_external_product_id(p)
            if int(p.get("stock", 0)) > 0 and usable_stock.get(ext_id, 0) <= 0:
                suspect_ids.add(ext_id)

        if suspect_ids:
            for _ in range(2):
                await asyncio.sleep(0.4)
                ok_retry, retry_raw, _ = await fetch_provider_stock(provider)
                if not ok_retry:
                    continue
                retry_usable = {k: usable_provider_stock(v) for k, v in retry_raw.items()}
                for sid in suspect_ids:
                    usable_stock[sid] = max(int(usable_stock.get(sid, 0)), int(retry_usable.get(sid, 0)))

        for p in prov_products:
            old_stock = max(0, int(p.get("stock", 0)))
            last_notified_date = str(p.get("out_of_stock_notified", "")).strip()
            was_notified_today = last_notified_date == today
            api_price = max(0, int(p.get("api_price", 0)))
            sale_price = max(0, int(p.get("price_vnd", 0)))
            provider_id = api_external_product_id(p)
            provider_cap = max(0, usable_stock.get(provider_id, 0))

            if api_price <= 0:
                if old_stock != 0:
                    p["stock"] = 0
                    changed = True
                if (old_stock > 0 or not was_notified_today) and bot:
                    p["out_of_stock_notified"] = today
                    changed = True
                continue
            if api_price > sale_price:
                if old_stock != 0:
                    p["stock"] = 0
                    changed = True
                if old_stock > 0 or not was_notified_today:
                    alerts.append(f"⚠️ {p.get('name')}: giá API ({vnd(api_price)}đ) > giá bán ({vnd(sale_price)}đ), đã khóa stock=0.")
                    p["out_of_stock_notified"] = today
                    changed = True
                continue
            if provider_cap <= 0:
                if old_stock != 0:
                    p["stock"] = 0
                    changed = True
                if old_stock > 0 or not was_notified_today:
                    alerts.append(f"⚠️ {p.get('name')}: phía API đã hết hàng/ẩn sản phẩm, stock bot đã về 0.")
                    p["out_of_stock_notified"] = today
                    changed = True
                continue
            if old_stock > provider_cap:
                p["stock"] = provider_cap
                changed = True
            if last_notified_date and not was_notified_today:
                p["out_of_stock_notified"] = ""
                changed = True

        # Nếu tổng tiền stock đã cấp vượt số dư provider => giảm dần cho hợp lệ.
        while True:
            allocated = sum(max(0, int(p.get("stock", 0))) * max(0, int(p.get("api_price", 0))) for p in prov_products)
            over = allocated - max(0, int(balance))
            if over <= 0:
                break
            reduced = False
            for p in sorted(prov_products, key=lambda x: int(x.get("api_price", 0)), reverse=True):
                if int(p.get("stock", 0)) <= 0:
                    continue
                p["stock"] = int(p.get("stock", 0)) - 1
                changed = True
                reduced = True
                allocated -= int(p.get("api_price", 0))
                if allocated <= int(balance):
                    break
            if not reduced:
                break

    if changed:
        await save_products(products)
    if bot:
        for line in alerts[:10]:
            await notify_admin(bot, line)
    return True, "ok"


def api_stock_delta_money(qty: int, api_price: int) -> int:
    return int(qty) * max(0, int(api_price))


async def api_stock_add_capacity(product: dict[str, Any]) -> dict[str, Any]:
    """max_add kèm lý do, để biết đang bị chặn bởi tồn nguồn hay bởi số dư."""
    provider = api_provider_key(product)
    external_id = api_external_product_id(product)
    api_price = max(0, int(product.get("api_price", 0)))
    current = max(0, int(product.get("stock", 0)))
    out: dict[str, Any] = {
        "max_add": 0, "provider": provider, "external_id": external_id,
        "api_price": api_price, "current": current,
        "provider_raw": 0, "provider_cap": 0, "provider_room": 0,
        "balance": 0, "money_allocated": 0, "money_available": 0, "money_room": 0,
        "key_found": False, "reason": "",
    }
    ok, snap, err = await get_api_stock_budget_snapshot(provider)
    if not ok:
        out["reason"] = f"Không lấy được số dư/tồn kho từ nguồn: {err}"
        return out

    raw_map = snap.get("provider_stock") if isinstance(snap.get("provider_stock"), dict) else {}
    provider_raw, key_found = _lookup_stock(raw_map, external_id)
    provider_cap = usable_provider_stock(provider_raw)
    money_available = max(0, int(snap.get("money_available", 0)))
    out.update({
        "key_found": key_found, "provider_raw": provider_raw, "provider_cap": provider_cap,
        "money_available": money_available,
        "balance": max(0, int(snap.get("balance", 0))),
        "money_allocated": max(0, int(snap.get("money_allocated", 0))),
    })

    if not key_found:
        out["reason"] = f"Nguồn không trả về sản phẩm id={external_id!r} (sai id hoặc đã bị ẩn)."
        return out
    if api_price <= 0:
        out["reason"] = "api_price = 0, chưa sync được giá gốc."
        return out
    if provider_cap <= 0:
        out["reason"] = f"Nguồn chỉ còn {provider_raw}, phải chừa {API_STOCK_SAFETY_BUFFER} nên không thêm được."
        return out

    provider_room = max(0, provider_cap - current)
    money_room = money_available // api_price
    out["provider_room"] = provider_room
    out["money_room"] = money_room
    out["max_add"] = max(0, min(provider_room, money_room))

    if out["max_add"] > 0:
        out["reason"] = "ok"
    elif provider_room <= 0:
        out["reason"] = f"Đã cấp {current}/{provider_cap} (nguồn {provider_raw} − chừa {API_STOCK_SAFETY_BUFFER})."
    else:
        out["reason"] = (
            f"Số dư khả dụng {vnd(money_available)}đ < giá gốc {vnd(api_price)}đ "
            f"(số dư {vnd(out['balance'])}đ, {vnd(out['money_allocated'])}đ đang bị stock đã cấp giữ chỗ)."
        )
    return out


async def api_max_addable_units(product: dict[str, Any]) -> tuple[int, int, int]:
    cap = await api_stock_add_capacity(product)
    return int(cap["max_add"]), int(cap["money_available"]), int(cap["provider_room"])


def api_provider_select_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔌 API cũ", callback_data="adm:add_api_provider:legacy")
    kb.button(text="🔌 FishyMarket", callback_data="adm:add_api_provider:fishy")
    kb.button(text="🔌 Sachtai", callback_data="adm:add_api_provider:sachtai")
    kb.button(text="⬅️ Quay lại Menu", callback_data="back_home")
    kb.adjust(1)
    return kb.as_markup()


def api_products_select_kb(provider: str, items: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for it in items[:80]:
        kb.button(
            text=f"🌐 {it['name']} | gốc {vnd(int(it.get('price', 0)))}đ | kho {int(it.get('stock', 0))}",
            callback_data=f"adm:pick_api:{provider}:{it['id']}"
        )
    kb.button(text="⬅️ Quay lại Menu", callback_data="back_home")
    kb.adjust(1)
    return kb.as_markup()



async def poll_api_constraints_forever(bot: Bot) -> None:
    while True:
        try:
            await enforce_api_stock_constraints(bot)
        except Exception:
            pass
        await asyncio.sleep(10)




# ================== AUTO ORDER POLLING / SYNC WORKERS ==================
def start_auto_order_poll_task(bot: Bot, order_code: str) -> None:
    old = ORDER_POLL_TASKS.get(order_code)
    if old and not old.done():
        return
    ORDER_POLL_TASKS[order_code] = asyncio.create_task(poll_single_auto_order(bot, order_code))


async def poll_single_auto_order(bot: Bot, order_code: str) -> None:
    while True:
        order = await get_auto_order(order_code)
        if not order or str(order.get("status")) not in {ORDER_STATUS_PROCESSING}:
            return
        if int(order.get("poll_count", 0) or 0) >= AUTO_ORDER_MAX_POLLS:
            async with _order_lock(order_code):
                order = await get_auto_order(order_code) or order
                before_s = str(order.get("status"))
                order["status"] = ORDER_STATUS_FAILED
                order["api_status"] = "timeout"
                order["error_message"] = "Quá số lần polling tối đa, cần Admin kiểm tra thủ công."
                order["updated_at"] = now_datetime_full()
                add_order_history(order, "system", "Polling timeout", before_s, ORDER_STATUS_FAILED, order["error_message"])
                await upsert_auto_order(order)
                await update_user_order_status(int(order.get("user_id", 0)), order_code, "❌ API thất bại - chờ Admin xử lý")
            try:
                await bot.send_message(int(order.get("user_id", 0)), f"⏳ ĐƠN HÀNG ĐANG GẶP SỰ CỐ\n\n🆔 #{order_code}\n📦 {order.get('product_name')}\n\nĐơn hàng đang được kiểm tra, Admin sẽ phản hồi cho bạn.")
            except Exception:
                pass
            await notify_admin(bot, f"❌ AUTO ORDER TIMEOUT\n\n🆔 #{order_code}\n📡 API Order: {order.get('external_order_code')}\n🆔 User ID: {order.get('user_id')}\n📦 {order.get('product_name')}\n\n👉 Chọn hành động:", reply_markup=auto_order_failed_admin_kb(order_code, int(order.get("user_id", 0))))
            return
        provider = SACHTAI_API if str(order.get("api_provider") or "").upper() == API_PROVIDER_SACHTAI else FISHY_API
        ok, status_data, err = await provider.get_order_status(str(order.get("external_order_code")))
        async with _order_lock(order_code):
            order = await get_auto_order(order_code) or order
            if str(order.get("status")) != ORDER_STATUS_PROCESSING:
                return
            order["poll_count"] = int(order.get("poll_count", 0) or 0) + 1
            before_s = str(order.get("status"))
            if not ok:
                order["last_poll_error"] = err
                order["updated_at"] = now_datetime_full()
                add_order_history(order, "system", "Poll API lỗi", before_s, before_s, err)
                await upsert_auto_order(order)
                await asyncio.sleep(AUTO_ORDER_POLL_INTERVAL_SECONDS)
                continue
            api_status = str(status_data.get("status", "pending")).lower()
            order["api_status"] = api_status
            order["updated_at"] = now_datetime_full()
            if api_status in {"pending", "processing"}:
                add_order_history(order, "system", "Poll API", before_s, before_s, api_status)
                await upsert_auto_order(order)
                if api_status == "processing" and int(order.get("poll_count", 0)) == 1:
                    try:
                        await bot.send_message(int(order.get("user_id", 0)), f"🔄 Đơn hàng #{order_code} đang được xử lý.\n\n📦 {order.get('product_name')}\n📧 {order.get('customer_email')}\n\nTrạng thái: PROCESSING\n\nVui lòng chờ, hệ thống đang xử lý tự động.")
                    except Exception:
                        pass
                await asyncio.sleep(AUTO_ORDER_POLL_INTERVAL_SECONDS)
                continue
            if api_status in {"invited", "completed", "in_team"}:
                order["status"] = ORDER_STATUS_COMPLETED
                order["completed_at"] = now_datetime_full()
                order["completed_at_unix"] = now_unix()
                order["email_change_deadline_unix"] = int(order.get("created_at_unix", now_unix())) + EMAIL_CHANGE_WINDOW_SECONDS
                add_order_history(order, "system", "API completed", before_s, ORDER_STATUS_COMPLETED, "API hoàn thành")
                await upsert_auto_order(order)
                await update_user_order_status(int(order.get("user_id", 0)), order_code, "✅ Đã hoàn thành")
                try:
                    await bot.send_message(int(order.get("user_id", 0)), f"✅ ĐƠN HÀNG HOÀN THÀNH\n\n🆔 #{order_code}\n📦 {order.get('product_name')}\n📧 {order.get('customer_email')}\n\n🎉 Đơn API đã được thực hiện thành công.\n\nBạn có thể sử dụng dịch vụ theo hướng dẫn.", reply_markup=auto_order_user_kb(order))
                except Exception:
                    pass
                await notify_admin(bot, f"✅ AUTO ORDER HOÀN THÀNH\n\n🆔 #{order_code}\n📡 API Order: {order.get('external_order_code')}\n🆔 User ID: {order.get('user_id')}\n📦 {order.get('product_name')}\n📧 {order.get('customer_email')}", reply_markup=auto_admin_reply_kb(order_code, int(order.get("user_id", 0))))
                return
            if api_status == "failed":
                order["status"] = ORDER_STATUS_FAILED
                order["error_message"] = str(status_data.get("error_message") or "API xử lý thất bại.")
                add_order_history(order, "system", "API failed", before_s, ORDER_STATUS_FAILED, order["error_message"])
                await upsert_auto_order(order)
                await update_user_order_status(int(order.get("user_id", 0)), order_code, "❌ API thất bại - chờ Admin xử lý")
                try:
                    await bot.send_message(int(order.get("user_id", 0)), f"⏳ ĐƠN HÀNG ĐANG GẶP SỰ CỐ\n\n🆔 #{order_code}\n📦 {order.get('product_name')}\n\nĐơn hàng đang được kiểm tra, Admin sẽ phản hồi cho bạn.")
                except Exception:
                    pass
                await notify_admin(bot, f"❌ AUTO ORDER FAILED\n\n🆔 #{order_code}\n📡 API Order: {order.get('external_order_code')}\n🆔 User ID: {order.get('user_id')}\n📝 {order.get('error_message')}\n\n👉 Chọn hành động:", reply_markup=auto_order_failed_admin_kb(order_code, int(order.get("user_id", 0))))
                return
        await asyncio.sleep(AUTO_ORDER_POLL_INTERVAL_SECONDS)


async def resume_auto_order_polling(bot: Bot) -> None:
    orders = await load_auto_orders()
    for oc, order in orders.items():
        if str(order.get("status")) == ORDER_STATUS_PROCESSING:
            start_auto_order_poll_task(bot, str(oc))


async def sync_api_products_forever(bot: Bot) -> None:
    last_errors: dict[str, str] = {}
    notified_price_changes: set[str] = set()
    while True:
        for name, sync_func in (("FishyMarket", sync_fishy_products), ("Sachtai", sync_sachtai_products)):
            try:
                ok, msg, created, updated, price_changes = await sync_func()
                if not ok:
                    # A persistent provider outage should not spam the admin on
                    # every polling cycle. Notify again only when it changes.
                    if last_errors.get(name) != msg:
                        await notify_admin(bot, f"⚠️ Đồng bộ {name} thất bại: {msg}")
                    last_errors[name] = msg
                else:
                    last_errors.pop(name, None)
                    # Process price changes detected during sync
                    for pc in price_changes:
                        change_key = f"{pc['product_id']}:{pc['old_api_price']}:{pc['new_api_price']}"
                        if change_key in notified_price_changes:
                            continue
                        notified_price_changes.add(change_key)
                        new_p = pc["new_api_price"]
                        sale_p = pc["sale_price"]
                        if new_p > sale_p:
                            await notify_admin(bot,
                                f"🚨 Giá API cao hơn giá bán — stock đã về 0\n\n"
                                f"📦 {pc['name']}\n"
                                f"💰 Giá API: {vnd(pc['old_api_price'])}đ → {vnd(new_p)}đ\n"
                                f"💵 Giá bán: {vnd(sale_p)}đ\n\n"
                                f"⚠️ Lý do: Giá API ({vnd(new_p)}đ) > Giá bán ({vnd(sale_p)}đ)\n"
                                f"🕒 {now_datetime_full()}"
                            )
                        else:
                            await notify_admin(bot,
                                f"⚠️ Giá API thay đổi\n\n"
                                f"📦 {pc['name']}\n"
                                f"💰 Giá API: {vnd(pc['old_api_price'])}đ → {vnd(new_p)}đ\n"
                                f"💵 Giá bán: {vnd(sale_p)}đ\n\n"
                                f"🕒 {now_datetime_full()}"
                            )
                    # Enforce stock constraints after sync (handles stock=0 when api_price > sale_price)
                    if updated > 0:
                        try:
                            await enforce_api_stock_constraints(bot)
                        except Exception:
                            pass
                    # Clean up old notification keys (keep last 500)
                    if len(notified_price_changes) > 500:
                        notified_price_changes = set(list(notified_price_changes)[-250:])
            except Exception as e:
                msg = str(e)
                if last_errors.get(name) != msg:
                    await notify_admin(bot, f"⚠️ Worker sync {name} lỗi: {msg}")
                last_errors[name] = msg
        await asyncio.sleep(API_SYNC_INTERVAL_SECONDS)


async def sync_fishy_products_forever(bot: Bot) -> None:
    await sync_api_products_forever(bot)


async def poll_auto_orders_forever(bot: Bot) -> None:
    await resume_auto_order_polling(bot)
    while True:
        try:
            await resume_auto_order_polling(bot)
        except Exception:
            pass
        await asyncio.sleep(AUTO_ORDER_POLL_INTERVAL_SECONDS)


# ================== TOPUP UI ==================
def vietqr_url_by_content(content: str, amount: int = 0) -> str:
    content = re.sub(r"\s+", "%20", content.strip())
    amount = max(0, int(amount))
    return (
        "https://img.vietqr.io/image/mbbank-0000032377312-compact2.jpg"
        f"?amount={amount}&addInfo={content}&accountName=Hoang%20Phu%20Minh"
    )

def vietqr_url(user_id: int) -> str:
    return vietqr_url_by_content(nap_code(user_id))

def naptien_caption(user_id: int, balance_vnd: int) -> str:
    code = nap_code(user_id)
    return (
        "╭─「 💳 NẠP TIỀN TỰ ĐỘNG 」─╮\n"
        f"🙍‍♂️ ID tài khoản: {user_id}\n"
        f"💼 Số dư ví: {vnd(balance_vnd)} đ\n"
        "╰────────────────────╯\n\n"
        "✅ Quét QR để chuyển khoản nhanh.\n"
        "📝 Nội dung chuyển khoản bắt buộc:\n"
        f"`{code}`\n\n"
        "⏱ Bot sẽ tự cộng tiền khi giao dịch khớp.\n"
        "📢 Nếu chuyển sai nội dung, hãy liên hệ hỗ trợ."
    )

def direct_pay_caption(payload: dict[str, Any]) -> str:
    return (
        "╭─「 🧾 XÁC NHẬN THANH TOÁN 」─╮\n"
        f"Mã đơn: {payload['order_code']}\n"
        "╰────────────────────╯\n\n"
        f"📦 Sản phẩm: {payload['product_name']}\n"
        f"🔢 Số lượng: {payload['qty']}\n"
        f"💰 Cần thanh toán: {vnd(int(payload['total']))} đ\n"
        f"⏳ Giữ hàng: {RESERVE_HOLD_SECONDS // 60} phút\n\n"
        "📝 Nội dung chuyển khoản bắt buộc:\n"
        f"`{payload['payment_desc']}`\n\n"
        "✅ Bot sẽ tự giao hàng khi giao dịch khớp đúng."
    )


# ================== LANGUAGE UI ==================
TEXTS = {
    "vi": {
        "menu": "Menu 🛍",
        "orders": "Đơn Hàng ⏱",
        "topup": "Nạp Tiền 💳",
        "support": "Hỗ Trợ 📢",
        "profile": "Tài Khoản 🙍‍♂️",
        "language": "🌐 Ngôn ngữ / Language",
        "back_home": "⬅️ Quay lại Menu",
        "custom_qty": "Tùy chọn",
        "confirm": "✅ Xác nhận",
        "change_qty": "✏️ Đổi số lượng",
        "cancel": "❌ Hủy",
        "pay_wallet": "💼 Thanh toán bằng ví ({balance}đ)",
        "pay_bank": "🏦 Chuyển khoản trực tiếp đơn này",
        "cancel_release": "❌ Hủy (hoàn kho)",
        "product_button": "✨ {name} • {price}đ • Còn {stock}",
        "menu_title": "Menu Đơn Hàng 🛍\nChọn sản phẩm bên dưới 👇",
        "lang_saved": "✅ Đã đổi giao diện sang Tiếng Việt.",
        "lang_title": "🌐 CHỌN NGÔN NGỮ GIAO DIỆN\n\nBạn có thể đổi ngôn ngữ bất cứ lúc nào.",
    },
    "en": {
        "menu": "Shop 🛍",
        "orders": "Orders ⏱",
        "topup": "Top Up 💳",
        "support": "Support 📢",
        "profile": "Account 🙍‍♂️",
        "language": "🌐 Language / Ngôn ngữ",
        "back_home": "⬅️ Back to Home",
        "custom_qty": "Custom",
        "confirm": "✅ Confirm",
        "change_qty": "✏️ Change quantity",
        "cancel": "❌ Cancel",
        "pay_wallet": "💼 Pay with wallet ({balance}đ)",
        "pay_bank": "🏦 Bank transfer for this order",
        "cancel_release": "❌ Cancel (release stock)",
        "product_button": "✨ {name} • {price}đ • Stock {stock}",
        "menu_title": "Shop Menu 🛍\nChoose a product below 👇",
        "lang_saved": "✅ Interface language changed to English.",
        "lang_title": "🌐 CHOOSE INTERFACE LANGUAGE\n\nYou can change this anytime.",
    },
}


def normalize_lang(value: Any) -> str:
    return "en" if str(value).lower().strip() == "en" else "vi"


def tr(lang: str, key: str, **kwargs: Any) -> str:
    text = TEXTS.get(normalize_lang(lang), TEXTS["vi"]).get(key, TEXTS["vi"].get(key, key))
    return text.format(**kwargs) if kwargs else text


async def get_user_lang(user_id: int) -> str:
    user = await load_user(user_id)
    return normalize_lang(user.get("lang", "vi"))


async def set_user_lang(user_id: int, lang: str) -> str:
    lang = normalize_lang(lang)
    user = await load_user(user_id)
    user["lang"] = lang
    await save_user(user_id, user)
    return lang


def language_kb(lang: str = "vi") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🇻🇳 Tiếng Việt", callback_data="setlang:vi")
    kb.button(text="🇬🇧 English", callback_data="setlang:en")
    kb.button(text=tr(lang, "back_home"), callback_data="back_home")
    kb.adjust(2, 1)
    return kb.as_markup()

def product_display_name(product: dict[str, Any]) -> str:
    name = str(product.get("name", ""))
    sale_percent = int(product.get("sale_percent", 0) or 0)
    if sale_percent > 0:
        return f"🔥 SALE {name} -{sale_percent}%"
    return name


def product_button_text(product: dict[str, Any], lang: str) -> str:
    display = product_display_name(product)
    price = vnd(int(product.get("price_vnd", 0)))
    sale_percent = int(product.get("sale_percent", 0) or 0)
    original = int(product.get("original_price_vnd", 0) or 0)
    stock = product.get("stock", 0)
    hot_prefix = "⭐ " if bool(product.get("is_hot", False)) else ""
    if sale_percent > 0 and original > int(product.get("price_vnd", 0)):
        if normalize_lang(lang) == "en":
            return f"{hot_prefix}🔥 {display} • {price}đ (was {vnd(original)}đ) • Stock {stock}"
        return f"{hot_prefix}🔥 {display} • {price}đ (giá gốc {vnd(original)}đ) • Còn {stock}"
    return f"{hot_prefix}{tr(lang, 'product_button', name=display, price=price, stock=stock)}"


def sale_time_text(product: dict[str, Any]) -> str:
    ends_at = int(product.get("sale_ends_at", 0) or 0)
    if ends_at <= 0:
        return "⏳ Thời gian: đến khi admin tắt sale"
    remain = max(0, ends_at - int(time.time()))
    end_text = datetime.fromtimestamp(ends_at).strftime("%d/%m/%Y %H:%M")
    if remain <= 0:
        return f"⏳ Đã hết hạn lúc: {end_text}"
    hours = remain // 3600
    minutes = (remain % 3600) // 60
    if hours > 0:
        return f"⏳ Còn khoảng: {hours} giờ {minutes} phút (đến {end_text})"
    return f"⏳ Còn khoảng: {minutes} phút (đến {end_text})"


async def set_product_sale(product_id: str, percent: int, duration_hours: int = 0) -> tuple[bool, dict[str, Any] | None, str]:
    products = await load_products()
    product = next((p for p in products if p.get("id") == product_id), None)
    if not product:
        return False, None, "Không tìm thấy sản phẩm."
    percent = max(0, min(99, int(percent)))
    duration_hours = max(0, int(duration_hours or 0))
    if percent <= 0:
        original = int(product.get("original_price_vnd", 0) or 0)
        if original > 0:
            product["price_vnd"] = original
        product["sale_percent"] = 0
        product["original_price_vnd"] = 0
        product["sale_ends_at"] = 0
    else:
        current_original = int(product.get("original_price_vnd", 0) or 0) or int(product.get("price_vnd", 0))
        product["original_price_vnd"] = current_original
        product["sale_percent"] = percent
        product["price_vnd"] = max(0, int(current_original * (100 - percent) / 100))
        product["sale_ends_at"] = int(time.time()) + duration_hours * 3600 if duration_hours > 0 else 0
    await save_products(products)
    return True, product, ""


def sale_announcement_text(product: dict[str, Any]) -> str:
    sale_percent = int(product.get("sale_percent", 0) or 0)
    if sale_percent <= 0:
        return (
            "╭─「 📢 THÔNG BÁO SHOP 」─╮\n"
            "✅ SALE ĐÃ KẾT THÚC\n"
            "╰────────────────────╯\n\n"
            f"📦 Sản phẩm: {product.get('name')}\n"
            f"💰 Giá hiện tại: {vnd(int(product.get('price_vnd', 0)))}đ\n\n"
            "👉 Vào /menu để xem sản phẩm mới nhất."
        )
    original = int(product.get("original_price_vnd", 0) or 0)
    sale_price = int(product.get("price_vnd", 0))
    saved = max(0, original - sale_price)
    return (
        "╭─「 🔥 SALE HOT 」─╮\n"
        f"🏷 GIẢM {sale_percent}% HÔM NAY\n"
        "╰────────────────────╯\n\n"
        f"📦 {product_display_name(product)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💵 Giá gốc: {vnd(original)}đ\n"
        f"💰 Giá sale: {vnd(sale_price)}đ\n"
        f"💸 Tiết kiệm: {vnd(saved)}đ\n"
        f"📊 Tồn kho: {int(product.get('stock', 0))}\n"
        f"{sale_time_text(product)}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👉 Bấm /menu để mua ngay khi còn hàng."
    )


def sale_after_update_kb(product_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📣 Thông báo sale cho TẤT CẢ", callback_data=f"adm:sale_notify:{product_id}")
    kb.button(text="🛍 Xem menu sản phẩm", callback_data="menu")
    kb.button(text="⬅️ Admin Panel", callback_data="adm:panel")
    kb.adjust(1)
    return kb.as_markup()


# ================== UI / KEYBOARDS ==================
def start_inline_kb(uid: Optional[int] = None, lang: str = "vi") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=tr(lang, "menu"), callback_data="menu")
    kb.button(text=tr(lang, "orders"), callback_data="orders:0")
    kb.button(text=tr(lang, "topup"), callback_data="naptien")
    kb.button(text=tr(lang, "support"), callback_data="support")
    kb.button(text=tr(lang, "profile"), callback_data="profile")
    kb.button(text=tr(lang, "language"), callback_data="language")
    if uid is not None and is_admin(uid):
        kb.button(text="🛠 Admin Panel", callback_data="adm:panel")
        kb.button(text="➕ Thêm sản phẩm", callback_data="adm:addsp")
        txt = "🟥 Tắt auto nạp" if TOPUP_ENABLED else "🟩 Bật auto nạp"
        kb.button(text=txt, callback_data="adm:toggle_topup")
    kb.adjust(2, 2, 2, 1, 2 if uid is not None and is_admin(uid) else 0)
    return kb.as_markup()

def back_to_menu_kb(lang: str = "vi") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=tr(lang, "back_home"), callback_data="back_home")
    return kb.as_markup()



def qty_kb(product_id: str, lang: str = "vi") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for q in (1, 2, 5, 10):
        kb.button(text=str(q), callback_data=f"buyqty:{product_id}:{q}")
    kb.button(text=tr(lang, "custom_qty"), callback_data=f"buyqty_custom:{product_id}")
    kb.button(text=tr(lang, "back_home"), callback_data="back_home")
    kb.adjust(4, 1, 1)
    return kb.as_markup()

def buy_confirm_kb(product_id: str, qty: int, lang: str = "vi") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=tr(lang, "confirm"), callback_data=f"buyconfirm:{product_id}:{qty}")
    kb.button(text=tr(lang, "change_qty"), callback_data=f"buychange:{product_id}")
    kb.button(text=tr(lang, "cancel"), callback_data=f"buycancel:{product_id}")
    kb.adjust(1, 2)
    return kb.as_markup()

def pay_method_kb(product_id: str, qty: int, balance: int, lang: str = "vi") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=tr(lang, "pay_wallet", balance=vnd(balance)), callback_data=f"paywallet:{product_id}:{qty}")
    kb.button(text=tr(lang, "pay_bank"), callback_data=f"paybank:{product_id}:{qty}")
    kb.button(text=tr(lang, "cancel_release"), callback_data=f"buycancel:{product_id}")
    kb.adjust(1, 1, 1)
    return kb.as_markup()

async def products_kb_for_user(uid: int) -> InlineKeyboardMarkup:
    products = await load_products()
    categories = await load_categories()
    lang = await get_user_lang(uid)
    kb = InlineKeyboardBuilder()
    # Hot products section - shown outside category groups
    hot_products = [p for p in products if bool(p.get("is_hot", False)) and (is_admin(uid) or bool(p.get("is_active", True)))]
    if hot_products:
        for hp in hot_products[:10]:
            kb.button(text=f"⭐ {product_button_text(hp, lang)}", callback_data=f"prod:{hp['id']}")
    active_cats = [c for c in categories if bool(c.get("is_active", True)) or is_admin(uid)]
    for c in active_cats:
        cid = str(c.get("id"))
        count = sum(1 for p in products if str(p.get("category_id", "cat_other")) == cid and (is_admin(uid) or bool(p.get("is_active", True))))
        if count > 0 or is_admin(uid):
            status = "" if bool(c.get("is_active", True)) else " 🔴"
            kb.button(text=f"{c.get('icon', '📁')} {c.get('name')} ({count}){status}", callback_data=f"cat:{cid}")
    uncategorized = [p for p in products if not str(p.get("category_id", "")) or str(p.get("category_id")) == "cat_other"]
    if uncategorized and not any(str(c.get("id")) == "cat_other" for c in active_cats):
        kb.button(text=f"🛍 Khác ({len(uncategorized)})", callback_data="cat:cat_other")
    if is_admin(uid):
        kb.button(text="➕ Thêm sản phẩm", callback_data="adm:addsp")
        kb.button(text="🌐 Thêm sản phẩm tự động", callback_data="adm:add_api_sp")
        kb.button(text="📝 Thêm sản phẩm đơn tay", callback_data="adm:add_manual_sp")
        kb.button(text="📁 Quản lý nhóm sản phẩm", callback_data="adm:categories")
        kb.button(text="⚙️ Cấu hình API", callback_data="adm:api_config")
        kb.button(text="🗑 Xóa sản phẩm", callback_data="adm:delsp")
    kb.button(text=tr(lang, "back_home"), callback_data="back_home")
    kb.adjust(1)
    return kb.as_markup()

async def products_kb_for_category(uid: int, category_id: str) -> InlineKeyboardMarkup:
    products = await load_products()
    lang = await get_user_lang(uid)
    kb = InlineKeyboardBuilder()
    for p in products:
        if str(p.get("category_id", "cat_other")) != category_id:
            continue
        if (not is_admin(uid)) and not bool(p.get("is_active", True)):
            continue
        kb.button(text=product_button_text(p, lang), callback_data=f"prod:{p['id']}")
    if is_admin(uid):
        label = "➕ Thêm sản phẩm ngoài nhóm" if category_id == "cat_other" else "➕ Thêm sản phẩm vào nhóm"
        kb.button(text=label, callback_data=f"adm:addsp_cat:{category_id}")
        kb.button(text="🗑 Xóa sản phẩm", callback_data=f"adm:delsp_cat:{category_id}")
    kb.button(text="⬅️ Danh sách nhóm", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()

async def products_delete_list_kb(category_id: Optional[str] = None) -> InlineKeyboardMarkup:
    products = await load_products()
    kb = InlineKeyboardBuilder()
    if category_id is not None:
        products = [p for p in products if str(p.get("category_id", "cat_other")) == category_id]
    for p in products[:80]:
        kb.button(
            text=f"🗑 {p['name']} - {vnd(int(p['price_vnd']))}đ [ {p.get('stock', 0)} ]",
            callback_data=f"adm:del:{p['id']}"
        )
    kb.button(text="⬅️ Quay lại Menu", callback_data="back_home")
    kb.adjust(1)
    return kb.as_markup()

def admin_data_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ XÁC NHẬN LƯU", callback_data="adm:data_confirm")
    kb.button(text="❌ HỦY", callback_data="adm:data_cancel")
    kb.adjust(2)
    return kb.as_markup()

def admin_after_add_kb(product_id: str, added_count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 XÓA HÀNG VỪA THÊM", callback_data=f"adm:undo_add:{product_id}:{added_count}")
    kb.button(text="🔁 THÊM TIẾP / GỬI LẠI", callback_data=f"adm:add_more:{product_id}")
    kb.button(text="📝 Sửa thông tin chi tiết", callback_data=f"adm:editdetail:{product_id}")
    kb.button(text="📣 Thông báo TẤT CẢ người dùng", callback_data=f"adm:notify_all:{product_id}:{added_count}")
    kb.button(text="⬅️ Quay lại Menu", callback_data="back_home")
    kb.adjust(1, 1, 1, 1, 1)
    return kb.as_markup()

def admin_product_manage_kb(product_id: str, product_type: str = "local", fishy_managed: bool = False, is_hot: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if product_type == "dontay":
        kb.button(text="➕ Thêm Stock", callback_data=f"adm:manual_stock_add:{product_id}")
        kb.button(text="➖ Giảm Stock", callback_data=f"adm:manual_stock_reduce:{product_id}")
        kb.button(text="📊 Xem Stock", callback_data=f"adm:manual_stock_view:{product_id}")
    elif product_type == "api":
        kb.button(text="➕ Thêm stock tự động", callback_data=f"adm:api_add:{product_id}")
        kb.button(text="➖ Giảm stock tự động", callback_data=f"adm:api_reduce:{product_id}")
        kb.button(text="💳 Quản lý nguồn hàng", callback_data="adm:api_balance")
        if fishy_managed:
            kb.button(text="🛡 Cài ngưỡng hết hàng trước", callback_data=f"adm:fishy_threshold:{product_id}")
    else:
        kb.button(text="📦 Thêm dữ liệu / hàng", callback_data=f"adm:manage_data:{product_id}")
    kb.button(text="📝 Sửa thông tin chi tiết", callback_data=f"adm:editdetail:{product_id}")
    kb.button(text="💵 Sửa giá bán", callback_data=f"adm:editprice:{product_id}")
    kb.button(text="🔥 Sale % / Tắt sale", callback_data=f"adm:sale:{product_id}")
    kb.button(text="📁 Chuyển nhóm / Ra ngoài nhóm", callback_data=f"adm:move_product:{product_id}")
    hot_label = "⭐ Tắt Hot" if is_hot else "⭐ Đánh dấu Hot"
    kb.button(text=hot_label, callback_data=f"adm:hot_toggle:{product_id}")
    kb.button(text="🗑 Xóa sản phẩm", callback_data=f"adm:del:{product_id}")
    kb.button(text="⬅️ Quay lại Menu", callback_data="back_home")
    kb.adjust(1)
    return kb.as_markup()


async def product_category_move_kb(product_id: str) -> InlineKeyboardMarkup:
    """Build the admin-only destination picker for an existing product."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Để ngoài nhóm", callback_data=f"adm:move_to:{product_id}:cat_other")
    for category in await load_categories():
        kb.button(
            text=f"{category.get('icon', '📁')} {category.get('name', 'Nhóm')}",
            callback_data=f"adm:move_to:{product_id}:{category.get('id')}",
        )
    kb.button(text="⬅️ Quay lại", callback_data=f"prod:{product_id}")
    kb.adjust(1)
    return kb.as_markup()


# ================== START TEXT ==================
def start_text(full_name: str, admin: bool, lang: str = "vi") -> str:
    if normalize_lang(lang) == "en":
        base = (
            "╭─「 ✨ PREMIUM SHOP 24/7 」─╮\n"
            f"👋 Hello {full_name or 'there'}!\n"
            "╰────────────────────╯\n\n"
            "🛍 Digital products with automatic delivery\n"
            "⚡ Fast checkout - instant fulfillment\n"
            "🛡 Order support/warranty available\n"
            "💳 Automatic top-up by QR transfer\n\n"
            "📌 Choose an option below to begin:\n"
            "• 🛍 Shop: browse products\n"
            "• ⏱ Orders: purchase history\n"
            "• 💳 Top Up: create a top-up QR/code\n"
            "• 📢 Support: contact admin\n"
            "• 🙍‍♂️ Account: balance & stats"
        )
        admin_note = "\n\n🛠 Admin tools are grouped in Admin Panel.\nQuick commands: /us, /edit, /tin, /topup, /members"
    else:
        base = (
            "╭─「 ✨ PREMIUM SHOP 24/7 」─╮\n"
            f"👋 Xin chào {full_name or 'bạn'}!\n"
            "╰────────────────────╯\n\n"
            "🛍 Sản phẩm số giao tự động\n"
            "⚡ Xử lý nhanh - nhận hàng ngay\n"
            "🛡 Có hỗ trợ/bảo hành theo đơn\n"
            "💳 Nạp tiền tự động bằng QR\n\n"
            "📌 Chọn chức năng bên dưới để bắt đầu:\n"
            "• 🛍 Menu: xem sản phẩm\n"
            "• ⏱ Đơn hàng: lịch sử mua\n"
            "• 💳 Nạp tiền: tạo mã/QR nạp\n"
            "• 📢 Hỗ trợ: liên hệ admin\n"
            "• 🙍‍♂️ Tài khoản: số dư & thống kê"
        )
        admin_note = "\n\n🛠 Khu quản trị đã được gom vào nút Admin Panel.\nLệnh nhanh: /us, /edit, /tin, /topup, /members"
    return base if not admin else base + admin_note


# ================== BOT COMMANDS ==================
async def setup_bot_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Bắt đầu"),
        BotCommand(command="menu", description="Danh Sách Sản Phẩm"),
        BotCommand(command="orders", description="Đơn Hàng Đã Mua"),
        BotCommand(command="naptien", description="Nạp Tiền"),
        BotCommand(command="support", description="Hỗ trợ"),
        BotCommand(command="profile", description="Chi Tiết Tài Khoản"),
    ])


# ================== MAIN ==================
async def main():
    acquire_bot_instance_lock()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    try:
        await setup_bot_commands(bot)
        await load_topup_enabled()
        asyncio.create_task(poll_topups_forever(bot))
        asyncio.create_task(poll_api_constraints_forever(bot))
        asyncio.create_task(sync_api_products_forever(bot))
        asyncio.create_task(poll_auto_orders_forever(bot))

        # -------- /start --------
        @dp.message(Command("start"))
        async def cmd_start(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            await touch_user_profile(message.from_user.id, message.from_user.username, message.from_user.full_name)
            lang = await get_user_lang(message.from_user.id)
            await message.answer(
                start_text(message.from_user.full_name, is_admin(message.from_user.id), lang),
                reply_markup=start_inline_kb(message.from_user.id, lang)
            )

        # -------- /menu --------
        @dp.message(Command("menu"))
        async def cmd_menu(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            await touch_user_profile(message.from_user.id, message.from_user.username, message.from_user.full_name)
            lang = await get_user_lang(message.from_user.id)
            await message.answer(
                tr(lang, "menu_title"),
                reply_markup=await products_kb_for_user(message.from_user.id)
            )

        @dp.callback_query(F.data == "menu")
        async def click_menu(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            await touch_user_profile(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
            lang = await get_user_lang(cb.from_user.id)
            await cb.message.answer(
                tr(lang, "menu_title"),
                reply_markup=await products_kb_for_user(cb.from_user.id)
            )

        @dp.callback_query(F.data.startswith("cat:"))
        async def click_category(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            cid = cb.data.split(":", 1)[1]
            cats = await load_categories()
            cat = next((c for c in cats if c.get("id") == cid), None)
            if not cat:
                await cb.message.answer("❌ Nhóm không tồn tại.", reply_markup=await products_kb_for_user(cb.from_user.id))
                return
            if (not is_admin(cb.from_user.id)) and not bool(cat.get("is_active", True)):
                await cb.message.answer("❌ Nhóm này đang tắt.", reply_markup=await products_kb_for_user(cb.from_user.id))
                return
            await cb.message.answer(
                f"📁 {cat.get('icon', '📁')} {cat.get('name')}\n\nChọn sản phẩm bên dưới 👇",
                reply_markup=await products_kb_for_category(cb.from_user.id, cid)
            )

        # -------- /orders --------
        @dp.message(Command("orders"))
        async def cmd_orders(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            await touch_user_profile(message.from_user.id, message.from_user.username, message.from_user.full_name)
            user = await load_user(message.from_user.id)
            text, total_pages, page = format_orders_text(user.get("orders", []), page=0, page_size=5)
            await message.answer(text, reply_markup=orders_nav_kb(page, total_pages))

        @dp.callback_query(F.data == "language")
        async def click_language(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            lang = await get_user_lang(cb.from_user.id)
            await cb.message.answer(tr(lang, "lang_title"), reply_markup=language_kb(lang))

        @dp.callback_query(F.data.startswith("setlang:"))
        async def click_set_language(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            lang = await set_user_lang(cb.from_user.id, cb.data.split(":", 1)[1])
            await cb.message.answer(
                tr(lang, "lang_saved"),
                reply_markup=start_inline_kb(cb.from_user.id, lang)
            )

        @dp.callback_query(F.data.startswith("orders:"))
        async def click_orders(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            await touch_user_profile(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
            user = await load_user(cb.from_user.id)
            orders = user.get("orders", [])
            try:
                page = int(cb.data.split(":", 1)[1])
            except ValueError:
                page = 0
            text, total_pages, page = format_orders_text(orders, page, page_size=5)
            await cb.message.answer(text, reply_markup=orders_nav_kb(page, total_pages))

        # -------- /support --------
        @dp.message(Command("support"))
        async def cmd_support(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            await touch_user_profile(message.from_user.id, message.from_user.username, message.from_user.full_name)
            await message.answer("Admin Hỗ Trợ\nZalo : 0898154136\n@thanhTr789", reply_markup=back_to_menu_kb())

        @dp.callback_query(F.data == "support")
        async def click_support(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            await touch_user_profile(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
            await cb.message.answer("Admin Hỗ Trợ\nZalo : 0898154136\n@thanhTr789", reply_markup=back_to_menu_kb())

        # -------- /profile --------
        @dp.message(Command("profile"))
        async def cmd_profile(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            await touch_user_profile(message.from_user.id, message.from_user.username, message.from_user.full_name)

            user = await load_user(message.from_user.id)
            stats = user.get("stats", {})
            balance = int(user.get("balance", 0))
            created = user.get("created_at", "N/A")

            await message.answer(
                "👤 THÔNG TIN TÀI KHOẢN\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🆔 ID: {message.from_user.id}\n"
                f"📦 Tổng đơn hàng: {int(stats.get('total_orders', 0))}\n"
                f"✅ Đơn đã hoàn thành: {int(stats.get('done_orders', 0))}\n"
                f"💰 Tổng chi tiêu: {vnd(int(stats.get('total_spent', 0)))} đ\n"
                f"💼 Số dư : {vnd(balance)} đ\n"
                f"📅 Ngày Tạo: {created}\n"
                "━━━━━━━━━━━━━━━━━━",
                reply_markup=back_to_menu_kb()
            )

        @dp.callback_query(F.data == "profile")
        async def click_profile(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            await touch_user_profile(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)

            user = await load_user(cb.from_user.id)
            stats = user.get("stats", {})
            balance = int(user.get("balance", 0))
            created = user.get("created_at", "N/A")

            await cb.message.answer(
                "👤 THÔNG TIN TÀI KHOẢN\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🆔 ID: {cb.from_user.id}\n"
                f"📦 Tổng đơn hàng: {int(stats.get('total_orders', 0))}\n"
                f"✅ Đơn đã hoàn thành: {int(stats.get('done_orders', 0))}\n"
                f"💰 Tổng chi tiêu: {vnd(int(stats.get('total_spent', 0)))} đ\n"
                f"💼 Số dư : {vnd(balance)} đ\n"
                f"📅 Ngày Tạo: {created}\n"
                "━━━━━━━━━━━━━━━━━━",
                reply_markup=back_to_menu_kb()
            )

        # -------- /naptien --------
        @dp.message(Command("naptien"))
        async def cmd_naptien(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            await touch_user_profile(message.from_user.id, message.from_user.username, message.from_user.full_name)

            user = await load_user(message.from_user.id)
            balance = int(user.get("balance", 0))
            qr = URLInputFile(vietqr_url(message.from_user.id))
            await message.answer_photo(photo=qr, caption=naptien_caption(message.from_user.id, balance))

        @dp.callback_query(F.data == "naptien")
        async def click_naptien(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            await touch_user_profile(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)

            user = await load_user(cb.from_user.id)
            balance = int(user.get("balance", 0))
            qr = URLInputFile(vietqr_url(cb.from_user.id))
            await cb.message.answer_photo(photo=qr, caption=naptien_caption(cb.from_user.id, balance))

        # -------- back_home --------
        @dp.callback_query(F.data == "back_home")
        async def back_home(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            await touch_user_profile(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
            lang = await get_user_lang(cb.from_user.id)
            await cb.message.answer(
                start_text(cb.from_user.full_name, is_admin(cb.from_user.id), lang),
                reply_markup=start_inline_kb(cb.from_user.id, lang)
            )

        # ================== ADMIN PANEL ==================
        @dp.callback_query(F.data == "adm:panel")
        async def adm_panel(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            products = await load_products()
            low_stock_count = sum(1 for p in products if int(p.get("stock", 0)) <= 3)
            await cb.message.answer(
                admin_panel_text(await total_members(), len(products), low_stock_count, TOPUP_ENABLED),
                reply_markup=admin_panel_kb()
            )

        @dp.callback_query(F.data == "adm:members")
        async def adm_members(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            await cb.message.answer(f"👥 Tổng số thành viên hiện tại: {await total_members()}", reply_markup=admin_panel_kb())

        @dp.callback_query(F.data == "adm:wallet_help")
        async def adm_wallet_help(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            await cb.message.answer(
                "💼 CỘNG/TRỪ SỐ DƯ\n\n"
                "Dùng lệnh:\n"
                "• /edit cong <user_id> <số_tiền> <lý_do>\n"
                "• /edit tru <user_id> <số_tiền> <lý_do>\n\n"
                "✅ Sau khi cập nhật, khách sẽ được bot thông báo tự động.",
                reply_markup=admin_panel_kb()
            )

        @dp.callback_query(F.data == "adm:sale_help")
        async def adm_sale_help(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            await cb.message.answer(
                "🔥 SALE SẢN PHẨM\n\n"
                "Cách 1: Vào Menu → chọn sản phẩm → bấm 🔥 Sale % / Tắt sale.\n"
                "Cách 2: dùng lệnh /sale <product_id> <percent> [số_giờ] hoặc /sale off <product_id>.\n\n"
                "Ví dụ: /sale sp_123 10 24 = sale 10% trong 24 tiếng.\n"
                "Sau khi bật/tắt sale, bot sẽ hiện nút 📣 để thông báo sale cho tất cả khách.",
                reply_markup=admin_panel_kb()
            )

        @dp.callback_query(F.data == "adm:orders")
        async def adm_orders_help(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            await cb.message.answer(
                "🧾 QUẢN LÝ ĐƠN HÀNG\n\n"
                "Hiện dữ liệu đơn vẫn nằm trong từng file user JSON và orders_data/*.txt để giữ tương thích.\n"
                "Có thể dùng /us <id> để xem khách hoặc /tin id <nội dung> để hỗ trợ nhanh.\n"
                "Bản nâng cấp này đã tách Admin Panel để tiếp tục gắn bộ lọc/tìm kiếm đơn mà không đổi data cũ.",
                reply_markup=admin_panel_kb()
            )

        @dp.callback_query(F.data == "adm:broadcast_help")
        async def adm_broadcast_help(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            await cb.message.answer(
                "📣 BROADCAST\n\n"
                "Dùng: /tin all <nội dung> hoặc /tin <user_id> <nội dung>.\n"
                "Khuyến nghị gửi thử cho chính admin trước khi gửi all.",
                reply_markup=admin_panel_kb()
            )

        @dp.callback_query(F.data == "adm:settings")
        async def adm_settings(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            await cb.message.answer(
                "⚙️ CÀI ĐẶT HỆ THỐNG\n\n"
                "Cấu hình bảo mật đã chuyển sang biến môi trường, không hiển thị secret trong mã nguồn.\n"
                "Không cần đổi cấu trúc thư mục data/ khi thay file bot chủ bằng bot.py.",
                reply_markup=admin_panel_kb()
            )

        # ================== ADMIN TOPUP TOGGLE ==================
        @dp.callback_query(F.data == "adm:toggle_topup")
        async def adm_toggle_topup(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            await save_topup_enabled(not TOPUP_ENABLED)
            await cb.message.answer(
                f"✅ Auto nạp hiện đang: {'BẬT' if TOPUP_ENABLED else 'TẮT'}",
                reply_markup=start_inline_kb(cb.from_user.id, await get_user_lang(cb.from_user.id))
            )

        @dp.message(Command("topup"))
        async def cmd_topup(message: Message):
            if not message.from_user or not is_admin(message.from_user.id):
                return
            parts = (message.text or "").split()
            if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
                await message.answer("Dùng: /topup on hoặc /topup off")
                return
            await save_topup_enabled(parts[1].lower() == "on")
            await message.answer(f"✅ Auto nạp hiện đang: {'BẬT' if TOPUP_ENABLED else 'TẮT'}")

        @dp.message(Command("members"))
        async def cmd_members(message: Message):
            if not message.from_user or not is_admin(message.from_user.id):
                return
            n = await total_members()
            await message.answer(f"👥 Tổng số thành viên hiện tại: {n}")

        # ================== PRODUCT CLICK ==================
        @dp.callback_query(F.data.startswith("prod:"))
        async def click_product(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            await touch_user_profile(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)

            pid = cb.data.split(":", 1)[1].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Sản phẩm không tồn tại.", reply_markup=back_to_menu_kb())
                return

            detail = await get_product_detail(pid)

            if (not is_admin(cb.from_user.id)) and not bool(product.get("is_active", True)):
                await cb.message.answer("❌ Sản phẩm này đang tạm tắt.", reply_markup=back_to_menu_kb())
                return

            if is_admin(cb.from_user.id):
                await cb.message.answer(
                    "🛠 QUẢN LÝ SẢN PHẨM\n\n"
                    f"📦 {product['name']}\n"
                    f"💰 Giá: {vnd(int(product['price_vnd']))}đ\n"
                    f"📊 Tồn hiện tại: {product.get('stock', 0)}\n"
                    f"📝 Thông tin chi tiết hiện tại:\n{detail if detail else '(chưa có)'}",
                    reply_markup=admin_product_manage_kb(pid, str(product.get("type", "local")), is_hot=bool(product.get("is_hot", False)))
                )
                return

            stock = int(product.get("stock", 0))
            if stock <= 0:
                await cb.message.answer("😢 Sản phẩm này tạm hết hàng. Bạn có thể quay lại menu để chọn sản phẩm khác hoặc liên hệ hỗ trợ.", reply_markup=back_to_menu_kb())
                today = alert_day_key()
                if bool(product.get("is_active", True)) and str(product.get("out_of_stock_notified", "")).strip() != today:
                    product["out_of_stock_notified"] = today
                    await save_products(products)
                    buyer_uname = f"@{cb.from_user.username}" if cb.from_user.username else "N/A"
                    await notify_admin(
                        bot,
                        "⚠️ HẾT HÀNG\n\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"📦 Sản phẩm: {product.get('name')} - {vnd(int(product.get('price_vnd', 0)))}đ [ 0 ]\n"
                        f"👤 Người bấm: {cb.from_user.full_name} ({buyer_uname})\n"
                        f"🆔 ID: {cb.from_user.id}\n"
                        f"🕒 Thời gian: {now_datetime_full()}\n"
                        "━━━━━━━━━━━━━━━━━━"
                    )
                return

            if str(product.get("type", "local")) == "api" and bool(product.get("requires_customer_email", False)):
                BUY_STATE[cb.from_user.id] = {"action": "await_api_email", "product_id": pid, "qty": 1}
                await cb.message.answer(
                    f"📧 {product['name']}\n\n"
                    f"💰 Giá: {vnd(int(product['price_vnd']))}đ\n"
                    "Vui lòng gửi Gmail cần đăng ký:",
                    reply_markup=back_to_menu_kb()
                )
                return

            BUY_STATE[cb.from_user.id] = {"product_id": pid, "qty": 1, "await_qty": False}
            text = (
                "🛒 CHỌN SỐ LƯỢNG\n\n"
                f"📦 {product['name']}\n"
                f"💰 Giá: {vnd(int(product['price_vnd']))} đ\n"
                f"📊 Tồn kho: {int(product.get('stock', 0))}\n"
            )
            if detail:
                text += f"\n📝 Thông tin chi tiết:\n{detail}\n"
            text += "\nChọn số lượng bên dưới 👇"
            await cb.message.answer(text, reply_markup=qty_kb(pid))

        # ================== ADMIN PRODUCT MANAGE ==================
        @dp.callback_query(F.data.startswith("adm:manage_data:"))
        async def adm_manage_data(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)

            pid = cb.data.split(":", 2)[2].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Sản phẩm không tồn tại.")
                return

            ADMIN_STATE[cb.from_user.id] = {"action": "add_data", "product_id": pid, "buffer": [], "prompt_msg_id": None}
            sent = await cb.message.answer(
                "🛠 THÊM DỮ LIỆU SẢN PHẨM\n\n"
                f"📦 {product['name']}\n"
                f"💰 Giá: {vnd(int(product['price_vnd']))}đ\n"
                f"📊 Tồn hiện tại: {product.get('stock', 0)}\n\n"
                "✅ BẮT BUỘC reply tin nhắn này để bot nhận hàng.\n\n"
                "➡️ Gửi TEXT (mỗi dòng = 1 dữ liệu)\n"
                "➡️ Hoặc gửi FILE .txt\n\n"
                "Sau khi gửi xong bấm ✅ XÁC NHẬN LƯU hoặc ❌ HỦY.",
                reply_markup=admin_data_confirm_kb()
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:editdetail:"))
        async def adm_edit_detail(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)

            pid = cb.data.split(":", 2)[2].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Sản phẩm không tồn tại.")
                return

            current_detail = await get_product_detail(pid)
            ADMIN_STATE[cb.from_user.id] = {"action": "edit_detail", "product_id": pid, "prompt_msg_id": None}
            sent = await cb.message.answer(
                "📝 SỬA THÔNG TIN CHI TIẾT SẢN PHẨM\n\n"
                f"📦 {product['name']}\n\n"
                f"Thông tin hiện tại:\n{current_detail if current_detail else '(chưa có)'}\n\n"
                "✅ BẮT BUỘC reply tin nhắn này bằng nội dung chi tiết mới.\n"
                "Nếu muốn xóa chi tiết, reply: xoa_chi_tiet\n\n"
                "Gõ /cancel để hủy."
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        # ================== BUY FLOW ==================
        @dp.callback_query(F.data.startswith("buyqty:"))
        async def buy_pick_qty(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)

            _, pid, qty_s = cb.data.split(":", 2)
            try:
                qty = int(qty_s)
            except ValueError:
                qty = 1

            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Sản phẩm không tồn tại.", reply_markup=back_to_menu_kb())
                return

            stock = int(product.get("stock", 0))
            if qty <= 0:
                await cb.message.answer("❌ Số lượng không hợp lệ.")
                return
            if stock <= 0:
                await cb.message.answer("😢 Sản phẩm này tạm hết hàng. Bạn có thể quay lại menu để chọn sản phẩm khác hoặc liên hệ hỗ trợ.", reply_markup=back_to_menu_kb())
                return
            if qty > stock:
                await cb.message.answer(f"❌ Số lượng vượt tồn kho. Tồn kho hiện tại: {stock}")
                return

            BUY_STATE[cb.from_user.id] = {"product_id": pid, "qty": qty, "await_qty": False}
            total = int(product["price_vnd"]) * qty
            await cb.message.answer(
                "✅ XÁC NHẬN MUA HÀNG\n\n"
                f"📦 Sản phẩm: {product['name']}\n"
                f"🔢 Số lượng: {qty}\n"
                f"💰 Tổng tiền: {vnd(total)} đ\n\n"
                "Bấm ✅ Xác nhận để giữ hàng và chọn cách thanh toán.",
                reply_markup=buy_confirm_kb(pid, qty)
            )

        @dp.callback_query(F.data.startswith("buyqty_custom:"))
        async def buy_custom_qty(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)

            pid = cb.data.split(":", 1)[1].strip()
            BUY_STATE[cb.from_user.id] = {"product_id": pid, "qty": 1, "await_qty": True}
            await cb.message.answer(
                "✏️ NHẬP SỐ LƯỢNG TÙY CHỌN\n\n"
                "Hãy gửi 1 con số hợp lệ (ví dụ: 3).\n"
                "• Số lượng phải > 0\n"
                "• Không được vượt tồn kho\n\n"
                "Gõ /cancel để hủy."
            )

        @dp.callback_query(F.data.startswith("buyconfirm:"))
        async def buy_confirm(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)

            _, pid, qty_s = cb.data.split(":", 2)
            try:
                qty = int(qty_s)
            except ValueError:
                qty = 1

            ok, msg = await reserve_items(cb.from_user.id, pid, qty)
            if not ok:
                await cb.message.answer(f"❌ {msg}", reply_markup=back_to_menu_kb())
                return

            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cancel_reserve_now(cb.from_user.id, pid)
                await cb.message.answer("❌ Lỗi sản phẩm. Đã hoàn kho.", reply_markup=back_to_menu_kb())
                return

            user = await load_user(cb.from_user.id)
            balance = int(user.get("balance", 0))
            total = int(product["price_vnd"]) * qty

            await cb.message.answer(
                "💳 CHỌN CÁCH THANH TOÁN\n\n"
                f"📦 {product['name']}\n"
                f"🔢 Số lượng: {qty}\n"
                f"💰 Tổng tiền: {vnd(total)} đ\n"
                f"💼 Số dư ví: {vnd(balance)} đ\n\n"
                f"⏳ Hàng đã được giữ trong {RESERVE_HOLD_SECONDS // 60} phút.\n"
                "Nếu quá thời gian không thanh toán, hệ thống sẽ hoàn kho tự động.",
                reply_markup=pay_method_kb(pid, qty, balance)
            )

        @dp.callback_query(F.data.startswith("buychange:"))
        async def buy_change(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)

            pid = cb.data.split(":", 1)[1].strip()
            BUY_STATE[cb.from_user.id] = {"product_id": pid, "qty": 1, "await_qty": False}
            await cb.message.answer("🔢 Chọn lại số lượng:", reply_markup=qty_kb(pid))

        @dp.callback_query(F.data.startswith("buycancel:"))
        async def buy_cancel(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)

            pid = cb.data.split(":", 1)[1].strip()
            await cancel_reserve_now(cb.from_user.id, pid)
            BUY_STATE.pop(cb.from_user.id, None)
            await cb.message.answer("✅ Đã hủy và hoàn kho (nếu đang giữ hàng).", reply_markup=back_to_menu_kb())

        @dp.callback_query(F.data.startswith("paybank:"))
        async def pay_bank(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)

            _, pid, qty_s = cb.data.split(":", 2)
            try:
                qty = int(qty_s)
            except ValueError:
                qty = 1

            rp = reserve_path(cb.from_user.id, pid)
            if not os.path.exists(rp):
                await cb.message.answer("❌ Giữ hàng đã hết hạn hoặc không tồn tại. Vui lòng mua lại.", reply_markup=back_to_menu_kb())
                return

            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cancel_reserve_now(cb.from_user.id, pid)
                await cb.message.answer("❌ Sản phẩm không còn tồn tại. Đã hoàn kho.", reply_markup=back_to_menu_kb())
                return

            unit_price = int(product["price_vnd"])
            total = unit_price * qty
            payload = await create_direct_payment(cb.from_user.id, pid, qty, product["name"], unit_price, total, str(BUY_STATE.get(cb.from_user.id, {}).get("customer_email", "")))
            qr = URLInputFile(vietqr_url_by_content(payload["payment_desc"], int(payload.get("total", 0))))

            await cb.message.answer_photo(
                photo=qr,
                caption=direct_pay_caption(payload),
                reply_markup=direct_pay_kb(payload["order_code"])
            )

        @dp.callback_query(F.data.startswith("directcheck:"))
        async def direct_check(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            order_code = cb.data.split(":", 1)[1].strip()
            pending = await load_direct_payment(order_code)
            if not pending:
                await cb.message.answer("✅ Đơn này đã thanh toán xong hoặc đã hết hạn.")
                return
            remain = max(0, int(pending.get("expires_at_unix", 0)) - int(time.time()))
            await cb.message.answer(
                "⏳ ĐƠN CHƯA GHI NHẬN THANH TOÁN\n\n"
                f"🧾 Mã đơn: {order_code}\n"
                f"💰 Cần thanh toán: {vnd(int(pending.get('total', 0)))} đ\n"
                f"⌛ Còn giữ hàng khoảng: {remain // 60} phút {remain % 60} giây\n\n"
                "Nếu bạn đã chuyển khoản đúng nội dung, vui lòng đợi hệ thống quét giao dịch."
            )

        @dp.callback_query(F.data.startswith("directcancel:"))
        async def direct_cancel(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            order_code = cb.data.split(":", 1)[1].strip()
            pending = await load_direct_payment(order_code)
            if not pending:
                await cb.message.answer("❌ Không tìm thấy đơn chờ chuyển khoản.")
                return
            if int(pending.get("user_id", 0)) != cb.from_user.id:
                await cb.message.answer("❌ Bạn không có quyền hủy đơn này.")
                return
            await delete_direct_payment(order_code)
            await cancel_reserve_now(cb.from_user.id, str(pending.get("product_id", "")))
            await cb.message.answer("✅ Đã hủy đơn chuyển khoản và hoàn kho.", reply_markup=back_to_menu_kb())

        @dp.callback_query(F.data.startswith("paywallet:"))
        async def pay_wallet(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)

            _, pid, qty_s = cb.data.split(":", 2)
            try:
                qty = int(qty_s)
            except ValueError:
                qty = 1

            rp = reserve_path(cb.from_user.id, pid)
            if not os.path.exists(rp):
                await cb.message.answer("❌ Giữ hàng đã hết hạn hoặc không tồn tại. Vui lòng mua lại.", reply_markup=back_to_menu_kb())
                return

            try:
                with open(rp, "r", encoding="utf-8") as f:
                    rdata = json.load(f)
            except Exception:
                await cb.message.answer("❌ Lỗi giữ hàng. Vui lòng mua lại.", reply_markup=back_to_menu_kb())
                return

            keys = rdata.get("keys", [])
            is_api = bool(rdata.get("is_api", False))
            is_manual = bool(rdata.get("is_manual", False))
            rqty = int(rdata.get("qty", 0))
            if rqty != qty or not isinstance(keys, list) or ((not is_api) and (not is_manual) and len(keys) != qty):
                await cb.message.answer("❌ Dữ liệu giữ hàng không hợp lệ. Vui lòng mua lại.", reply_markup=back_to_menu_kb())
                return

            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cancel_reserve_now(cb.from_user.id, pid)
                await cb.message.answer("❌ Sản phẩm không còn tồn tại. Đã hoàn kho.", reply_markup=back_to_menu_kb())
                return

            unit_price = int(product["price_vnd"])
            total = unit_price * qty

            # Per-user lock: prevents double-click double-deduction
            wp_key = cb.from_user.id
            if wp_key not in WALLET_PAY_LOCKS:
                WALLET_PAY_LOCKS[wp_key] = asyncio.Lock()
            if WALLET_PAY_LOCKS[wp_key].locked():
                await cb.message.answer("⏳ Đang xử lý thanh toán trước đó. Vui lòng đợi...")
                return

            user = await load_user(cb.from_user.id)
            balance = int(user.get("balance", 0))
            if balance < total:
                await cb.message.answer(
                    "❌ Số dư ví không đủ để thanh toán.\n\n"
                    f"💼 Số dư: {vnd(balance)} đ\n"
                    f"💰 Cần: {vnd(total)} đ\n\n"
                    "👉 Hãy nạp thêm bằng /naptien hoặc chọn chuyển khoản trực tiếp.\n"
                    "Hoặc bấm ❌ Hủy (hoàn kho).",
                    reply_markup=pay_method_kb(pid, qty, balance)
                )
                return

            is_api_product = str(product.get("type", "local")) == "api"
            if is_api_product:
                loading_msg = await cb.message.answer("⏳ Đang xử lý thanh toán qua ví, vui lòng chờ...")

            before = balance
            after = before - total
            user["balance"] = after
            user["last_seen"] = now_datetime_full()
            user.setdefault("wallet_logs", [])
            user["wallet_logs"].insert(0, {
                "time": now_datetime_full(),
                "type": "debit",
                "source": "wallet_pay",
                "amount": total,
                "before": before,
                "after": after,
                "note": f"Thanh toán {qty} x {product.get('name')}"
            })
            await save_user(cb.from_user.id, user)

            if str(product.get("type", "local")) == "api" and str(product.get("api_provider") or "").upper() == API_PROVIDER_SACHTAI:
                customer_email = str(BUY_STATE.get(cb.from_user.id, {}).get("customer_email", "")).strip()
                ext = str(product.get("external_product_id") or product.get("api_product_id") or "").strip()
                fields = {str(x).lower() for x in product.get("required_fields", []) if isinstance(product.get("required_fields", []), list)}
                needs_email = bool(product.get("requires_customer_email")) or "email" in fields or ext == SACHTAI_CAPCUT_ADD_TEAM_KEY
                if needs_email and not is_valid_email(customer_email):
                    user2 = await load_user(cb.from_user.id)
                    bal2 = int(user2.get("balance", 0))
                    user2["balance"] = bal2 + total
                    user2.setdefault("wallet_logs", []).insert(0, {"time": now_datetime_full(), "type": "credit", "source": "wallet_refund_missing_sachtai_email", "amount": total, "before": bal2, "after": bal2 + total, "note": f"Hoàn tiền do thiếu email Sachtai: {product.get('name')}"})
                    await save_user(cb.from_user.id, user2)
                    await cancel_reserve_now(cb.from_user.id, pid)
                    BUY_STATE[cb.from_user.id] = {"action": "await_api_email", "product_id": pid, "qty": qty}
                    await cb.message.answer("❌ Sản phẩm này cần email hợp lệ. Vui lòng gửi email để mua lại.")
                    return
                lock_key = f"{cb.from_user.id}:{pid}:{customer_email or ext}"
                if lock_key not in API_PURCHASE_LOCKS:
                    API_PURCHASE_LOCKS[lock_key] = asyncio.Lock()
                async with API_PURCHASE_LOCKS[lock_key]:
                    existing = await find_user_open_auto_order(cb.from_user.id, pid)
                    if existing:
                        await cb.message.answer(f"⚠️ Bạn đang có đơn API #{existing.get('order_code')} đang xử lý. Vui lòng chờ hoàn tất trước khi tạo đơn mới.")
                        return
                    internal_ref = gen_sachtai_order_code()
                    ok_buy, buy_data, err = await SACHTAI_API.purchase(ext, qty, customer_email, internal_ref)
                    # Errors that GUARANTEE no order was created on the provider side
                    safe_no_order_errors = {"hết hàng", "out_of_stock", "số dư api không đủ", "insufficient_balance",
                                            "sản phẩm hoặc order sachtai không tồn tại", "product_not_found",
                                            "số lượng không hợp lệ", "invalid_quantity",
                                            "api key không hợp lệ hoặc đã bị thu hồi", "invalid_api_key",
                                            "api key không có scope cần thiết", "missing_scope",
                                            "api đang rate limit. vui lòng thử lại sau.", "rate_limited",
                                            "thiếu sachtai_api_base_url hoặc sachtai_api_key"}
                    api_clearly_failed = (not ok_buy) and any(e in str(err).lower() for e in safe_no_order_errors)
                    api_ambiguous = (not ok_buy) and not any(e in str(err).lower() for e in safe_no_order_errors) or (ok_buy and not buy_data.get("external_order_code"))
                    
                    if api_clearly_failed or api_ambiguous:
                        # No auto-refund: notify admin with action buttons
                        fail_key = f"{cb.from_user.id}:{pid}"
                        API_FAIL_STATE[fail_key] = {
                            "user_id": cb.from_user.id,
                            "product_id": pid,
                            "product_name": product.get("name"),
                            "customer_email": customer_email,
                            "qty": qty,
                            "total": total,
                            "internal_ref": internal_ref,
                            "error": str(err),
                            "provider": API_PROVIDER_SACHTAI,
                            "created_at": now_datetime_full()
                        }
                        await notify_admin(
                            bot,
                            f"⚠️ SACHTAI PURCHASE THẤT BẠI - CẦN XỬ LÝ THỦ CÔNG\n\n"
                            f"🆔 User ID: {cb.from_user.id}\n"
                            f"📦 Sản phẩm: {product.get('name')} x{qty}\n"
                            f"💰 Tổng tiền: {vnd(total)} đ\n"
                            f"📧 Email: {customer_email or 'N/A'}\n"
                            f"📝 Internal ref: {internal_ref}\n"
                            f"❌ Lỗi: {err}\n\n"
                            f"👉 Chọn hành động:",
                            reply_markup=api_fail_admin_kb(cb.from_user.id, pid)
                        )
                        await cb.message.answer(
                            "⏳ Đơn hàng đang gặp sự cố. Admin sẽ kiểm tra và phản hồi cho bạn.\n"
                            "Tiền của bạn chưa bị mất.",
                            reply_markup=back_to_menu_kb()
                        )
                        return
                    await finalize_reserve(cb.from_user.id, pid)
                    if buy_data.get("status") == "completed" and buy_data.get("items"):
                        order = await add_order(cb.from_user.id, product["name"], qty, total, "✅ Đã hoàn thành")
                        order_file = create_order_file_text(order["order_code"], utc_iso_z(), product["name"], unit_price, qty, total, [str(x).strip() for x in buy_data.get("items", [])])
                        await cb.message.answer_document(document=FSInputFile(order_file), caption=customer_order_success_caption(order["order_code"], product["name"], qty, total, after), reply_markup=warranty_request_kb(order["order_code"]))
                        BUY_STATE.pop(cb.from_user.id, None)
                        return
                    order = await add_order(cb.from_user.id, product["name"], qty, total, "🔄 Đang xử lý API")
                    auto_order = {"order_code": order["order_code"], "external_order_code": buy_data["external_order_code"], "internal_reference": internal_ref, "user_id": cb.from_user.id, "full_name": cb.from_user.full_name, "username": f"@{cb.from_user.username}" if cb.from_user.username else "Không có", "product_id": pid, "category_id": product.get("category_id", "cat_other"), "product_name": product["name"], "product_type": "API", "api_provider": API_PROVIDER_SACHTAI, "external_product_id": ext, "customer_email": customer_email, "quantity": qty, "selling_price": total, "api_price": int(product.get("api_price", 0)), "status": ORDER_STATUS_PROCESSING, "api_status": "processing", "email_statuses": buy_data.get("email_statuses", []), "delivery_status": "PROCESSING", "delivery_data": [], "refund_status": "NONE", "refund_amount": 0, "poll_count": 0, "created_at": now_datetime_full(), "created_at_unix": now_unix(), "updated_at": now_datetime_full(), "history": []}
                    add_order_history(auto_order, f"user:{cb.from_user.id}", "Tạo đơn Sachtai", "PENDING_API_PURCHASE", ORDER_STATUS_PROCESSING, f"external={buy_data['external_order_code']}")
                    await upsert_auto_order(auto_order)
                    await cb.message.answer(f"🔄 Đơn hàng #{order['order_code']} đang được xử lý.\n\n📦 {product['name']}\n📧 {customer_email}\n\nTrạng thái: PROCESSING", reply_markup=auto_order_user_kb(auto_order))
                    await notify_admin(bot, f"🔌 SACHTAI ORDER PROCESSING\n\n🧾 {order['order_code']}\n📡 API Order: {buy_data['external_order_code']}\n📦 {product['name']}")
                    start_auto_order_poll_task(bot, auto_order["order_code"])
                    BUY_STATE.pop(cb.from_user.id, None)
                    return

            if str(product.get("type", "local")) == "api" and bool(product.get("fishy_managed", False)) and str(product.get("external_product_id") or product.get("api_product_id") or "").strip():
                customer_email = str(BUY_STATE.get(cb.from_user.id, {}).get("customer_email", "")).strip()
                if bool(product.get("requires_customer_email", False)) and not is_valid_email(customer_email):
                    # Hoàn ví + giữ hàng vì thiếu email hợp lệ.
                    user2 = await load_user(cb.from_user.id)
                    bal2 = int(user2.get("balance", 0))
                    user2["balance"] = bal2 + total
                    user2.setdefault("wallet_logs", []).insert(0, {"time": now_datetime_full(), "type": "credit", "source": "wallet_refund_missing_api_email", "amount": total, "before": bal2, "after": bal2 + total, "note": f"Hoàn tiền do thiếu email đơn API: {product.get('name')}"})
                    await save_user(cb.from_user.id, user2)
                    await cancel_reserve_now(cb.from_user.id, pid)
                    BUY_STATE[cb.from_user.id] = {"action": "await_api_email", "product_id": pid, "qty": 1}
                    await cb.message.answer("❌ Đơn API này cần email hợp lệ. Vui lòng gửi email để mua lại.")
                    return
                lock_key = f"{cb.from_user.id}:{pid}:{customer_email}"
                if lock_key not in API_PURCHASE_LOCKS:
                    API_PURCHASE_LOCKS[lock_key] = asyncio.Lock()
                async with API_PURCHASE_LOCKS[lock_key]:
                    existing = await find_user_open_auto_order(cb.from_user.id, pid)
                    if existing:
                        await cb.message.answer(f"⚠️ Bạn đang có đơn API #{existing.get('order_code')} đang xử lý. Vui lòng chờ hoàn tất trước khi tạo đơn mới.")
                        return
                    ok_buy, buy_data, err = await FISHY_API.purchase(str(product.get("external_product_id") or product.get("api_product_id")), qty, customer_email)
                    # Errors that GUARANTEE no order was created on the provider side
                    fishy_safe_errors = {"hết stock", "out of stock", "409", "product hoặc order không tồn tại",
                                         "api key sai hoặc hết hạn", "401", "thiếu dữ liệu hoặc số dư api không đủ", "400",
                                         "api server đang lỗi"}
                    fishy_clearly_failed = (not ok_buy) and any(e in str(err).lower() for e in fishy_safe_errors)
                    fishy_ambiguous = (not ok_buy) and not any(e in str(err).lower() for e in fishy_safe_errors) or (ok_buy and not buy_data.get("order_code"))
                    
                    if fishy_clearly_failed or fishy_ambiguous:
                        # No auto-refund: notify admin with action buttons
                        fail_key = f"{cb.from_user.id}:{pid}"
                        API_FAIL_STATE[fail_key] = {
                            "user_id": cb.from_user.id,
                            "product_id": pid,
                            "product_name": product.get("name"),
                            "customer_email": customer_email,
                            "qty": qty,
                            "total": total,
                            "error": str(err),
                            "provider": API_PROVIDER_FISHYMARKET,
                            "created_at": now_datetime_full()
                        }
                        await notify_admin(
                            bot,
                            f"⚠️ FISHY PURCHASE THẤT BẠI - CẦN XỬ LÝ THỦ CÔNG\n\n"
                            f"🆔 User ID: {cb.from_user.id}\n"
                            f"📦 Sản phẩm: {product.get('name')} x{qty}\n"
                            f"💰 Tổng tiền: {vnd(total)} đ\n"
                            f"📧 Email: {customer_email or 'N/A'}\n"
                            f"❌ Lỗi: {err}\n\n"
                            f"👉 Chọn hành động:",
                            reply_markup=api_fail_admin_kb(cb.from_user.id, pid)
                        )
                        await cb.message.answer(
                            "⏳ Đơn hàng đang gặp sự cố. Admin sẽ kiểm tra và phản hồi cho bạn.\n"
                            "Tiền của bạn chưa bị mất.",
                            reply_markup=back_to_menu_kb()
                        )
                        return
                    await finalize_reserve(cb.from_user.id, pid)
                    order = await add_order(cb.from_user.id, product["name"], qty, total, "🔄 Đang xử lý API")
                    cats = await load_categories()
                    auto_order = {
                        "order_code": order["order_code"], "external_order_code": buy_data["order_code"], "user_id": cb.from_user.id,
                        "full_name": cb.from_user.full_name, "username": f"@{cb.from_user.username}" if cb.from_user.username else "Không có",
                        "product_id": pid, "category_id": product.get("category_id", "cat_other"), "product_name": product["name"],
                        "product_type": "AUTO_API", "customer_email": customer_email or buy_data.get("customer_email", ""),
                        "quantity": qty, "selling_price": total, "api_price": int(product.get("api_price", 0)), "status": ORDER_STATUS_PROCESSING,
                        "api_status": buy_data.get("status", "processing"), "refund_status": "NONE", "refund_amount": 0,
                        "change_email_count": 0, "email_change_deadline_unix": 0, "poll_count": 0,
                        "created_at": now_datetime_full(), "created_at_unix": now_unix(), "updated_at": now_datetime_full(), "history": []
                    }
                    add_order_history(auto_order, f"user:{cb.from_user.id}", "Tạo đơn API", "PENDING_PAYMENT", ORDER_STATUS_PROCESSING, f"external={buy_data['order_code']}")
                    await upsert_auto_order(auto_order)
                    await cb.message.answer(f"🔄 Đơn hàng #{order['order_code']} đang được xử lý.\n\n📦 {product['name']}\n📧 {auto_order['customer_email']}\n\nTrạng thái: PROCESSING", reply_markup=auto_order_user_kb(auto_order))
                    await notify_admin(bot, auto_admin_text(auto_order, cats))
                    start_auto_order_poll_task(bot, auto_order["order_code"])
                    BUY_STATE.pop(cb.from_user.id, None)
                    return

            if str(product.get("type", "local")) == "dontay":
                await finalize_reserve(cb.from_user.id, pid)
                order = await add_order(
                    user_id=cb.from_user.id, product_name=product["name"], quantity=qty, total_money=total,
                    status="⏳ Chờ khách gửi dữ liệu"
                )
                manual_order = {
                    "order_code": order["order_code"], "user_id": cb.from_user.id,
                    "full_name": cb.from_user.full_name, "username": f"@{cb.from_user.username}" if cb.from_user.username else "Không có",
                    "product_id": pid, "product_name": product["name"], "quantity": qty, "unit_price": unit_price, "total": total,
                    "status": "WAITING_CUSTOMER_DATA", "created_time": order.get("created_time"),
                    "manual_fields": product.get("manual_fields", []), "customer_data": {}, "stock_after": product.get("stock", 0),
                    "refunded": False, "stock_refunded": False, "paid_by": "wallet"
                }
                add_order_history(manual_order, f"user:{cb.from_user.id}", "Thanh toán thành công", "PAID", "WAITING_CUSTOMER_DATA", "Bot yêu cầu dữ liệu")
                await upsert_manual_order(manual_order)
                await cb.message.answer(
                    "✅ Đơn hàng của bạn đã được tạo thành công.\n"
                    "Vui lòng gửi các thông tin cần thiết để Admin xử lý đơn hàng.\n\n"
                    f"🧾 Mã đơn: {order['order_code']}\n📦 Sản phẩm: {product['name']}\n\n"
                    + manual_fields_text(product.get("manual_fields", [])),
                    reply_markup=manual_customer_kb(order["order_code"])
                )
                await notify_admin(bot, manual_admin_text(manual_order), reply_markup=manual_admin_kb(order["order_code"], cb.from_user.id))
                BUY_STATE.pop(cb.from_user.id, None)
                return

            api_buy_meta = None
            if str(product.get("type", "local")) == "api":
                ok_buy, buy_data, err_code, err_msg = await shop_api_buy(str(product.get("api_product_id", "")).strip(), qty)
                if not ok_buy:
                    # Hoàn ví + hoàn giữ hàng để tránh trừ tiền oan khi API lỗi.
                    user2 = await load_user(cb.from_user.id)
                    bal2 = int(user2.get("balance", 0))
                    user2["balance"] = bal2 + total
                    user2.setdefault("wallet_logs", [])
                    user2["wallet_logs"].insert(0, {
                        "time": now_datetime_full(),
                        "type": "credit",
                        "source": "wallet_refund_api_buy_fail",
                        "amount": total,
                        "before": bal2,
                        "after": bal2 + total,
                        "note": f"Hoàn tiền do API buy lỗi: {product.get('name')} x{qty}"
                    })
                    await save_user(cb.from_user.id, user2)
                    await cancel_reserve_now(cb.from_user.id, pid)

                    await notify_admin(
                        bot,
                        "❌ API BUY THẤT BẠI (đơn ví)\n\n"
                        f"🆔 User ID: {cb.from_user.id}\n"
                        f"📦 SP: {product.get('name')}\n"
                        f"🔢 SL: {qty}\n"
                        f"🔎 Mã lỗi: {err_code or 'N/A'}\n"
                        f"📝 Chi tiết: {err_msg or 'N/A'}\n"
                        "💸 Đã tự hoàn ví + hoàn giữ hàng."
                    )
                    await cb.message.answer(
                        "❌ Xin lỗi, hàng từ API hiện không thể giao ngay.\n"
                        "💸 Tiền đã được hoàn lại ví của bạn.",
                        reply_markup=back_to_menu_kb()
                    )
                    return

                buy_accounts = buy_data.get("accounts", [])
                keys = [str(x).strip() for x in buy_accounts if str(x).strip()]
                if not keys:
                    user2 = await load_user(cb.from_user.id)
                    bal2 = int(user2.get("balance", 0))
                    user2["balance"] = bal2 + total
                    user2.setdefault("wallet_logs", [])
                    user2["wallet_logs"].insert(0, {
                        "time": now_datetime_full(),
                        "type": "credit",
                        "source": "wallet_refund_api_empty_accounts",
                        "amount": total,
                        "before": bal2,
                        "after": bal2 + total,
                        "note": f"Hoàn tiền do API trả accounts rỗng: {product.get('name')} x{qty}"
                    })
                    await save_user(cb.from_user.id, user2)
                    await cancel_reserve_now(cb.from_user.id, pid)
                    await notify_admin(bot, f"❌ API /buy trả về rỗng accounts cho user {cb.from_user.id}. Đã hoàn tiền.")
                    await cb.message.answer("❌ Giao dịch lỗi do nguồn API không trả dữ liệu hàng. Đã hoàn tiền vào ví.", reply_markup=back_to_menu_kb())
                    return

                api_buy_meta = buy_data

            await finalize_reserve(cb.from_user.id, pid)

            # xóa pending direct pay liên quan nếu có
            for fn in os.listdir(DIRECT_PAY_DIR):
                fp = os.path.join(DIRECT_PAY_DIR, fn)
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if int(d.get("user_id", 0)) == cb.from_user.id and str(d.get("product_id", "")) == pid:
                        os.remove(fp)
                except Exception:
                    pass

            order = await add_order(
                user_id=cb.from_user.id,
                product_name=product["name"],
                quantity=qty,
                total_money=total,
                status="✅ Đã hoàn thành"
            )

            iso = utc_iso_z()
            order_file = create_order_file_text(
                order_code=order["order_code"],
                iso_date=iso,
                product_name=product["name"],
                price_vnd=unit_price,
                qty=qty,
                total_vnd=total,
                keys=[str(x).strip() for x in keys]
            )
            await cb.message.answer_document(
                document=FSInputFile(order_file),
                caption=customer_order_success_caption(
                    order_code=order["order_code"],
                    product_name=order["product_name"],
                    qty=qty,
                    total=total,
                    wallet_after=after,
                ),
                reply_markup=warranty_request_kb(order["order_code"])
            )

            buyer_uname = f"@{cb.from_user.username}" if cb.from_user.username else "N/A"
            await notify_admin(
                bot,
                "🛒 ĐƠN HÀNG THÀNH CÔNG (THANH TOÁN VÍ)\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"👤 User: {cb.from_user.full_name}\n"
                f"🔗 Username: {buyer_uname}\n"
                f"🆔 ID: {cb.from_user.id}\n"
                f"🕒 Thời gian: {order.get('created_time')}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🧾 Mã đơn: {order.get('order_code')}\n"
                f"📦 SP: {order.get('product_name')}\n"
                f"🔢 SL: {qty}\n"
                f"💰 Tổng: {vnd(total)} đ\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💼 Ví trước: {vnd(before)} đ\n"
                f"💼 Ví sau: {vnd(after)} đ\n"
                f"📦 Tồn kho còn: {product.get('stock', 0)}\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            if api_buy_meta is not None:
                await notify_admin(
                    bot,
                    "🌐 MUA NGUỒN TỰ ĐỘNG THÀNH CÔNG\n\n"
                    f"🧾 Mã đơn bot: {order.get('order_code')}\n"
                    f"🧾 Mã đơn nguồn: {api_buy_meta.get('order_code', 'N/A')}\n"
                    f"💸 Chi phí nguồn: {vnd(int(api_buy_meta.get('amount', 0) or 0))}đ\n"
                    f"💳 Ngân sách còn lại: {vnd(int(api_buy_meta.get('balance_after', 0) or 0))}đ"
                )

            if str(product.get("type", "local")) == "api" and int(product.get("stock", 0)) <= 0:
                await notify_admin(
                    bot,
                    "⚠️ SẢN PHẨM TỰ ĐỘNG ĐÃ HẾT STOCK\n\n"
                    f"📦 {product.get('name')}\n"
                    "👉 Vui lòng vào quản lý sản phẩm và bấm '➕ Thêm stock tự động' để cấp thêm thủ công."
                )

            BUY_STATE.pop(cb.from_user.id, None)

        # ================== WARRANTY FLOW ==================
        @dp.callback_query(F.data.startswith("warranty:"))
        async def click_warranty(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)

            order_code = cb.data.split(":", 1)[1].strip()
            user = await load_user(cb.from_user.id)
            order = next((x for x in user.get("orders", []) if x.get("order_code") == order_code), None)
            if not order:
                await cb.message.answer("❌ Không tìm thấy đơn hàng để yêu cầu bảo hành.")
                return

            old = await load_warranty(order_code)
            if old and bool(old.get("closed", False)):
                await cb.message.answer("❌ Mã bảo hành này đã được đóng.")
                return

            WARRANTY_STATE[cb.from_user.id] = {
                "action": "await_claim",
                "order_code": order_code,
                "prompt_msg_id": None
            }

            sent = await cb.message.answer(
                "🛡 YÊU CẦU BẢO HÀNH\n\n"
                f"🧾 Mã đơn: {order_code}\n\n"
                "✅ Hãy reply tin nhắn này bằng tin nhắn hoặc ảnh.\n"
                "• Nếu gửi ảnh, có thể kèm caption lý do.\n"
                "• Nếu gửi chữ, hãy mô tả lỗi cần bảo hành.\n\n"
                "Ví dụ: Tài khoản lỗi đăng nhập / bị đổi mật khẩu / không dùng được\n\n"
                "Gõ /cancel để hủy."
            )
            WARRANTY_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.message(F.photo)
        async def warranty_photo_handler(message: Message):
            if not message.from_user:
                return

            uid = message.from_user.id
            st = WARRANTY_STATE.get(uid)
            if not st or st.get("action") != "await_claim":
                return

            prompt_id = st.get("prompt_msg_id")
            if not message.reply_to_message or not prompt_id or message.reply_to_message.message_id != prompt_id:
                await message.answer("⚠️ Bạn phải reply đúng tin nhắn yêu cầu bảo hành và gửi ảnh hoặc tin nhắn mô tả lỗi.")
                return

            reason = (message.caption or "").strip() or "Khách gửi ảnh bảo hành (không kèm caption)."

            order_code = st.get("order_code", "").strip()
            file_id = message.photo[-1].file_id
            wdata = {
                "order_code": order_code,
                "user_id": uid,
                "reason": reason,
                "photo_file_id": file_id,
                "created_time": now_datetime_full(),
                "status": "pending",
                "closed": False,
                "close_reason": "",
                "chat_messages": []
            }
            await save_warranty(wdata)

            uname = f"@{message.from_user.username}" if message.from_user.username else "N/A"
            await notify_admin_photo(
                bot,
                photo=file_id,
                caption=(
                    "🛡 CÓ YÊU CẦU BẢO HÀNH MỚI\n\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"👤 User: {message.from_user.full_name}\n"
                    f"🔗 Username: {uname}\n"
                    f"🆔 ID: {uid}\n"
                    f"🧾 Mã đơn: {order_code}\n"
                    f"🕒 Thời gian: {now_datetime_full()}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📝 Lý do: {reason}"
                ),
                reply_markup=admin_warranty_chat_kb(uid, order_code)
            )

            WARRANTY_STATE.pop(uid, None)
            await message.answer("✅ Đã gửi yêu cầu bảo hành tới admin. Vui lòng chờ phản hồi.")

        @dp.callback_query(F.data.startswith("adm:wchat:"))
        async def adm_warranty_chat(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)

            try:
                _, _, uid_s, order_code = cb.data.split(":", 3)
                target_uid = int(uid_s)
            except Exception:
                await cb.message.answer("❌ Dữ liệu không hợp lệ.")
                return

            data = await load_warranty(order_code)
            if not data:
                await cb.message.answer("❌ Không tìm thấy mã bảo hành.")
                return
            if bool(data.get("closed", False)):
                await cb.message.answer("❌ Mã bảo hành này đã đóng.")
                return

            ADMIN_REPLY_STATE[cb.from_user.id] = {
                "action": "reply_warranty_user",
                "target_uid": target_uid,
                "order_code": order_code,
                "prompt_msg_id": None
            }

            sent = await cb.message.answer(
                "💬 GỬI TIN NHẮN CHO KHÁCH BẢO HÀNH\n\n"
                f"🆔 User ID: {target_uid}\n"
                f"🧾 Mã đơn: {order_code}\n\n"
                "✅ Hãy reply tin nhắn này bằng nội dung cần gửi cho khách.\n"
                "Gõ /cancel để hủy."
            )
            ADMIN_REPLY_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:wclose:"))
        async def adm_warranty_close(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)

            try:
                _, _, uid_s, order_code = cb.data.split(":", 3)
                target_uid = int(uid_s)
            except Exception:
                await cb.message.answer("❌ Dữ liệu không hợp lệ.")
                return

            data = await load_warranty(order_code)
            if not data:
                await cb.message.answer("❌ Không tìm thấy mã bảo hành.")
                return

            ADMIN_REPLY_STATE[cb.from_user.id] = {
                "action": "close_warranty",
                "target_uid": target_uid,
                "order_code": order_code,
                "prompt_msg_id": None
            }
            sent = await cb.message.answer(
                "🔒 ĐÓNG MÃ BẢO HÀNH\n\n"
                f"🆔 User ID: {target_uid}\n"
                f"🧾 Mã đơn: {order_code}\n\n"
                "Reply tin nhắn này với lý do đóng.\n"
                "Nếu không có lý do, reply: khong\n\n"
                "Gõ /cancel để hủy."
            )
            ADMIN_REPLY_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:wclear:"))
        async def adm_warranty_clear(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)

            try:
                _, _, uid_s, order_code = cb.data.split(":", 3)
                target_uid = int(uid_s)
            except Exception:
                await cb.message.answer("❌ Dữ liệu không hợp lệ.")
                return

            ok, archive_file, count = await clear_warranty_chat_and_archive(order_code)
            if not ok or not archive_file:
                await cb.message.answer("❌ Không tìm thấy mã bảo hành.")
                return

            await cb.message.answer_document(
                document=FSInputFile(archive_file),
                caption=(
                    "✅ ĐÃ XÓA TOÀN BỘ CHAT REPLY CỦA MÃ BẢO HÀNH\n\n"
                    f"🧾 Mã đơn: {order_code}\n"
                    f"🆔 User ID: {target_uid}\n"
                    f"🗑 Số tin nhắn đã lưu/xóa: {count}\n"
                    f"🕒 Thời gian: {now_datetime_full()}\n\n"
                    "Yêu cầu bảo hành gốc vẫn được giữ nguyên."
                )
            )

        def categories_admin_kb() -> InlineKeyboardMarkup:
            kb = InlineKeyboardBuilder()
            kb.button(text="➕ Thêm nhóm", callback_data="adm:cat_add")
            kb.button(text="✏️ Sửa nhóm", callback_data="adm:cat_edit_list")
            kb.button(text="🗑 Xóa nhóm", callback_data="adm:cat_del_list")
            kb.button(text="📦 Xem sản phẩm trong nhóm", callback_data="adm:cat_view_list")
            kb.button(text="🔼 Thứ tự hiển thị", callback_data="adm:cat_sort_list")
            kb.button(text="🟢/🔴 Bật/Tắt nhóm", callback_data="adm:cat_toggle_list")
            kb.button(text="⬅️ Admin Panel", callback_data="adm:panel")
            kb.adjust(1)
            return kb.as_markup()

        async def categories_list_kb(prefix: str) -> InlineKeyboardMarkup:
            cats = await load_categories()
            kb = InlineKeyboardBuilder()
            for c in cats:
                status = "🟢" if bool(c.get("is_active", True)) else "🔴"
                kb.button(text=f"{status} {c.get('icon','📁')} {c.get('name')} #{c.get('sort_order')}", callback_data=f"{prefix}:{c.get('id')}")
            kb.button(text="⬅️ Quản lý nhóm", callback_data="adm:categories")
            kb.adjust(1)
            return kb.as_markup()

        @dp.callback_query(F.data == "adm:categories")
        async def adm_categories(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            cats = await load_categories()
            await cb.message.answer(f"📁 QUẢN LÝ NHÓM SẢN PHẨM\n\nTổng nhóm: {len(cats)}", reply_markup=categories_admin_kb())

        @dp.callback_query(F.data == "adm:cat_add")
        async def adm_cat_add(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            ADMIN_STATE[cb.from_user.id] = {"action": "cat_add", "prompt_msg_id": None}
            sent = await cb.message.answer("➕ THÊM NHÓM\n\nReply theo cú pháp:\nIcon | Tên nhóm | Mô tả (tùy chọn)\n\nVí dụ: 📺 | YouTube | Dịch vụ YouTube")
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.in_({"adm:cat_edit_list", "adm:cat_del_list", "adm:cat_view_list", "adm:cat_sort_list", "adm:cat_toggle_list"}))
        async def adm_cat_pick_list(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            mapping = {
                "adm:cat_edit_list": ("adm:cat_edit", "✏️ Chọn nhóm cần sửa"),
                "adm:cat_del_list": ("adm:cat_del", "🗑 Chọn nhóm cần xóa"),
                "adm:cat_view_list": ("adm:cat_view", "📦 Chọn nhóm cần xem"),
                "adm:cat_sort_list": ("adm:cat_sort", "🔼 Chọn nhóm cần đổi thứ tự"),
                "adm:cat_toggle_list": ("adm:cat_toggle", "🟢/🔴 Chọn nhóm cần bật/tắt"),
            }
            prefix, title = mapping[cb.data]
            await cb.message.answer(title, reply_markup=await categories_list_kb(prefix))

        @dp.callback_query(F.data.startswith("adm:cat_view:"))
        async def adm_cat_view(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            cid = cb.data.split(":", 2)[2]
            await cb.message.answer("📦 Sản phẩm trong nhóm:", reply_markup=await products_kb_for_category(cb.from_user.id, cid))

        @dp.callback_query(F.data.startswith("adm:cat_toggle:"))
        async def adm_cat_toggle(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            cid = cb.data.split(":", 2)[2]
            cats = await load_categories()
            cat = next((c for c in cats if c.get("id") == cid), None)
            if not cat:
                await cb.message.answer("❌ Không tìm thấy nhóm.")
                return
            cat["is_active"] = not bool(cat.get("is_active", True))
            cat["updated_at"] = now_datetime_full()
            await save_categories(cats)
            await cb.message.answer(f"✅ Nhóm {cat.get('name')} hiện: {'BẬT' if cat['is_active'] else 'TẮT'}", reply_markup=categories_admin_kb())

        @dp.callback_query(F.data.startswith("adm:cat_del:"))
        async def adm_cat_del(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            cid = cb.data.split(":", 2)[2]
            if cid == "cat_other":
                await cb.message.answer("❌ Không thể xóa nhóm \"Khác\" — đây là nhóm mặc định cho sản phẩm chưa phân loại.")
                return
            products = await load_products()
            used = [p for p in products if str(p.get("category_id")) == cid]
            if used:
                await cb.message.answer(f"❌ Không thể xóa nhóm vì đang có {len(used)} sản phẩm thuộc nhóm. Hãy chuyển sản phẩm sang nhóm khác trước.")
                return
            cats = await load_categories()
            before = len(cats)
            cats = [c for c in cats if c.get("id") != cid]
            await save_categories(cats)
            await cb.message.answer("✅ Đã xóa nhóm." if len(cats) < before else "❌ Không tìm thấy nhóm.", reply_markup=categories_admin_kb())

        @dp.callback_query(F.data.startswith("adm:cat_edit:") | F.data.startswith("adm:cat_sort:"))
        async def adm_cat_edit_sort(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            is_sort = cb.data.startswith("adm:cat_sort:")
            cid = cb.data.split(":", 2)[2]
            ADMIN_STATE[cb.from_user.id] = {"action": "cat_sort" if is_sort else "cat_edit", "category_id": cid, "prompt_msg_id": None}
            prompt = "Reply số thứ tự mới (số nguyên)." if is_sort else "Reply theo cú pháp mới: Icon | Tên nhóm | Mô tả"
            sent = await cb.message.answer(prompt)
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        def api_config_kb() -> InlineKeyboardMarkup:
            kb = InlineKeyboardBuilder()
            kb.button(text="🔌 FishyMarket API", callback_data="adm:api_fishy")
            kb.button(text="🔌 Sachtai API", callback_data="adm:api_sachtai")
            kb.button(text="🔑 Cập nhật Key FishyMarket", callback_data="adm:api_set_key:fishy")
            kb.button(text="🔑 Cập nhật Key Sachtai", callback_data="adm:api_set_key:sachtai")
            kb.button(text="🔗 Đổi Base URL FishyMarket", callback_data="adm:api_set_base:fishy")
            kb.button(text="🔗 Đổi Base URL Sachtai", callback_data="adm:api_set_base:sachtai")
            kb.button(text="🔗 Xem Base URL", callback_data="adm:api_base")
            kb.button(text="🔄 Test API", callback_data="adm:api_test")
            kb.button(text="📦 Đồng bộ sản phẩm", callback_data="adm:api_sync")
            kb.button(text="📊 Kiểm tra Balance", callback_data="adm:fishy_balance")
            kb.button(text="📡 API Status", callback_data="adm:api_status")
            kb.button(text="⬅️ Admin Panel", callback_data="adm:panel")
            kb.adjust(1)
            return kb.as_markup()

        @dp.callback_query(F.data == "adm:api_config")
        async def adm_api_config(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            await cb.message.answer("⚙️ CẤU HÌNH API\n\n🔌 FishyMarket API\n🔌 Sachtai API", reply_markup=api_config_kb())

        @dp.callback_query(F.data.in_({"adm:api_fishy", "adm:api_sachtai"}))
        async def adm_api_provider_view(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            provider = API_PROVIDER_SACHTAI if cb.data.endswith("sachtai") else API_PROVIDER_FISHYMARKET
            api = SACHTAI_API if provider == API_PROVIDER_SACHTAI else FISHY_API
            base_url, api_key = await get_provider_config(provider)
            ok, bal, err = await api.get_balance()
            products = [p for p in await load_products() if str(p.get("api_provider") or "").upper() == provider]
            await cb.message.answer(
                f"🔌 {provider} API\n\n"
                f"🔑 API Key: {'********' if api_key else 'Chưa cấu hình'}\n"
                f"🔗 Base URL:\n{base_url or 'Chưa cấu hình'}\n\n"
                f"💰 Balance:\n{bal.get('balance_text') if ok else err}\n\n"
                f"📦 Products: {len(products)}\n"
                f"🟢 API: {'Online' if ok else 'Offline'}",
                reply_markup=api_config_kb()
            )

        @dp.callback_query(F.data.startswith("adm:api_set_base:"))
        async def adm_api_set_base(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            provider = "sachtai" if cb.data.endswith(":sachtai") else "fishy"
            provider_name = "Sachtai" if provider == "sachtai" else "FishyMarket"
            pkey = API_PROVIDER_SACHTAI if provider == "sachtai" else API_PROVIDER_FISHYMARKET
            base_url, _ = await get_provider_config(pkey)
            ADMIN_STATE[cb.from_user.id] = {"action": "api_set_base", "api_provider": provider, "prompt_msg_id": None}
            sent = await cb.message.answer(
                f"\U0001f517 Đổi Base URL {provider_name}\n\n"
                f"URL hiện tại:\n`{base_url}`\n\n"
                "Reply link mới dạng `https://domain.tld`.\n"
                "Ví dụ FishyMarket: `https://radio-awoke-bobcat.ngrok-free.dev`\n"
                "Ví dụ Sachtai: `https://bot.sachtai.com`\n\n"
                "Chỉ lưu khi validate URL hợp lệ."
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:api_set_key"))
        async def adm_api_set_key(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            provider = "sachtai" if cb.data.endswith(":sachtai") else "fishy"
            provider_name = "Sachtai" if provider == "sachtai" else "FishyMarket"
            key_format = "kk_live_..." if provider == "sachtai" else "fishy_live_..."
            ADMIN_STATE[cb.from_user.id] = {"action": "api_set_key", "api_provider": provider, "prompt_msg_id": None}
            sent = await cb.message.answer(
                f"🔑 Reply API Key {provider_name} mới (dạng `{key_format}`).\n"
                "Bot sẽ lưu riêng key của API này vào data/api_settings.json, không gửi key cho khách."
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data == "adm:api_base")
        async def adm_api_base(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            fishy_base, _ = await get_provider_config(API_PROVIDER_FISHYMARKET)
            sachtai_base, _ = await get_provider_config(API_PROVIDER_SACHTAI)
            text = (
                f"🔗 FishyMarket Base URL:\n`{fishy_base}`\n\n"
                f"🔗 Sachtai Base URL:\n`{sachtai_base}`"
            )
            await cb.message.answer(text, reply_markup=api_config_kb())

        @dp.callback_query(F.data.in_({"adm:api_test", "adm:fishy_balance", "adm:api_status"}))
        async def adm_api_test_balance(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            ok, bal, err = await FISHY_API.get_balance()
            if not ok:
                await cb.message.answer(f"❌ API lỗi: {err}", reply_markup=api_config_kb())
                return
            st = await load_api_settings()
            low = int(bal.get("balance", 0)) < int(st.get("low_balance_threshold", FISHY_LOW_BALANCE_THRESHOLD))
            await cb.message.answer(f"💰 API Balance\n\n{bal.get('balance_text')}\nCurrency: {bal.get('wallet_currency')}\n\n{'⚠️ API Balance thấp' if low else '✅ API sẵn sàng'}", reply_markup=api_config_kb())

        @dp.callback_query(F.data == "adm:api_sync")
        async def adm_api_sync(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            ok, msg, created, updated, price_changes1 = await sync_fishy_products()
            ok2, msg2, created2, updated2, price_changes2 = await sync_sachtai_products()
            all_price_changes = price_changes1 + price_changes2
            if not ok:
                await cb.message.answer(f"❌ Đồng bộ thất bại: {msg}", reply_markup=api_config_kb())
                return
            # Enforce stock constraints after manual sync
            if updated > 0 or updated2 > 0:
                try:
                    await enforce_api_stock_constraints(bot)
                except Exception:
                    pass
            # Notify about price changes
            for pc in all_price_changes:
                new_p = pc["new_api_price"]
                sale_p = pc["sale_price"]
                if new_p > sale_p:
                    await notify_admin(bot,
                        f"🚨 Giá API cao hơn giá bán — stock đã về 0\n\n"
                        f"📦 {pc['name']}\n"
                        f"💰 Giá API: {vnd(pc['old_api_price'])}đ → {vnd(new_p)}đ\n"
                        f"💵 Giá bán: {vnd(sale_p)}đ\n\n"
                        f"⚠️ Lý do: Giá API ({vnd(new_p)}đ) > Giá bán ({vnd(sale_p)}đ)\n"
                        f"🕒 {now_datetime_full()}"
                    )
                else:
                    await notify_admin(bot,
                        f"⚠️ Giá API thay đổi\n\n"
                        f"📦 {pc['name']}\n"
                        f"💰 Giá API: {vnd(pc['old_api_price'])}đ → {vnd(new_p)}đ\n"
                        f"💵 Giá bán: {vnd(sale_p)}đ\n\n"
                        f"🕒 {now_datetime_full()}"
                    )
            price_msg = f"\n🔔 {len(all_price_changes)} sản phẩm thay đổi giá API" if all_price_changes else ""
            await cb.message.answer(
                "✅ Đã đồng bộ dữ liệu cho các sản phẩm API đã tạo.\n"
                "Không tự thêm sản phẩm hoặc nhóm sản phẩm.\n\n"
                f"FishyMarket: 🔄 {updated}\n"
                f"Sachtai: {'✅' if ok2 else '❌'} 🔄 {updated2} {msg2 if not ok2 else ''}"
                f"{price_msg}",
                reply_markup=api_config_kb(),
            )

        @dp.callback_query(F.data == "adm:add_api_sp")
        async def adm_add_api_sp(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            if not is_admin(cb.from_user.id):
                return

            await cb.message.answer("🌐 CHỌN API ĐỂ THÊM SẢN PHẨM", reply_markup=api_provider_select_kb())

        @dp.callback_query(F.data.startswith("adm:add_api_provider:"))
        async def adm_add_api_provider(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            provider = cb.data.rsplit(":", 1)[1]
            if provider not in {"legacy", "fishy", "sachtai"}:
                return
            ok, items, err = await fetch_api_products_for_admin(provider)
            if not ok:
                await cb.message.answer(f"❌ Không lấy được danh sách API: {err}")
                return
            if not items:
                await cb.message.answer("⚠️ API chưa có sản phẩm để thêm.")
                return

            ADMIN_STATE[cb.from_user.id] = {"action": "pick_api_product", "api_items": items, "api_provider": provider}
            await cb.message.answer("🌐 CHỌN SẢN PHẨM TỪ API", reply_markup=api_products_select_kb(provider, items))

        @dp.callback_query(F.data.startswith("adm:pick_api:"))
        async def adm_pick_api_product(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            parts = cb.data.split(":", 3)
            if len(parts) != 4:
                await cb.message.answer("❌ Dữ liệu sản phẩm API không hợp lệ. Vui lòng chọn lại.")
                return
            provider, pid = parts[2], parts[3].strip()
            st = ADMIN_STATE.get(cb.from_user.id, {})
            items = st.get("api_items", []) if isinstance(st.get("api_items", []), list) else []
            item = next((x for x in items if str(x.get("id")) == pid), None) if st.get("api_provider") == provider else None
            if not item:
                await cb.message.answer("❌ Không tìm thấy sản phẩm API đã chọn. Vui lòng mở lại danh sách.")
                return

            ADMIN_STATE[cb.from_user.id] = {
                "action": "add_api_price",
                "api_item": item,
                "api_provider": provider,
                "prompt_msg_id": None
            }
            sent = await cb.message.answer(
                "🌐 THÊM SẢN PHẨM TỪ API\n\n"
                f"Tên gốc: {item.get('name')}\n"
                f"Giá gốc API: {vnd(int(item.get('price', 0)))}đ\n"
                f"Tồn API: {int(item.get('stock', 0))}\n\n"
                "Bước 1/3: Reply giá bán tại bot (số nguyên >= 0)."
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:editprice:"))
        async def adm_edit_price(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            pid = cb.data.split(":", 2)[2].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Sản phẩm không tồn tại.")
                return

            ADMIN_STATE[cb.from_user.id] = {"action": "edit_price", "product_id": pid, "prompt_msg_id": None}
            sent = await cb.message.answer(
                "💵 SỬA GIÁ SẢN PHẨM\n\n"
                f"📦 {product['name']}\n"
                f"💰 Giá hiện tại: {vnd(int(product['price_vnd']))}đ\n\n"
                "Reply tin nhắn này bằng giá mới (số nguyên >= 0).\n"
                "Gõ /cancel để hủy."
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        

        @dp.callback_query(F.data.startswith("adm:sale:"))
        async def adm_sale_prompt(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            pid = cb.data.split(":", 2)[2].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Sản phẩm không tồn tại.")
                return
            ADMIN_STATE[cb.from_user.id] = {"action": "sale_product", "product_id": pid, "prompt_msg_id": None}
            sent = await cb.message.answer(
                "🔥 CÀI SALE SẢN PHẨM\n\n"
                f"📦 {product_display_name(product)}\n"
                f"💰 Giá hiện tại: {vnd(int(product.get('price_vnd', 0)))}đ\n"
                f"🏷 Sale hiện tại: {int(product.get('sale_percent', 0) or 0)}%\n\n"
                "Reply phần trăm giảm giá từ 1-99 (ví dụ: 10).\n"
                "Có thể kèm thời gian sale theo giờ: 10 24 = giảm 10% trong 24 giờ.\n"
                "Reply 0 hoặc off để tắt sale và khôi phục giá gốc.\n"
                "Gõ /cancel để hủy."
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data == "adm:api_balance")
        async def adm_api_balance(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            all_products = await load_products()
            providers = {
                api_provider_key(p) for p in all_products
                if str(p.get("type", "local")) == "api" and api_external_product_id(p)
            } or {API_PROVIDER_LEGACY}
            ready = False
            errs: list[str] = []
            for prov in sorted(providers):
                ok, snap, err = await get_api_stock_budget_snapshot(prov)
                if not ok:
                    errs.append(f"{prov}: {err}")
                elif int(snap.get("money_available", 0)) > 0:
                    ready = True
            if errs and not ready:
                await cb.message.answer("❌ Không lấy được trạng thái nguồn hàng:\n" + "\n".join(errs[:3]))
                return
            status = "✅ Sẵn sàng" if ready else "⚠️ Cần kiểm tra"
            lines = ["💳 TRẠNG THÁI NGUỒN HÀNG (ADMIN)\n", f"• Trạng thái: {status}"]
            for prov in sorted(providers):
                ok, snap, err = await get_api_stock_budget_snapshot(prov)
                if not ok:
                    lines.append(f"\n❌ {prov}: {err}")
                else:
                    lines.append(f"\n🏭 {prov}:")
                    lines.append(f"  💰 Số dư: {vnd(int(snap.get('balance', 0)))}đ")
                    lines.append(f"  📦 Đang giữ chỗ: {vnd(int(snap.get('money_allocated', 0)))}đ")
                    lines.append(f"  ✅ Khả dụng: {vnd(int(snap.get('money_available', 0)))}đ")
            await cb.message.answer("\n".join(lines))

        @dp.callback_query(F.data.startswith("adm:api_add:"))
        async def adm_api_add(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            pid = cb.data.split(":", 2)[2].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product or str(product.get("type", "local")) != "api":
                await cb.message.answer("❌ Sản phẩm tự động không tồn tại.")
                return
            ADMIN_STATE[cb.from_user.id] = {"action": "api_stock_add", "product_id": pid, "prompt_msg_id": None}
            cap = await api_stock_add_capacity(product)
            sent = await cb.message.answer(
                "➕ THÊM STOCK TỰ ĐỘNG (THỦ CÔNG)\n\n"
                f"📦 {product['name']}\n"
                f"📊 Tồn bot hiện tại: {cap['current']}\n"
                f"🏭 Tồn nguồn: {cap['provider_raw']} (dùng được {cap['provider_cap']}, chừa {API_STOCK_SAFETY_BUFFER})\n"
                f"💰 Khả dụng: {vnd(cap['money_available'])}đ / giá gốc {vnd(cap['api_price'])}đ = {cap['money_room']}\n"
                f"✅ Có thể thêm tối đa: {cap['max_add']}\n"
                f"ℹ️ {cap['reason']}\n\n"
                "Reply số lượng cần THÊM (số nguyên dương)."
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:api_reduce:"))
        async def adm_api_reduce(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            pid = cb.data.split(":", 2)[2].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product or str(product.get("type", "local")) != "api":
                await cb.message.answer("❌ Sản phẩm tự động không tồn tại.")
                return
            ADMIN_STATE[cb.from_user.id] = {"action": "api_stock_reduce", "product_id": pid, "prompt_msg_id": None}
            sent = await cb.message.answer(
                "➖ GIẢM STOCK TỰ ĐỘNG (THỦ CÔNG)\n\n"
                f"📦 {product['name']}\n"
                f"📊 Tồn hiện tại: {int(product.get('stock', 0))}\n"
                "Reply số lượng cần GIẢM (số nguyên dương)."
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data == "adm:addsp")
        async def adm_addsp(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)

            if not is_admin(cb.from_user.id):
                return

            ADMIN_STATE[cb.from_user.id] = {"action": "add_product", "prompt_msg_id": None}
            sent = await cb.message.answer(
                "➕ THÊM SẢN PHẨM\n\n"
                "✅ BẮT BUỘC reply tin nhắn này để bot nhận.\n\n"
                "Gửi theo cú pháp:\n"
                "Tên sản phẩm | Giá\n\n"
                "Ví dụ: capcut859 | 10000\n"
                "✅ Stock khi tạo = 0, khi thêm hàng sẽ tự tăng.\n\n"
                "Gõ /cancel để hủy."
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data == "adm:add_manual_sp")
        async def adm_add_manual_sp(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            ADMIN_STATE[cb.from_user.id] = {"action": "add_manual_product", "prompt_msg_id": None}
            sent = await cb.message.answer(
                "📝 THÊM SẢN PHẨM ĐƠN TAY\n\n"
                "Reply tin nhắn này theo cú pháp:\n"
                "Tên sản phẩm | Giá tiền | dontay | trường 1, trường 2?, ghi chú?\n\n"
                "Dấu ? hoặc (optional) = không bắt buộc.\n"
                "Ví dụ: CapCut Pro | 50000 | dontay | email, sdt?, ghi chú?"
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:manual_stock_view:"))
        async def adm_manual_stock_view(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            pid = cb.data.split(":", 2)[2]
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Sản phẩm không tồn tại.")
                return
            await cb.message.answer(f"📊 STOCK ĐƠN TAY\n\n📦 {product['name']}\n💰 Giá: {vnd(int(product['price_vnd']))}đ\n📊 Stock: {product.get('stock', 0)}")

        @dp.callback_query(F.data.startswith("adm:manual_stock_add:") | F.data.startswith("adm:manual_stock_reduce:"))
        async def adm_manual_stock_change(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            reduce = cb.data.startswith("adm:manual_stock_reduce:")
            pid = cb.data.split(":", 2)[2]
            ADMIN_STATE[cb.from_user.id] = {"action": "manual_stock_reduce" if reduce else "manual_stock_add", "product_id": pid, "prompt_msg_id": None}
            sent = await cb.message.answer("Reply số lượng cần thay đổi stock (ví dụ: +10 hoặc -5).")
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:addsp_cat:"))
        async def adm_addsp_cat(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            cid = cb.data.split(":", 2)[2]
            ADMIN_STATE[cb.from_user.id] = {"action": "add_product", "category_id": cid, "prompt_msg_id": None}
            sent = await cb.message.answer("➕ THÊM SẢN PHẨM VÀO NHÓM\n\nReply: Tên sản phẩm | Giá")
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:hot_toggle:"))
        async def adm_hot_toggle(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            pid = cb.data.split(":", 2)[2].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Sản phẩm không tồn tại.")
                return
            current = bool(product.get("is_hot", False))
            product["is_hot"] = not current
            await save_products(products)
            status = "⭐ ĐÃ BẬT HOT" if not current else "⬜ ĐÃ TẮT HOT"
            await cb.message.answer(
                f"{status}\n\n📦 {product['name']}\n\n"
                + ("Sản phẩm sẽ hiển thị nổi bật ở menu chính." if not current else "Sản phẩm trở về vị trí bình thường trong nhóm.")
            )
            await cb.message.answer(
                "🛠 QUẢN LÝ SẢN PHẨM",
                reply_markup=admin_product_manage_kb(pid, str(product.get("type", "local")), is_hot=not current),
            )

        @dp.callback_query(F.data.startswith("adm:move_product:"))
        async def adm_move_product(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            product_id = cb.data.split(":", 2)[2]
            products = await load_products()
            if not any(str(product.get("id")) == product_id for product in products):
                await cb.message.answer("❌ Không tìm thấy sản phẩm.")
                return
            await cb.message.answer(
                "📁 CHUYỂN VỊ TRÍ SẢN PHẨM\n\nChọn nhóm đích hoặc để sản phẩm ở ngoài nhóm:",
                reply_markup=await product_category_move_kb(product_id),
            )

        @dp.callback_query(F.data.startswith("adm:move_to:"))
        async def adm_move_product_to_category(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            parts = cb.data.split(":", 3)
            if len(parts) != 4:
                await cb.message.answer("❌ Dữ liệu chuyển nhóm không hợp lệ.")
                return
            product_id, category_id = parts[2], parts[3]
            if category_id != "cat_other":
                categories = await load_categories()
                if not any(str(category.get("id")) == category_id for category in categories):
                    await cb.message.answer("❌ Nhóm đích không tồn tại.")
                    return
            products = await load_products()
            product = next((item for item in products if str(item.get("id")) == product_id), None)
            if not product:
                await cb.message.answer("❌ Không tìm thấy sản phẩm.")
                return
            product["category_id"] = category_id
            await save_products(products)
            destination = "ngoài nhóm" if category_id == "cat_other" else category_name(await load_categories(), category_id)
            await cb.message.answer(
                f"✅ Đã chuyển **{product.get('name')}** vào {destination}.",
                reply_markup=admin_product_manage_kb(product_id, str(product.get("type", "local")), is_hot=bool(product.get("is_hot", False))),
            )

        @dp.callback_query(F.data.startswith("adm:delsp_cat:"))
        async def adm_delsp_cat(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            cid = cb.data.split(":", 2)[2]
            title = "ngoài nhóm" if cid == "cat_other" else "trong nhóm"
            await cb.message.answer(
                f"🗑 XÓA SẢN PHẨM {title.upper()}\nChọn sản phẩm cần xóa:",
                reply_markup=await products_delete_list_kb(cid),
            )

        # ================== ADMIN: delete product ==================
        @dp.callback_query(F.data == "adm:delsp")
        async def adm_delsp(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            if not is_admin(cb.from_user.id):
                return

            await cb.message.answer(
                "🗑 XÓA SẢN PHẨM\nChọn sản phẩm cần xóa bên dưới:",
                reply_markup=await products_delete_list_kb()
            )

        @dp.callback_query(F.data.startswith("adm:del:"))
        async def adm_del_one(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            await add_user_global(cb.from_user.id)
            if not is_admin(cb.from_user.id):
                return

            pid = cb.data.split("adm:del:", 1)[1].strip()
            products_before = await load_products()
            was_api = any(p.get("id") == pid and str(p.get("type","local")) == "api" for p in products_before)
            ok = await delete_product(pid)
            if ok:
                if was_api:
                    await enforce_api_stock_constraints(bot)
                await cb.message.answer("✅ Đã xóa sản phẩm.", reply_markup=await products_kb_for_user(cb.from_user.id))
            else:
                await cb.message.answer("❌ Không tìm thấy sản phẩm để xóa.", reply_markup=await products_kb_for_user(cb.from_user.id))

        # ================== ADMIN confirm/cancel add_data ==================
        @dp.callback_query(F.data == "adm:data_cancel")
        async def adm_data_cancel(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            if not is_admin(cb.from_user.id):
                return
            ADMIN_STATE.pop(cb.from_user.id, None)
            await cb.message.answer("❌ Đã hủy thao tác thêm dữ liệu.")

        @dp.callback_query(F.data == "adm:data_confirm")
        async def adm_data_confirm(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            if not is_admin(cb.from_user.id):
                return

            st = ADMIN_STATE.get(cb.from_user.id)
            if not st or st.get("action") != "add_data":
                await cb.message.answer("❌ Không có dữ liệu để xác nhận.")
                return

            pid = st.get("product_id")
            lines = [str(x).strip() for x in st.get("buffer", []) if str(x).strip()]
            if not pid or not lines:
                await cb.message.answer("❌ Không có dữ liệu hợp lệ để lưu.\n\n⚠️ Nhắc: Bạn phải reply tin nhắn bot để gửi hàng.")
                return

            await append_product_lines(pid, lines)

            products = await load_products()
            p = next((x for x in products if x.get("id") == pid), None)
            if not p:
                ADMIN_STATE.pop(cb.from_user.id, None)
                await cb.message.answer("❌ Không tìm thấy sản phẩm.")
                return

            added = len(lines)
            p["stock"] = int(p.get("stock", 0)) + added
            p["out_of_stock_notified"] = ""
            await save_products(products)

            ADMIN_LAST_ADDED[cb.from_user.id] = {
                "product_id": pid,
                "added_count": added,
                "time": now_datetime_full()
            }

            ADMIN_STATE[cb.from_user.id] = {"action": None}

            await cb.message.answer(
                "✅ ĐÃ LƯU DỮ LIỆU\n\n"
                f"📦 {p['name']}\n💰 Giá: {vnd(int(p.get('price_vnd', 0)))}đ\n➕ Thêm: {added} hàng\n📊 Stock mới: {p['stock']}\n\n"
                "Bạn muốn làm gì tiếp theo?",
                reply_markup=admin_after_add_kb(pid, added)
            )

        # ================== ADMIN undo batch just added ==================
        @dp.callback_query(F.data.startswith("adm:undo_add:"))
        async def adm_undo_add(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            if not is_admin(cb.from_user.id):
                return

            try:
                _, _, pid, added_s = cb.data.split(":", 3)
                added = int(added_s)
            except Exception:
                await cb.message.answer("❌ Dữ liệu undo không hợp lệ.")
                return

            last = ADMIN_LAST_ADDED.get(cb.from_user.id)
            if not last or last.get("product_id") != pid or int(last.get("added_count", 0)) != added:
                await cb.message.answer("⚠️ Không tìm thấy batch vừa thêm để xóa (hoặc bạn đã thao tác batch khác).")
                return

            removed = await remove_last_product_lines(pid, added)
            if len(removed) != added:
                await cb.message.answer("❌ Không thể xóa đủ số dòng (file đã thay đổi).")
                return

            products = await load_products()
            p = next((x for x in products if x.get("id") == pid), None)
            if not p:
                await cb.message.answer("❌ Không tìm thấy sản phẩm.")
                return

            p["stock"] = max(0, int(p.get("stock", 0)) - added)
            await save_products(products)
            ADMIN_LAST_ADDED.pop(cb.from_user.id, None)

            await cb.message.answer(
                "✅ ĐÃ XÓA HÀNG VỪA THÊM\n\n"
                f"🗑 Đã gỡ: {added} dòng\n"
                f"📊 Stock mới: {p['stock']}\n\n"
                "Bạn có thể bấm 🔁 THÊM TIẾP để gửi lại.",
                reply_markup=admin_after_add_kb(pid, 0)
            )

        # ================== ADMIN add more data ==================
        @dp.callback_query(F.data.startswith("adm:add_more:"))
        async def adm_add_more(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            if not is_admin(cb.from_user.id):
                return

            pid = cb.data.split(":", 2)[2].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Sản phẩm không tồn tại.")
                return

            ADMIN_STATE[cb.from_user.id] = {"action": "add_data", "product_id": pid, "buffer": [], "prompt_msg_id": None}
            sent = await cb.message.answer(
                "🛠 THÊM DỮ LIỆU SẢN PHẨM\n\n"
                f"📦 {product['name']}\n"
                f"💰 Giá: {vnd(int(product['price_vnd']))}đ\n"
                f"📊 Tồn hiện tại: {product.get('stock', 0)}\n\n"
                "✅ BẮT BUỘC reply tin nhắn này để bot nhận hàng.\n\n"
                "➡️ Gửi TEXT (mỗi dòng = 1 dữ liệu)\n"
                "➡️ Hoặc gửi FILE .txt\n\n"
                "Sau khi gửi xong bấm ✅ XÁC NHẬN LƯU hoặc ❌ HỦY.",
                reply_markup=admin_data_confirm_kb()
            )
            ADMIN_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("adm:sale_notify:"))
        async def adm_sale_notify_all(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            pid = cb.data.split(":", 2)[2].strip()
            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Không tìm thấy sản phẩm để thông báo sale.")
                return
            users = await load_all_users()
            if not users:
                await cb.message.answer("⚠️ Chưa có người dùng nào.")
                return
            msg = sale_announcement_text(product)
            ok = 0
            fail = 0
            for target_uid in users:
                try:
                    await bot.send_message(target_uid, msg)
                    ok += 1
                except Exception:
                    fail += 1
                await asyncio.sleep(0.05)
            await cb.message.answer(
                "✅ ĐÃ THÔNG BÁO SALE TỚI TẤT CẢ\n\n"
                f"📦 {product_display_name(product)}\n"
                f"📨 Thành công: {ok}\n"
                f"❌ Thất bại: {fail}",
                reply_markup=admin_panel_kb()
            )

        # ================== ADMIN notify all after add ==================
        @dp.callback_query(F.data.startswith("adm:notify_all:"))
        async def adm_notify_all(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            if not is_admin(cb.from_user.id):
                return

            try:
                _, _, pid, added_s = cb.data.split(":", 3)
                added = int(added_s)
            except Exception:
                await cb.message.answer("❌ Dữ liệu thông báo không hợp lệ.")
                return

            products = await load_products()
            product = next((p for p in products if p.get("id") == pid), None)
            if not product:
                await cb.message.answer("❌ Không tìm thấy sản phẩm.")
                return

            users = await load_all_users()
            if not users:
                await cb.message.answer("⚠️ Chưa có người dùng nào.")
                return

            msg = (
                "╭─ 📣 HÀNG MỚI VỀ SHOP ─╮\n"
                f"✨ {product_display_name(product)}\n"
                "╰────────────────────╯\n\n"
                f"✅ Vừa được bổ sung: +{added} hàng\n"
                f"📦 Tồn kho hiện tại: {product.get('stock', 0)}\n\n"
                "🛒 Sản phẩm đã sẵn sàng để mua.\n"
                "👉 Bấm /menu để xem và đặt hàng ngay nhé!"
            )

            ok = 0
            fail = 0
            for uid in users:
                try:
                    await bot.send_message(uid, msg)
                    ok += 1
                except Exception:
                    fail += 1
                await asyncio.sleep(0.05)

            await cb.message.answer(
                "✅ ĐÃ GỬI THÔNG BÁO\n"
                f"📨 Thành công: {ok}\n"
                f"❌ Thất bại: {fail}"
            )

        # ================== /cancel ==================
        @dp.message(Command("cancel"))
        async def cmd_cancel(message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            await add_user_global(uid)

            if uid in ADMIN_STATE:
                ADMIN_STATE.pop(uid, None)
                await message.answer("✅ Đã hủy thao tác admin.", reply_markup=await products_kb_for_user(uid))
                return

            if uid in ADMIN_REPLY_STATE:
                ADMIN_REPLY_STATE.pop(uid, None)
                await message.answer("✅ Đã hủy thao tác phản hồi bảo hành.")
                return

            if uid in BUY_STATE and BUY_STATE[uid].get("await_qty"):
                BUY_STATE.pop(uid, None)
                await message.answer("✅ Đã hủy thao tác mua.", reply_markup=back_to_menu_kb())
                return

            if uid in WARRANTY_STATE:
                WARRANTY_STATE.pop(uid, None)
                await message.answer("✅ Đã hủy yêu cầu bảo hành.")
                return

            await message.answer("✅ Không có thao tác nào để hủy.")

        # ================== MANUAL ORDER FLOW ==================
        @dp.callback_query(F.data.startswith("mo:send:"))
        async def manual_send_data(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            oc = cb.data.split(":", 2)[2]
            order = await get_manual_order(oc)
            if not order or int(order.get("user_id", 0)) != cb.from_user.id:
                await cb.message.answer("❌ Không tìm thấy đơn hoặc bạn không có quyền thao tác.")
                return
            if order.get("status") not in {"WAITING_CUSTOMER_DATA", "CUSTOMER_SUBMITTED"}:
                await cb.message.answer("❌ Chỉ có thể bổ sung/chỉnh sửa khi đơn đang chờ xử lý.")
                return
            BUY_STATE[cb.from_user.id] = {"action": "manual_data", "order_code": oc}
            await cb.message.answer("📤 Reply/Gửi thông tin đơn hàng theo mẫu:\n\n" + manual_fields_text(order.get("manual_fields", [])) + "\n\nCó thể gửi dạng `Email: abc@gmail.com` mỗi dòng.")

        @dp.callback_query(F.data.startswith("mo:chat:"))
        async def manual_chat_start(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            oc = cb.data.split(":", 2)[2]
            order = await get_manual_order(oc)
            if not order:
                order = await get_auto_order(oc)
            if not order or int(order.get("user_id", 0)) != cb.from_user.id:
                await cb.message.answer("❌ Không tìm thấy đơn hoặc bạn không có quyền thao tác.")
                return
            BUY_STATE[cb.from_user.id] = {"action": "manual_chat", "order_code": oc}
            await cb.message.answer(f"💬 Gửi nội dung cần trao đổi về đơn #{oc}.")

        @dp.callback_query(F.data.startswith("mo:ucancel:"))
        async def manual_user_cancel(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            oc = cb.data.split(":", 2)[2]
            order = await get_manual_order(oc)
            if not order or int(order.get("user_id", 0)) != cb.from_user.id:
                await cb.message.answer("❌ Không tìm thấy đơn.")
                return
            await notify_admin(bot, f"⚠️ Khách yêu cầu hủy đơn tay #{oc}. Vui lòng kiểm tra và bấm hủy nếu cần hoàn tiền.", reply_markup=manual_admin_kb(oc, cb.from_user.id))
            await cb.message.answer("✅ Đã báo Admin yêu cầu hủy đơn. Vui lòng chờ xử lý.")

        @dp.callback_query(F.data.startswith("adm:mo_done:") | F.data.startswith("adm:mo_cancel:") | F.data.startswith("adm:mo_reply:"))
        async def admin_manual_action(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            parts = cb.data.split(":")
            action, target_uid, oc = parts[1], int(parts[2]), parts[3]
            order = await get_manual_order(oc)
            is_auto = False
            if not order:
                order = await get_auto_order(oc)
                is_auto = order is not None
            if not order or int(order.get("user_id", 0)) != target_uid:
                await cb.message.answer("❌ Không tìm thấy đơn.")
                return
            if is_auto and action != "mo_reply":
                await cb.message.answer("❌ Đơn API có quy trình tự động riêng. Chỉ dùng được nút Trả lời khách.")
                return
            if not is_auto:
                if action == "mo_done" and order.get("status") in {"CANCELLED", "REFUNDED", "COMPLETED"}:
                    await cb.message.answer("❌ Không thể hoàn thành đơn đã hủy/hoàn thành.")
                    return
                if action == "mo_cancel" and order.get("status") in {"COMPLETED", "CANCELLED", "REFUNDED"}:
                    await cb.message.answer("❌ Không thể hủy đơn đã hoàn thành/đã hủy.")
                    return
            ADMIN_REPLY_STATE[cb.from_user.id] = {"action": action, "target_uid": target_uid, "order_code": oc, "prompt_msg_id": None, "is_auto": is_auto}
            prompt = "Nhập dữ liệu/kết quả trả cho khách:" if action == "mo_done" else ("Nhập lý do hủy đơn:" if action == "mo_cancel" else "Nhập nội dung trả lời khách:")
            sent = await cb.message.answer(prompt)
            ADMIN_REPLY_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        @dp.callback_query(F.data.startswith("auto:change_email:"))
        async def auto_change_email_start(cb: CallbackQuery):
            if not cb.from_user:
                return
            await safe_cb_answer(cb)
            oc = cb.data.split(":", 2)[2]
            order = await get_auto_order(oc)
            if not order or int(order.get("user_id", 0)) != cb.from_user.id:
                await cb.message.answer("❌ Không tìm thấy đơn hoặc bạn không có quyền thao tác.")
                return
            ok, err = await can_request_email_change(order)
            if not ok:
                await cb.message.answer(f"❌ {err}")
                return
            BUY_STATE[cb.from_user.id] = {"action": "await_change_email", "order_code": oc}
            await cb.message.answer("📧 Nhập email mới:")

        @dp.callback_query(F.data.startswith("adm:email_ok:"))
        async def adm_email_ok(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            req_id = cb.data.split(":", 2)[2]
            req = await get_email_change_request(req_id)
            if not req or req.get("status") != "PENDING":
                await cb.message.answer("❌ Request không tồn tại hoặc đã xử lý.")
                return
            oc = str(req.get("order_code"))
            async with _order_lock(oc):
                order = await get_auto_order(oc)
                if not order:
                    await cb.message.answer("❌ Không tìm thấy đơn.")
                    return
                ok, err = await can_request_email_change(order)
                if not ok:
                    req["status"] = "REJECTED"; req["reason"] = err; req["processed_at"] = now_datetime_full(); req["admin_id"] = cb.from_user.id
                    await upsert_email_change_request(req)
                    await cb.message.answer(f"❌ Không thể duyệt: {err}")
                    return
                old = str(order.get("customer_email", ""))
                new = str(req.get("new_email", ""))
                order["customer_email"] = new
                order["change_email_count"] = int(order.get("change_email_count", 0) or 0) + 1
                order.setdefault("email_change_history", []).append({"old_email": old, "new_email": new, "changed_at": now_datetime_full(), "admin_id": cb.from_user.id})
                add_order_history(order, f"admin:{cb.from_user.id}", "Admin xác nhận đổi email", ORDER_STATUS_COMPLETED, ORDER_STATUS_COMPLETED, f"{old} -> {new}")
                await upsert_auto_order(order)
                req["status"] = "APPROVED"; req["processed_at"] = now_datetime_full(); req["admin_id"] = cb.from_user.id; await upsert_email_change_request(req)
            try:
                await bot.send_message(int(order.get("user_id", 0)), f"✅ ĐỔI EMAIL THÀNH CÔNG\n\n🆔 Đơn hàng: #{oc}\n\n📧 Email mới:\n{new}\n\n🔢 Lượt đã sử dụng:\n{order.get('change_email_count')}/2", reply_markup=auto_order_user_kb(order))
            except Exception:
                pass
            await cb.message.answer("✅ Đã xác nhận đổi email cho khách.")

        @dp.callback_query(F.data.startswith("adm:email_reject:") | F.data.startswith("adm:email_msg:"))
        async def adm_email_reject_msg(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            action = "email_reject" if cb.data.startswith("adm:email_reject:") else "email_msg"
            req_id = cb.data.split(":", 2)[2]
            req = await get_email_change_request(req_id)
            if not req:
                await cb.message.answer("❌ Request không tồn tại.")
                return
            ADMIN_REPLY_STATE[cb.from_user.id] = {"action": action, "request_id": req_id, "prompt_msg_id": None}
            sent = await cb.message.answer("Nhập lý do từ chối:" if action == "email_reject" else "Nhập nội dung nhắn khách:")
            ADMIN_REPLY_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id


        # ================== ADMIN AUTO ORDER FAILED ACTIONS ==================
        @dp.callback_query(F.data.startswith("adm:auto_refund:"))
        async def adm_auto_refund(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            order_code = cb.data.split(":", 2)[2]
            async with _order_lock(order_code):
                order = await get_auto_order(order_code)
                if not order:
                    await cb.message.answer("❌ Không tìm thấy đơn.")
                    return
                if str(order.get("refund_status", "")) == ORDER_STATUS_REFUNDED:
                    await cb.message.answer("⚠️ Đơn này đã hoàn tiền trước đó.")
                    return
                refunded = await refund_auto_order_once(int(order.get("user_id", 0)), order, f"Admin hoàn tiền thủ công {order_code}")
                if not refunded:
                    await cb.message.answer("❌ Không thể hoàn tiền (đơn chưa thanh toán hoặc đã hoàn).")
                    return
                await update_user_order_status(int(order.get("user_id", 0)), order_code, "❌ Đã hoàn tiền (Admin xử lý)")
            try:
                await bot.send_message(int(order.get("user_id", 0)), f"💰 ĐƠN HÀNG ĐÃ ĐƯỢC HOÀN TIỀN\n\n🆔 #{order_code}\n📦 {order.get('product_name')}\n\n✅ Admin đã xử lý hoàn tiền cho bạn. Số tiền đã được cộng lại vào ví.")
            except Exception:
                pass
            await cb.message.answer(f"✅ Đã hoàn tiền thành công cho đơn #{order_code}.")

        @dp.callback_query(F.data.startswith("adm:auto_send_account:"))
        async def adm_auto_send_account(cb: CallbackQuery):
            if not cb.from_user or not is_admin(cb.from_user.id):
                return
            await safe_cb_answer(cb)
            order_code = cb.data.split(":", 2)[2]
            order = await get_auto_order(order_code)
            if not order:
                await cb.message.answer("❌ Không tìm thấy đơn.")
                return
            ADMIN_REPLY_STATE[cb.from_user.id] = {"action": "send_account_info", "target_uid": int(order.get("user_id", 0)), "order_code": order_code, "prompt_msg_id": None}
            sent = await cb.message.answer(
                f"📧 GỬI THÔNG TIN TÀI KHOẢN CHO KHÁCH\n\n"
                f"🧾 Mã đơn: {order_code}\n"
                f"👤 User ID: {order.get('user_id')}\n"
                f"📦 Sản phẩm: {order.get('product_name')}\n\n"
                f"✅ Reply tin nhắn này bằng nội dung tài khoản cần gửi cho khách.\n"
                f"Gõ /cancel để hủy."
            )
            ADMIN_REPLY_STATE[cb.from_user.id]["prompt_msg_id"] = sent.message_id

        # ================== TEXT ROUTER ==================
        @dp.message(F.text & ~F.text.startswith("/"))
        async def text_router(message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            text = (message.text or "").strip()

            # ---- user reply tiếp bảo hành cho admin ----
            if message.reply_to_message:
                map_key = f"{uid}:{message.reply_to_message.message_id}"
                order_code = USER_WARRANTY_REPLY_MAP.get(map_key)
                if order_code:
                    data = await load_warranty(order_code)
                    if not data:
                        await message.answer("❌ Không tìm thấy mã bảo hành.")
                        return
                    if bool(data.get("closed", False)):
                        await message.answer("❌ Mã bảo hành này đã đóng.")
                        return

                    await append_warranty_chat(order_code, {
                        "time": now_datetime_full(),
                        "sender": f"user:{uid}",
                        "text": text
                    })

                    uname = f"@{message.from_user.username}" if message.from_user.username else "N/A"
                    await notify_admin(
                        bot,
                        "📩 KHÁCH REPLY TIẾP BẢO HÀNH\n\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"🧾 Mã đơn: {order_code}\n"
                        f"🆔 ID khách: {uid}\n"
                        f"👤 User: {message.from_user.full_name}\n"
                        f"🔗 Username: {uname}\n"
                        f"🕒 Thời gian: {now_datetime_full()}\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"📝 Nội dung:\n{text}",
                        reply_markup=admin_warranty_chat_kb(uid, order_code)
                    )
                    await message.answer(
                        "✅ Đã gửi tin nhắn tới admin.\n\n"
                        f"🧾 Mã đơn: {order_code}\n"
                        f"🆔 ID của bạn: {uid}"
                    )
                    return

            # ---- USER initial warranty claim by text ----
            st_warranty = WARRANTY_STATE.get(uid)
            if st_warranty and st_warranty.get("action") == "await_claim":
                prompt_id = st_warranty.get("prompt_msg_id")
                if not message.reply_to_message or not prompt_id or message.reply_to_message.message_id != prompt_id:
                    await message.answer("⚠️ Bạn phải reply đúng tin nhắn yêu cầu bảo hành và gửi ảnh hoặc tin nhắn mô tả lỗi.")
                    return
                order_code = str(st_warranty.get("order_code", "")).strip()
                wdata = {
                    "order_code": order_code,
                    "user_id": uid,
                    "reason": text,
                    "photo_file_id": "",
                    "created_time": now_datetime_full(),
                    "status": "pending",
                    "closed": False,
                    "close_reason": "",
                    "chat_messages": []
                }
                await save_warranty(wdata)
                uname = f"@{message.from_user.username}" if message.from_user.username else "N/A"
                await notify_admin(
                    bot,
                    "🛡 CÓ YÊU CẦU BẢO HÀNH MỚI\n\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"👤 User: {message.from_user.full_name}\n"
                    f"🔗 Username: {uname}\n"
                    f"🆔 ID: {uid}\n"
                    f"🧾 Mã đơn: {order_code}\n"
                    f"🕒 Thời gian: {now_datetime_full()}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📝 Nội dung:\n{text}",
                    reply_markup=admin_warranty_chat_kb(uid, order_code)
                )
                WARRANTY_STATE.pop(uid, None)
                await message.answer("✅ Đã gửi yêu cầu bảo hành tới admin. Vui lòng chờ phản hồi.")
                return

            # ---- USER email-change request ----
            st_buy = BUY_STATE.get(uid)
            if st_buy and st_buy.get("action") == "await_change_email":
                oc = str(st_buy.get("order_code", ""))
                order = await get_auto_order(oc)
                if not order or int(order.get("user_id", 0)) != uid:
                    BUY_STATE.pop(uid, None)
                    await message.answer("❌ Không tìm thấy đơn.")
                    return
                ok, err = await can_request_email_change(order)
                if not ok:
                    BUY_STATE.pop(uid, None)
                    await message.answer(f"❌ {err}")
                    return
                if not is_valid_email(text):
                    await message.answer("❌ Email mới không hợp lệ. Vui lòng nhập lại.")
                    return
                req_id = gen_change_request_id(oc)
                req = {"id": req_id, "order_code": oc, "user_id": uid, "old_email": order.get("customer_email", ""), "new_email": text, "status": "PENDING", "reason": "", "admin_id": None, "created_at": now_datetime_full(), "processed_at": ""}
                await upsert_email_change_request(req)
                BUY_STATE.pop(uid, None)
                username = f"@{message.from_user.username}" if message.from_user.username else "Không có"
                await notify_admin(
                    bot,
                    "🔄 YÊU CẦU ĐỔI EMAIL\n\n"
                    f"🆔 Order: #{oc}\n\n👤 Khách:\n{message.from_user.full_name}\n\n🆔 User ID:\n{uid}\n\n🔗 Username:\n{username}\n\n"
                    f"📦 Sản phẩm:\n{order.get('product_name')}\n\n📧 Email cũ:\n{order.get('customer_email')}\n\n📧 Email mới:\n{text}\n\n"
                    f"⏱ Thời gian mua:\n{order.get('created_at')}\n\n🔢 Lần đổi:\n{int(order.get('change_email_count', 0)) + 1}/2",
                    reply_markup=email_change_admin_kb(req_id)
                )
                await message.answer("✅ Yêu cầu đổi email đã được gửi Admin.", reply_markup=auto_order_user_kb(order))
                return

            # ---- USER API email input ----
            st_buy = BUY_STATE.get(uid)
            if st_buy and st_buy.get("action") == "await_api_email":
                if not is_valid_email(text):
                    await message.answer("❌ Email không hợp lệ. Vui lòng gửi đúng định dạng Gmail/email.")
                    return
                pid = str(st_buy.get("product_id", ""))
                products = await load_products()
                product = next((p for p in products if p.get("id") == pid), None)
                if not product or str(product.get("type")) != "api":
                    BUY_STATE.pop(uid, None)
                    await message.answer("❌ Sản phẩm API không tồn tại.", reply_markup=back_to_menu_kb())
                    return
                if not bool(product.get("is_active", True)) or int(product.get("stock", 0)) <= 0:
                    BUY_STATE.pop(uid, None)
                    await message.answer("❌ Sản phẩm API đang hết hàng hoặc tạm tắt.", reply_markup=back_to_menu_kb())
                    return
                ok, msg = await reserve_items(uid, pid, 1)
                if not ok:
                    BUY_STATE.pop(uid, None)
                    await message.answer(f"❌ {msg}", reply_markup=back_to_menu_kb())
                    return
                user = await load_user(uid)
                BUY_STATE[uid] = {"action": "api_ready", "product_id": pid, "qty": 1, "customer_email": text}
                await message.answer(
                    "✅ XÁC NHẬN ĐƠN AUTO API\n\n"
                    f"📦 Sản phẩm: {product['name']}\n"
                    f"📧 Email: {text}\n"
                    f"💰 Tổng tiền: {vnd(int(product['price_vnd']))}đ\n"
                    f"💼 Số dư ví: {vnd(int(user.get('balance', 0)))}đ",
                    reply_markup=pay_method_kb(pid, 1, int(user.get("balance", 0)))
                )
                return

            # ---- USER manual order data/chat ----
            st_buy = BUY_STATE.get(uid)
            if st_buy and st_buy.get("action") in {"manual_data", "manual_chat"}:
                oc = str(st_buy.get("order_code", ""))
                order = await get_manual_order(oc)
                is_auto_chat = order is None
                if not order:
                    order = await get_auto_order(oc)
                if not order or int(order.get("user_id", 0)) != uid:
                    BUY_STATE.pop(uid, None)
                    await message.answer("❌ Không tìm thấy đơn.")
                    return
                if st_buy.get("action") == "manual_data":
                    if order.get("status") not in {"WAITING_CUSTOMER_DATA", "CUSTOMER_SUBMITTED"}:
                        BUY_STATE.pop(uid, None)
                        await message.answer("❌ Đơn không còn ở trạng thái chờ khách gửi dữ liệu.")
                        return
                    ok, data, err = parse_customer_manual_data(text, order.get("manual_fields", []))
                    if not ok:
                        await message.answer(f"❌ {err}. Vui lòng gửi lại.\n\n" + manual_fields_text(order.get("manual_fields", [])))
                        return
                    async with _order_lock(oc):
                        order = await get_manual_order(oc) or order
                        before_s = str(order.get("status"))
                        order["customer_data"] = data
                        order["status"] = "CUSTOMER_SUBMITTED"
                        order["submitted_time"] = now_datetime_full()
                        add_order_history(order, f"user:{uid}", "Khách gửi dữ liệu", before_s, "CUSTOMER_SUBMITTED", text)
                        await upsert_manual_order(order)
                    await notify_admin(bot, manual_admin_text(order), reply_markup=manual_admin_kb(oc, uid))
                    BUY_STATE.pop(uid, None)
                    await message.answer("✅ Đã gửi thông tin tới Admin. Vui lòng chờ xử lý.", reply_markup=manual_chat_kb(oc))
                    return
                order.setdefault("chat_messages", []).append({"time": now_datetime_full(), "sender": f"user:{uid}", "text": text})
                add_order_history(order, f"user:{uid}", "Khách nhắn về đơn", str(order.get("status")), str(order.get("status")), text)
                if is_auto_chat:
                    await upsert_auto_order(order)
                else:
                    await upsert_manual_order(order)
                await notify_admin(bot, f"💬 KHÁCH NHẮN VỀ ĐƠN #{oc}\n\n👤 {message.from_user.full_name}\n🆔 User ID: {uid}\n🛒 {order.get('product_name')}\n\n💬 Nội dung:\n```\n{text}\n```", reply_markup=manual_admin_kb(oc, uid))
                BUY_STATE.pop(uid, None)
                await message.answer("✅ Đã gửi tin nhắn về đơn hàng tới Admin.", reply_markup=manual_chat_kb(oc))
                return

            # ---- USER custom qty ----
            st_buy = BUY_STATE.get(uid)
            if st_buy and st_buy.get("await_qty"):
                if not text.isdigit():
                    await message.answer("❌ Vui lòng gửi số nguyên hợp lệ (ví dụ: 3).")
                    return
                qty = int(text)
                if qty <= 0:
                    await message.answer("❌ Số lượng phải > 0.")
                    return
                pid = st_buy["product_id"]
                products = await load_products()
                product = next((p for p in products if p.get("id") == pid), None)
                if not product:
                    BUY_STATE.pop(uid, None)
                    await message.answer("❌ Sản phẩm không tồn tại.", reply_markup=back_to_menu_kb())
                    return
                stock = int(product.get("stock", 0))
                if stock <= 0:
                    BUY_STATE.pop(uid, None)
                    await message.answer("❌ Xin lỗi, sản phẩm đã hết hàng.", reply_markup=back_to_menu_kb())
                    return
                if qty > stock:
                    await message.answer(f"❌ Số lượng vượt tồn kho. Tồn kho hiện tại: {stock}")
                    return

                BUY_STATE[uid] = {"product_id": pid, "qty": qty, "await_qty": False}
                total = int(product["price_vnd"]) * qty
                await message.answer(
                    "✅ XÁC NHẬN MUA HÀNG\n\n"
                    f"📩 Sản phẩm: {product['name']}\n"
                    f"🔢 Số lượng: {qty}\n"
                    f"💰 Tổng tiền: {vnd(total)} đ\n\n"
                    "Bấm ✅ Xác nhận để giữ hàng và chọn cách thanh toán.",
                    reply_markup=buy_confirm_kb(pid, qty)
                )
                return

            # ---- ADMIN reply/close warranty ----
            if is_admin(uid):
                rep = ADMIN_REPLY_STATE.get(uid)
                if rep and str(rep.get("action", "")) in {"email_reject", "email_msg"}:
                    prompt_id = rep.get("prompt_msg_id")
                    if not message.reply_to_message or not prompt_id or message.reply_to_message.message_id != prompt_id:
                        await message.answer("⚠️ Bạn phải reply đúng tin nhắn bot để xử lý yêu cầu email.")
                        return
                    req = await get_email_change_request(str(rep.get("request_id")))
                    if not req:
                        ADMIN_REPLY_STATE.pop(uid, None); await message.answer("❌ Request không tồn tại."); return
                    target_uid = int(req.get("user_id", 0)); oc = str(req.get("order_code"))
                    if rep.get("action") == "email_reject":
                        if req.get("status") != "PENDING":
                            ADMIN_REPLY_STATE.pop(uid, None); await message.answer("❌ Request đã xử lý."); return
                        req["status"] = "REJECTED"; req["reason"] = text; req["processed_at"] = now_datetime_full(); req["admin_id"] = uid
                        await upsert_email_change_request(req)
                        try:
                            await bot.send_message(target_uid, f"❌ YÊU CẦU ĐỔI EMAIL BỊ TỪ CHỐI\n\n🆔 Đơn hàng: #{oc}\n\n📝 Lý do:\n{text}\n\nBạn có thể liên hệ Admin để được hỗ trợ.")
                        except Exception:
                            pass
                        await message.answer("✅ Đã từ chối yêu cầu đổi email.")
                    else:
                        try:
                            await bot.send_message(target_uid, f"💬 ADMIN NHẮN VỀ YÊU CẦU ĐỔI EMAIL #{oc}\n\n```\n{text}\n```")
                        except Exception:
                            pass
                        await message.answer("✅ Đã nhắn khách.")
                    ADMIN_REPLY_STATE.pop(uid, None)
                    return

                if rep and str(rep.get("action", "")).startswith("mo_"):
                    prompt_id = rep.get("prompt_msg_id")
                    if not message.reply_to_message or not prompt_id or message.reply_to_message.message_id != prompt_id:
                        await message.answer("⚠️ Bạn phải reply đúng tin nhắn bot để xử lý đơn tay.")
                        return
                    target_uid = int(rep.get("target_uid"))
                    oc = str(rep.get("order_code"))
                    is_auto = bool(rep.get("is_auto"))
                    async with _order_lock(oc):
                        if is_auto:
                            order = await get_auto_order(oc)
                        else:
                            order = await get_manual_order(oc)
                            if not order:
                                order = await get_auto_order(oc)
                                is_auto = order is not None
                        if not order:
                            ADMIN_REPLY_STATE.pop(uid, None); await message.answer("❌ Không tìm thấy đơn."); return
                        if is_auto and rep.get("action") != "mo_reply":
                            ADMIN_REPLY_STATE.pop(uid, None); await message.answer("❌ Đơn API chỉ hỗ trợ trả lời khách."); return
                        before_s = str(order.get("status"))
                        if rep.get("action") == "mo_done":
                            if before_s in {"CANCELLED", "REFUNDED", "COMPLETED"}:
                                ADMIN_REPLY_STATE.pop(uid, None); await message.answer("❌ Trạng thái không hợp lệ."); return
                            order["status"] = "COMPLETED"; order["admin_result"] = text; order["completed_by"] = uid; order["completed_time"] = now_datetime_full()
                            add_order_history(order, f"admin:{uid}", "Admin hoàn thành", before_s, "COMPLETED", text)
                            await upsert_manual_order(order)
                            await update_user_order_status(target_uid, oc, "✅ Đã hoàn thành")
                            await bot.send_message(target_uid, f"✅ Đơn hàng #{oc} đã hoàn thành.\n\n📦 Sản phẩm: {order.get('product_name')}\n\n📥 Thông tin đơn hàng:\n```\n{text}\n```\n\nCảm ơn bạn đã sử dụng dịch vụ.", reply_markup=manual_chat_kb(oc))
                            await message.answer("✅ Đã hoàn thành và gửi kết quả cho khách.")
                        elif rep.get("action") == "mo_cancel":
                            if before_s in {"COMPLETED", "CANCELLED", "REFUNDED"}:
                                ADMIN_REPLY_STATE.pop(uid, None); await message.answer("❌ Trạng thái không hợp lệ."); return
                            user = await load_user(target_uid)
                            refunded = False
                            if not order.get("refunded"):
                                bal = int(user.get("balance", 0)); total = int(order.get("total", 0)); user["balance"] = bal + total; user.setdefault("wallet_logs", []).insert(0, {"time": now_datetime_full(), "type": "credit", "source": "manual_order_refund", "amount": total, "before": bal, "after": bal+total, "note": f"Hoàn đơn {oc}: {text}"}); await save_user(target_uid, user); order["refunded"] = True; refunded = True
                            if not order.get("stock_refunded"):
                                products = await load_products(); prod = next((p for p in products if p.get("id") == order.get("product_id")), None)
                                if prod: prod["stock"] = int(prod.get("stock", 0)) + int(order.get("quantity", 1)); await save_products(products)
                                order["stock_refunded"] = True
                            order["status"] = "REFUNDED"; order["cancel_reason"] = text; order["cancelled_by"] = uid; order["cancelled_time"] = now_datetime_full()
                            add_order_history(order, f"admin:{uid}", "Admin hủy và hoàn tiền", before_s, "REFUNDED", text)
                            await upsert_manual_order(order)
                            await update_user_order_status(target_uid, oc, "❌ Đã hủy/hoàn tiền")
                            await bot.send_message(target_uid, f"❌ Đơn hàng #{oc} đã bị hủy.\n\n📦 Sản phẩm: {order.get('product_name')}\n💰 Số tiền hoàn: {vnd(int(order.get('total',0)))}đ\n📝 Lý do: {text}\n\n💰 Số dư đã được hoàn lại vào tài khoản của bạn.", reply_markup=manual_chat_kb(oc))
                            await message.answer("✅ Đã hủy đơn, hoàn tiền và hoàn stock." if refunded else "✅ Đơn đã được đánh dấu hủy/hoàn trước đó.")
                        else:
                            order.setdefault("chat_messages", []).append({"time": now_datetime_full(), "sender": f"admin:{uid}", "text": text})
                            add_order_history(order, f"admin:{uid}", "Admin nhắn khách", before_s, before_s, text)
                            if is_auto:
                                await upsert_auto_order(order)
                            else:
                                await upsert_manual_order(order)
                            await bot.send_message(target_uid, f"💬 ADMIN NHẮN VỀ ĐƠN #{oc}\n\n```\n{text}\n```", reply_markup=manual_chat_kb(oc))
                            await message.answer("✅ Đã gửi tin nhắn cho khách.")
                    ADMIN_REPLY_STATE.pop(uid, None)
                    return


                if rep and rep.get("action") == "send_account_info":
                    prompt_id = rep.get("prompt_msg_id")
                    if not message.reply_to_message or not prompt_id or message.reply_to_message.message_id != prompt_id:
                        await message.answer("⚠️ Bạn phải reply đúng tin nhắn bot để gửi thông tin tài khoản.")
                        return
                    target_uid = int(rep.get("target_uid"))
                    oc = str(rep.get("order_code"))
                    order = await get_auto_order(oc)
                    if not order:
                        ADMIN_REPLY_STATE.pop(uid, None); await message.answer("❌ Không tìm thấy đơn."); return
                    try:
                        await bot.send_message(target_uid, f"📧 THÔNG TIN TÀI KHOẢN CHO ĐƠN #{oc}\n\n📦 {order.get('product_name')}\n\n```\n{text}\n```\n\n👉 Vui lòng kiểm tra và sử dụng. Nếu có vấn đề hãy liên hệ Admin.")
                    except Exception:
                        ADMIN_REPLY_STATE.pop(uid, None); await message.answer("❌ Gửi thất bại cho khách."); return
                    add_order_history(order, f"admin:{uid}", "Admin gửi thông tin tài khoản", str(order.get("status")), str(order.get("status")), text[:300])
                    await upsert_auto_order(order)
                    ADMIN_REPLY_STATE.pop(uid, None)
                    await message.answer("✅ Đã gửi thông tin tài khoản cho khách.")
                    return

                if rep:
                    prompt_id = rep.get("prompt_msg_id")
                    if not message.reply_to_message or not prompt_id or message.reply_to_message.message_id != prompt_id:
                        await message.answer("⚠️ Bạn phải reply đúng tin nhắn bot để tiếp tục thao tác bảo hành.")
                        return

                    target_uid = int(rep.get("target_uid"))
                    order_code = str(rep.get("order_code"))
                    data = await load_warranty(order_code)
                    if not data:
                        ADMIN_REPLY_STATE.pop(uid, None)
                        await message.answer("❌ Không tìm thấy mã bảo hành.")
                        return

                    # trả lời khách
                    if rep.get("action") == "reply_warranty_user":
                        if bool(data.get("closed", False)):
                            ADMIN_REPLY_STATE.pop(uid, None)
                            await message.answer("❌ Mã bảo hành đã đóng.")
                            return

                        try:
                            sent = await bot.send_message(
                                target_uid,
                                "💬 PHẢN HỒI TỪ ADMIN VỀ BẢO HÀNH\n\n"
                                f"🧾 Mã đơn: {order_code}\n"
                                f"🆔 ID của bạn: {target_uid}\n"
                                f"📝 Nội dung: {text}\n\n"
                                "👉 Bạn có thể reply trực tiếp tin nhắn này để nhắn tiếp cho admin."
                            )
                            USER_WARRANTY_REPLY_MAP[f"{target_uid}:{sent.message_id}"] = order_code
                            await append_warranty_chat(order_code, {
                                "time": now_datetime_full(),
                                "sender": f"admin:{uid}",
                                "text": text
                            })
                            await message.answer("✅ Đã gửi tin nhắn cho khách.")
                        except Exception as e:
                            await message.answer(f"❌ Gửi thất bại: {e}")
                        ADMIN_REPLY_STATE.pop(uid, None)
                        return

                    # đóng bảo hành
                    if rep.get("action") == "close_warranty":
                        reason = "" if text.lower() == "khong" else text
                        data["closed"] = True
                        data["close_reason"] = reason
                        data["closed_time"] = now_datetime_full()
                        data["closed_by"] = uid
                        await save_warranty(data)

                        try:
                            await bot.send_message(
                                target_uid,
                                "🔒 MÃ BẢO HÀNH ĐÃ ĐƯỢC ĐÓNG\n\n"
                                f"🧾 Mã đơn: {order_code}\n"
                                f"🕒 Thời gian: {now_datetime_full()}\n"
                                f"📝 Lý do: {reason or 'Không có'}"
                            )
                        except Exception:
                            pass

                        await message.answer(
                            "✅ Đã đóng mã bảo hành.\n\n"
                            f"🧾 Mã đơn: {order_code}\n"
                            f"📝 Lý do: {reason or 'Không có'}"
                        )
                        ADMIN_REPLY_STATE.pop(uid, None)
                        return

            # ---- ADMIN flow ----
            if not is_admin(uid):
                return

            st_adm = ADMIN_STATE.get(uid)
            if not st_adm:
                return

            prompt_id = st_adm.get("prompt_msg_id")
            if not message.reply_to_message or not prompt_id or message.reply_to_message.message_id != prompt_id:
                if st_adm.get("action") in ("add_product", "add_manual_product", "manual_stock_add", "manual_stock_reduce", "cat_add", "cat_edit", "cat_sort", "api_set_key", "api_set_base", "add_data", "edit_detail", "edit_price", "add_api_price", "add_api_name", "add_api_stock", "api_stock_add", "api_stock_reduce", "sale_product"):
                    await message.answer("⚠️ Bạn phải reply đúng tin nhắn bot để bot nhận dữ liệu.")
                return

            if st_adm.get("action") == "api_set_key":
                provider = str(st_adm.get("api_provider", "fishy"))
                provider_key = API_PROVIDER_SACHTAI if provider == "sachtai" else API_PROVIDER_FISHYMARKET
                expected_prefix = "kk_" if provider == "sachtai" else "fishy_"
                provider_name = "Sachtai" if provider == "sachtai" else "FishyMarket"
                if not text.startswith(expected_prefix):
                    await message.answer(
                        f"⚠️ API Key {provider_name} phải bắt đầu bằng `{expected_prefix}`. "
                        "Hãy reply lại đúng API Key của nhà cung cấp này."
                    )
                    return
                data = await load_api_settings()
                providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
                current = providers.get(provider_key) if isinstance(providers.get(provider_key), dict) else {}
                base_url, _ = await get_provider_config(provider_key)
                providers[provider_key] = {"base_url": str(current.get("base_url") or base_url), "api_key": text.strip()}
                data["providers"] = providers
                # Keep the former top-level value updated for FishyMarket so
                # installations created before provider-specific settings work.
                if provider_key == API_PROVIDER_FISHYMARKET:
                    data["api_key"] = text.strip()
                await save_api_settings(data)
                ADMIN_STATE.pop(uid, None)
                await message.answer(f"✅ Đã cập nhật API Key {provider_name}.")
                return

            if st_adm.get("action") == "api_set_base":
                provider = str(st_adm.get("api_provider", "fishy"))
                provider_key = API_PROVIDER_SACHTAI if provider == "sachtai" else API_PROVIDER_FISHYMARKET
                provider_name = "Sachtai" if provider == "sachtai" else "FishyMarket"
                url = text.strip()
                if not url.startswith(("http://", "https://")):
                    await message.answer(
                        f"⚠️ Base URL {provider_name} phải bắt đầu bằng `https://` hoặc `http://`."
                        f" Ví dụ: `https://bot.sachtai.com`. Reply lại link hợp lệ."
                    )
                    return
                if len(url) > 200:
                    await message.answer(f"⚠️ Base URL quá dài ({len(url)} ký tự). Tối đa 200 ký tự.")
                    return
                url = url.rstrip("/")
                data = await load_api_settings()
                providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
                current = providers.get(provider_key) if isinstance(providers.get(provider_key), dict) else {}
                old_url = str(current.get("base_url") or "").rstrip("/")
                providers[provider_key] = {**current, "base_url": url, "api_key": str(current.get("api_key") or "")}
                data["providers"] = providers
                if provider_key == API_PROVIDER_FISHYMARKET:
                    data["base_url"] = url
                await save_api_settings(data)
                ADMIN_STATE.pop(uid, None)
                await message.answer(
                    f"✅ Đã cập nhật Base URL {provider_name}.\n\n"
                    f"🔗 Cũ: `{old_url}`\n"
                    f"🔗 Mới: `{url}`\n\n"
                    "Dùng nút 'Test API' để kiểm tra.",
                    reply_markup=api_config_kb(),
                )
                return

            if st_adm.get("action") == "cat_add":
                parts = [x.strip() for x in text.split("|")]
                if len(parts) < 2:
                    await message.answer("❌ Sai cú pháp. Ví dụ: 📺 | YouTube | Dịch vụ YouTube")
                    return
                cats = await load_categories()
                now = now_datetime_full()
                cid = gen_category_id(parts[1])
                cats.append({"id": cid, "icon": parts[0] or "📁", "name": parts[1], "description": parts[2] if len(parts) > 2 else "", "sort_order": max([int(c.get("sort_order", 0)) for c in cats] + [0]) + 10, "is_active": True, "created_at": now, "updated_at": now})
                await save_categories(cats)
                ADMIN_STATE.pop(uid, None)
                await message.answer(f"✅ Đã thêm nhóm {parts[0]} {parts[1]}.", reply_markup=categories_admin_kb())
                return

            if st_adm.get("action") == "cat_edit":
                cid = str(st_adm.get("category_id", ""))
                parts = [x.strip() for x in text.split("|")]
                if len(parts) < 2:
                    await message.answer("❌ Sai cú pháp. Ví dụ: 📺 | YouTube | Mô tả")
                    return
                cats = await load_categories()
                cat = next((c for c in cats if c.get("id") == cid), None)
                if not cat:
                    ADMIN_STATE.pop(uid, None); await message.answer("❌ Không tìm thấy nhóm."); return
                cat["icon"] = parts[0] or "📁"; cat["name"] = parts[1]; cat["description"] = parts[2] if len(parts) > 2 else ""; cat["updated_at"] = now_datetime_full()
                await save_categories(cats)
                ADMIN_STATE.pop(uid, None)
                await message.answer("✅ Đã sửa nhóm.", reply_markup=categories_admin_kb())
                return

            if st_adm.get("action") == "cat_sort":
                try:
                    order_no = int(text)
                except Exception:
                    await message.answer("❌ Thứ tự phải là số nguyên.")
                    return
                cats = await load_categories(); cid = str(st_adm.get("category_id", "")); cat = next((c for c in cats if c.get("id") == cid), None)
                if not cat:
                    ADMIN_STATE.pop(uid, None); await message.answer("❌ Không tìm thấy nhóm."); return
                cat["sort_order"] = order_no; cat["updated_at"] = now_datetime_full(); await save_categories(cats)
                ADMIN_STATE.pop(uid, None)
                await message.answer("✅ Đã cập nhật thứ tự nhóm.", reply_markup=categories_admin_kb())
                return

            if st_adm.get("action") == "add_product":
                parts = [x.strip() for x in text.split("|")]
                if len(parts) != 2:
                    await message.answer("❌ Sai cú pháp. Ví dụ: capcut859 | 10000")
                    return
                name = parts[0]
                price_s = parts[1]
                if not name:
                    await message.answer("❌ Tên sản phẩm không được trống.")
                    return
                try:
                    price = int(price_s)
                    if price < 0:
                        raise ValueError()
                except Exception:
                    await message.answer("❌ Giá phải là số nguyên >= 0.")
                    return

                newp = await add_product(name, price)
                if st_adm.get("category_id"):
                    products = await load_products()
                    target = next((p for p in products if p.get("id") == newp.get("id")), None)
                    if target:
                        target["category_id"] = str(st_adm.get("category_id"))
                        await save_products(products)
                        newp = target
                ADMIN_STATE.pop(uid, None)
                await message.answer(
                    "✅ Đã thêm sản phẩm!\n\n"
                    f"• ID: {newp['id']}\n"
                    f"• Tên: {newp['name']}\n"
                    f"• Giá: {vnd(int(newp['price_vnd']))}đ\n"
                    f"• Stock: {newp['stock']} (tự tăng khi thêm hàng)",
                    reply_markup=await products_kb_for_user(uid)
                )
                return

            if st_adm.get("action") == "add_manual_product":
                parts = [x.strip() for x in text.split("|")]
                if len(parts) != 4 or parts[2].lower() != "dontay":
                    await message.answer("❌ Sai cú pháp. Ví dụ: CapCut Pro | 50000 | dontay | email, sdt?, ghi chú?")
                    return
                try:
                    price = int(parts[1])
                    if price < 0:
                        raise ValueError()
                except Exception:
                    await message.answer("❌ Giá phải là số nguyên >= 0.")
                    return
                fields = parse_manual_fields(parts[3])
                if not parts[0] or not fields:
                    await message.answer("❌ Tên sản phẩm và danh sách trường không được trống.")
                    return
                newp = await add_manual_product(parts[0], price, fields)
                if st_adm.get("category_id"):
                    products = await load_products()
                    target = next((p for p in products if p.get("id") == newp.get("id")), None)
                    if target:
                        target["category_id"] = str(st_adm.get("category_id"))
                        await save_products(products)
                        newp = target
                ADMIN_STATE.pop(uid, None)
                await message.answer(
                    "✅ Đã thêm sản phẩm đơn tay!\n\n"
                    f"• ID: {newp['id']}\n• Tên: {newp['name']}\n• Giá: {vnd(int(newp['price_vnd']))}đ\n"
                    f"• Loại: dontay\n• Stock: {newp['stock']}\n\n" + manual_fields_text(fields),
                    reply_markup=await products_kb_for_user(uid)
                )
                return

            if st_adm.get("action") in {"manual_stock_add", "manual_stock_reduce"}:
                pid = str(st_adm.get("product_id", "")).strip()
                try:
                    qty = int(text.replace("+", ""))
                except Exception:
                    await message.answer("❌ Vui lòng nhập số hợp lệ, ví dụ +10 hoặc -5.")
                    return
                if st_adm.get("action") == "manual_stock_reduce":
                    qty = -abs(qty)
                else:
                    qty = abs(qty)
                products = await load_products()
                product = next((p for p in products if p.get("id") == pid), None)
                if not product or str(product.get("type")) != "dontay":
                    ADMIN_STATE.pop(uid, None); await message.answer("❌ Không tìm thấy sản phẩm đơn tay."); return
                cur = int(product.get("stock", 0))
                new_stock = cur + qty
                if new_stock < 0:
                    await message.answer(f"❌ Không thể giảm quá stock hiện tại ({cur}).")
                    return
                product["stock"] = new_stock
                product["out_of_stock_notified"] = ""
                await save_products(products)
                ADMIN_STATE.pop(uid, None)
                added_qty = abs(qty)
                await message.answer(
                    f"✅ Đã cập nhật stock.\n\n📦 {product['name']}\n💰 Giá: {vnd(int(product.get('price_vnd', 0)))}đ\n➕ Thêm: {added_qty}\n📊 Stock cũ: {cur}\n📊 Stock mới: {new_stock}\n\n"
                    "Bạn muốn làm gì tiếp?",
                    reply_markup=admin_after_add_kb(pid, added_qty)
                )
                return

            if st_adm.get("action") == "edit_price":
                pid = str(st_adm.get("product_id", "")).strip()
                if not pid:
                    ADMIN_STATE.pop(uid, None)
                    await message.answer("❌ Không tìm thấy sản phẩm.")
                    return
                try:
                    new_price = int(text)
                    if new_price < 0:
                        raise ValueError()
                except Exception:
                    await message.answer("❌ Giá phải là số nguyên >= 0.")
                    return

                products = await load_products()
                product = next((p for p in products if p.get("id") == pid), None)
                if not product:
                    ADMIN_STATE.pop(uid, None)
                    await message.answer("❌ Sản phẩm không tồn tại.")
                    return
                old_price = int(product.get("price_vnd", 0))
                product["price_vnd"] = new_price
                await save_products(products)
                if str(product.get("type", "local")) == "api":
                    await enforce_api_stock_constraints(bot)
                ADMIN_STATE.pop(uid, None)
                await message.answer(
                    "✅ Đã cập nhật giá sản phẩm.\n\n"
                    f"📦 {product['name']}\n"
                    f"💰 Giá cũ: {vnd(old_price)}đ\n"
                    f"💰 Giá mới: {vnd(new_price)}đ"
                )
                return

            if st_adm.get("action") == "sale_product":
                pid = str(st_adm.get("product_id", "")).strip()
                raw = text.lower().strip()
                try:
                    tokens = raw.replace("%", "").split()
                    percent = 0 if tokens and tokens[0] in {"0", "off", "tat", "tắt"} else int(tokens[0])
                    duration_hours = int(tokens[1]) if len(tokens) >= 2 else 0
                    if percent < 0 or percent > 99 or duration_hours < 0:
                        raise ValueError()
                except Exception:
                    await message.answer("❌ Vui lòng nhập % sale từ 1-99, có thể kèm số giờ. Ví dụ: 10 24. Nhập 0/off để tắt sale.")
                    return
                ok, product, err = await set_product_sale(pid, percent, duration_hours)
                ADMIN_STATE.pop(uid, None)
                if not ok or not product:
                    await message.answer(f"❌ {err}")
                    return
                if percent <= 0:
                    await message.answer(
                        "✅ Đã tắt sale sản phẩm.\n\n"
                        f"📦 {product.get('name')}\n"
                        f"💰 Giá hiện tại: {vnd(int(product.get('price_vnd', 0)))}đ",
                        reply_markup=sale_after_update_kb(pid)
                    )
                else:
                    await message.answer(
                        "🔥 ĐÃ BẬT SALE SẢN PHẨM\n\n"
                        f"📦 {product_display_name(product)}\n"
                        f"🏷 Giảm: {percent}%\n"
                        f"💰 Giá sale: {vnd(int(product.get('price_vnd', 0)))}đ\n"
                        f"💵 Giá gốc: {vnd(int(product.get('original_price_vnd', 0)))}đ\n"
                        f"{sale_time_text(product)}",
                        reply_markup=sale_after_update_kb(pid)
                    )
                return

            if st_adm.get("action") == "add_api_price":
                try:
                    sale_price = int(text)
                    if sale_price < 0:
                        raise ValueError()
                except Exception:
                    await message.answer("❌ Giá bán phải là số nguyên >= 0.")
                    return
                st_adm["sale_price"] = sale_price
                st_adm["action"] = "add_api_name"
                sent = await message.answer("Bước 2/3: Reply tên mới cho sản phẩm hoặc gõ 'skip' để giữ tên gốc API.")
                st_adm["prompt_msg_id"] = sent.message_id
                ADMIN_STATE[uid] = st_adm
                return

            if st_adm.get("action") == "add_api_name":
                item = st_adm.get("api_item", {}) if isinstance(st_adm.get("api_item"), dict) else {}
                new_name = str(item.get("name", "Sản phẩm API")) if text.lower() == "skip" else text
                st_adm["new_name"] = new_name
                st_adm["action"] = "add_api_stock"
                sent = await message.answer("Bước 3/3: Reply số stock muốn cấp ban đầu cho sản phẩm API (>=0).")
                st_adm["prompt_msg_id"] = sent.message_id
                ADMIN_STATE[uid] = st_adm
                return

            if st_adm.get("action") == "add_api_stock":
                try:
                    initial_stock = int(text)
                    if initial_stock < 0:
                        raise ValueError()
                except Exception:
                    await message.answer("❌ Stock ban đầu phải là số nguyên >= 0.")
                    return

                item = st_adm.get("api_item", {}) if isinstance(st_adm.get("api_item"), dict) else {}
                new_name = str(st_adm.get("new_name") or item.get("name") or "Sản phẩm API")
                sale_price = int(st_adm.get("sale_price", 0))
                api_price = max(0, int(item.get("price", 0)))
                provider_cap = usable_provider_stock(item.get("stock", 0))
                selected_provider = str(st_adm.get("api_provider", "legacy"))
                provider_for_check = {
                    "legacy": API_PROVIDER_LEGACY,
                    "fishy": API_PROVIDER_FISHYMARKET,
                    "sachtai": API_PROVIDER_SACHTAI,
                }.get(selected_provider, API_PROVIDER_FISHYMARKET)

                if api_price > 0:
                    ok_snap, snap, err_snap = await get_api_stock_budget_snapshot(provider_for_check)
                    if not ok_snap:
                        await message.answer(f"❌ Không lấy được số dư API để kiểm tra stock ban đầu: {err_snap}")
                        return
                    money_available = max(0, int(snap.get("money_available", 0)))
                    money_cap = money_available // api_price
                    max_initial = max(0, min(provider_cap, money_cap))
                    if initial_stock > max_initial:
                        await message.answer(
                            "❌ Stock ban đầu vượt mức hợp lệ.\n"
                            f"• Tối đa cho phép hiện tại: {max_initial}\n"
                            f"• Theo tồn provider: {provider_cap}\n"
                            f"• Theo số dư API khả dụng: {money_cap}"
                        )
                        return
                elif initial_stock > provider_cap:
                    await message.answer(f"❌ Stock ban đầu vượt tồn hiện có của API ({provider_cap}).")
                    return
                elif api_price <= 0:
                    initial_stock = 0

                try:
                    provider_name = {
                        "legacy": API_PROVIDER_LEGACY,
                        "fishy": API_PROVIDER_FISHYMARKET,
                        "sachtai": API_PROVIDER_SACHTAI,
                    }.get(selected_provider, API_PROVIDER_FISHYMARKET)
                    created = await add_api_product(
                        name=new_name,
                        sale_price_vnd=sale_price,
                        api_product_id=str(item.get("id", "")).strip(),
                        api_price=api_price,
                        api_provider=provider_name,
                        required_fields=item.get("required_fields", []),
                        fulfillment=str(item.get("fulfillment", "instant")),
                        warranty_hours=int(item.get("warranty_hours", 0)),
                        api_stock=provider_cap,
                    )

                    products = await load_products()
                    target = next((x for x in products if x.get("id") == created.get("id")), None)
                    if target:
                        target["stock"] = max(0, initial_stock)
                        await save_products(products)

                    desc = str(item.get("description", "")).strip()
                    if desc:
                        await set_product_detail(created["id"], desc)

                    ok_rb, msg_rb = await enforce_api_stock_constraints(bot)
                    refreshed = await load_products()
                    created_now = next((x for x in refreshed if x.get("id") == created.get("id")), created)

                    ADMIN_STATE.pop(uid, None)
                    extra = "" if ok_rb else f"\n⚠️ Cảnh báo đồng bộ stock: {msg_rb}"
                    await message.answer(
                        "✅ Đã thêm sản phẩm từ API vào bot.\n\n"
                        f"• ID: {created_now['id']}\n"
                        f"• Tên: {created_now['name']}\n"
                        f"• Giá bán: {vnd(int(created_now.get('price_vnd', 0)))}đ\n"
                        f"• Stock hiện tại: {created_now.get('stock', 0)}"
                        f"{extra}",
                        reply_markup=await products_kb_for_user(uid)
                    )
                except Exception as e:
                    await message.answer(f"❌ Không thể tạo sản phẩm API: {e}")
                return
            if st_adm.get("action") == "api_stock_add":
                pid = str(st_adm.get("product_id", "")).strip()
                try:
                    qty = int(text)
                    if qty <= 0:
                        raise ValueError()
                except Exception:
                    await message.answer("❌ Nhập số nguyên dương.")
                    return
                products = await load_products()
                product = next((p for p in products if p.get("id") == pid), None)
                if not product or str(product.get("type", "local")) != "api":
                    ADMIN_STATE.pop(uid, None)
                    await message.answer("❌ Sản phẩm tự động không tồn tại.")
                    return
                cap = await api_stock_add_capacity(product)
                if qty > int(cap["max_add"]):
                    await message.answer(
                        "❌ Số lượng thêm vượt mức hợp lệ hiện tại.\n"
                        f"• Tối đa có thể thêm: {cap['max_add']}\n"
                        f"• Giới hạn theo nguồn: {cap['provider_room']}\n"
                        f"• Giới hạn theo số dư: {cap['money_room']}\n"
                        f"• {cap['reason']}"
                    )
                    return
                product["stock"] = max(0, int(product.get("stock", 0))) + qty
                await save_products(products)
                await enforce_api_stock_constraints(bot)
                ADMIN_STATE.pop(uid, None)
                await message.answer(
                    f"✅ Đã thêm {qty} stock cho {product['name']}.\n"
                    f"💰 Giá: {vnd(int(product.get('price_vnd', 0)))}đ\n"
                    f"📊 Tồn bot: {product['stock']} | Nguồn: {cap['provider_raw']} (dùng {cap['provider_cap']})\n"
                    f"💰 Số dư khả dụng: {vnd(cap['money_available'])}đ\n\n"
                    "Bạn muốn làm gì tiếp?",
                    reply_markup=admin_after_add_kb(pid, qty)
                )
                return

            if st_adm.get("action") == "api_stock_reduce":
                pid = str(st_adm.get("product_id", "")).strip()
                try:
                    qty = int(text)
                    if qty <= 0:
                        raise ValueError()
                except Exception:
                    await message.answer("❌ Nhập số nguyên dương.")
                    return
                products = await load_products()
                product = next((p for p in products if p.get("id") == pid), None)
                if not product or str(product.get("type", "local")) != "api":
                    ADMIN_STATE.pop(uid, None)
                    await message.answer("❌ Sản phẩm tự động không tồn tại.")
                    return
                cur = int(product.get("stock", 0))
                if qty > cur:
                    await message.answer(f"❌ Không thể giảm quá tồn hiện tại ({cur}).")
                    return
                product["stock"] = cur - qty
                await save_products(products)
                await enforce_api_stock_constraints(bot)
                ADMIN_STATE.pop(uid, None)
                await message.answer(
                    f"✅ Đã giảm {qty} stock cho {product['name']}.\n"
                    f"📊 Tồn bot: {product['stock']}"
                )
                return

            if st_adm.get("action") == "edit_detail":
                pid = str(st_adm.get("product_id", "")).strip()
                if not pid:
                    ADMIN_STATE.pop(uid, None)
                    await message.answer("❌ Không tìm thấy sản phẩm.")
                    return

                if text.lower() == "xoa_chi_tiet":
                    await remove_product_detail(pid)
                    ADMIN_STATE.pop(uid, None)
                    await message.answer("✅ Đã xóa thông tin chi tiết sản phẩm.")
                    return

                await set_product_detail(pid, text)
                ADMIN_STATE.pop(uid, None)
                await message.answer("✅ Đã cập nhật thông tin chi tiết sản phẩm.")
                return

            if st_adm.get("action") == "add_data":
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                if not lines:
                    await message.answer("⚠️ Không có dòng nào hợp lệ.")
                    return
                st_adm.setdefault("buffer", [])
                st_adm["buffer"].extend(lines)
                ADMIN_STATE[uid] = st_adm
                await message.answer(
                    f"➕ Đã nhận {len(lines)} dòng.\n"
                    f"📦 Tổng tạm thời: {len(st_adm['buffer'])} dòng.\n\n"
                    "Bấm ✅ XÁC NHẬN LƯU để lưu vào kho."
                )
                return

        # ================== ADMIN FILE (.txt) ==================
        @dp.message(F.document)
        async def admin_file_handler(message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not is_admin(uid):
                return

            st = ADMIN_STATE.get(uid)
            if not st or st.get("action") != "add_data":
                return

            prompt_id = st.get("prompt_msg_id")
            if not message.reply_to_message or not prompt_id or message.reply_to_message.message_id != prompt_id:
                await message.answer("⚠️ Bạn phải reply đúng tin nhắn bot để gửi file thêm hàng.")
                return

            doc = message.document
            if not doc or not (doc.file_name or "").lower().endswith(".txt"):
                await message.answer("❌ Chỉ chấp nhận file .txt")
                return

            file = await bot.get_file(doc.file_id)
            downloaded = await bot.download_file(file.file_path)
            content = downloaded.read().decode("utf-8", errors="ignore")

            lines = [l.strip() for l in content.splitlines() if l.strip()]
            if not lines:
                await message.answer("⚠️ File không có dòng hợp lệ.")
                return

            st.setdefault("buffer", [])
            st["buffer"].extend(lines)
            ADMIN_STATE[uid] = st

            await message.answer(
                f"📄 Đã đọc file {doc.file_name}\n"
                f"➕ Thêm {len(lines)} dòng\n"
                f"📦 Tổng tạm thời: {len(st['buffer'])} dòng\n\n"
                "Bấm ✅ XÁC NHẬN LƯU để lưu vào kho."
            )

        # ================== ADMIN COMMANDS ==================
        @dp.message(Command("us"))
        async def cmd_us(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            if not is_admin(message.from_user.id):
                return

            parts = (message.text or "").split()
            if len(parts) != 2:
                await message.answer("Dùng: /us <id>")
                return
            try:
                uid = int(parts[1])
            except ValueError:
                await message.answer("❌ ID không hợp lệ.")
                return

            user = await load_user(uid)
            stats = user.get("stats", {})
            username = f"@{user.get('username')}" if user.get("username") else "N/A"
            fullname = user.get("full_name") or "N/A"

            await message.answer(
                "👤 THÔNG TIN TÀI KHOẢN\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"👤 User: {fullname}\n"
                f"🔗 Username: {username}\n"
                f"🆔 ID: {uid}\n"
                f"💼 Số dư : {vnd(int(user.get('balance', 0)))} đ\n"
                f"📦 Tổng đơn hàng: {int(stats.get('total_orders', 0))}\n"
                f"✅ Đơn đã hoàn thành: {int(stats.get('done_orders', 0))}\n"
                f"💰 Tổng chi tiêu: {vnd(int(stats.get('total_spent', 0)))} đ\n"
                f"📅 Ngày Tạo: {user.get('created_at', 'N/A')}\n"
                f"🕒 Lần cuối: {user.get('last_seen', 'N/A')}\n"
                "━━━━━━━━━━━━━━━━━━"
            )

        @dp.message(Command("edit"))
        async def cmd_edit(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            if not is_admin(message.from_user.id):
                return

            parts = (message.text or "").split()
            if len(parts) < 4:
                await message.answer("Dùng: /edit cong|tru <id> <so_tien> <ly_do?>")
                return

            mode = parts[1].lower().strip()
            try:
                uid = int(parts[2])
                amount = int(parts[3])
            except ValueError:
                await message.answer("❌ ID và số tiền phải là số.")
                return

            reason = " ".join(parts[4:]).strip() if len(parts) > 4 else ""

            try:
                result = await admin_edit_balance(uid, mode, amount, admin_id=message.from_user.id, reason=reason)
                username = f"@{result.get('user_username')}" if result.get("user_username") else "N/A"
                fullname = result.get("user_full_name") or "N/A"
                delta = result["delta"]
                delta_str = (f"+{vnd(delta)}" if delta > 0 else f"-{vnd(abs(delta))}")

                try:
                    action_text = "được cộng" if delta > 0 else "bị trừ"
                    await bot.send_message(
                        uid,
                        "💼 SỐ DƯ VÍ ĐÃ ĐƯỢC CẬP NHẬT\n\n"
                        f"Bạn vừa {action_text}: {vnd(abs(delta))} đ\n"
                        f"💼 Số dư trước: {vnd(result['before'])} đ\n"
                        f"💼 Số dư sau: {vnd(result['after'])} đ\n"
                        f"📝 Lý do: {reason or 'Không'}"
                    )
                except Exception:
                    pass

                await message.answer(
                    "✅ CẬP NHẬT SỐ DƯ THÀNH CÔNG\n\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"👤 User: {fullname}\n"
                    f"🔗 Username: {username}\n"
                    f"🆔 ID: {uid}\n"
                    f"🕒 Thời gian: {result['log']['time']}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"💼 Số dư trước: {vnd(result['before'])} đ\n"
                    f"➕/➖ Thay đổi: {delta_str} đ\n"
                    f"💼 Số dư sau: {vnd(result['after'])} đ\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📝 Lý do: {reason or 'Không'}\n"
                    f"👑 Admin: {message.from_user.id}\n"
                    "━━━━━━━━━━━━━━━━━━"
                )
            except Exception as e:
                await message.answer(f"❌ Không thể cập nhật: {e}")

        @dp.message(Command("editsp"))
        async def cmd_editsp(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            if not is_admin(message.from_user.id):
                return
            await message.answer("🛠 Quản lý sản phẩm ngay trong /menu (bấm vào sản phẩm để thêm hàng hoặc sửa chi tiết).")

        @dp.message(Command("sale"))
        async def cmd_sale(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            if not is_admin(message.from_user.id):
                return
            parts = (message.text or "").split()
            if len(parts) not in {3, 4}:
                await message.answer("Dùng: /sale <product_id> <percent> [số_giờ] hoặc /sale off <product_id>")
                return
            if parts[1].lower() == "off":
                pid, percent, duration_hours = parts[2], 0, 0
            else:
                pid = parts[1]
                try:
                    percent = int(parts[2].replace("%", ""))
                    duration_hours = int(parts[3]) if len(parts) == 4 else 0
                except Exception:
                    await message.answer("❌ Percent phải là số 1-99, có thể kèm số giờ. Ví dụ: /sale sp_123 10 24")
                    return
            ok, product, err = await set_product_sale(pid, percent, duration_hours)
            if not ok or not product:
                await message.answer(f"❌ {err}")
                return
            await message.answer(
                "✅ Đã cập nhật sale.\n\n"
                f"📦 {product_display_name(product)}\n"
                f"🏷 Sale: {int(product.get('sale_percent', 0) or 0)}%\n"
                f"💰 Giá hiện tại: {vnd(int(product.get('price_vnd', 0)))}đ\n"
                f"{sale_time_text(product)}",
                reply_markup=sale_after_update_kb(str(product.get("id")))
            )

        @dp.message(Command("tin"))
        async def cmd_tin(message: Message):
            if not message.from_user:
                return
            await add_user_global(message.from_user.id)
            if not is_admin(message.from_user.id):
                return

            parts = (message.text or "").split(maxsplit=2)
            if len(parts) < 2:
                await message.answer(
                    "📣 GỬI THÔNG BÁO\n\n"
                    "Cách dùng mới: admin phải reply vào nội dung cần gửi.\n\n"
                    "1️⃣ Reply một tin nhắn text / ảnh / ảnh có caption / video / sticker.\n"
                    "2️⃣ Gõ: /tin all hoặc /tin <user_id>.\n"
                    "3️⃣ Nếu muốn đổi caption/text, gõ thêm sau lệnh: /tin all nội dung mới."
                )
                return

            target = parts[1].strip().lower()
            override_text = parts[2].strip() if len(parts) >= 3 else ""
            reply = message.reply_to_message

            if not reply:
                await message.answer(
                    "❌ Vui lòng reply vào nội dung cần gửi.\n\n"
                    "Hỗ trợ: tin nhắn text, ảnh, ảnh kèm caption, video hoặc sticker.\n"
                    "Ví dụ: reply ảnh rồi gõ /tin all"
                )
                return

            if target == "all":
                targets = await load_all_users()
            else:
                try:
                    targets = [int(target)]
                except ValueError:
                    await message.answer("❌ Tham số phải là all hoặc id (số). Ví dụ: /tin all hoặc /tin 123456789")
                    return

            if not targets:
                await message.answer("⚠️ Chưa có người dùng nào trong danh sách.")
                return

            reply_text = override_text or reply.text or reply.caption or ""
            has_supported_payload = bool(reply.photo or reply.video or reply.sticker or reply.text or reply.caption)
            if not has_supported_payload:
                await message.answer("❌ Loại tin nhắn này chưa hỗ trợ. Hãy reply text, ảnh, ảnh kèm caption, video hoặc sticker.")
                return

            ok = 0
            fail = 0
            for target_uid in targets:
                try:
                    if reply.photo:
                        await bot.send_photo(target_uid, reply.photo[-1].file_id, caption=reply_text or None)
                    elif reply.video:
                        await bot.send_video(target_uid, reply.video.file_id, caption=reply_text or None)
                    elif reply.sticker:
                        await bot.send_sticker(target_uid, reply.sticker.file_id)
                        if reply_text:
                            await bot.send_message(target_uid, reply_text)
                    else:
                        await bot.send_message(target_uid, reply_text)
                    ok += 1
                except Exception:
                    fail += 1
                await asyncio.sleep(0.05)

            if target == "all":
                await message.answer(
                    "✅ ĐÃ GỬI THÔNG BÁO\n\n"
                    f"📨 Thành công: {ok}\n"
                    f"❌ Thất bại: {fail}\n"
                    f"👑 Admin: {message.from_user.id}"
                )
            else:
                await message.answer(
                    "✅ ĐÃ GỬI TIN NHẮN\n\n"
                    f"🆔 ID: {targets[0]}\n"
                    f"📨 Thành công: {ok}\n"
                    f"❌ Thất bại: {fail}"
                )

        # Clear webhook trước khi polling để tránh xung đột mode webhook/polling.
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass

        await dp.start_polling(bot)

    except TelegramConflictError:
        # Có instance khác đang polling token này.
        print("[FATAL] TelegramConflictError: Bot token đang được polling ở nơi khác. Hãy chỉ chạy 1 instance.")

    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
        release_bot_instance_lock()


if __name__ == "__main__":
    asyncio.run(main())
