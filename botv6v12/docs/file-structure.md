# Cấu trúc file deploy bot

Cấu trúc bạn đang dùng là đúng. Khi chạy thật chỉ cần để `bot.py`, `.env`, thư mục `shopbot/` và thư mục `data/` cùng một cấp.

```text
thu muc/
├── bot.py              # File chạy chính: python bot.py
├── .env                # Key thật: BOT_TOKEN, ADMIN_IDS, API_BASE_URL, API_KEY, TOPUP_API_URL, DATA_DIR
├── shopbot/
│   ├── __init__.py
│   ├── config.py       # Đọc .env / biến môi trường
│   ├── paths.py        # Giữ nguyên đường dẫn data cũ
│   ├── admin_panel.py  # Giao diện Admin Panel Telegram
│   └── json_store.py   # Helper đọc/ghi JSON atomic
└── data/               # Thư mục data thật giữ nguyên từ bot cũ
    ├── users.json
    ├── products.json
    ├── product_details.json
    ├── processed_topup_tx.json
    ├── topup_control.json
    ├── <user_id>.json
    ├── product_data/
    ├── orders_data/
    ├── reserve/
    ├── warranty/
    ├── warranty_archive/
    └── direct_payments/
```

## File bắt buộc khi chạy production

Bắt buộc có đúng 4 phần này:

- `bot.py`
- `.env` đã điền key thật
- `shopbot/`
- `data/` từ bot cũ

Không bắt buộc có khi chạy production:

- `bot.zip`
- `scripts/`
- `docs/`
- `.env.example`

## Nội dung `.env` cần có

File `.env` phải nằm cùng cấp với `bot.py` và nên có dạng dưới đây. Lưu ý trên Windows không được đặt nhầm tên thành `.env.txt`.

```dotenv
BOT_TOKEN=...
ADMIN_IDS=123456789,987654321
API_BASE_URL=...
API_KEY=...
TOPUP_API_URL=...
DATA_DIR=data
```

## Luồng chạy

```bash
python bot.py
```

Bot sẽ đọc `.env`, sau đó dùng `DATA_DIR=data` để đọc lại toàn bộ dữ liệu cũ trong thư mục `data/`.
