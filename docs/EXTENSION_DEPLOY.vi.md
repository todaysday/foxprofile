# Bàn giao triển khai protected extension

Đây là bàn giao offline slice 8.3. **Cả hai host thật: BLOCKED; slice 09: NOT RUN.**
Chưa có địa chỉ host, quyền truy cập hoặc topology thực tế. Test synthetic và
fixture native tại máy local không xác nhận acceptance ARM64/x86-64. Deployment
agent riêng thực hiện [slice 09](../openspec/changes/add-multi-user-manual-browser-workflow/slices/09-live-deployment-and-acceptance.md),
ghi từng case live và số đo tài nguyên. [DEPLOY.md](DEPLOY.md) giữ nguyên cho
upstream. [English](EXTENSION_DEPLOY.md).

## Release, dependency và topology

Khóa đúng fork commit, distribution và dependency artifact tương ứng. Đối chiếu
`foxprofile_ext/compatibility.json`; manifest không thay thế acceptance trên host.
Cài distribution và `requirements-extension.txt` trong environment riêng. Lưu
riêng kết quả `python -m pip freeze --all` vào `dependencies.txt`, giữ wheel,
environment/image theo digest, phiên bản Python, browser build và native package.
Report dependency không đủ khôi phục package không còn truy cập được. Rollback
không download dependency.

Mỗi installation chỉ một lifecycle manager, một ASGI worker. Panel/API/WebSocket
cùng origin và cùng manager. Proxy có nhiều installation phải sticky routing;
không dùng chung dataset giữa các manager. SQLite admission không cung cấp backend
IPC giữa worker. Không dùng `uvicorn --workers`, gunicorn nhiều worker hoặc thêm
desktop manager trên cùng installation. Capacity mặc định 10; giữ giá trị hợp lệ
đã lưu. Hạ capacity không đồng nghĩa dừng browser đang chạy.

[deploy/extension](../deploy/extension) chứa template, không mô tả service đã được
khảo sát. Điền bản riêng của `target.json.example` cho từng host; giải quyết mọi
`UNRESOLVED`, `LOCKED_COMMIT`, `REPLACE_...`, owner, đường dẫn, TLS, namespace và
resource policy trước khi dùng.

## Cấu hình và native dependency

Tạo `runtime.env`, `secrets.env` private từ template: thư mục 0700, file 0600,
service owner đọc được. Đặt `FOXPROFILE_EXT_ENABLED=true`, `FOXPROFILE_EXT_DB`,
`FOXPROFILE_DATA_DIR`, `FOXPROFILE_PROFILES_FILE`, **`FOXPROFILE_HEADLESS=virtual`
trước khi Python import `src.core.config`**. EnvironmentFile của systemd thực
hiện việc này. Không tắt protected mode để vượt lỗi startup/restore.

Service credential chỉ nằm trong secret manager hoặc file private. Với installation
mới, tạo trực tiếp bằng local terminal; không in token, không ghi đè credential cũ:

```bash
python - <<'PY'
import os
import secrets
path = "/etc/foxprofile-extension/secrets.env"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as output:
    output.write("FOXPROFILE_API_TOKEN=" + secrets.token_urlsafe(36) + "\n")
PY
```

Chạy với owner/quyền phù hợp. Giữ credential hiện tại khi migration/rollback.
Không đưa token vào argument, URL, history, screenshot hoặc report. Người dùng
panel dùng account/session riêng; service credential chỉ ở server.

Cài và xác minh Camoufox/Chrome build trước startup, đặt đúng executable path.
Có thể bỏ engine không dùng. Protected runtime không tự download browser còn thiếu.
Cài bubblewrap (`bwrap`), Xvfb, xauth, x11vnc và `tzdata` qua native package system của host; `tzdata` cung cấp IANA timezone database cho image tối giản.
Kiểm tra user namespace/confinement tại staging. Lỗi phải fail closed, không chạy
browser thiếu confinement.

