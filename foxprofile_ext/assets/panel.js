import { createManualViewer } from "./panel-manual.js";

const STRINGS = {
  en: {
    username: "Username", password: "Password", personal_hint: "Sign in with your personal account.", bad_credentials: "Invalid username or password.",
    personal_side: "Manage the profiles assigned to your account.", admin: "Administration",
    users: "Users", role: "Role", active: "Active", inactive: "Inactive", user: "User", admin_role: "Administrator",
    add_user: "Create user", reset_password: "Reset password", assignments: "Assigned profiles",
    change_user: "Change access for {name}? Existing sessions will be revoked.",
    reset_confirm: "Reset password for {name}? Existing sessions will be revoked.",
    capacity: "Shared profile limit", occupancy: "Occupied: {n} · Limit: {limit}",
    capacity_hint: "Minimum 1. Lowering the limit below occupancy keeps existing browsers open and blocks new starts.",
    capacity_full_limit: "Shared profile limit ({limit}) reached. Stop an accessible profile, then retry, or contact an administrator.",
    action_failed: "The operation failed. Refresh and try again.", profile_busy: "Profile is busy.", resource_unavailable: "Resource unavailable.",
    capacity_full: "Shared profile limit reached. Stop an accessible profile, then retry, or contact an administrator.",
    stamp_recovering: "Recovering",
    session_expired: "Sign in again.", manual_open: "Open in noVNC", manual_back: "Return to Viewer",
    manual_connected: "Manual control connected", manual_disconnected: "Manual control ended",
    manual_unsupported: "Manual control requires a running Camoufox virtual display. This runtime is unsupported.",
    manual_busy: "Manual access is busy. Try again later.", manual_error: "Cannot connect manual control.",
    runtime_recovering: "Runtime recovery is incomplete. Try again after recovery.",
  },
  vi: {
    username: "Tên đăng nhập", password: "Mật khẩu", personal_hint: "Đăng nhập bằng tài khoản cá nhân.", bad_credentials: "Tên đăng nhập hoặc mật khẩu không đúng.",
    personal_side: "Quản lý các profile được cấp cho tài khoản của bạn.", admin: "Quản trị",
    users: "Người dùng", role: "Vai trò", active: "Hoạt động", inactive: "Đã khóa", user: "Người dùng", admin_role: "Quản trị viên",
    add_user: "Tạo người dùng", reset_password: "Đặt lại mật khẩu", assignments: "Profile được cấp",
    change_user: "Đổi quyền của {name}? Các phiên đăng nhập hiện tại sẽ bị thu hồi.",
    reset_confirm: "Đặt lại mật khẩu cho {name}? Các phiên đăng nhập hiện tại sẽ bị thu hồi.",
    capacity: "Giới hạn profile chung", occupancy: "Đang chiếm: {n} · Giới hạn: {limit}",
    capacity_hint: "Tối thiểu 1. Giảm giới hạn dưới số đang chiếm giữ browser hiện tại và chặn lượt mở mới.",
    capacity_full_limit: "Đã đạt giới hạn profile chung ({limit}). Dừng profile được cấp rồi thử lại hoặc liên hệ quản trị viên.",
    action_failed: "Thao tác thất bại. Làm mới rồi thử lại.", profile_busy: "Profile đang bận.", resource_unavailable: "Không thể truy cập tài nguyên.",
    capacity_full: "Đã đạt giới hạn profile chung. Dừng profile được cấp rồi thử lại hoặc liên hệ quản trị viên.",
    stamp_recovering: "Đang khôi phục",
    session_expired: "Vui lòng đăng nhập lại.", manual_open: "Mở bằng noVNC", manual_back: "Trở lại Viewer",
    manual_connected: "Đã kết nối điều khiển thủ công", manual_disconnected: "Điều khiển thủ công đã kết thúc",
    manual_unsupported: "Điều khiển thủ công cần Camoufox đang chạy với màn hình ảo. Runtime này không hỗ trợ.",
    manual_busy: "Điều khiển thủ công đang bận. Thử lại sau.", manual_error: "Không thể kết nối điều khiển thủ công.",
    runtime_recovering: "Runtime chưa khôi phục xong. Thử lại sau khi khôi phục.",
  },
};
const ERROR_KEYS = {
  PROFILE_CAPACITY_EXCEEDED: "capacity_full", AUTH_REQUIRED: "session_expired",
  MANUAL_UNSUPPORTED_RUNTIME: "manual_unsupported", MANUAL_SESSION_BUSY: "manual_busy",
  RUNTIME_RECOVERING: "runtime_recovering", PROFILE_BUSY: "profile_busy",
  FORBIDDEN_OR_NOT_FOUND: "resource_unavailable", STALE_BROWSER_RUN: "manual_disconnected", MANUAL_SESSION_CLOSED: "manual_disconnected",
};

