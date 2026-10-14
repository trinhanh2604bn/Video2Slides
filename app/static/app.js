"use strict";

const $ = (id) => document.getElementById(id);

const ALL_STAGES = [
  ["uploading", "Tải video lên"],
  ["extracting_audio", "Tách âm thanh & khung hình"],
  ["transcribing", "Nhận dạng giọng nói"],
  ["analyzing", "Phân tích nội dung & lập dàn ý bằng AI"],
  ["writing_slides", "Viết nội dung slide & ghi chú"],
  ["planning_illustrations", "Lập kế hoạch ảnh minh họa"],
  ["generating_images", "Tạo ảnh minh họa AI (GPU)"],
  ["generating", "Dựng bố cục slide"],
  ["finalizing", "Hoàn thiện & kiểm tra PowerPoint"],
  ["exporting_google", "Tạo Google Slides & ghi chú thuyết trình"],
  ["completed", "Hoàn tất"],
];
const IMAGE_STAGES = new Set(["planning_illustrations", "generating_images"]);
let STAGES = ALL_STAGES;
function useStages(images, google) {
  STAGES = ALL_STAGES.filter(([k]) => (images || !IMAGE_STAGES.has(k)) && (google || k !== "exporting_google"));
}
useStages(false, false);

const STYLE_INFO = {
  professional: { desc: "Tối giản, sang trọng cho doanh nghiệp", bg: "#ffffff", title: "#1E3A5F", accent: "#2F80ED", border: "#e3e8f0" },
  academic: { desc: "Trang trọng, rõ ràng cho giáo dục", bg: "#FBFAF6", title: "#6B1D2A", accent: "#B08D57", border: "#e8e1d2" },
  creative: { desc: "Hiện đại, nổi bật cho marketing", bg: "#111827", title: "#F9FAFB", accent: "#F472B6", border: "#111827" },
};
const LAYOUT_NAMES = {
  title: "Tiêu đề", section: "Phân đoạn", bullets: "Gạch đầu dòng", image_left: "Ảnh trái",
  image_right: "Ảnh phải", two_column: "So sánh 2 cột", process: "Quy trình", timeline: "Dòng thời gian",
  cards: "Thẻ lưới", stats: "Số liệu", summary: "Tổng kết", agenda: "Mục lục", concept: "Khái niệm",
  key_insight: "Điểm cốt lõi", conclusion: "Kết luận",
};
const LANG_NAMES = { vi: "Tiếng Việt", en: "English" };
const PLATFORM_NAMES = { youtube: "YouTube", facebook: "Facebook" };
const GOOGLE_STEPS = { uploading: "đang tải lên Google Drive", validating: "đang kiểm tra bản chuyển đổi", notes: "đang ghi chú thuyết trình" };
const VIEWS = ["authView", "uploadView", "processView", "resultView", "readingView", "libraryView"];

const state = {
  config: null, file: null, duration: null, jobId: null, pollTimer: null, job: null,
  mode: "file", fileReady: false, urlCheck: null, source: null, google: null, googleTimer: null,
};

// ------------------------------------------------------------------ storage (best effort)
const store = {
  get(k, session) { try { return (session ? sessionStorage : localStorage).getItem(k); } catch { return null; } },
  set(k, v, session) { try { (session ? sessionStorage : localStorage).setItem(k, v); } catch { /* ignore */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

// ------------------------------------------------------------------ theme (system / light / dark)
// The <head> script applies the saved choice before first paint; this keeps it live afterwards.
const THEME_LABELS = { system: "Theo hệ thống", light: "Sáng", dark: "Tối" };
const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function applyTheme(pref) {
  if (!THEME_LABELS[pref]) pref = "light";
  const dark = pref === "dark" || (pref === "system" && Boolean(darkQuery && darkQuery.matches));
  const root = document.documentElement;
  root.setAttribute("data-theme", dark ? "dark" : "light");
  root.setAttribute("data-theme-pref", pref);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? "#1B1A17" : "#F3F1E8";
  $("themeBtn").setAttribute("aria-label", `Giao diện: ${THEME_LABELS[pref]}`);
  $("themeBtn").title = `Giao diện: ${THEME_LABELS[pref]}`;
  for (const b of $("themeList").querySelectorAll("[data-theme-option]")) {
    b.setAttribute("aria-checked", String(b.dataset.themeOption === pref));
  }
}

function setTheme(pref) {
  store.set("theme", pref);
  applyTheme(pref);
}

function themePref() {
  return document.documentElement.getAttribute("data-theme-pref") || "light";
}

function toggleThemeMenu(open, focusItem = false) {
  const list = $("themeList");
  list.hidden = !open;
  $("themeBtn").setAttribute("aria-expanded", String(open));
  if (open && focusItem) (list.querySelector('[aria-checked="true"]') || list.querySelector("button")).focus();
}

function setupTheme() {
  applyTheme(store.get("theme") || "light");
  const btn = $("themeBtn"), list = $("themeList");
  btn.addEventListener("click", () => toggleThemeMenu(list.hidden, true));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); e.stopPropagation(); toggleThemeMenu(true, true); }
  });
  list.addEventListener("click", (e) => {
    const opt = e.target.closest("[data-theme-option]");
    if (!opt) return;
    setTheme(opt.dataset.themeOption);
    toggleThemeMenu(false);
    btn.focus();
  });
  list.addEventListener("keydown", (e) => {
    const items = [...list.querySelectorAll("button")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Home") { e.preventDefault(); items[0].focus(); }
    else if (e.key === "End") { e.preventDefault(); items[items.length - 1].focus(); }
    else if (e.key === "Escape" || e.key === "Tab") { toggleThemeMenu(false); if (e.key === "Escape") btn.focus(); }
    e.stopPropagation();
  });
  document.addEventListener("click", (e) => { if (!list.hidden && !$("themeMenu").contains(e.target)) toggleThemeMenu(false); });
  // Follow the OS setting live while "system" is selected.
  if (darkQuery) {
    const onChange = () => { if (themePref() === "system") applyTheme("system"); };
    if (darkQuery.addEventListener) darkQuery.addEventListener("change", onChange);
    else if (darkQuery.addListener) darkQuery.addListener(onChange);
  }
  // Another tab changed the theme.
  window.addEventListener("storage", (e) => { if (e.key === "theme") applyTheme(e.newValue || "light"); });
}

