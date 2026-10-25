# AI Agent — Assignment 1

Agent local có Google Drive, Google Calendar, Gmail (chỉ đọc) và bộ nhớ RAG.

## Cấu trúc

```text
Assignemnt-1/
├── .env
├── .env.example
├── src/
│   ├── agents/agent.py
│   ├── api/server.py
│   ├── cli/main.py
│   ├── configs/
│   │   ├── config.py
│   │   ├── prompts.py
│   │   ├── prompts.yaml
│   │   ├── tool_loader.py
│   │   └── tools/<tool_name>.yaml
│   ├── observability/
│   │   ├── telemetry.py
│   │   └── grafana/, otel-collector.yaml, prometheus.yml, tempo.yaml
│   ├── registry/
│   ├── services/
│   ├── static/
│   └── tools/
├── deploy/docker/docker-compose.yml
├── scripts/start.sh
├── scripts/start.ps1
└── tests/
```

Agent, HTTP API, CLI và telemetry nằm trong các package riêng dưới `src/`.
Cấu hình ứng dụng nằm trong `src/configs/config.py`; system prompt nằm trong
`src/configs/prompts.yaml`. Mỗi tool có một file YAML riêng trong
`src/configs/tools/`, gồm `name`, `description`, `input_schema`, `required_scopes`.
Code xử lý và liên kết handler vẫn ở `src/tools/`; YAML không chứa code thực thi.
Các định nghĩa được kiểm tra khi khởi động, gồm tên khớp filename, schema và scopes.
Sau khi sửa prompt hoặc YAML tool, khởi động lại server để nạp phiên bản mới.

## Chạy server

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```

Nếu chưa có file môi trường:

```bash
cp .env.example .env
```

Điền API key Anthropic và OpenAI trong `.env`. Cần Qdrant để dùng bộ nhớ;
`deploy/docker/docker-compose.yml` cung cấp bộ observability nếu cần:

```bash
docker compose -f deploy/docker/docker-compose.yml up -d
```

Model mặc định: **Claude Haiku 5.5**, effort **low**. Có thể đặt trong `.env`:

```dotenv
LLM_MODEL=claude-haiku-5-5
LLM_EFFORT=low
```

Effort được gửi qua `output_config.effort`; Haiku 5.5 tự quyết định lượng thinking.

```bash
.venv/bin/python -m src.api.server
```

Mở http://localhost:9004. Trang sẽ yêu cầu đăng nhập Google trước khi vào chat.

CLI:

```bash
.venv/bin/python -m src.cli.main
```

Khởi chạy kèm observability từ thư mục gốc:

```bash
bash scripts/start.sh
# Windows PowerShell: ./scripts/start.ps1
```

Ứng dụng mặc định nạp `.env` ngay tại thư mục gốc `Assignemnt-1/`, bất kể thư mục
làm việc hiện tại. Có thể đặt `AGENT_ENV_FILE` tới file khác khi triển khai; đường
dẫn tương đối của biến này được tính từ thư mục gốc project. Các biến môi trường
đã được cấp cho process được ưu tiên hơn giá trị trong file `.env`.


## Kết nối Gmail của bạn

Kết nối Gmail trong ChatGPT không cấp quyền cho project local này.
Project cần OAuth riêng cho tài khoản Google của bạn.

1. Trong Google Cloud project sở hữu OAuth client, bật **Gmail API**.
2. Cấu hình Google Auth Platform. Nếu app ở chế độ External / Testing,
   thêm tài khoản Gmail của bạn vào **Audience → Test users**.
3. Dùng OAuth client loại **Web application** trong `src/configs/client_secret.json`, hoặc đặt
   `GOOGLE_WEB_CLIENT_FILE` trong `.env` tới file OAuth client khác.
   Redirect URI phải khớp chính xác `http://localhost:9004` (không có dấu `/` cuối).
   File client hiện tại đã liệt kê URI này.
4. Mở http://localhost:9004 → **Đăng nhập bằng Google**.
5. Chọn tài khoản và cấp quyền đọc Gmail. Sau khi thành công, trình duyệt tự về chat;
   địa chỉ tài khoản và nút **Đăng xuất** nằm ở đầu trang.

Cấu hình tùy chọn trong `.env`:

```dotenv
GOOGLE_WEB_CLIENT_FILE=client_secret.json
GOOGLE_WEB_REDIRECT_URI=http://localhost:9004
```

Chưa đăng nhập thì các API chat, lịch sử thao tác và tài khoản trả về `401`.
Cookie chỉ chứa mã phiên ngẫu nhiên với `HttpOnly` và `SameSite=Lax`; token Google
được giữ ở server trong bộ nhớ. Phiên hết hạn sau 8 giờ hoặc khi server khởi động lại.
Đăng xuất hủy phiên local. Chat, audit và token Gmail của mỗi phiên đăng nhập
được giữ riêng. Google Drive, Calendar và bộ nhớ RAG vẫn dùng cấu hình local của
project; đây chưa phải hệ thống đa người dùng cho các dịch vụ đó.