`FOXPROFILE_X11VNC_EXECUTABLE` là executable tuyệt đối chính xác;
`FOXPROFILE_X11VNC_SHA256` là SHA-256 được operator xác minh độc lập. Build distro
amd64 `0.9.17-1` quan sát offline có digest
`249bdc17e09fd96ea73ca7fbec2d8f3cd9ff411d0bbfb634d99431548fb77132`.
Giá trị này không tự xác nhận binary/host khác. Chưa biết hash ARM64; deployment
agent phải xác minh, không sao chép hash amd64. Thiếu/sai setting thì từ chối
manual access.

Browser, Xvfb do runner quản lý, Xauthority và VNC helper phải cùng service/container
private temporary namespace. DISPLAY giống nhau chưa đủ. Giữ `PrivateTmp=true`
với topology child dùng chung này; không chạy VNC ở service khác, không thêm Xvfb.
noVNC gắn đúng Camoufox run virtual hiện tại; Chrome chưa hỗ trợ manual access.

Dùng HTTPS/WSS, cookie cùng origin. Nginx template chuyển WebSocket Upgrade,
proxy timeout 90 giây. Keepalive ứng dụng: Ping 10 giây + timeout 10 giây; cleanup
còn 40 giây, tổng tối đa 60 giây. Proxy timeout không phải đồng hồ cleanup. Không
mở VNC công khai, không đưa credential dài hạn vào URL.

## Backup nhất quán, migration và restore

CLI có trong package: `python -m foxprofile_ext.maintenance --help`. Ba command
không mở browser, không liên hệ host. Điền `installation.json.example` private với
đường dẫn tuyệt đối và fork commit 40 ký tự hex. Sáu root file/thư mục riêng biệt,
không lồng nhau. `release_dir` là distribution ứng dụng đã giải nén, bất biến;
không phải checkout có `.git`, virtualenv hoặc link. Giữ riêng artifact dependency,
browser/native đã pin. `configuration_dir` chứa toàn bộ cấu hình service, environment
private, bản unit/proxy thực tế. Đặt descriptor ngoài thư mục config nếu mô tả
layout restore khác.

Trước backup/migration: chặn admission ở proxy/service thật, đóng browser bình
thường, xác nhận mọi browser/helper/display đã thoát, rồi dừng manager. Xác nhận
không manager thứ hai/cookie helper dùng data. Không dừng profile chỉ để thỏa
capacity thấp hơn. `--stopped-and-closed` là operator xác nhận: copy filesystem
không tự chứng minh process đã dừng. CLI giữ `profiles.lock` chung, deadline lấy
lock 10 giây; từ chối run active/reserved/không chắc chắn, recovery blocked,
runtime descriptor chưa dọn, manual lease mở và browser lock marker. Không sửa
DB/xóa marker để bỏ qua; dùng manager sở hữu để reconcile, xác nhận shutdown.

Chạy dưới service data owner, thư mục maintenance private, `umask 077`. Các path
dưới đây là ví dụ, phải điền theo host:

```bash
umask 077
python -m foxprofile_ext.maintenance backup \
  --installation /srv/foxprofile-maintenance/installation.json \
  --stopped-and-closed \
  --destination /srv/foxprofile-extension-backups/before-change

mkdir -m 700 /srv/foxprofile-extension-staging
python -m foxprofile_ext.maintenance restore \
  --snapshot /srv/foxprofile-extension-backups/before-change \
  --empty-disposable-destination /srv/foxprofile-extension-staging

python -m foxprofile_ext.maintenance migrate \
  --installation /srv/foxprofile-extension-staging/installation.json \
  --stopped-and-closed
python -m foxprofile_ext.maintenance migrate \
  --installation /srv/foxprofile-extension-staging/installation.json \
  --stopped-and-closed
```

Backup công bố atomically một thư mục có SQLite snapshot qua backup API, catalog,
toàn bộ profile directory (cả residual/quarantine), config, release và dependency
report tương ứng. Installation upstream chưa có extension DB được ghi nhận vắng
DB trong manifest. Inventory file/thư mục, SHA-256 và executable bit giúp phát
hiện thay đổi ngoài ý muốn. Đây là snapshot private local, không phải archive ký
số hoặc backup browser đang chạy. Từ chối symlink, hardlink file, device, FIFO;
không giải nén archive nên không có archive path do caller chọn. Input phải bất
biến suốt maintenance. Snapshot chứa cookie, proxy credential, service secret,
password/session hash: không đưa vào git/report/public artifact store.

