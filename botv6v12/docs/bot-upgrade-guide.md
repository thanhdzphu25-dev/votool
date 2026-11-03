# Hướng dẫn thay bot theo cấu trúc module

## Mục tiêu

Bản nâng cấp giữ luồng và layout dữ liệu cũ, nhưng tách phần cấu hình, đường dẫn data và Admin Panel ra module để mã sạch hơn và dễ được review. `bot.py` vẫn là entrypoint chính để chạy bot.

## Cách thay

1. Backup toàn bộ thư mục `data/` hiện tại.
2. Copy `bot.py`, thư mục `shopbot/`, file `.env` đã điền key thật, và thư mục `data/` cũ vào cùng thư mục deploy bot.
3. Kiểm tra tên file cấu hình phải đúng là `.env` (không phải `.env.txt`) và nằm cùng cấp với `bot.py`.
4. Chạy `bot.py` như file bot chủ.

## Data được giữ nguyên

Bot vẫn dùng các đường dẫn cũ:

- `data/users.json`
- `data/<user_id>.json`
- `data/products.json`
- `data/product_details.json`
- `data/product_data/`
- `data/orders_data/`
- `data/reserve/`
- `data/warranty/`
- `data/warranty_archive/`
- `data/direct_payments/`
- `data/processed_topup_tx.json`
- `data/topup_control.json`

## Cấu trúc module mới

- `bot.py`: entrypoint và luồng handler chính, giữ cấu trúc legacy để giảm rủi ro lỗi data.
- `shopbot/config.py`: đọc biến môi trường, parse admin ID, không hardcode secret.
- `shopbot/paths.py`: định nghĩa đường dẫn data cũ và tạo thư mục cần thiết.
- `shopbot/admin_panel.py`: keyboard/text cho Telegram Admin Panel.
- `shopbot/json_store.py`: tiện ích đọc/ghi JSON atomic dùng cho các bước tách module tiếp theo.

## Nâng cấp đã có

- Không hardcode `BOT_TOKEN`, `API_KEY`, `TOPUP_API_URL`; chuyển sang biến môi trường.
- Giữ nguyên các hàm và luồng cũ để hạn chế lỗi khi thay file.
- Thêm nút `🛠 Admin Panel` cho admin ở menu chính.
- Thêm dashboard Telegram Admin Panel cơ bản: số thành viên, số sản phẩm, số sản phẩm tồn thấp/hết hàng, trạng thái auto nạp.
- Thêm các mục Admin Panel cho sản phẩm/kho, đơn hàng, khách hàng, auto nạp, số dư API, broadcast và cài đặt.
- Giữ nguyên cơ chế data cũ để các đơn đã mua và số dư khách không bị reset.

## Lưu ý bảo mật

Token/API key cũ từng nằm trong archive cần được rotate ngay. Sau khi đổi sang bản mới, không để secret thật trong source code.

## File `.env`

File `.env` có thể đặt cùng cấp với `bot.py` hoặc export các biến môi trường tương ứng trong process manager. Theo yêu cầu hiện tại, `.env` đã được tạo sẵn từ mã gốc để triển khai nhanh; nếu dùng repo công khai, hãy bỏ file này khỏi commit và rotate secret.

## `.env` lấy từ mã gốc

Theo yêu cầu triển khai nhanh, repo đã có file `.env` chứa các giá trị lấy từ `bot.zip/bot.py` cũ, gồm bot token, admin IDs, API base URL, API key, topup API URL và data dir. Khi đưa lên production, nên rotate lại token/key vì các giá trị cũ từng nằm trong source archive.

## Chạy trực tiếp trên Windows

Khi chạy thật trên Windows/server, thư mục deploy không cần `bot.zip`, `scripts/` hoặc `docs/`. Hãy đặt file `.env` đã điền key thật cùng cấp với `bot.py`, giữ nguyên thư mục `data/`, rồi chạy `python bot.py`.

## Xem thêm cấu trúc deploy

Xem `docs/file-structure.md` để biết chính xác file nào cần copy lên server và file nào chỉ dùng để hỗ trợ nâng cấp.

## Nếu Windows không thấy file `.env`

Một số trình giải nén hoặc File Explorer có thể làm bạn không thấy file bắt đầu bằng dấu chấm. Hãy bật hiển thị phần mở rộng file và kiểm tra file phải tên đúng là `.env`, không phải `.env.txt`. Nếu bot vẫn báo thiếu `BOT_TOKEN`, thông báo lỗi mới sẽ liệt kê chính xác các đường dẫn `.env` mà bot đã kiểm tra.
