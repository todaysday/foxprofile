# Fixture nghiệm thu extension dùng một lần

[English](EXTENSION_ACCEPTANCE.md) · [Triển khai](EXTENSION_DEPLOY.vi.md) · [Danh sách ca chuẩn](../openspec/changes/add-multi-user-manual-browser-workflow/references/acceptance-plan.md)

`python -m foxprofile_ext.acceptance` có ba lệnh nhỏ: tạo fixture ngoại tuyến, phục vụ trang nhập liệu trên loopback và kiểm tra HTTP với staging được chỉ định. Không tìm máy chủ, tải/chạy trình duyệt, cấu hình agent, gọi CAPTCHA, reset máy chủ hoặc xóa profile. Agent triển khai chịu trách nhiệm đưa bản phát hành lên hệ thống thật.

## Tạo ngoại tuyến

Chạy tại thư mục gốc của bản phát hành đã khóa, bằng môi trường Python đã cài phụ thuộc runtime và extension. Chọn thư mục staging cục bộ riêng biệt, có sẵn thư mục cha. Không dùng dữ liệu production hoặc thư mục trình duyệt production đã khôi phục làm fixture này.

`--credentials-file` nhận tệp JSON riêng tư ngoài repository: tệp thường, thuộc người chạy, một hard link, quyền `0600`; từ chối đường dẫn symlink. Tạo/sửa tệp bằng cách riêng tư. Đây chỉ là **giá trị minh họa giả**, không phải mặc định hay thông tin đăng nhập thật:

```json
{
  "admin0_password": "example-admin-password",
  "a_password": "example-a-password",
  "b_password": "example-b-password",
  "service_token": "example-service-token-at-least-24-characters"
}
```

Thay toàn bộ giá trị trong tệp riêng tư. Token dịch vụ cần ít nhất 24 ký tự ASCII. Bỏ `--credentials-file` để nhập cả bốn giá trị bằng lời nhắc terminal ẩn nội dung. Không có cờ dòng lệnh chứa mật khẩu/token.

```bash
python -m foxprofile_ext.acceptance prepare \
  --destination /tmp/foxprofile-acceptance \
  --credentials-file /tmp/acceptance-credentials.json
```

Đích phải chưa tồn tại hoặc rỗng; từ chối symlink trong đường dẫn và không ghi đè thư mục có dữ liệu. Không kết nối máy chủ/mạng. Lệnh dùng `ProfileManager`, `ProfileCatalog`, DAO tài khoản/băm mật khẩu, phân quyền, thiết lập và migration thật. Không chạy trình duyệt hoặc tạo fingerprint trình duyệt.

Đầu ra có quyền thư mục/tệp riêng tư (`0700`/`0600` trên POSIX):

| Đầu ra | Nội dung |
| --- | --- |
| `fixture.json` | ID fixture duy nhất, ID người dùng/profile đã lưu, tên và giới hạn dự kiến |
| `runtime-env.json` | Đường dẫn dữ liệu riêng, host loopback, headless virtual, **token dịch vụ riêng tư** |
| `foxprofile_ext.sqlite3` | Quản trị admin0, người dùng A/B, mật khẩu đã băm, phân quyền, giới hạn 10 |
| `profiles.json`, `browserdata/` | 11 profile Camoufox/Linux vô hại, không proxy, cookie, đăng nhập hay fingerprint |

A có PA, B có PB; chín profile chưa gán chỉ hiện với quản trị/dịch vụ. Tên chứa tiền tố ID fixture. ID giữ nguyên khi dùng lại fixture đã lưu; fixture tạo riêng có ID khác. Không tạo tài khoản người dùng dịch vụ hay phiên cá nhân dùng lại. Token dịch vụ chỉ thuộc cấu hình máy chủ. Mật khẩu rõ chỉ nằm trong đầu vào của người vận hành.

## Khởi động staging riêng một cách tường minh