## Đăng nhập Gmail cho CLI (tùy chọn)

Giao diện web không cần lệnh này. Nếu dùng CLI `.venv/bin/python -m src.cli.main`:

   ```bash
   .venv/bin/python -m src.services.gmail_service
   ```

Chọn đúng tài khoản Google và cấp quyền đọc Gmail trong trình duyệt.
Quá trình chờ tối đa 5 phút. Token CLI được lưu riêng vào `gmail_token.json`
với quyền file `600`, không thay token Calendar và không đưa vào Git.
Web login không đọc hoặc ghi đè token CLI.
Đăng nhập Calendar cho CLI bằng `.venv/bin/python -m src.services.calendar_service`.

Cấu hình tùy chọn trong `.env`:

```dotenv
GOOGLE_GMAIL_CLIENT_FILE=credential.json
GOOGLE_GMAIL_TOKEN_FILE=gmail_token.json
GOOGLE_GMAIL_OAUTH_PORT=8000
```

CLI dùng redirect URI `http://localhost:8000` với Web client.
Nếu đổi cổng, cập nhật redirect URI tương ứng trong Google Cloud.

Mọi đường dẫn credential/token tương đối trong cấu hình được tính từ
`src/configs/`, ví dụ `GOOGLE_WEB_CLIENT_FILE=client_secret.json` trỏ tới
`src/configs/client_secret.json`. Có thể dùng đường dẫn tuyệt đối để đặt file
ở vị trí khác. `.env` và tài nguyên ứng dụng được nạp theo đường dẫn của project,
không phụ thuộc thư mục làm việc hiện tại.
Nếu cổng đang bận, dùng cổng khác đã đăng ký hoặc dừng dịch vụ chiếm cổng.

Sau khi đăng nhập, có thể hỏi trong giao diện chat:

- “Đọc và tóm tắt 5 email mới nhất trong hộp thư đến.”
- “Tìm email chưa đọc trong 7 ngày qua.”
- “Tìm email từ someone@example.com về hóa đơn.”

Tool ưu tiên khi đọc/tóm tắt email là `search_and_read_gmail_messages`: backend
tìm và lấy nội dung bằng batch rồi trả các `body` cho LLM trong cùng một kết quả.
Luồng thông thường của một trang email cần **2 lần gọi LLM**: chọn tool → tóm tắt.
Nếu cần phân trang, xử lý lỗi hoặc thêm công cụ, số lần gọi có thể tăng.

`list_gmail_messages` vẫn dùng khi chỉ cần danh sách/preview, và
`read_gmail_message` dùng khi đã biết ID của một email cụ thể. Các tool không gửi,
xóa hoặc đánh dấu đã đọc email. Nội dung dài hơn 20.000 ký tự sẽ bị cắt và
gắn cờ; tệp đính kèm chỉ liệt kê tên. Email cần được đọc sẽ được gửi tới
model Anthropic đang cấu hình để agent xử lý câu hỏi của bạn.

Gmail lấy nội dung bằng `format=full` theo batch (tối đa 20 email mỗi trang),
thay cho việc lấy metadata từng email rồi tải lại nội dung. Trong một lượt
`Agent.run`, kết quả batch được giữ tạm để `read_gmail_message` dùng lại;
tool tìm kiếm vẫn chỉ trả header và snippet cho model. Dữ liệu tạm được xóa
khi lượt xử lý kết thúc, kể cả khi lỗi, và không dùng chung giữa các lượt hoặc
tài khoản. Tệp đính kèm nhị phân không được tải xuống; phần nội dung văn bản
lớn lưu riêng có thể cần thêm request khi đọc.

Với 3 email thông thường, luồng tìm rồi đọc cả 3 giảm từ 7 request HTTP Gmail
xuống 2 (1 tìm kiếm + 1 batch). Batch vẫn tính quota cho từng API call bên trong.
Tool gộp tái sử dụng phần batch này, không tạo thêm lượt HTTP để đọc từng email.
Trong Tempo có span `gmail.batch_get` với `gmail.batch_size` và span
`gmail.read_message` với `gmail.cache_hit` để kiểm tra số liệu thực tế.

Nếu báo thiếu quyền hoặc token hết hạn/bị thu hồi trên web, đăng xuất rồi đăng nhập lại.
Với CLI, chạy lại `.venv/bin/python -m src.services.gmail_service`.
Nếu báo API bị từ chối, kiểm tra Gmail API và tài khoản Test users trong đúng
Google Cloud project của OAuth client.

Trang đăng nhập web phân biệt lỗi cấu hình client, mã đăng nhập hết hạn, lỗi
mạng, thiếu quyền Gmail và Gmail API chưa bật. Khi API chưa bật, trang có liên
kết tới Google Cloud project của OAuth client để bật API. Log server chỉ ghi
bước thất bại và mã lỗi; không ghi nội dung ngoại lệ OAuth hoặc URL callback.

Tham khảo: [Google Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python).

## Kiểm tra

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Các bài kiểm tra dùng dữ liệu giả, không đọc hộp thư thật hay gọi model.