export function createPanel(ctx, info) {
  for (const language of ["en", "vi"]) Object.assign(ctx.strings[language], STRINGS[language]);
  const { $, h, t, modal, modalHead, field, toast, confirmDialog } = ctx;
  const personal = info.auth_mode === "personal";
  let user = null, csrf = "", epoch = 0, manual = null;
  if (!personal) return { personal, labels() {}, clear() {} };
  ctx.removeServiceToken();
  const link = h("link", { rel: "stylesheet", href: "/assets/ext/panel.css" });
  document.head.append(link);
  const username = h("input", { id: "username", autocomplete: "username", required: true });
  const password = h("input", { id: "password", type: "password", autocomplete: "current-password", required: true });
  $("token").closest(".field").replaceWith(field(t("username"), username), field(t("password"), password));
  $("login").querySelector("[data-t=\"login_hint\"]").dataset.t = "personal_hint";
  $("login").querySelector("[data-t=\"login_side\"]").dataset.t = "personal_side";
  const actor = h("div", { class: "personal-user", id: "personal-user" });
  const admin = h("button", { class: "rail-link hidden", id: "admin-btn", type: "button", onclick: openAdmin });
  $("logout-btn").before(actor, admin);
  $("mcp-btn").classList.add("hidden");
  document.querySelector("a[href=\"/docs\"]").classList.add("hidden");

  function errorMessage(code, limit) {
    if (code === "PROFILE_CAPACITY_EXCEEDED" && Number.isInteger(limit) && limit >= 1) return t("capacity_full_limit", { limit });
    return t(ERROR_KEYS[code] || "action_failed");
  }
  async function request(method, path, body, { raw = false, expectedEpoch = epoch } = {}) {
    const version = expectedEpoch, headers = {};
    if (version !== epoch) throw new ctx.ApiError(401, t("session_expired"));
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (!["GET", "HEAD"].includes(method) && csrf) headers["X-CSRF-Token"] = csrf;
    const response = await fetch("/api/v1" + path, { method, credentials: "same-origin", headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (version !== epoch) throw new ctx.ApiError(401, t("session_expired"));
    if (response.status === 401) {
      if (path === "/ext/auth/login") throw new ctx.ApiError(401, t("bad_credentials"));
      ctx.showLogin(); throw new ctx.ApiError(401, t("session_expired"));
    }
    if (!response.ok) {
      let message = response.statusText, code;
      try {
        const data = await response.json(); code = data.detail?.code;
        if (ERROR_KEYS[code]) message = errorMessage(code, data.detail?.max_concurrent_profiles);
        else if (typeof data.detail === "string") message = data.detail;
        else message = data.detail?.message || t("action_failed");
      } catch {}
      if (version !== epoch) throw new ctx.ApiError(401, t("session_expired"));
      const error = new ctx.ApiError(response.status, message); error.code = code; throw error;
    }
    if (raw) return response;
    const data = await (response.headers.get("content-type")?.includes("json") ? response.json() : response.text());
    if (version !== epoch) throw new ctx.ApiError(401, t("session_expired"));
    return data;
  }
  function isAdmin(version = epoch) { return version === epoch && user?.role === "admin"; }
  async function adminRequest(method, path, body, version) {
    if (!isAdmin(version)) throw new ctx.ApiError(403, t("resource_unavailable"));
    return request(method, path, body, { expectedEpoch: version });
  }
  function authenticated(data) {
    ++epoch; manual?.reset(); ctx.reset();
    user = data.user; csrf = data.csrf_token;
    actor.textContent = user.username;
    const administrator = user.role === "admin";
    admin.classList.toggle("hidden", !administrator);
    $("mcp-btn").classList.toggle("hidden", !administrator);
    document.querySelector('a[href="/docs"]').classList.toggle("hidden", !administrator);
  }
  function labels() {
    admin.textContent = t("admin"); manual?.labels();
    username.labels[0].textContent = t("username"); password.labels[0].textContent = t("password");
  }
  async function enter() {
    const version = epoch;
    try { authenticated(await request("GET", "/ext/auth/me")); await ctx.showApp(true); }
    catch (error) { if (version !== epoch) return; if (error.status !== 401) toast(error.message, true); ctx.showLogin(); }
  }
  $("login-form").onsubmit = async (event) => {
    event.preventDefault();
    const version = epoch;
    const submit = event.currentTarget.querySelector("button[type=\"submit\"]"); submit.disabled = true;
    try {
      const secret = password.value; password.value = "";
      const data = await request("POST", "/ext/auth/login", { username: username.value.trim(), password: secret });
      authenticated(data); $("login-err").textContent = ""; await ctx.showApp(true);
    } catch (error) { if (version === epoch) $("login-err").textContent = error.message; }
    finally { submit.disabled = false; }
  };
  $("logout-btn").onclick = async () => {
    const version = epoch;
    try { await manual.close(false); await request("POST", "/ext/auth/logout", {}, { expectedEpoch: version }); }
    catch (error) { if (version === epoch && error.status !== 401) toast(error.message, true); }
    finally { if (version === epoch) ctx.showLogin(); }
  };
  async function openAdmin() {
    const version = epoch;
    if (!isAdmin(version)) return;
    try {
      const [roster, settings] = await Promise.all([adminRequest("GET", "/ext/admin/users", undefined, version), adminRequest("GET", "/ext/admin/settings", undefined, version)]);
      if (!isAdmin(version)) return;
      modal((close) => {
        const limit = h("input", { id: "admin-limit", type: "number", min: 1, step: 1, required: true, value: settings.max_concurrent_profiles });
        const occupancy = h("p", { class: "data", id: "admin-occupancy" }, t("occupancy", { n: settings.occupied, limit: settings.max_concurrent_profiles }));
        const createName = h("input", { id: "admin-username", autocomplete: "off", required: true });
        const createPassword = h("input", { id: "admin-password", type: "password", autocomplete: "new-password", required: true });
        const createRole = roleSelect("admin-new-role", "user");
        return h("div", {}, modalHead(t("admin"), null, close), h("div", { class: "modal-body" },
          h("form", { onsubmit: async (event) => {
            event.preventDefault();
            if (!isAdmin(version) || !event.currentTarget.isConnected) return;
            try {
              const result = await adminRequest("PATCH", "/ext/admin/settings", { max_concurrent_profiles: Number(limit.value) }, version);
              if (!isAdmin(version) || !occupancy.isConnected) return;
              occupancy.textContent = t("occupancy", { n: result.occupied, limit: result.max_concurrent_profiles }); toast(t("saved"));
            } catch (error) { if (isAdmin(version)) toast(error.message, true); }
          } }, field(t("capacity"), limit, t("capacity_hint")), occupancy, h("button", { class: "btn btn-act" }, t("save"))),
          h("h3", {}, t("users")), h("div", { class: "admin-users" }, roster.users.map((entry) => userRow(entry, close))),
          h("form", { onsubmit: async (event) => {
            event.preventDefault();
            if (!isAdmin(version) || !event.currentTarget.isConnected) return;
            const secret = createPassword.value; createPassword.value = "";
            try { await adminRequest("POST", "/ext/admin/users", { username: createName.value.trim(), password: secret, role: createRole.value }, version); if (!isAdmin(version)) return; close(); await openAdmin(); }
            catch (error) { if (isAdmin(version)) toast(error.message, true); }
          } }, field(t("username"), createName), field(t("password"), createPassword), field(t("role"), createRole), h("button", { class: "btn btn-act" }, t("add_user")))));
      }, { wide: true });
    } catch (error) { if (isAdmin(version) && error.status !== 401) toast(error.message, true); }
  }
  function roleSelect(id, value) {
    return h("select", { id }, ["user", "admin"].map((role) => h("option", { value: role, selected: role === value }, t(role === "admin" ? "admin_role" : "user"))));
  }
  function userRow(entry, closeAdmin) {
    const version = epoch;
    const role = roleSelect("role-" + entry.user_id, entry.role);
    const active = h("input", { type: "checkbox", checked: entry.active, "aria-label": t("active") });
    const mutate = async (path, body, confirmKey) => {
      if (!isAdmin(version) || !row.isConnected || !(await confirmDialog(t(confirmKey, { name: entry.username }), t("save"), true)) || !isAdmin(version) || !row.isConnected) return;
      try { await adminRequest(path.endsWith("reset-password") ? "POST" : "PATCH", path, body, version); if (!isAdmin(version)) return; closeAdmin(); await openAdmin(); }
      catch (error) { if (isAdmin(version)) toast(error.message, true); }
    };
    const prefix = `/ext/admin/users/${encodeURIComponent(entry.user_id)}`;
    const row = h("div", { class: "admin-user" }, h("b", {}, entry.username), role,
      h("label", {}, active, " ", t("active")),
      h("button", { class: "btn btn-line", onclick: () => mutate(prefix, { role: role.value, active: active.checked }, "change_user") }, t("save")),
      h("button", { class: "btn btn-line", onclick: () => { if (!isAdmin(version) || !row.isConnected) return; passwordDialog(entry, (password) => mutate(prefix + "/reset-password", { password }, "reset_confirm")); } }, t("reset_password")),
      h("button", { class: "btn btn-line", onclick: () => { if (isAdmin(version) && row.isConnected) void assignmentDialog(entry, prefix, row); } }, t("assignments")));
    return row;
  }
  function passwordDialog(entry, save) {
    const version = epoch;
    modal((close) => {
      const password = h("input", { id: "reset-password", type: "password", required: true, autocomplete: "new-password" });
      return h("form", { onsubmit: (event) => { event.preventDefault(); if (!isAdmin(version) || !event.currentTarget.isConnected) return; const secret = password.value; password.value = ""; close(); void save(secret); } },
        modalHead(t("reset_password"), entry.username, close), h("div", { class: "modal-body" }, field(t("password"), password)), h("div", { class: "modal-foot" }, h("button", { class: "btn btn-act" }, t("save"))));
    });
  }
  async function assignmentDialog(entry, prefix, source) {
    const version = epoch;
    try {
      if (!isAdmin(version)) return;
      const [assigned, catalog] = await Promise.all([
        adminRequest("GET", prefix + "/profile-assignments", undefined, version),
        request("GET", "/profiles", undefined, { expectedEpoch: version }),
      ]);
      if (!isAdmin(version) || !source.isConnected) return;
      modal((close) => {
        const ids = new Set(assigned.profile_ids);
        const boxes = catalog.profiles.map((profile) => {
          const input = h("input", { type: "checkbox", checked: ids.has(profile.profile_id), value: profile.profile_id });
          return h("label", {}, input, profile.name);
        });
        return h("form", { onsubmit: async (event) => {
          event.preventDefault();
          const form = event.currentTarget;
          if (!isAdmin(version) || !form.isConnected || !(await confirmDialog(t("change_user", { name: entry.username }), t("save"), true)) || !isAdmin(version) || !form.isConnected) return;
          const selection = new Set(ids);
          for (const box of boxes) {
            const input = box.firstChild;
            if (input.checked) selection.add(input.value); else selection.delete(input.value);
          }
          try { await adminRequest("PUT", prefix + "/profile-assignments", { profile_ids: [...selection] }, version); if (!isAdmin(version)) return; close(); toast(t("saved")); }
          catch (error) { if (isAdmin(version)) toast(error.message, true); }
        } }, modalHead(t("assignments"), entry.username, close), h("div", { class: "modal-body admin-grants" }, boxes), h("div", { class: "modal-foot" }, h("button", { class: "btn btn-act" }, t("save"))));
      });
    } catch (error) { if (isAdmin(version)) toast(error.message, true); }
  }
  manual = createManualViewer({ ...ctx, scopedRequest() {
    const version = epoch;
    return (method, path, body) => request(method, path, body, { expectedEpoch: version });
  } });
  labels();
  return { personal, request, enter, labels, manual, errorMessage, isAdmin,
    clear() { ++epoch; manual.reset(); user = null; csrf = ""; password.value = ""; actor.textContent = ""; admin.classList.add("hidden"); $("mcp-btn").classList.add("hidden"); document.querySelector('a[href="/docs"]').classList.add("hidden"); ctx.reset(); },
  };
}
