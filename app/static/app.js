"use strict";

const $ = (id) => document.getElementById(id);

const ALL_STAGES = [
  ["uploading", "Tải video lên"],
  ["extracting_audio", "Tách âm thanh & khung hình"],
  ["transcribing", "Nhận dạng giọng nói"],
  ["analyzing", "Phân tích nội dung & viết giải thích bằng AI"],
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
  if (!THEME_LABELS[pref]) pref = "system";
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
  return document.documentElement.getAttribute("data-theme-pref") || "system";
}

function toggleThemeMenu(open, focusItem = false) {
  const list = $("themeList");
  list.hidden = !open;
  $("themeBtn").setAttribute("aria-expanded", String(open));
  if (open && focusItem) (list.querySelector('[aria-checked="true"]') || list.querySelector("button")).focus();
}

function setupTheme() {
  applyTheme(store.get("theme") || "system");
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
  window.addEventListener("storage", (e) => { if (e.key === "theme") applyTheme(e.newValue || "system"); });
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
    state.config = await (await fetch("/api/config")).json();
  } catch {
    $("fileError").textContent = "Không kết nối được máy chủ.";
    $("fileError").hidden = false;
    showView("uploadView");
    return;
  }
  const c = state.config;
  $("limitsText").textContent = `MP4 · tối đa ${c.max_upload_mb} MB · tối đa ${Math.round(c.max_duration_sec / 60)} phút`;

  const badge = $("providerBadge");
  badge.hidden = false;
  if (c.ai_provider === "gemini") {
    badge.textContent = "AI: Gemini";
  } else {
    badge.textContent = "AI: offline";
    badge.title = "Chưa cấu hình GEMINI_API_KEY — dàn ý được tạo bằng chế độ offline đơn giản";
    badge.classList.add("warn");
  }

  const ui = c.url_import || { enabled: false };
  $("sourceTabs").hidden = !ui.enabled;
  if (ui.enabled) $("urlLimits").textContent = `${Math.round(ui.max_duration_sec / 60)} phút, ${ui.max_download_mb} MB`;

  setupAiImages(c.ai_images || {});
  const dm = c.default_layout_mode || "presentation";
  const radio = document.querySelector(`input[name="layoutMode"][value="${dm}"]`);
  if (radio) radio.checked = true;
  setGoogle(c.google);

  for (const [code, name] of Object.entries(c.languages)) {
    $("languageSelect").add(new Option(name, code, false, code === c.default_language));
  }
  for (let n = 5; n <= 20; n++) $("slideCountSelect").add(new Option(`${n} slide`, String(n)));

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

  if (c.password_required && !store.get("appPassword", true)) { showView("authView"); return; }
  route();
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
    const { auth_url } = await (await postJson("/api/google/connect")).json();
    window.location.assign(auth_url);
  } catch (e) {
    toast(e.message, true);
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
    const rec = Math.max(6, Math.min(15, Math.round(v.duration / 50) + 2));
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
  state.source = { type: "url", platform: info.platform };
  useStages(opts.ai_images, opts.google_slides);
  showView("processView");
  $("processTitle").textContent = "Đang xử lý video";
  $("processFile").textContent = info.title || info.url;
  $("processError").hidden = true;
  renderStages("uploading");
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
function upload(ev) {
  ev.preventDefault();
  if (state.mode === "url") { importUrl(); return; }
  if (!state.file) return;
  state.source = { type: "upload" };
  const opts = commonOptions();
  store.set("googleAuto", opts.google_slides ? "1" : "0");
  const fd = new FormData();
  fd.append("file", state.file);
  for (const [k, v] of Object.entries(opts)) fd.append(k, typeof v === "boolean" ? String(v) : v);
  useStages(opts.ai_images, opts.google_slides);

  showView("processView");
  $("processTitle").textContent = "Đang xử lý video";
  $("processFile").textContent = state.file.name;
  $("processError").hidden = true;
  renderStages("uploading");

  // XHR gives real byte-level upload progress (fetch does not).
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) setStageMeta("uploading", `${Math.floor((e.loaded / e.total) * 100)}%`);
  };
  xhr.upload.onload = () => setStageMeta("uploading", "đang kiểm tra tệp…");
  xhr.onload = () => {
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch { /* ignore */ }
    if (xhr.status === 401) { showView("authView"); return; }
    if (xhr.status !== 202) {
      showError(body.detail && typeof body.detail === "string" ? body.detail : `Tải lên thất bại (HTTP ${xhr.status}).`, false);
      return;
    }
    followJob(body.job_id);
  };
  xhr.onerror = () => showError("Mất kết nối khi tải lên. Vui lòng thử lại.", false);
  xhr.send(fd);
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

  if (job.status === "done") {
    renderStages("completed");
    showResult(job);
    return;
  }
  if (job.status === "failed" || job.status === "cancelled") {
    renderStages(stage, true, times);
    showError(job.error || "Đã xảy ra lỗi.", true, job.status === "cancelled");
    return;
  }
  showView("processView");
  $("processError").hidden = true;
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
  if (job.status === "queued" && job.queue_length > 1) {
    qn.textContent = `Đang chờ: có ${job.queue_length - 1} video khác đang xử lý trước.`;
    qn.hidden = false;
  } else if (job.stage === "downloading") {
    qn.textContent = "Đang tải bản độ phân giải thấp (đủ cho nhận dạng giọng nói và trích khung hình).";
    qn.hidden = false;
  } else if (stage === "generating_images") {
    qn.textContent = "GPU có thể cần thêm 1–2 phút để khởi động ở ảnh đầu tiên; các ảnh sau nhanh hơn.";
    qn.hidden = false;
  } else if (stage === "transcribing") {
    qn.textContent = "Nhận dạng giọng nói chạy trên CPU nên có thể mất vài phút với video dài.";
    qn.hidden = false;
  } else {
    qn.hidden = true;
  }
  state.pollTimer = setTimeout(poll, 2000);
}

