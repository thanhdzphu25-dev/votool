# AI Answer Helper

Extension Chrome/Chromium Manifest V3. Nút hỏi AI được ẩn hoàn toàn. Click 3 lần liên tiếp ở góc phải dưới màn hình để đọc message assistant mới nhất, gọi OpenRouter, điền câu trả lời vào textarea rồi gửi bằng phím Enter.

## Cài đặt

1. Mở `chrome://extensions` hoặc `chrom://extensions`.
2. Bật **Developer mode**.
3. Chọn **Load unpacked** và trỏ tới thư mục `auto/`.
4. Mở extension -> **Mở cài đặt API**.
5. Cấu hình endpoint OpenAI-compatible, model và API key nếu cần.
6. Tải lại tab đích sau khi cài extension.
7. Bấm biểu tượng extension để mở popup, sau đó click 3 lần liên tiếp trong popup để mở trang cài đặt.
8. Nhấn `Ctrl+B` để hiện/ẩn nút trả lời nhanh một câu. Click nút này hoặc click 3 lần liên tiếp ở góc phải dưới để trả lời câu hiện tại. Nhấn `Ctrl+B`, rồi `Ctrl+A` trong vòng 1,2 giây để bật/tắt chế độ tự trả lời đến khi bài kết thúc.

Nếu thấy `Missing Authentication Header`, mở Settings, dán XAH API key, bấm **Lưu cấu hình**, rồi reload extension.

## XAH API

Cấu hình mặc định:

```text
https://api.xah.io/v1/chat/completions
```

Model mặc định là `dungcsnd113/gpt-5.6-terra`. Extension gửi request OpenAI-compatible và đọc `choices[0].message.content`. Trước khi gửi, extension chờ nội dung ổn định; nếu message có ảnh như thẻ `<img src="https://...">`, extension tải ảnh tạm vào bộ nhớ; với canvas (`data-testid="chat-canvas-message"`), extension chụp vùng đang hiển thị nhiều lần và chỉ gửi ảnh khi hai lần liên tiếp giống nhau. Ảnh không được lưu xuống ổ đĩa. Model XAH phải hỗ trợ vision mới phân tích được ảnh. Video được bỏ qua.

Nếu API bị giới hạn `HTTP 429`, extension tự thử lại tối đa 3 lần với thời gian chờ tăng dần.

## OpenAI-compatible API

Extension gửi `POST` với JSON:

```json
{
  "model": "llama3.2",
  "temperature": 0.2,
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "câu hỏi mới nhất"}
  ]
}
```

API cần trả về `choices[0].message.content`. Có thể dùng gateway local tại `http://localhost:8787/v1/chat/completions` để tránh CORS. API key Gemini được lưu trong `chrome.storage.local`, vì vậy chỉ dùng trên profile trình duyệt cá nhân.

## Luồng hoạt động

- `background.js`: nhận phím tắt và gọi AI, không truy cập DOM.
- `content.js`: tạo vùng click vô hình 48px ở góc phải dưới; click 3 lần liên tiếp hoặc bấm nút trả lời nhanh để trả lời một câu. Chế độ `Ctrl+B`, rồi `Ctrl+A` tiếp tục theo dõi và trả lời câu mới đến khi gặp thông báo hoàn thành.
- `options.html`: lưu endpoint/model/prompt.

## Điều chỉnh selector

Nếu website thay đổi giao diện, sửa `selectors` đầu file `content.js`:

- Câu hỏi: `.message-container.message-assistant`
- Text box: `textarea.w-md-editor-text-input`
