// Only this Viewer is suspended; browser and agent lifetimes remain independent.
export function createManualViewer(ctx) {
  const { h, t, $ } = ctx;
  const open = h("button", { class: "btn btn-line", type: "button", id: "manual-open" });
  const back = h("button", { class: "btn btn-line hidden", type: "button", id: "manual-back" });
  const status = h("span", { role: "status", id: "manual-status" });
  const host = h("div", { id: "manual-screen", class: "hidden" });
  const bar = h("div", { class: "manual-bar" }, open, back, status);
  $("v-tabs").after(bar);
  bar.after(host);
  let generation = 0, current = null, closing = null, timer = null;
  function labels() { open.textContent = t("manual_open"); back.textContent = t("manual_back"); }
  labels();
  function disconnect(session) {
    if (session?.rfb) { session.rfb.viewOnly = true; session.rfb.disconnect(); }
  }
  function suspend(value) {
    ctx.suspend(value);
    $("v-screen").parentElement.classList.toggle("hidden", value);
    $("v-type").classList.toggle("hidden", value);
    $("v-tabs").classList.toggle("hidden", value);
    $("v-nav").inert = value;
    for (const id of ["v-back", "v-reload", "v-newtab"]) $(id).disabled = value;
    host.classList.toggle("hidden", !value);
    open.classList.toggle("hidden", value);
    back.classList.toggle("hidden", !value);
  }
  function close(restore = true) {
    if (closing) return closing;
    if (!current) return Promise.resolve();
    const version = ++generation, operation = current;
    current = null; clearInterval(timer); timer = null;
    back.disabled = true;
    if (operation.session?.rfb) operation.session.rfb.viewOnly = true;
    const cleanup = (async () => {
      let session = operation.session;
      try {
        session ||= await operation.opening;
        await operation.request("DELETE", `/ext/manual-sessions/${encodeURIComponent(session.session_id)}`, { browser_run_id: session.browser_run_id });
      } catch (error) {
        if (version === generation && error.status !== 401) status.textContent = error.message;
      } finally {
        disconnect(session);
        if (version === generation) {
          host.replaceChildren(); suspend(false); back.disabled = false; open.disabled = false;
          if (restore) await ctx.resume();
        }
      }
    })();
    closing = cleanup;
    void cleanup.finally(() => { if (closing === cleanup) closing = null; });
    return cleanup;
  }
  async function start() {
    if (current || closing) return;
    const profile = ctx.profile();
    if (!profile || profile.engine !== "camoufox" || profile.state !== "running") { status.textContent = t("manual_unsupported"); return; }
    const version = ++generation;
    const operation = { request: ctx.scopedRequest(), session: null, opening: null };
    current = operation;
    open.disabled = true; status.textContent = t("working"); suspend(true); back.disabled = false;
    try {
      operation.opening = operation.request("POST", `/ext/profiles/${encodeURIComponent(profile.profile_id)}/manual-sessions`, {});
      const session = await operation.opening;
      operation.session = session;
      if (version !== generation) return;
      const url = new URL(session.websocket_path, location.origin);
      if (url.origin !== location.origin || url.search || url.hash || url.pathname !== `/api/v1/ext/manual-sessions/${encodeURIComponent(session.session_id)}/ws`) throw new Error(t("manual_error"));
      url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const { default: RFB } = await import("/assets/ext/novnc/core/rfb.js");
      if (version !== generation) return;
      const rfb = new RFB(host, url.href);
      session.rfb = rfb;
      rfb.scaleViewport = true; rfb.resizeSession = false; rfb.viewOnly = false;
      rfb.addEventListener("connect", () => { if (version === generation) { status.textContent = t("manual_connected"); rfb.focus(); } });
      rfb.addEventListener("disconnect", () => { if (version === generation) { status.textContent = t("manual_disconnected"); void close(); } });
      rfb.addEventListener("securityfailure", () => { if (version === generation) { status.textContent = t("manual_error"); void close(); } });
      let checking = false;
      timer = setInterval(async () => {
        if (checking) return;
        checking = true;
        try {
          const state = await operation.request("GET", `/ext/manual-sessions/${encodeURIComponent(session.session_id)}`);
          if (version === generation && ["closed", "closing", "recovering"].includes(state.state)) { status.textContent = t("manual_disconnected"); void close(); }
        } catch (error) { if (version === generation) { status.textContent = error.message; void close(); } }
        finally { checking = false; }
      }, 2000);
    } catch (error) {
      if (version === generation) { status.textContent = error.message; await close(); }
    }
  }
  open.onclick = start;
  back.onclick = () => close();
  return {
    close, labels,
    reset() {
      ++generation; clearInterval(timer); timer = null;
      disconnect(current?.session); current = null; closing = null;
      host.replaceChildren(); suspend(false); open.disabled = false; back.disabled = false; status.textContent = "";
    },
  };
}