Hoàn thành [preflight triển khai](EXTENSION_DEPLOY.vi.md) trước: kiểm tra bản phát hành/phụ thuộc đã khóa, dữ liệu/tiến trình riêng, kiến trúc máy thật, bản trình duyệt/adapter đã thử, confinement, namespace riêng và listener được bảo vệ. `runtime-env.json` mô tả dữ liệu fixture, không phải manifest triển khai đầy đủ. Kết hợp thiết lập staging đã rà soát qua cơ chế cấu hình bí mật hiện có; không sao chép mù cấu hình/thông tin đăng nhập production. Thiếu máy thật, ARM64, digest bản trình duyệt hoặc giá trị adapter nghĩa là preflight **BLOCKED**, không tự bịa hoặc báo đạt.

Sau preflight, khởi động cục bộ tường minh bằng cấu hình riêng trước khi import ứng dụng:

```bash
python - /tmp/foxprofile-acceptance/runtime-env.json <<'PY'
import json
import os
import runpy
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    os.environ.update(json.load(source))
sys.argv = ["src.server", "--host", "127.0.0.1", "--port", "8800"]
runpy.run_module("src.server", run_name="__main__")
PY
```

Giữ máy chủ trong cài đặt riêng này. Reverse proxy nếu có phải dùng HTTPS/WSS đã rà soát. Lệnh không chạy profile. Dừng máy chủ tường minh sau khi thử; helper không quản lý vòng đời máy chủ.

## Smoke HTTP với staging chỉ định

Đọc ID fixture trong `fixture.json` cục bộ, rồi cung cấp ID và origin staging chủ động:

```bash
python -m foxprofile_ext.acceptance smoke \
  --fixture /tmp/foxprofile-acceptance \
  --expect-fixture FIXTURE_UUID_FROM_LOCAL_MANIFEST \
  --base-url http://127.0.0.1:8800 \
  --credentials-file /tmp/acceptance-credentials.json
```

Origin công khai bắt buộc HTTPS. Từ chối URL chứa thông tin đăng nhập, path, query hoặc fragment. Tắt chuyển hướng và proxy HTTP từ môi trường. Helper tạo ba phiên cá nhân thật và client HTTP dịch vụ riêng. Kiểm tra ID/vai trò người dùng và toàn bộ catalog 11 profile riêng, rồi kiểm tra lọc A/B, từ chối ID/tên profile người khác, từ chối thiết lập cho người thường, phạm vi dịch vụ, tách phiên cá nhân/dịch vụ và đọc giới hạn bằng quản trị. Sai ID xác nhận fixture dừng trước HTTP; sai ID/catalog đích dừng trước các kiểm tra quyền. Xác minh đích cần đăng nhập nên chỉ hàng phiên login/logout của lệnh có thể đổi. Không tạo/chạy/dừng/sửa/xóa profile, người dùng, phân quyền hay thiết lập trên đích. Từ chối đích có profile thừa.

Bốn nhóm `PASS` chỉ áp dụng smoke hẹp này. Lỗi không in URL yêu cầu, thông tin đăng nhập, cookie hay nội dung phản hồi. Không đính kèm tệp thông tin đăng nhập/cấu hình riêng vào báo cáo.

## Trang nhập liệu trình duyệt/noVNC

Chạy trang đóng gói một cách tường minh trên máy chứa trình duyệt:

```bash
python -m foxprofile_ext.acceptance serve-page --port 8765
```

Chỉ bind `127.0.0.1`. `/` và `/acceptance-input.html` trả trang; đường dẫn khác trả 404. Không liệt kê thư mục, agent, CAPTCHA, telemetry, tài nguyên ngoài hoặc yêu cầu mạng; CSP chặn kết nối ra ngoài. Namespace mạng trình duyệt cần cách truy cập loopback đã rà soát; không mở rộng listener để chữa topology chưa kiểm tra. Máy từ xa dùng forwarding loopback đã rà soát. Ctrl+C dừng lệnh.