// ------------------------------------------------------------------ API helpers
function authHeaders() {
  const pw = store.get("appPassword", true);
  return pw ? { "X-App-Password": pw } : {};
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
  if (res.status === 401) { showView("authView"); throw new Error("Cần mật khẩu truy cập."); }
  if (!res.ok) {
    let detail = `Lỗi máy chủ (HTTP ${res.status})`;
    try { const j = await res.json(); if (j.detail) detail = typeof j.detail === "string" ? j.detail : detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res;
}

function postJson(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

// Images go through fetch so the optional password header is sent; cached as object URLs.
const blobCache = new Map();
function imageUrl(url) {
  if (!blobCache.has(url)) {
    const p = api(url).then((r) => r.blob()).then((b) => URL.createObjectURL(b));
    p.catch(() => blobCache.delete(url));
    blobCache.set(url, p);
  }
  return blobCache.get(url);
}
function clearImages() {
  for (const p of blobCache.values()) p.then((u) => URL.revokeObjectURL(u)).catch(() => {});
  blobCache.clear();
}

// ------------------------------------------------------------------ views
function showView(id) {
  const changed = $(id).hidden;
  for (const v of VIEWS) $(v).hidden = v !== id;
  document.body.classList.toggle("wide", id === "readingView");
  if (id !== "processView") { stopClock(); setTabTitle(""); }
  if (id !== "readingView" && document.fullscreenElement) document.exitFullscreen().catch(() => {});
  if (changed) window.scrollTo(0, 0);
}

function fmtSize(bytes) {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`;
}
function fmtDuration(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m ? `${m} phút ${String(s).padStart(2, "0")} giây` : `${s} giây`;
}
function fmtClock(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
function fmtRemaining(sec) {
  if (!isFinite(sec)) return "—";
  if (sec < 60) return "dưới 1 phút";
  if (sec < 3600) return `~${Math.ceil(sec / 60)} phút`;
  return `~${Math.floor(sec / 3600)} giờ ${Math.round((sec % 3600) / 60)} phút`;
}
function fmtDate(ts) {
  try { return new Date(ts * 1000).toLocaleString("vi-VN", { dateStyle: "short", timeStyle: "short" }); } catch { return ""; }
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// ------------------------------------------------------------------ setup
async function init() {
  try {
    state.config = await (await fetch("/api/config", { headers: authHeaders() })).json();
  } catch {
    $("fileError").textContent = "Không kết nối được máy chủ.";
    $("fileError").hidden = false;
    showView("uploadView");
    return;
  }
  const c = state.config;
  $("limitsText").textContent = `MP4 · tối đa ${c.max_upload_mb} MB · tối đa ${Math.round(c.max_duration_sec / 60)} phút`;

  renderProviderBadge(c.llm || { provider: c.ai_provider });

  const ui = c.url_import || { enabled: false };
  $("sourceTabs").hidden = !ui.enabled;
  $("pasteTip").hidden = !ui.enabled;
  if (ui.enabled) $("urlLimits").textContent = `${Math.round(ui.max_duration_sec / 60)} phút, ${ui.max_download_mb} MB`;

  setupAiImages(c.ai_images || {});
  const dm = c.default_layout_mode || "presentation";
  const radio = document.querySelector(`input[name="layoutMode"][value="${dm}"]`);
  if (radio) radio.checked = true;
  setGoogle(c.google);

  for (const [code, name] of Object.entries(c.languages)) {
    $("languageSelect").add(new Option(name, code, false, code === c.default_language));
  }
  for (let n = 5; n <= 40; n++) $("slideCountSelect").add(new Option(`${n} slide`, String(n)));

  const wrap = $("styleOptions");
  for (const [key, label] of Object.entries(c.styles)) {
    const info = STYLE_INFO[key] || { desc: "", bg: "#fff", title: "#333", accent: "#888", border: "#ddd" };
    const lab = document.createElement("label");
    lab.className = "style-opt";
    lab.innerHTML = `
      <input type="radio" name="style" value="${key}" ${key === c.default_style ? "checked" : ""}>
      <span class="style-card">
        <span class="swatch" style="background:${info.bg};border:1px solid ${info.border};--sw-title:${info.title};--sw-accent:${info.accent}"></span>
        <span class="style-name"></span>
        <span class="style-desc"></span>
      </span>`;
    lab.querySelector(".style-name").textContent = label;
    lab.querySelector(".style-desc").textContent = info.desc;
    wrap.appendChild(lab);
  }

  if (c.media_processing === "modal") {
    $("footText").textContent = "Video được xử lý trên máy chủ đám mây riêng của ứng dụng và tự động xóa sau khi hoàn tất.";
  }
  restorePrefs();
  updateSubmit();

  if (c.password_required && !store.get("appPassword", true)) { showView("authView"); return; }
  route();
}

// Remember the last choices so a returning user only has to pick a video.
const PREF_KEY = "prefs";
function restorePrefs() {
  let p;
  try { p = JSON.parse(store.get(PREF_KEY) || "{}"); } catch { p = {}; }
  const pick = (sel, v) => { if (v != null && [...sel.options].some((o) => o.value === v)) sel.value = v; };
  pick($("languageSelect"), p.language);
  pick($("imageDensity"), p.image_density);
  for (const name of ["style", "layoutMode"]) {
    const v = p[name === "style" ? "style" : "layout_mode"];
    const r = v && document.querySelector(`input[name="${name}"][value="${v}"]`);
    if (r) r.checked = true;
  }
  if (typeof p.ai_images === "boolean" && !$("aiImagesCheck").disabled) {
    $("aiImagesCheck").checked = p.ai_images;
    $("aiImagesCheck").dispatchEvent(new Event("change"));
  }
}
function savePrefs(opts) {
  const { language, style, layout_mode, image_density, ai_images } = opts;
  store.set(PREF_KEY, JSON.stringify({ language, style, layout_mode, image_density, ai_images }));
}

function setupAiImages(ai) {
  if (!ai.enabled) return;
  $("aiImagesField").hidden = false;
  const hint = $("aiImagesHint");
  if (!ai.available) {
    $("aiImagesCheck").checked = false;
    $("aiImagesCheck").disabled = true;
    $("imageDensity").disabled = true;
    hint.textContent = ai.reason || "Hiện không thể tạo ảnh AI.";
    return;
  }
  hint.textContent = `Tối đa ${ai.max_per_presentation} ảnh mỗi bài. Ảnh chỉ chứa hình, mọi chữ trên slide vẫn chỉnh sửa được.`
    + (ai.placeholder ? " (Chế độ thử nghiệm: ảnh giả lập, không phải AI.)" : "");
  const sync = () => { $("imageDensity").disabled = !$("aiImagesCheck").checked; };
  $("aiImagesCheck").addEventListener("change", sync);
  sync();
}

function aiImagesChosen() {
  return !$("aiImagesField").hidden && $("aiImagesCheck").checked && !$("aiImagesCheck").disabled;
}
function layoutModeChosen() {
  return (document.querySelector('input[name="layoutMode"]:checked') || {}).value || "presentation";
}
function googleChosen() {
  return !$("googleField").hidden && $("googleCheck").checked && !$("googleCheck").disabled;
}

// Routes: #job=<id> (processing/result), #read=<id>&s=<n> (Reading Mode), #library, #google=connected|error
function route() {
  const params = new URLSearchParams(location.hash.slice(1));
  const g = params.get("google");
  if (g) {
    refreshGoogle().then(() => {
      if (g === "connected" && state.google && state.google.connected) toast("Đã kết nối tài khoản Google.");
      else if (g === "error") toast("Chưa kết nối được tài khoản Google. Hãy thử lại.", true);
    });
    history.replaceState(null, "", location.pathname + (store.get("lastJob") ? `#job=${store.get("lastJob")}` : ""));
    return route();
  }
  const read = params.get("read");
  if (read && /^[0-9a-f]{32}$/.test(read)) {
    openReader(read, Math.max(0, (parseInt(params.get("s"), 10) || 1) - 1), false);
    return;
  }
  if (params.has("library")) { openLibrary(false); return; }
  const jobId = params.get("job") || store.get("lastJob");
  if (jobId && /^[0-9a-f]{32}$/.test(jobId)) {
    state.jobId = jobId;
    showView("processView");
    renderStages("queued");
    poll();
  } else {
    showView("uploadView");
  }
}

let toastTimer = null;
function toast(msg, warn = false) {
  let t = $("toast");
  if (!t) {
    t = el("div", "toast");
    t.id = "toast";
    t.setAttribute("role", "status");
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.toggle("warn", warn);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

// ------------------------------------------------------------------ Google account
function setGoogle(g) {
  state.google = g || null;
  const chip = $("googleChip");
  const field = $("googleField");
  if (!g || !g.enabled) { chip.hidden = true; field.hidden = true; return; }
  chip.hidden = false;
  chip.classList.toggle("connected", Boolean(g.connected));
  chip.classList.toggle("warn", !g.configured || g.last_error === "revoked");
  const acct = g.account && (g.account.email || g.account.name);
  $("googleChipText").textContent = g.connected ? (acct || "Google đã kết nối")
    : g.configured ? "Kết nối Google" : "Google chưa cấu hình";
  chip.title = g.connected ? "Bấm để ngắt kết nối tài khoản Google" : g.configured
    ? "Đăng nhập Google để tạo Google Slides trong Drive của bạn" : "Xem docs/GOOGLE_SLIDES_SETUP_VI.md để cấu hình";
  field.hidden = false;
  const check = $("googleCheck");
  check.disabled = !g.connected;
  if (!g.connected) check.checked = false;
  else if (store.get("googleAuto") !== "0") check.checked = true;
  $("googleHint").textContent = g.connected
    ? "Bài trình chiếu được tạo trong Google Drive của bạn (riêng tư, không chia sẻ công khai). Ứng dụng chỉ truy cập các tệp do chính nó tạo."
    : g.configured
      ? (g.last_error === "revoked" ? "Quyền truy cập Google đã bị thu hồi hoặc hết hạn — bấm 'Kết nối Google' ở góc trên để kết nối lại."
        : "Bấm 'Kết nối Google' ở góc trên để bật tùy chọn này. Tệp PowerPoint vẫn luôn được tạo.")
      : "Chưa cấu hình OAuth client của Google trên máy này (xem docs/GOOGLE_SLIDES_SETUP_VI.md). Tệp PowerPoint vẫn luôn được tạo.";
}

async function refreshGoogle() {
  try { setGoogle(await (await api("/api/google/status")).json()); } catch { /* keep previous */ }
}

async function googleChipClick() {
  const g = state.google || {};
  if (!g.configured) {
    toast("Chưa cấu hình Google: tạo OAuth client loại 'Desktop app' và đặt tệp JSON theo hướng dẫn trong docs/GOOGLE_SLIDES_SETUP_VI.md.", true);
    return;
  }
  if (g.connected) {
    const who = (g.account && g.account.email) || "tài khoản Google";
    if (!window.confirm(`Ngắt kết nối ${who}? Các bài đã tạo trong Google Drive vẫn được giữ nguyên.`)) return;
    try { setGoogle(await (await postJson("/api/google/disconnect")).json()); toast("Đã ngắt kết nối Google."); }
    catch (e) { toast(e.message, true); }
    if (state.job) renderGoogleBox(state.job);
    return;
  }
  connectGoogle();
}

async function connectGoogle() {
  try {
    const { auth_url, manual } = await (await postJson("/api/google/connect")).json();
    if (!manual) { window.location.assign(auth_url); return; }
    // Desktop OAuth client on a public host (Modal): Google cannot come back here, the user pastes the address.
    window.open(auth_url, "_blank", "noopener");
    $("googlePasteOpen").href = auth_url;
    $("googlePasteInput").value = "";
    $("googlePasteError").hidden = true;
    $("googlePasteDialog").showModal();
  } catch (e) {
    toast(e.message, true);
  }
}

async function finishGooglePaste(e) {
  e.preventDefault();
  const btn = $("googlePasteSubmit");
  btn.disabled = true;
  try {
    setGoogle(await (await postJson("/api/google/oauth/complete", { url: $("googlePasteInput").value })).json());
    $("googlePasteDialog").close();
    toast("Đã kết nối tài khoản Google.");
    if (state.job) renderGoogleBox(state.job);
  } catch (err) {
    $("googlePasteError").textContent = err.message;
    $("googlePasteError").hidden = false;
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------ file selection
function setFileError(msg) {
  $("fileError").textContent = msg || "";
  $("fileError").hidden = !msg;
}

function selectFile(file) {
  setFileError("");
  state.file = null; state.duration = null;
  setFileReady(false);
  if (!file) return;
  const c = state.config;
  if (!/\.mp4$/i.test(file.name) && file.type !== "video/mp4") return setFileError("Chỉ hỗ trợ tệp MP4.");
  if (file.size > c.max_upload_mb * 1048576) return setFileError(`Tệp ${fmtSize(file.size)} vượt quá giới hạn ${c.max_upload_mb} MB.`);
  if (file.size === 0) return setFileError("Tệp rỗng.");

  state.file = file;
  $("fileName").textContent = file.name;
  $("fileDetails").textContent = `${fmtSize(file.size)} · đang đọc thời lượng…`;
  $("dropzone").hidden = true;
  $("fileInfo").hidden = false;

  // Read duration locally before uploading, so oversize videos are rejected early.
  const url = URL.createObjectURL(file);
  const v = document.createElement("video");
  v.preload = "metadata";
  v.onloadedmetadata = () => {
    URL.revokeObjectURL(url);
    state.duration = v.duration;
    $("fileDetails").textContent = `${fmtSize(file.size)} · ${fmtDuration(v.duration)}`;
    if (v.duration > c.max_duration_sec) {
      setFileError(`Video dài ${fmtDuration(v.duration)}, vượt giới hạn ${Math.round(c.max_duration_sec / 60)} phút.`);
      return;
    }
    // Same rule as app/ai/base.py recommend_slide_count (long videos: +1 slide per ~4 min, max 40)
    const est = Math.round(v.duration / 50) + 2;
    const rec = est <= 15 ? Math.max(6, est) : Math.max(15, Math.min(40, 15 + Math.round((v.duration / 60 - 10) / 4)));
    $("slideHint").textContent = `Đề xuất khoảng ${rec} slide cho video này.`;
    setFileReady(true);
  };
  v.onerror = () => {
    URL.revokeObjectURL(url);
    // The browser may lack a codec; the server re-validates anyway.
    $("fileDetails").textContent = `${fmtSize(file.size)} · không đọc được thời lượng trên trình duyệt`;
    setFileReady(true);
  };
  v.src = url;
}

function clearFile() {
  state.file = null;
  $("fileInput").value = "";
  $("fileInfo").hidden = true;
  $("dropzone").hidden = false;
  setFileReady(false);
  $("slideHint").textContent = "";
  setFileError("");
}

function setFileReady(ready) {
  state.fileReady = ready;
  updateSubmit();
}

function updateSubmit() {
  const ok = state.mode === "url"
    ? Boolean(state.urlCheck && state.urlCheck.retrieval_permitted && $("rightsCheck").checked)
    : state.fileReady;
  $("submitBtn").disabled = !ok;
  // Say what is still missing instead of leaving a silently disabled button.
  let note = "Tệp PowerPoint (.pptx) luôn được tạo; Google Slides là tùy chọn.";
  if (!ok && state.mode === "url") {
    note = !state.urlCheck ? "Dán liên kết rồi bấm “Kiểm tra URL” để tiếp tục."
      : state.urlCheck.retrieval_permitted ? "Đánh dấu ô xác nhận quyền sử dụng video để tiếp tục."
        : "Liên kết này không tải được — hãy tải tệp MP4 lên thay thế.";
  } else if (!ok) {
    note = state.file ? "Video chưa hợp lệ — xem thông báo ở trên." : "Chọn một video ở bước 01 để bắt đầu.";
  }
  $("actionsNote").textContent = note;
}

// ------------------------------------------------------------------ source tabs + URL import
function setMode(mode) {
  state.mode = mode;
  $("tabFile").setAttribute("aria-selected", String(mode === "file"));
  $("tabUrl").setAttribute("aria-selected", String(mode === "url"));
  // Roving tabindex: only the selected tab is in the Tab order; arrow keys move between tabs.
  $("tabFile").tabIndex = mode === "file" ? 0 : -1;
  $("tabUrl").tabIndex = mode === "url" ? 0 : -1;
  $("filePanel").hidden = mode !== "file";
  $("urlPanel").hidden = mode !== "url";
  updateSubmit();
}

// Client-side hint only; the server does the real (strict) validation.
function detectPlatform(text) {
  let host;
  try { host = new URL(/^[a-z]+:\/\//i.test(text) ? text : `https://${text}`).hostname.toLowerCase(); } catch { return null; }
  if (/(^|\.)(youtube\.com|youtu\.be)$/.test(host)) return "youtube";
  if (/(^|\.)(facebook\.com|fb\.watch)$/.test(host)) return "facebook";
  return null;
}

function setUrlError(msg) {
  $("urlError").textContent = msg || "";
  $("urlError").hidden = !msg;
}

function resetUrlCheck() {
  state.urlCheck = null;
  $("videoCard").hidden = true;
  $("urlStatus").hidden = true;
  $("consentRow").hidden = true;
  $("useUploadBtn").hidden = true;
  $("rightsCheck").checked = false;
  setUrlError("");
  updateSubmit();
}

function onUrlInput() {
  const text = $("urlInput").value.trim();
  const platform = detectPlatform(text);
  const chip = $("platformChip");
  chip.hidden = !platform;
  chip.className = `platform-chip ${platform || ""}`;
  chip.textContent = PLATFORM_NAMES[platform] || "";
  $("validateUrlBtn").disabled = !text;
  if (state.urlCheck) resetUrlCheck();
}

async function validateUrl() {
  const url = $("urlInput").value.trim();
  if (!url) return;
  resetUrlCheck();
  const btn = $("validateUrlBtn");
  btn.disabled = true;
  btn.textContent = "Đang kiểm tra…";
  $("actionsNote").textContent = "Đang kiểm tra liên kết…";
  try {
    renderUrlCheck(await (await postJson("/api/videos/url/validate", { url })).json());
  } catch (e) {
    setUrlError(e.message);
    $("useUploadBtn").hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Kiểm tra URL";
  }
}

function renderUrlCheck(info) {
  state.urlCheck = info;
  const card = $("videoCard");
  const tag = $("videoPlatform");
  tag.className = `platform-tag ${info.platform}`;
  tag.textContent = info.platform_label || PLATFORM_NAMES[info.platform] || info.platform;
  $("videoTitle").textContent = info.title || "Không lấy được tiêu đề video";
  $("videoAuthor").textContent = info.author ? `Đăng bởi ${info.author}` : "";
  $("videoDuration").textContent = info.duration_seconds
    ? `Thời lượng: ${fmtDuration(info.duration_seconds)}` : "Thời lượng: chưa xác định";
  const link = $("videoLink");
  link.href = info.url;
  link.textContent = info.url;
  const img = $("videoThumb");
  card.classList.toggle("no-thumb", !info.thumbnail);
  if (info.thumbnail) {
    img.onerror = () => card.classList.add("no-thumb");
    img.src = info.thumbnail;
  } else {
    img.removeAttribute("src");
  }
  card.hidden = false;

  const st = $("urlStatus");
  st.className = `access ${info.retrieval_permitted ? "ok" : "warn"}`;
  st.textContent = info.message;
  st.hidden = false;
  $("consentRow").hidden = !info.retrieval_permitted;
  $("useUploadBtn").hidden = info.retrieval_permitted;
  if (!$("titleInput").value && info.title) $("titleInput").placeholder = info.title;
  updateSubmit();
}

function commonOptions() {
  return {
    title: $("titleInput").value.trim(),
    language: $("languageSelect").value,
    style: (document.querySelector('input[name="style"]:checked') || {}).value || "professional",
    slide_count: Number($("slideCountSelect").value) || 0,
    ai_images: aiImagesChosen(),
    image_density: $("imageDensity").value,
    layout_mode: layoutModeChosen(),
    google_slides: googleChosen(),
  };
}

async function importUrl() {
  const info = state.urlCheck;
  if (!info || !info.retrieval_permitted || !$("rightsCheck").checked) return;
  const opts = commonOptions();
  savePrefs(opts);
  state.source = { type: "url", platform: info.platform };
  useStages(opts.ai_images, opts.google_slides);
  showView("processView");
  $("processTitle").textContent = "Đang xử lý video";
  $("processFile").textContent = info.title || info.url;
  $("processError").hidden = true;
  renderStages("uploading");
  startClock(Date.now() / 1000);
  try {
    const body = await (await postJson("/api/videos/url/import", { url: info.url, rights_confirmed: true, ...opts })).json();
    followJob(body.job_id);
  } catch (e) {
    showError(e.message, false);
  }
}

function followJob(jobId) {
  state.jobId = jobId;
  store.set("lastJob", jobId);
  history.replaceState(null, "", `#job=${jobId}`);
  poll();
}

// ------------------------------------------------------------------ upload
// The file goes up in small PUTs: a hosted web request is time-limited (Modal: 150 s), so one big POST fails
// on a slow connection. Chunk size adapts to the measured speed (~CHUNK_TARGET_SEC per request) and a failed
// chunk is retried from the byte count the server reports.
const CHUNK_MIN = 1 << 20, CHUNK_START = 4 << 20, CHUNK_TARGET_SEC = 30, CHUNK_RETRIES = 6;

async function upload(ev) {
  ev.preventDefault();
  if (state.mode === "url") { importUrl(); return; }
  if (!state.file) return;
  const file = state.file;
  state.source = { type: "upload" };
  const opts = commonOptions();
  store.set("googleAuto", opts.google_slides ? "1" : "0");
  savePrefs(opts);
  useStages(opts.ai_images, opts.google_slides);

  showView("processView");
  $("processTitle").textContent = "Đang xử lý video";
  $("processFile").textContent = file.name;
  $("processError").hidden = true;
  renderStages("uploading");
  startClock(Date.now() / 1000);
  setAway("upload");
  state.uploading = true;

  try {
    const start = await (await postJson("/api/uploads", { filename: file.name, size: file.size })).json();
    await sendChunks(file, start);
    setStageMeta("uploading", "đang kiểm tra tệp…");
    const body = await (await postJson(`/api/uploads/${start.upload_id}/finish`, opts)).json();
    state.uploading = false;
    followJob(body.job_id);
  } catch (e) {
    state.uploading = false;
    if ($("authView").hidden) showError(e.message, false);  // a 401 already switched to the password view
  }
}

async function sendChunks(file, start) {
  const maxChunk = start.max_chunk_bytes || 64 << 20;
  let offset = start.received || 0, size = CHUNK_START, failures = 0;
  state.upRate = { t0: performance.now(), from: offset };
  while (offset < file.size) {
    const end = Math.min(file.size, offset + size);
    const t0 = performance.now();
    let res;
    try {
      res = await putChunk(start.upload_id, offset, file.slice(offset, end), file.size);
    } catch (netErr) {
      res = { status: 0 };
    }
    if (res.status === 401) { showView("authView"); throw new Error("auth"); }
    if (res.status === 200) {
      const bps = (end - offset) / Math.max(0.5, (performance.now() - t0) / 1000);
      offset = res.body.received;
      failures = 0;
      // Aim for ~CHUNK_TARGET_SEC per request, well under the 150 s limit.
      size = Math.max(CHUNK_MIN, Math.min(maxChunk, Math.round((bps * CHUNK_TARGET_SEC) / CHUNK_MIN) * CHUNK_MIN));
      continue;
    }
    if (res.status === 409 && res.body && res.body.detail && typeof res.body.detail.received === "number") {
      offset = res.body.detail.received;  // server already has more/less: resume from there
      continue;
    }
    if (res.status >= 400 && res.status < 500 && res.status !== 408 && res.status !== 429) {
      throw new Error(typeof res.body?.detail === "string" ? res.body.detail : `Tải lên thất bại (HTTP ${res.status}).`);
    }
    // Network error, timeout or 5xx: smaller chunk, back off, then ask the server where to resume.
    if (++failures > CHUNK_RETRIES) throw new Error("Mất kết nối khi tải lên. Vui lòng kiểm tra mạng và thử lại.");
    size = Math.max(CHUNK_MIN, Math.floor(size / 2));
    setStageMeta("uploading", `${Math.floor((offset / file.size) * 100)}% · mạng chập chờn, thử lại (${failures}/${CHUNK_RETRIES})…`);
    await new Promise((r) => setTimeout(r, Math.min(30000, 2000 * 2 ** (failures - 1))));
    try {
      const st = await (await api(`/api/uploads/${start.upload_id}`)).json();
      offset = st.received;
    } catch (e) {
      if (/hết hạn/.test(e.message)) throw e;
    }
  }
}

// One chunk via XHR (byte-level progress; fetch has none). Resolves with {status, body}; rejects on network error.
function putChunk(uploadId, offset, blob, total) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/uploads/${uploadId}?offset=${offset}`);
    for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.timeout = 140000;  // just under the hosting limit, so a stalled chunk is retried instead of hanging
    xhr.upload.onprogress = (e) => {
      const sent = offset + e.loaded;
      let text = `${Math.floor((sent / total) * 100)}% · ${fmtSize(sent)} / ${fmtSize(total)}`;
      // Average speed since the upload began (smoother than per-chunk) and the time it implies.
      const r = state.upRate, secs = r ? (performance.now() - r.t0) / 1000 : 0;
      if (r && secs > 3 && sent > r.from) {
        const bps = (sent - r.from) / secs;
        text += ` · ${fmtSize(bps)}/s · còn ${fmtRemaining((total - sent) / bps)}`;
      }
      setStageMeta("uploading", text);
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => reject(new Error("network"));
    xhr.ontimeout = () => reject(new Error("timeout"));
    xhr.send(blob);
  });
}

// ------------------------------------------------------------------ progress: clock, tab title, "you can leave", notify
const BASE_TITLE = document.title;

function fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mmss = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return h ? `${h}:${mmss}` : mmss;
}

function startClock(fromTs) {
  if (state.clockStart === fromTs && state.clockTimer) return;
  stopClock();
  state.clockStart = fromTs;
  const tick = () => {
    const sec = Date.now() / 1000 - state.clockStart;
    $("processElapsedTime").textContent = fmtElapsed(sec);
    $("processElapsed").setAttribute("aria-label", `Đã chạy ${fmtDuration(sec)}`);
  };
  tick();
  $("processElapsed").hidden = false;
  state.clockTimer = setInterval(tick, 1000);
}

function stopClock() {
  clearInterval(state.clockTimer);
  state.clockTimer = null;
}

function setAway(kind) {
  const box = $("awayNote");
  if (!kind) { box.hidden = true; return; }
  $("awayText").textContent = kind === "upload"
    ? "Giữ tab này mở cho tới khi tải lên xong. Sau đó bạn có thể đóng tab — máy chủ vẫn tiếp tục xử lý, kết quả nằm trong “Bài đã tạo”."
    : "Bạn có thể đóng tab này — máy chủ vẫn tiếp tục xử lý. Mở lại trang hoặc vào “Bài đã tạo” để xem kết quả.";
  box.hidden = false;
  renderNotifyBtn();
}

function setTabTitle(prefix) {
  document.title = prefix ? `${prefix} · ${BASE_TITLE}` : BASE_TITLE;
}

const canNotify = () => "Notification" in window && window.isSecureContext;
function renderNotifyBtn() {
  const b = $("notifyBtn");
  b.hidden = !canNotify() || Notification.permission === "denied";
  if (b.hidden) return;
  const on = Notification.permission === "granted" && store.get("notify") === "1";
  b.setAttribute("aria-pressed", String(on));
  b.textContent = on ? "✓ Sẽ báo khi xong" : "Báo cho tôi khi xong";
}

async function toggleNotify() {
  if (!canNotify()) return;
  if (Notification.permission === "granted" && store.get("notify") === "1") store.set("notify", "0");
  else if ((await Notification.requestPermission()) === "granted") store.set("notify", "1");
  renderNotifyBtn();
}

// Called once when a job this tab was watching finishes: tab title, toast and (opt-in) a system notification.
function announceDone(job, ok) {
  const title = (job.result && job.result.title) || job.filename || "Bài trình chiếu";
  if (document.hidden) {
    setTabTitle(ok ? "✓ Đã xong" : "⚠ Lỗi");
    const reset = () => { if (!document.hidden) { setTabTitle(""); document.removeEventListener("visibilitychange", reset); } };
    document.addEventListener("visibilitychange", reset);
  }
  if (ok) toast(`Đã xong: ${title}`);
  if (canNotify() && Notification.permission === "granted" && store.get("notify") === "1" && document.hidden) {
    try {
      const n = new Notification(ok ? "Bài trình chiếu đã sẵn sàng" : "Không thể hoàn tất bài trình chiếu", { body: title, tag: job.id });
      n.onclick = () => { window.focus(); n.close(); };
    } catch { /* some browsers only allow notifications from a service worker */ }
  }
}

// ------------------------------------------------------------------ progress
function renderStages(current, failed = false, stageTimes = {}) {
  const list = $("stageList");
  list.innerHTML = "";
  const idx = STAGES.findIndex(([k]) => k === current);
  STAGES.forEach(([key, label], i) => {
    const li = el("li", "stage");
    li.dataset.stage = key;
    let mark = String(i + 1);
    if (current === "completed" || i < idx) { li.classList.add("done"); mark = "✓"; }
    else if (i === idx) { li.classList.add(failed ? "failed" : "active"); if (failed) mark = "!"; }
    if (key === "completed" && current === "completed") { li.classList.add("done"); }
    li.innerHTML = `<span class="dot"></span><span class="label"></span><span class="meta"></span>`;
    li.querySelector(".dot").textContent = li.classList.contains("active") ? "" : mark;
    li.querySelector(".label").textContent = key === "uploading" && state.source && state.source.type === "url"
      ? `Tải video từ ${PLATFORM_NAMES[state.source.platform] || "URL"}`
      : label;
    const nextKey = STAGES[i + 1] && STAGES[i + 1][0];
    if (stageTimes[key] && stageTimes[nextKey]) {
      li.querySelector(".meta").textContent = `${Math.max(1, Math.round(stageTimes[nextKey] - stageTimes[key]))} giây`;
    }
    list.appendChild(li);
  });
}

function setStageMeta(stage, text) {
  const m = document.querySelector(`.stage[data-stage="${stage}"] .meta`);
  if (m) m.textContent = text;
}

async function poll() {
  clearTimeout(state.pollTimer);
  if (!state.jobId) return;
  let job;
  try {
    job = await (await api(`/api/jobs/${state.jobId}`)).json();
  } catch (e) {
    if (/Không tìm thấy/.test(e.message)) {
      store.del("lastJob");
      history.replaceState(null, "", location.pathname);
      showView("uploadView");
      return;
    }
    state.pollTimer = setTimeout(poll, 4000);
    return;
  }
  state.job = job;
  $("processFile").textContent = job.filename || "";
  state.source = job.source || state.source;
  const opts = job.options || {};
  useStages(Boolean(opts.illustrations && opts.illustrations.enabled), Boolean(opts.google_slides) || job.stage === "exporting_google");
  // "queued" maps to the step right after upload; "downloading" (URL import) is the first step.
  const stage = job.stage === "queued" ? "extracting_audio" : job.stage === "downloading" ? "uploading" : job.stage;
  const times = { ...(job.stage_times || {}) };
  if (times.downloading) times.uploading = times.downloading;

  const watched = state.watching === job.id;
  if (job.status === "done") {
    state.watching = null;
    stopClock();
    renderStages("completed");
    showResult(job);
    if (watched) announceDone(job, true);
    return;
  }
  if (job.status === "failed" || job.status === "cancelled") {
    state.watching = null;
    stopClock();
    renderStages(stage, true, times);
    showError(job.error || "Đã xảy ra lỗi.", true, job.status === "cancelled");
    if (watched && job.status === "failed") announceDone(job, false);
    return;
  }
  state.watching = job.id;
  showView("processView");
  $("processError").hidden = true;
  startClock(job.created_at || Date.now() / 1000);
  setAway("server");
  const si = STAGES.findIndex(([k]) => k === stage);
  setTabTitle(si >= 0 ? `(${si + 1}/${STAGES.length - 1}) ${STAGES[si][1]}` : "Đang xử lý");
  $("cancelRow").hidden = job.status === "downloading";
  $("cancelBtn").disabled = Boolean(job.cancel_requested);
  $("cancelBtn").textContent = job.cancel_requested ? "Đang hủy…" : "Hủy xử lý";
  renderStages(stage, false, times);
  renderProgress(job, stage);
  const ip = job.illustrations;
  if (stage === "generating_images" && ip && ip.total) {
    // Real counts reported by the GPU worker, not an estimated percentage.
    setStageMeta("generating_images", `${ip.done}/${ip.total} ảnh` + (ip.failed ? ` · ${ip.failed} lỗi` : ""));
  }
  if (stage === "exporting_google" && job.google && GOOGLE_STEPS[job.google.step]) {
    setStageMeta("exporting_google", GOOGLE_STEPS[job.google.step]);
  }
  const qn = $("queueNote");
  const onModal = state.config && state.config.media_processing === "modal";
  if (job.status_message && job.status === "running") {
    // Short server-side status (e.g. Gemini -> Ollama on Modal fallback, GPU start); never secrets.
    qn.textContent = job.status_message;
    qn.hidden = false;
  } else if (job.status === "queued" && job.queue_length > 1) {
    qn.textContent = `Đang chờ: có ${job.queue_length - 1} video khác đang xử lý trước.`;
    qn.hidden = false;
  } else if (job.stage === "downloading") {
    qn.textContent = "Đang tải bản độ phân giải thấp (đủ cho nhận dạng giọng nói và trích khung hình).";
    qn.hidden = false;
  } else if (stage === "generating_images") {
    qn.textContent = "GPU có thể cần thêm 1–2 phút để khởi động ở ảnh đầu tiên; các ảnh sau nhanh hơn.";
    qn.hidden = false;
  } else if (stage === "transcribing") {
    qn.textContent = onModal
      ? "Nhận dạng giọng nói chạy trên GPU của Modal; lần đầu GPU có thể cần thêm thời gian khởi động."
      : "Nhận dạng giọng nói chạy trên CPU nên có thể mất vài phút với video dài.";
    qn.hidden = false;
  } else {
    qn.hidden = true;
  }
  state.pollTimer = setTimeout(poll, 2000);
}

async function renderProviderBadge(llm) {
  const badge = $("providerBadge");
  badge.hidden = false;
  badge.classList.remove("warn");
  badge.title = "";
  if (llm.provider === "ollama") {
    badge.textContent = `AI: ${llm.model} (Ollama, trên máy)`;
    try {
      const st = await (await api("/api/llm/status")).json();
      if (!st.ready) {
        badge.classList.add("warn");
        badge.textContent = `AI: ${llm.model} — chưa sẵn sàng`;
        badge.title = st.message || "";
      }
    } catch (e) { /* the job itself reports a precise error */ }
  } else if (llm.provider === "auto") {
    badge.textContent = llm.gemini_configured
      ? `AI: Gemini → dự phòng ${llm.fallback_model} (Ollama trên Modal)`
      : `AI: ${llm.fallback_model} (Ollama trên Modal GPU)`;
    badge.title = "Tự động: Gemini trước khi có khóa; khi Gemini không dùng được sẽ chuyển sang Ollama trên Modal.";
  } else if (llm.provider === "modal_ollama") {
    badge.textContent = `AI: ${llm.model} (Ollama trên Modal GPU)`;
  } else if (llm.provider === "gemini") {
    badge.textContent = `AI: Gemini (API, có thể tính phí)`;
  } else {
    badge.textContent = "AI: offline";
    badge.title = "LLM_PROVIDER=offline — dàn ý được tạo bằng chế độ offline đơn giản";
    badge.classList.add("warn");
  }
}

function renderProgress(job, stage) {
  // Only measured values: decoded audio seconds (Whisper), transcript parts analysed and slides written
  // (LLM) or finished images (GPU). No invented percentages.
  const p = job.progress, ip = job.illustrations;
  let done = null, total = null, text = "";
  if (stage === "transcribing" && p && p.stage === "transcribing" && p.total_sec > 0) {
    done = p.done_sec; total = p.total_sec;
    text = `Đã nhận dạng ${fmtClock(done)} / ${fmtClock(total)} âm thanh`;
  } else if (stage === "analyzing" && p && p.stage === "analyzing" && p.total > 1) {
    done = p.done; total = p.total;
    text = `Đã phân tích ${p.done}/${p.total} bước (các đoạn transcript + dàn ý)`;
  } else if (stage === "writing_slides" && p && p.stage === "writing_slides" && p.total) {
    done = p.done; total = p.total;
    text = `Đã viết ${p.done}/${p.total} slide`;
  } else if (stage === "generating_images" && ip && ip.total) {
    done = ip.done; total = ip.total;
    text = `${ip.done}/${ip.total} ảnh hoàn tất` + (ip.failed ? ` · ${ip.failed} lỗi` : "");
  }
  $("stageProgress").hidden = total === null;
  if (total !== null) {
    $("stageProgressFill").style.width = `${Math.min(100, Math.round((done / total) * 100))}%`;
    $("stageProgressText").textContent = text;
  }
}

async function cancelJob() {
  const btn = $("cancelBtn");
  btn.disabled = true;
  btn.textContent = "Đang hủy…";
  try {
    await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Hủy xử lý";
    $("queueNote").textContent = e.message;
    $("queueNote").hidden = false;
  }
  poll();
}

function showError(message, canRetry, cancelled = false) {
  showView("processView");
  stopClock();
  setAway(null);
  setTabTitle("");
  $("cancelRow").hidden = true;
  $("stageProgress").hidden = true;
  $("processTitle").textContent = cancelled ? "Đã hủy xử lý" : "Không thể hoàn tất";
  $("processErrorText").textContent = message;
  $("retryBtn").hidden = !canRetry;
  // A failed link import (e.g. YouTube blocking the cloud server): offer the direct upload instead.
  const src = (state.job && state.job.source) || state.source || {};
  $("restartBtn").dataset.toFile = src.type === "url" ? "1" : "";
  $("restartBtn").textContent = src.type === "url" ? "Tải tệp MP4 lên thay thế" : "Tải video khác";
  $("processError").hidden = false;
  $("queueNote").hidden = true;
}

// ------------------------------------------------------------------ result
function showResult(job) {
  const r = job.result || {};
  $("resultTitle").textContent = r.title || "Bài trình chiếu";
  $("resultSlides").textContent = r.slide_count ?? "—";
  const total = job.finished_at && job.created_at ? job.finished_at - job.created_at : r.processing_seconds;
  $("resultTime").textContent = fmtDuration(total);
  $("resultPictures").textContent = r.pictures ?? 0;
  $("resultLang").textContent = LANG_NAMES[r.language] || r.language || "—";
  const notices = [...(r.notices || [])];
  const route = r.llm_route;
  if (route && route.successful_provider) {
    const used = route.successful_provider === "gemini" ? "Gemini" : `${route.model || "Qwen3"} (Ollama trên Modal)`;
    notices.unshift(`Nội dung được tạo bằng ${used}` + (route.fallback_reason_text ? ` — lý do: ${route.fallback_reason_text}.` : "."));
  }
  $("resultNotice").textContent = notices.join(" ");
  $("resultNotice").hidden = notices.length === 0;
  const lw = $("resultLayouts");
  lw.innerHTML = "";
  if (r.layout_mode) lw.appendChild(el("span", null, r.layout_mode === "reading" ? "Bố cục: Chế độ đọc" : "Bố cục: Trình chiếu"));
  for (const l of [...new Set(r.layouts || [])]) lw.appendChild(el("span", null, LAYOUT_NAMES[l] || l));
  $("transcriptCard").hidden = true;
  renderGoogleBox(job);
  loadPreview();
  const il = r.illustrations;
  const illusText = $("resultIllus");
  if (il && il.requested) {
    illusText.textContent = `Ảnh minh họa AI: ${il.generated}/${il.requested}`
      + (il.from_cache ? ` (${il.from_cache} lấy từ bộ nhớ đệm)` : "")
      + (il.real_model ? "" : " — ảnh giả lập, không phải AI");
    illusText.hidden = false;
    loadIllustrations();
  } else {
    illusText.hidden = true;
    $("illusCard").hidden = true;
  }
  showView("resultView");
}

function googleButton(label, primary, onClick) {
  const b = el("button", `btn ${primary ? "primary" : "ghost"}`, label);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function renderGoogleBox(job) {
  const box = $("googleBox");
  const actions = $("googleBoxActions");
  const g = job.google;
  const gs = state.google || {};
  const open = $("openGoogleBtn");
  actions.innerHTML = "";
  box.className = "google-box";
  clearTimeout(state.googleTimer);
  open.hidden = !(g && g.url && (g.state === "done" || g.state === "stale" || g.state === "failed"));
  if (g && g.url) open.href = g.url;
  $("readBtn").classList.toggle("primary", open.hidden);
  if (!gs.enabled) { box.hidden = true; return; }
  box.hidden = false;
  const title = $("googleBoxTitle"), detail = $("googleBoxDetail");

  if (g && (g.state === "queued" || g.state === "running")) {
    title.textContent = "Đang tạo bản Google Slides…";
    detail.textContent = GOOGLE_STEPS[g.step] ? `Bước hiện tại: ${GOOGLE_STEPS[g.step]}.` : "Đang chờ.";
    state.googleTimer = setTimeout(refreshJobGoogle, 2000);
    return;
  }
  if (g && g.state === "done") {
    box.classList.add("ok");
    title.textContent = "Đã tạo Google Slides trong Drive của bạn";
    const n = g.notes || {};
    const parts = [];
    if (n.total) parts.push(`Ghi chú thuyết trình: ${n.verified}/${n.total} slide đã kiểm tra` + (n.fixed ? ` (${n.fixed} slide được bổ sung sau chuyển đổi)` : ""));
    if (g.warnings && g.warnings.length) parts.push(`Lưu ý: ${g.warnings.slice(0, 2).join(" ")}`);
    detail.textContent = parts.join(". ") || "Bản trình chiếu chỉnh sửa được trực tiếp trong Google Slides.";
    if (gs.connected) actions.appendChild(googleButton("Tạo bản mới", false, () => exportGoogle(true)));
    return;
  }
  if (g && g.state === "stale") {
    title.textContent = "Bản Google Slides được tạo trước lần dựng lại gần nhất";
    detail.textContent = "Bản cũ được giữ nguyên để không ghi đè chỉnh sửa của bạn.";
    if (gs.connected) actions.appendChild(googleButton("Tạo bản Google Slides mới", true, () => exportGoogle(true)));
    return;
  }
  if (g && g.state === "failed") {
    box.classList.add("err");
    title.textContent = "Chưa tạo được bản Google Slides";
    detail.textContent = g.error || "";
    if (gs.connected) actions.appendChild(googleButton("Thử lại", true, () => exportGoogle(false)));
    else if (gs.configured) actions.appendChild(googleButton("Kết nối lại Google", true, connectGoogle));
    return;
  }
  // Not exported yet.
  if (!gs.configured) {
    title.textContent = "Google Slides chưa được cấu hình";
    detail.textContent = "Làm theo docs/GOOGLE_SLIDES_SETUP_VI.md (khoảng 10 phút, miễn phí) để tạo bài trực tiếp trong Google Slides.";
  } else if (!gs.connected) {
    title.textContent = "Tạo bản Google Slides gốc";
    detail.textContent = "Kết nối tài khoản Google một lần, sau đó bài trình chiếu được tạo tự động trong Drive của bạn.";
    actions.appendChild(googleButton("Kết nối Google", true, connectGoogle));
  } else {
    title.textContent = "Tạo bản Google Slides gốc";
    detail.textContent = "Dùng lại đúng bài này (kể cả ảnh minh họa AI đã tạo) — không tốn thêm GPU.";
    actions.appendChild(googleButton("Tạo trên Google Slides", true, () => exportGoogle(false)));
  }
}

async function exportGoogle(newCopy) {
  try {
    await postJson(`/api/jobs/${state.jobId}/google`, { new_copy: newCopy });
  } catch (e) {
    toast(e.message, true);
  }
  refreshJobGoogle();
}

async function refreshJobGoogle() {
  if (!state.jobId || $("resultView").hidden) return;
  try {
    const job = await (await api(`/api/jobs/${state.jobId}`)).json();
    state.job = job;
    renderGoogleBox(job);
  } catch { /* ignore */ }
}

// ------------------------------------------------------------------ slide preview (result page)
const preview = { urls: [], index: 0 };

async function loadPreview() {
  const card = $("previewCard");
  preview.urls = [];
  $("previewStrip").innerHTML = "";
  $("previewStage").hidden = true;
  $("previewCounter").textContent = "";
  $("previewNote").textContent = "Đang tạo ảnh xem trước…";
  $("previewNote").hidden = false;
  card.hidden = false;
  let data;
  try {
    data = await (await api(`/api/jobs/${state.jobId}/preview`)).json();
  } catch (e) {
    $("previewNote").textContent = e.message;
    return;
  }
  if (!data.available) {
    $("previewNote").textContent = `${data.reason} Hãy dùng Chế độ đọc hoặc tải tệp .pptx để xem.`;
    return;
  }
  preview.urls = data.slides;
  $("previewNote").hidden = true;
  $("previewStage").hidden = false;
  const strip = $("previewStrip");
  preview.urls.forEach((url, i) => {
    const b = el("button");
    b.type = "button";
    b.setAttribute("role", "listitem");
    b.setAttribute("aria-label", `Slide ${i + 1}`);
    const img = el("img");
    img.alt = "";
    img.loading = "lazy";
    imageUrl(url).then((u) => { img.src = u; }).catch(() => {});
    b.appendChild(img);
    b.addEventListener("click", () => showSlide(i));
    strip.appendChild(b);
  });
  showSlide(0);
}

// Scroll a thumbnail strip horizontally only, so selecting a slide never jumps the whole page.
function revealThumb(b) {
  const strip = b.parentElement;
  const left = b.offsetLeft - strip.offsetLeft;
  if (left < strip.scrollLeft) strip.scrollLeft = left;
  else if (left + b.offsetWidth > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = left + b.offsetWidth - strip.clientWidth;
}

function showSlide(i) {
  if (!preview.urls.length) return;
  preview.index = Math.max(0, Math.min(preview.urls.length - 1, i));
  const n = preview.index;
  $("previewCounter").textContent = `Slide ${n + 1} / ${preview.urls.length}`;
  $("previewMain").alt = `Slide ${n + 1}`;
  imageUrl(preview.urls[n]).then((u) => { if (preview.index === n) $("previewMain").src = u; }).catch(() => {});
  $("previewPrev").disabled = n === 0;
  $("previewNext").disabled = n === preview.urls.length - 1;
  $("previewStrip").querySelectorAll("button").forEach((b, k) => {
    b.setAttribute("aria-current", String(k === n));
    if (k === n) revealThumb(b);
  });
}

async function loadIllustrations() {
  const grid = $("illusGrid");
  for (const img of grid.querySelectorAll("img")) URL.revokeObjectURL(img.src);
  grid.innerHTML = "";
  let data;
  try {
    data = await (await api(`/api/jobs/${state.jobId}/illustrations`)).json();
  } catch { $("illusCard").hidden = true; return; }
  if (!data.items.length) { $("illusCard").hidden = true; return; }
  const left = data.regenerations_max - data.regenerations_used;
  $("illusNote").textContent = `Bạn có thể tạo lại từng ảnh (còn ${Math.max(0, left)} lượt). Bài trình chiếu sẽ được dựng lại tự động.`;
  for (const it of data.items) {
    const card = el("div", "illus-item");
    const ph = el("div", "ph", it.has_image ? "Đang tải ảnh…" : (it.error || "Chưa có ảnh"));
    const cap = el("div", "cap");
    const title = el("span", null, it.slide_title || it.slide_id);
    title.title = title.textContent;
    const btn = el("button", "btn ghost", "Tạo lại");
    btn.type = "button";
    btn.disabled = left <= 0;
    btn.addEventListener("click", () => regenerateIllustration(it.slide_id, btn));
    cap.append(title, btn);
    card.append(ph, cap);
    grid.appendChild(card);
    if (it.has_image) {
      api(`/api/jobs/${state.jobId}/illustrations/${it.slide_id}.jpg`)
        .then((res) => res.blob())
        .then((blob) => {
          const img = el("img");
          img.alt = `Ảnh minh họa cho slide: ${it.slide_title}`;
          img.src = URL.createObjectURL(blob);
          ph.replaceWith(img);
        })
        .catch(() => { ph.textContent = "Không tải được ảnh"; });
    }
  }
  $("illusCard").hidden = false;
}

async function regenerateIllustration(slideId, btn) {
  btn.disabled = true;
  try {
    await api(`/api/jobs/${state.jobId}/illustrations/${slideId}/regenerate`, { method: "POST" });
    clearImages();
    showView("processView");
    $("processTitle").textContent = "Đang tạo lại ảnh minh họa";
    $("processError").hidden = true;
    poll();
  } catch (e) {
    $("resultNotice").textContent = e.message;
    $("resultNotice").hidden = false;
    btn.disabled = false;
  }
}

async function downloadPptx(jobId = state.jobId) {
  const btn = $("downloadBtn");
  btn.disabled = true;
  try {
    const res = await api(`/api/jobs/${jobId}/download`);
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd);
    const name = m ? decodeURIComponent(m[1] || m[2]) : "presentation.pptx";
    const a = el("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function toggleTranscript() {
  const card = $("transcriptCard");
  if (!card.hidden) { card.hidden = true; return; }
  try {
    const data = await (await api(`/api/jobs/${state.jobId}/transcript`)).json();
    const body = $("transcriptBody");
    body.innerHTML = "";
    for (const seg of data.segments || []) {
      const row = el("div", "seg");
      row.append(el("time", null, fmtClock(seg.start)), el("span", null, seg.text));
      body.appendChild(row);
    }
    card.hidden = false;
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    $("resultNotice").textContent = e.message;
    $("resultNotice").hidden = false;
  }
}

function startOver() {
  clearTimeout(state.pollTimer);
  state.watching = null;
  clearTimeout(state.googleTimer);
  state.jobId = null;
  state.job = null;
  store.del("lastJob");
  history.replaceState(null, "", location.pathname);
  clearFile();
  resetUrlCheck();
  refreshGoogle();
  showView("uploadView");
}

async function retry() {
  try {
    await api(`/api/jobs/${state.jobId}/retry`, { method: "POST" });
    $("processTitle").textContent = "Đang xử lý video";
    $("processError").hidden = true;
    poll();
  } catch (e) {
    showError(e.message, false);
  }
}

// ------------------------------------------------------------------ Reading Mode
// The app owns the current slide index; slide image, explanation, title, counter, thumbnails
// and progress are all rendered from it (no reliance on an embedded Google Slides iframe).
const reader = { data: null, index: 0, jobId: null, returnTo: "resultView" };

async function openReader(jobId, index = 0, push = true) {
  reader.returnTo = !$("libraryView").hidden ? "libraryView" : "resultView";
  reader.jobId = jobId;
  showView("readingView");
  $("readerTitle").textContent = "Đang tải…";
  $("readerCounter").textContent = "";
  $("readerNotes").innerHTML = "";
  $("readerStrip").innerHTML = "";
  $("readerNotice").hidden = true;
  $("readerImg").hidden = true;
  $("readerFallback").hidden = true;
  if (push) history.pushState(null, "", `#read=${jobId}&s=${index + 1}`);
  let data;
  try {
    data = await (await api(`/api/jobs/${jobId}/reading`)).json();
  } catch (e) {
    $("readerTitle").textContent = "Không mở được Chế độ đọc";
    $("readerNotice").textContent = e.message;
    $("readerNotice").hidden = false;
    return;
  }
  reader.data = data;
  $("readerTitle").textContent = data.title;
  const g = $("readerGoogle");
  g.hidden = !data.google_url;
  if (data.google_url) g.href = data.google_url;
  $("readerCheck").hidden = !data.google_url || !(state.google && state.google.connected);
  if (!data.image_source) {
    $("readerNotice").textContent = "Không có ảnh slide (máy chưa có PowerPoint/LibreOffice và bài chưa có bản Google Slides), nên slide được hiển thị dạng văn bản.";
    $("readerNotice").hidden = false;
  }
  buildReaderStrip();
  goTo(Math.min(index, data.slides.length - 1), false);
  $("readerNotes").focus({ preventScroll: true });
}

function buildReaderStrip() {
  const strip = $("readerStrip");
  strip.innerHTML = "";
  reader.data.slides.forEach((s, i) => {
    const b = el("button");
    b.type = "button";
    b.setAttribute("role", "listitem");
    b.setAttribute("aria-label", `Slide ${i + 1}: ${s.title}`);
    b.title = s.title;
    if (s.image) {
      const img = el("img");
      img.alt = "";
      img.loading = "lazy";
      imageUrl(s.image).then((u) => { img.src = u; }).catch(() => { img.replaceWith(el("span", "thumb-text", s.title)); });
      b.appendChild(img);
    } else {
      b.appendChild(el("span", "thumb-text", s.title));
    }
    b.appendChild(el("span", "num", String(i + 1)));
    b.addEventListener("click", () => goTo(i));
    strip.appendChild(b);
  });
}

function goTo(i, updateHash = true) {
  const slides = reader.data ? reader.data.slides : [];
  if (!slides.length) return;
  reader.index = Math.max(0, Math.min(slides.length - 1, i));
  const n = reader.index;
  const s = slides[n];
  $("readerCounter").textContent = `Slide ${n + 1} / ${slides.length}`;
  $("readerProgress").style.width = `${Math.round(((n + 1) / slides.length) * 100)}%`;
  $("readerPrev").disabled = n === 0;
  $("readerNext").disabled = n === slides.length - 1;
  renderReaderSlide(s, n);
  renderReaderNotes(s, n);
  $("readerStrip").querySelectorAll("button").forEach((b, k) => {
    b.setAttribute("aria-current", String(k === n));
    if (k === n) revealThumb(b);
  });
  if (updateHash) history.replaceState(null, "", `#read=${reader.jobId}&s=${n + 1}`);
  // Warm the next image so navigation feels instant.
  const next = slides[n + 1];
  if (next && next.image) imageUrl(next.image).catch(() => {});
}

function renderReaderSlide(s, n) {
  const img = $("readerImg");
  const fb = $("readerFallback");
  const showFallback = () => {
    fb.innerHTML = "";
    fb.appendChild(el("h3", null, s.title));
    const ul = el("ul");
    for (const p of (s.main_points || []).slice(0, 6)) ul.appendChild(el("li", null, p));
    fb.appendChild(ul);
    fb.appendChild(el("span", "fb-note", "Ảnh slide không có sẵn — đang hiển thị nội dung dạng văn bản."));
    fb.hidden = false;
    img.hidden = true;
  };
  if (!s.image) { showFallback(); return; }
  imageUrl(s.image).then((u) => {
    if (reader.index !== n) return;
    img.src = u;
    img.alt = `Slide ${n + 1}: ${s.title}`;
    img.hidden = false;
    fb.hidden = true;
  }).catch(() => { if (reader.index === n) showFallback(); });
}

// Highlight short key terms of the slide (item headings, column titles) inside a paragraph.
function keyTerms(s) {
  const terms = new Set();
  for (const p of s.main_points || []) {
    const i = p.indexOf(":");
    if (i > 1 && i <= 40) terms.add(p.slice(0, i).trim());
  }
  if (s.layout_type === "concept") terms.add(s.title);
  return [...terms].filter((t) => t.length >= 3 && t.length <= 40);
}

function richParagraph(text, terms) {
  const p = el("p");
  if (!terms.length) { p.textContent = text; return p; }
  const esc = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).sort((a, b) => b.length - a.length);
  const re = new RegExp(`(${esc.join("|")})`, "gi");
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) p.appendChild(document.createTextNode(text.slice(last, m.index)));
    p.appendChild(el("mark", null, m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) p.appendChild(document.createTextNode(text.slice(last)));
  return p;
}

function renderReaderNotes(s, n) {
  const box = $("readerNotes");
  box.innerHTML = "";
  const rn = s.reading_notes || {};
  const terms = keyTerms(s);
  const head = el("div", "rn-head");
  const copy = el("button", "btn ghost rn-copy", "Sao chép");
  copy.type = "button";
  copy.title = "Sao chép tiêu đề và phần giải thích của slide này";
  copy.addEventListener("click", () => copyNotes(s, n, copy));
  head.append(el("p", "rn-kicker", `Slide ${n + 1}`), copy);
  box.appendChild(head);
  box.appendChild(el("h2", null, s.title));

  const main = rn.summary || s.brief_explanation;
  if (main) {
    box.appendChild(el("h3", null, "Ý chính"));
    box.appendChild(el("div", "rn-main", main));
  }
  const details = (rn.detailed_explanation || "").split(/\n{2,}|\n/).map((t) => t.trim()).filter(Boolean);
  if (details.length) {
    box.appendChild(el("h3", null, "Giải thích chi tiết"));
    for (const para of details) box.appendChild(richParagraph(para, terms));
  } else if (!main) {
    box.appendChild(el("p", "muted", "Slide này không có phần giải thích thêm."));
  }
  if (rn.examples && rn.examples.length) {
    box.appendChild(el("h3", null, "Ví dụ minh họa"));
    const ul = el("ul");
    for (const e of rn.examples) ul.appendChild(el("li", null, e));
    box.appendChild(ul);
  }
  if (rn.key_takeaway) {
    const t = el("div", "rn-takeaway");
    t.append(el("h3", null, "Điều cần nhớ"), el("p", null, rn.key_takeaway));
    box.appendChild(t);
  }
  if (s.source_timestamps && s.source_timestamps.length) {
    box.appendChild(el("h3", null, "Thời điểm trong video gốc"));
    const times = el("div", "rn-times");
    const src = reader.data.source_url;
    for (const r of s.source_timestamps) {
      const label = r.end > r.start ? `${fmtClock(r.start)} – ${fmtClock(r.end)}` : fmtClock(r.start);
      if (src) {
        // Open the original YouTube video at this moment.
        const a = el("a", null, label);
        const u = new URL(src);
        u.searchParams.set("t", `${Math.floor(r.start)}s`);
        a.href = u.toString();
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.title = "Mở video gốc tại thời điểm này";
        times.appendChild(a);
      } else {
        times.appendChild(el("span", null, label));
      }
    }
    box.appendChild(times);
  }
  if (s.transcript_excerpt) {
    const d = el("details");
    d.append(el("summary", null, "Trích lời giảng gốc"), el("p", null, s.transcript_excerpt));
    box.appendChild(d);
  }
  box.scrollTop = 0;
}

function notesAsText(s, n) {
  const rn = s.reading_notes || {};
  const out = [`Slide ${n + 1}: ${s.title}`];
  const main = rn.summary || s.brief_explanation;
  if (main) out.push("", main);
  if (rn.detailed_explanation) out.push("", rn.detailed_explanation.trim());
  if (rn.examples && rn.examples.length) out.push("", "Ví dụ:", ...rn.examples.map((e) => `- ${e}`));
  if (rn.key_takeaway) out.push("", `Điều cần nhớ: ${rn.key_takeaway}`);
  return out.join("\n");
}

async function copyNotes(s, n, btn) {
  try {
    await navigator.clipboard.writeText(notesAsText(s, n));
    btn.textContent = "✓ Đã chép";
  } catch {
    btn.textContent = "Không chép được";
  }
  setTimeout(() => { btn.textContent = "Sao chép"; }, 1800);
}

function closeReader() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  const back = reader.returnTo;
  if (back === "libraryView") { openLibrary(true); return; }
  if (state.job && state.job.id === reader.jobId && state.job.status === "done") {
    history.replaceState(null, "", `#job=${reader.jobId}`);
    showView("resultView");
  } else {
    history.replaceState(null, "", `#job=${reader.jobId}`);
    state.jobId = reader.jobId;
    store.set("lastJob", reader.jobId);
    poll();
  }
}

function toggleFullscreen() {
  const stage = $("readerStage");
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else if (stage.requestFullscreen) stage.requestFullscreen().catch(() => toast("Trình duyệt không cho phép toàn màn hình.", true));
}

async function checkGoogleSync() {
  const btn = $("readerCheck");
  btn.disabled = true;
  btn.textContent = "Đang kiểm tra…";
  const notice = $("readerNotice");
  try {
    const r = await (await postJson(`/api/jobs/${reader.jobId}/google/check`)).json();
    let msg;
    if (!r.exists) msg = "Không tìm thấy bản Google Slides (có thể đã bị xóa). Phần giải thích ở đây vẫn dùng được.";
    else if (r.in_sync) msg = "Bản Google Slides khớp với dữ liệu của ứng dụng (thứ tự slide và ghi chú thuyết trình).";
    else {
      const parts = [];
      if (r.missing.length) parts.push(`${r.missing.length} slide đã bị xóa`);
      if (r.added) parts.push(`${r.added} slide được thêm`);
      if (r.reordered) parts.push("thứ tự slide đã thay đổi");
      if (r.notes_edited.length) parts.push(`ghi chú của ${r.notes_edited.length} slide đã được sửa`);
      msg = `Bản Google Slides đã được chỉnh sửa (${parts.join(", ")}). Chế độ đọc vẫn hiển thị nội dung gốc do AI tạo; ứng dụng không tự ghi đè chỉnh sửa của bạn.`;
    }
    notice.textContent = msg;
  } catch (e) {
    notice.textContent = e.message;
  }
  notice.hidden = false;
  btn.disabled = false;
  btn.textContent = "Kiểm tra đồng bộ";
}

// ------------------------------------------------------------------ library
async function openLibrary(push = true) {
  showView("libraryView");
  if (push) history.pushState(null, "", "#library");
  const list = $("libraryList");
  list.innerHTML = "";
  $("libraryNote").textContent = "Đang tải…";
  let data;
  try {
    data = await (await api("/api/library")).json();
  } catch (e) {
    $("libraryNote").textContent = e.message;
    return;
  }
  library.items = data.items;
  const has = data.items.length > 0;
  $("libraryTools").hidden = !has;
  $("libraryEmpty").hidden = has;
  $("libraryNote").textContent = has ? "Mở lại bài cũ ở Chế độ đọc, trong Google Slides hoặc xem chi tiết để tải lại tệp PowerPoint." : "";
  $("libraryNote").hidden = !has;
  // A job still running in this browser: offer the way back to its progress instead of losing it.
  const j = state.job;
  if (j && ["queued", "running", "downloading"].includes(j.status)) {
    const li = el("li", "lib-running");
    const meta = el("div", "lib-meta");
    meta.append(el("strong", null, j.filename || "Video đang xử lý"), el("span", "muted small", "Đang xử lý…"));
    const actions = el("div", "lib-actions");
    actions.appendChild(googleButton("Xem tiến trình", true, () => {
      history.replaceState(null, "", `#job=${j.id}`);
      state.jobId = j.id;
      poll();
    }));
    li.append(el("span", "lib-num", "··"), meta, actions);
    list.appendChild(li);
  }
  renderLibraryItems();
}

const library = { items: [] };

function renderLibraryItems() {
  const list = $("libraryList");
  for (const li of [...list.children]) if (!li.classList.contains("lib-running")) li.remove();
  const q = $("librarySearch").value.trim().toLocaleLowerCase("vi");
  const items = library.items.filter((it) => !q || (it.title || "").toLocaleLowerCase("vi").includes(q));
  $("libraryCount").textContent = q ? `${items.length} / ${library.items.length} bài` : `${library.items.length} bài`;
  items.forEach((it, i) => {
    const li = el("li");
    const meta = el("div", "lib-meta");
    const sub = el("span", "lib-sub muted small");
    if (it.platform && PLATFORM_NAMES[it.platform]) sub.appendChild(el("span", `platform-tag ${it.platform}`, PLATFORM_NAMES[it.platform]));
    sub.appendChild(el("span", null, `${fmtDate(it.created_at)} · ${it.slide_count || "?"} slide · ${it.layout_mode === "reading" ? "Chế độ đọc" : "Trình chiếu"}`));
    meta.append(el("strong", null, it.title), sub);
    const actions = el("div", "lib-actions");
    actions.appendChild(googleButton("Chế độ đọc", true, () => openReader(it.job_id)));
    if (it.google_url) {
      const a = el("a", "btn ghost btn-ext", "Google Slides");
      a.href = it.google_url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      actions.appendChild(a);
    }
    if (it.available) {
      actions.appendChild(googleButton("Chi tiết", false, () => {
        state.jobId = it.job_id;
        store.set("lastJob", it.job_id);
        history.replaceState(null, "", `#job=${it.job_id}`);
        poll();
      }));
    }
    const del = googleButton("Xóa", false, () => deleteLibraryItem(it, del));
    del.classList.add("lib-del");
    del.setAttribute("aria-label", `Xóa “${it.title}”`);
    actions.appendChild(del);
    li.append(el("span", "lib-num", String(i + 1).padStart(2, "0")), meta, actions);
    list.appendChild(li);
  });
  if (q && !items.length) {
    const li = el("li", "muted small", `Không có bài nào khớp “${$("librarySearch").value.trim()}”.`);
    li.style.display = "block";
    list.appendChild(li);
  }
}

async function deleteLibraryItem(it, btn) {
  if (!window.confirm(`Xóa “${it.title}” khỏi thư viện?\nTệp PowerPoint và Chế độ đọc của bài này sẽ không mở lại được. Bản Google Slides (nếu có) vẫn còn trong Drive của bạn.`)) return;
  btn.disabled = true;
  try {
    await api(`/api/jobs/${it.job_id}`, { method: "DELETE" });
  } catch (e) {
    btn.disabled = false;
    toast(e.message, true);
    return;
  }
  library.items = library.items.filter((x) => x.job_id !== it.job_id);
  if (store.get("lastJob") === it.job_id) store.del("lastJob");
  if (!library.items.length) { openLibrary(false); return; }
  renderLibraryItems();
  toast("Đã xóa bài trình chiếu.");
}

// ------------------------------------------------------------------ events
$("fileInput").addEventListener("change", (e) => selectFile(e.target.files[0]));
$("clearFile").addEventListener("click", clearFile);
const dz = $("dropzone");
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => selectFile(e.dataTransfer.files[0]));
$("tabFile").addEventListener("click", () => setMode("file"));
$("tabUrl").addEventListener("click", () => setMode("url"));
$("sourceTabs").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  e.preventDefault();
  const mode = e.key === "Home" ? "file" : e.key === "End" ? "url" : state.mode === "file" ? "url" : "file";
  setMode(mode);
  $(mode === "file" ? "tabFile" : "tabUrl").focus();
});
$("urlInput").addEventListener("input", onUrlInput);
$("urlInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); validateUrl(); } });
$("validateUrlBtn").addEventListener("click", validateUrl);
$("rightsCheck").addEventListener("change", updateSubmit);
$("useUploadBtn").addEventListener("click", () => setMode("file"));
$("uploadForm").addEventListener("submit", upload);
$("downloadBtn").addEventListener("click", () => downloadPptx());
$("transcriptBtn").addEventListener("click", toggleTranscript);
$("newBtn").addEventListener("click", startOver);
$("restartBtn").addEventListener("click", () => {
  const toFile = $("restartBtn").dataset.toFile === "1";
  startOver();
  if (toFile) setMode("file");
});
$("retryBtn").addEventListener("click", retry);
$("cancelBtn").addEventListener("click", cancelJob);
$("previewPrev").addEventListener("click", () => showSlide(preview.index - 1));
$("previewNext").addEventListener("click", () => showSlide(preview.index + 1));
$("googleChip").addEventListener("click", googleChipClick);
$("googlePasteForm").addEventListener("submit", finishGooglePaste);
$("googlePasteCancel").addEventListener("click", () => $("googlePasteDialog").close());
$("googleCheck").addEventListener("change", () => store.set("googleAuto", $("googleCheck").checked ? "1" : "0"));
$("readBtn").addEventListener("click", () => openReader(state.jobId, preview.index || 0));
$("readerBack").addEventListener("click", closeReader);
$("readerPrev").addEventListener("click", () => goTo(reader.index - 1));
$("readerNext").addEventListener("click", () => goTo(reader.index + 1));
$("readerFull").addEventListener("click", toggleFullscreen);
$("readerCheck").addEventListener("click", checkGoogleSync);
$("libraryBtn").addEventListener("click", () => openLibrary());
$("libraryClose").addEventListener("click", startOver);
$("libraryStart").addEventListener("click", startOver);
$("librarySearch").addEventListener("input", renderLibraryItems);
$("notifyBtn").addEventListener("click", toggleNotify);

// Closing the tab mid-upload loses the upload (processing afterwards runs on the server and survives it).
window.addEventListener("beforeunload", (e) => {
  if (state.uploading) { e.preventDefault(); e.returnValue = ""; }
});

// Drop a video anywhere on the start page, not only on the dropzone.
const hasFiles = (e) => Boolean(e.dataTransfer && [...e.dataTransfer.types].includes("Files"));
let dragDepth = 0;
document.addEventListener("dragenter", (e) => {
  if (!hasFiles(e) || $("uploadView").hidden) return;
  dragDepth++;
  $("dropOverlay").hidden = false;
});
document.addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $("dropOverlay").hidden = true;
});
document.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });  // never navigate to the file
document.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  $("dropOverlay").hidden = true;
  if ($("uploadView").hidden || dz.contains(e.target)) return;  // the dropzone handles its own drops
  setMode("file");
  selectFile(e.dataTransfer.files[0]);
});

