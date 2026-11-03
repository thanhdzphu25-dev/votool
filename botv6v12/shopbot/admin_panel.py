"""Telegram Admin Panel UI helpers.

This stays Telegram-only per the requested scope: no Web Admin implementation.
"""

from __future__ import annotations

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup


def admin_panel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Sản phẩm & kho", callback_data="menu")
    kb.button(text="📁 Quản lý nhóm sản phẩm", callback_data="adm:categories")
    kb.button(text="🧾 Đơn hàng", callback_data="adm:orders")
    kb.button(text="👥 Khách hàng", callback_data="adm:members")
    kb.button(text="💼 Cộng/trừ số dư", callback_data="adm:wallet_help")
    kb.button(text="🔥 Sale sản phẩm", callback_data="adm:sale_help")
    kb.button(text="💳 Auto nạp", callback_data="adm:toggle_topup")
    kb.button(text="⚙️ Cấu hình API", callback_data="adm:api_config")
    kb.button(text="📣 Broadcast", callback_data="adm:broadcast_help")
    kb.button(text="⚙️ Cài đặt", callback_data="adm:settings")
    kb.button(text="⬅️ Quay lại Menu", callback_data="back_home")
    kb.adjust(1)
    return kb.as_markup()


def admin_panel_text(total_users: int, products_count: int, low_stock_count: int, topup_enabled: bool) -> str:
    return (
        "🛠 ADMIN PANEL\n\n"
        f"👥 Thành viên: {total_users}\n"
        f"📦 Sản phẩm: {products_count}\n"
        f"⚠️ Tồn thấp/hết hàng: {low_stock_count}\n"
        f"💳 Auto nạp: {'BẬT' if topup_enabled else 'TẮT'}\n\n"
        "Chọn khu vực quản lý bên dưới. Dữ liệu user/order/product vẫn dùng cấu trúc cũ trong thư mục data/."
    )