function renderProgress(job, stage) {
  // Only measured values: decoded audio seconds (Whisper) or finished images (GPU). No invented percentages.
  const p = job.progress, ip = job.illustrations;
  let done = null, total = null, text = "";
  if (stage === "transcribing" && p && p.stage === "transcribing" && p.total_sec > 0) {
    done = p.done_sec; total = p.total_sec;
    text = `Đã nhận dạng ${fmtClock(done)} / ${fmtClock(total)} âm thanh`;
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
  $("cancelRow").hidden = true;
  $("stageProgress").hidden = true;
  $("processTitle").textContent = cancelled ? "Đã hủy xử lý" : "Không thể hoàn tất";
  $("processErrorText").textContent = message;
  $("retryBtn").hidden = !canRetry;
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
  box.appendChild(el("p", "rn-kicker", `Slide ${n + 1}`));
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
    for (const r of s.source_timestamps) {
      times.appendChild(el("span", null, r.end > r.start ? `${fmtClock(r.start)} – ${fmtClock(r.end)}` : fmtClock(r.start)));
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
  $("libraryNote").textContent = data.items.length
    ? "Mở lại bài cũ ở Chế độ đọc, trong Google Slides hoặc tải lại tệp PowerPoint."
    : "Chưa có bài trình chiếu nào. Hãy tạo bài đầu tiên.";
  for (const it of data.items) {
    const li = el("li");
    const meta = el("div", "lib-meta");
    meta.append(el("strong", null, it.title),
      el("span", "muted small", `${fmtDate(it.created_at)} · ${it.slide_count || "?"} slide · ${it.layout_mode === "reading" ? "Chế độ đọc" : "Trình chiếu"}`));
    li.appendChild(meta);
    const read = googleButton("Chế độ đọc", true, () => openReader(it.job_id));
    li.appendChild(read);
    if (it.google_url) {
      const a = el("a", "btn ghost", "Google Slides");
      a.href = it.google_url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      li.appendChild(a);
    }
    if (it.available) {
      li.appendChild(googleButton("Chi tiết", false, () => {
        state.jobId = it.job_id;
        store.set("lastJob", it.job_id);
        history.replaceState(null, "", `#job=${it.job_id}`);
        poll();
      }));
    }
    list.appendChild(li);
  }
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
$("restartBtn").addEventListener("click", startOver);
$("retryBtn").addEventListener("click", retry);
$("cancelBtn").addEventListener("click", cancelJob);
$("previewPrev").addEventListener("click", () => showSlide(preview.index - 1));
$("previewNext").addEventListener("click", () => showSlide(preview.index + 1));
$("googleChip").addEventListener("click", googleChipClick);
$("googleCheck").addEventListener("change", () => store.set("googleAuto", $("googleCheck").checked ? "1" : "0"));
$("readBtn").addEventListener("click", () => openReader(state.jobId, preview.index || 0));
$("readerBack").addEventListener("click", closeReader);
$("readerPrev").addEventListener("click", () => goTo(reader.index - 1));
$("readerNext").addEventListener("click", () => goTo(reader.index + 1));
$("readerFull").addEventListener("click", toggleFullscreen);
$("readerCheck").addEventListener("click", checkGoogleSync);
$("libraryBtn").addEventListener("click", () => openLibrary());
$("libraryClose").addEventListener("click", startOver);

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