// Paste a YouTube/Facebook link anywhere on the start page: switch to the URL tab and check it right away.
function takePastedUrl(text) {
  const url = (text || "").trim();
  if (!url || /\s/.test(url) || !detectPlatform(url) || $("sourceTabs").hidden) return false;
  setMode("url");
  $("urlInput").value = url;
  onUrlInput();
  validateUrl();
  return true;
}
document.addEventListener("paste", (e) => {
  if ($("uploadView").hidden || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (takePastedUrl(e.clipboardData && e.clipboardData.getData("text"))) e.preventDefault();
});
$("urlInput").addEventListener("paste", () => {
  setTimeout(() => { if (detectPlatform($("urlInput").value.trim()) && !state.urlCheck) validateUrl(); }, 0);
});

// Swipe left/right on the slide (touch screens).
let touchX = null;
$("readerStage").addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, { passive: true });
$("readerStage").addEventListener("touchend", (e) => {
  if (touchX === null) return;
  const dx = e.changedTouches[0].clientX - touchX;
  touchX = null;
  if (Math.abs(dx) > 50) goTo(reader.index + (dx < 0 ? 1 : -1));
});

document.addEventListener("keydown", (e) => {
  if (/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName) || e.altKey || e.ctrlKey || e.metaKey) return;
  if (!$("readingView").hidden && reader.data) {
    if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); goTo(reader.index - 1); }
    else if (e.key === "ArrowRight" || e.key === "PageDown") { e.preventDefault(); goTo(reader.index + 1); }
    else if (e.key === "Home") { e.preventDefault(); goTo(0); }
    else if (e.key === "End") { e.preventDefault(); goTo(reader.data.slides.length - 1); }
    else if (e.key === "f" || e.key === "F") toggleFullscreen();
    else if (e.key === "Escape" && !document.fullscreenElement) closeReader();
    return;
  }
  if ($("resultView").hidden || !preview.urls.length) return;
  if (e.key === "ArrowLeft") showSlide(preview.index - 1);
  if (e.key === "ArrowRight") showSlide(preview.index + 1);
});
$("authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  store.set("appPassword", $("authInput").value, true);
  try {
    await api("/api/jobs/" + "0".repeat(32));
  } catch (err) {
    if (/mật khẩu/.test(err.message)) { $("authError").textContent = "Sai mật khẩu."; $("authError").hidden = false; return; }
  }
  $("authError").hidden = true;
  refreshGoogle();  // /api/config leaves out the Google account until the password is known
  route();
});

window.addEventListener("hashchange", () => {
  if (!state.config) return;
  const p = new URLSearchParams(location.hash.slice(1));
  const read = p.get("read");
  if (read && read === reader.jobId && !$("readingView").hidden) {
    const s = (parseInt(p.get("s"), 10) || 1) - 1;
    if (s !== reader.index) goTo(s, false);
    return;
  }
  const onJob = !$("processView").hidden || !$("resultView").hidden;
  if (p.has("job") && p.get("job") === state.jobId && onJob) return;
  route();
});

setupTheme();
init();