Restore kiểm tra layout/inventory/path và runtime state, copy vào sibling staging
private rồi chỉ thay **thư mục đích disposable đã tồn tại và rỗng** được chỉ định.
Không ghi đè production tree. Thư mục 0700, file 0600, giữ owner executable bit.
Chạy dưới service owner để giữ ownership đó; ghi nhận/áp dụng ACL hoặc security
label theo host trước startup. Không giữ ACL/xattr/foreign ownership tùy ý.
`installation.json` sinh ra trỏ vào bản restore. Config gốc giữ nguyên byte:
**không source trực tiếp tại staging** vì path có thể trỏ production. Tạo environment/
unit staging riêng với data/DB/profile/log, installation/runtime identity, listener,
namespace riêng. Giữ config matched gốc trong snapshot cho rollback.

Migration dùng schema transaction, stable-ID migration và pending-operation recovery
sẵn có của `ProfileCatalog`. Giữ ID/grant và capacity hợp lệ; thiếu capacity thành
10, capacity sai thì lỗi, không reset. Không ghi lại browser data. Schema migration
bị ngắt rollback và rerun được; catalog migration bị ngắt dùng intent ledger.
Pending profile chưa được truy cập cho tới khi recovery xong. Test synthetic thực
thi ngắt trước/sau catalog save, schema rollback, rerun, matched restore; chưa
chứng minh login thật được giữ.

Sau staging migration thành công, bootstrap admin đầu tiên ở local, đúng DB
staging; password nhập tương tác, không truyền argument:

```bash
python -m foxprofile_ext.bootstrap \
  --database /srv/foxprofile-extension-staging/state.sqlite3 --username admin0
```

Có admin sẵn thì bootstrap từ chối lần hai. Sau staging acceptance và backup
production mới, chạy `migrate` với descriptor **production** đã xác minh trong khi
installation dừng, rồi chỉ chạy locked protected release với environment matched.

## Health, acceptance và matched rollback

Kiểm tra unit đã điền bằng `systemd-analyze verify`, nginx đã điền bằng `nginx -t`.
Phải giải quyết placeholder, certificate/user/executable còn thiếu; parse template
không xác nhận host. Chạy staging riêng, kiểm tra `curl --fail
http://127.0.0.1:8000/health` và `/info` public qua loopback. Sau đó kiểm tra
login/session, từ chối route protected khi chưa login, assignment filtering,
service separation, capacity đã lưu. Health public không chứng minh auth. Request
auth dùng secret manager hoặc header file private; không dùng token trong argument
`curl -H`.

Deployment agent chạy đầy đủ [acceptance plan](../openspec/changes/add-multi-user-manual-browser-workflow/references/acceptance-plan.md)
trên cả hai host: browser/proxy/login/fingerprint thật, native dialog/environment
confinement, input/Back/cleanup cùng run, failure injection, đo tài nguyên 1/5/10
browser. Ghi PASS/FAIL/BLOCKED/NOT RUN theo chứng cứ; thiếu host giữ BLOCKED/NOT RUN.

Rollback: chặn admission, đóng browser sở hữu, dừng manager lỗi; giữ chứng cứ
private. Dùng cùng command `restore` từ snapshot trước vào **đích disposable mới,
rỗng**. Xác minh code/config/data/dependency matched; khôi phục dependency executable
và browser/native build đã pin, không download; trỏ service vào cả protected
dataset/release/config đã test. Xác nhận auth, ID/grant, capacity, fingerprint/login,
ordinary control rồi mở truy cập. Không restore riêng SQLite/catalog, hứa schema
downgrade, chạy upstream thiếu bảo vệ trên managed data hoặc tắt extension để
fallback. Snapshot trước protected mode chỉ là nguồn recovery private cho staging
migration protected mới, không phải rollback public thiếu bảo vệ.

Kiểm tra offline:

```bash
python -m pytest -q tests/test_ext_maintenance.py
ruff check foxprofile_ext/maintenance.py foxprofile_ext/locking.py tests/test_ext_maintenance.py
```