Dùng PA của A và PB của B trong `fixture.json`, thực hiện riêng A01–A30, N01–N13, S01–S03 và các ca X theo danh sách chuẩn:

1. Chủ động chạy PA bằng điều khiển thông thường có quyền, mở `http://127.0.0.1:8765/`. Ghi run ID, tab, framebuffer/display, hình học cửa sổ và `#geometry` trước truy cập thủ công. Trang có viewport, cửa sổ ngoài, screen/available screen, DPR, vị trí cuộn; vị trí cửa sổ native/framebuffer cần công cụ trên host.
2. Mở noVNC từ Viewer PA hiện tại. Nhập `#text-input`, bấm `#ordinary-click`, kéo `#drag-source` vào `#drop-target`, cuộn `#scroll-box` và trang ngoài. Quan sát văn bản, bộ đếm click/drop và vị trí cuộn; trang không tự kết luận thành công.
3. Thử giữ phím/nút trong từng ca Back/ngắt kết nối/hết hạn được phép. Đọc `#held-input` và sự kiện; blur cố ý giữ trạng thái quan sát. Sau khi quay lại, dùng điều khiển Playwright/API thông thường bấm `#next-click`, đọc `#next-click-count` và log sự kiện. Endpoint hiện có: `POST /api/v1/browser/id%3A{PA_ID}/page/click`, body `{"selector":"#next-click"}`, dùng phiên có quyền với Origin/CSRF hoặc client dịch vụ riêng. `PA_ID` là UUID thô trong `fixture.json`; `id%3A` mã hóa tiền tố định vị rõ ràng `id:` của route cũ này. Path `profile_id` chuẩn của extension và body `profile_ids` vẫn dùng UUID thô. Trang không tự gửi yêu cầu này.
4. So sánh cùng run/tab và hình học trước/trong/sau; PB và profile lân cận không bị ảnh hưởng. Lặp mở/đóng, đo số tiến trình VNC/display/browser thuộc cài đặt và thời gian dọn dẹp đầu-cuối theo từng ca.
5. Lặp khi không cấu hình agent và khi agent đang chờ độc lập. FoxProfile không gửi tin, tự đánh giá hoàn thành hay gọi pause/resume. Người dùng thông báo sẵn sàng ngoài FoxProfile.

Marker DOM giữ lại không chứng minh OS đã nhả mọi phím/nút. Kiểm tra trạng thái input/tiến trình host và lần click thông thường tiếp theo theo ca chuẩn. Helper không mở endpoint công khai gây crash, chèn lỗi hoặc reset. Test supervisor/manual-service ngoại tuyến hiện có kiểm tra chèn lỗi xác định; crash parent/gateway, đua giới hạn, canary file/proc native, restart/persistence, proxy/WebRTC và tải host cần quy trình máy dùng một lần cùng bằng chứng của runbook triển khai.

## Giới hạn bằng chứng

`pytest tests/test_ext_acceptance_fixture.py -q` kiểm tra tạo fixture ngoại tuyến, quyền/từ chối đường dẫn, người dùng/phiên/phân quyền/catalog/thiết lập thật, HTTP loopback thật và smoke qua route ứng dụng thật trên HTTP cục bộ. Không tải trình duyệt hoặc dùng adapter VNC thật. Đây chỉ là bằng chứng bàn giao ngoại tuyến.

Thực thi đích thật, đủ 30 ca truy cập, 13 ca noVNC, ba ca stealth mỗi host, crash/recovery/isolation và số đo ARM64/x86-64 vẫn **NOT RUN** đến khi thực hiện/ghi từng ca. Thiếu điều kiện đích là **BLOCKED** trong preflight triển khai. Test ngoại tuyến đạt hoặc bốn nhóm smoke HTTP không tự đổi trạng thái ca chuẩn.
