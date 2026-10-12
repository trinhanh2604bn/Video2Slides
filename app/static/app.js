"use strict";

const $ = (id) => document.getElementById(id);

const ALL_STAGES = [
  ["uploading", "Tải video lên"],
  ["extracting_audio", "Tách âm thanh & khung hình"],
  ["transcribing", "Nhận dạng giọng nói"],
  ["analyzing", "Phân tích nội dung bằng AI"],
  ["planning_illustrations", "Lập kế hoạch ảnh minh họa"],
  ["generating_images", "Tạo ảnh minh họa AI (GPU)"],
  ["generating", "Dựng bố cục slide"],
  ["finalizing", "Hoàn thiện & kiểm tra PowerPoint"],
  ["completed", "Hoàn tất"],
];
const IMAGE_STAGES = new Set(["planning_illustrations", "generating_images"]);
let STAGES = ALL_STAGES.filter(([k]) => !IMAGE_STAGES.has(k));
function useImageStages(on) {
  STAGES = ALL_STAGES.filter(([k]) => on || !IMAGE_STAGES.has(k));
}
const STYLE_INFO = {
  professional: { desc: "Tối giản, sang trọng cho doanh nghiệp", bg: "#ffffff", title: "#1E3A5F", accent: "#2F80ED", border: "#e3e8f0" },
  academic: { desc: "Trang trọng, rõ ràng cho giáo dục", bg: "#FBFAF6", title: "#6B1D2A", accent: "#B08D57", border: "#e8e1d2" },
  creative: { desc: "Hiện đại, nổi bật cho marketing", bg: "#111827", title: "#F9FAFB", accent: "#F472B6", border: "#111827" },
};
const LAYOUT_NAMES = {
  title: "Tiêu đề", section: "Phân đoạn", bullets: "Gạch đầu dòng", image_left: "Ảnh trái",
  image_right: "Ảnh phải", two_column: "So sánh 2 cột", process: "Quy trình", timeline: "Dòng thời gian",
  cards: "Thẻ lưới", stats: "Số liệu", summary: "Tổng kết",
};
const LANG_NAMES = { vi: "Tiếng Việt", en: "English" };

const state = { config: null, file: null, duration: null, jobId: null, pollTimer: null, uploadStart: null };

// ------------------------------------------------------------------ storage (best effort)
const store = {
  get(k, session) { try { return (session ? sessionStorage : localStorage).getItem(k); } catch { return null; } },
  set(k, v, session) { try { (session ? sessionStorage : localStorage).setItem(k, v); } catch { /* ignore */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

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

// ------------------------------------------------------------------ views
function showView(id) {
  const changed = $(id).hidden;
  for (const v of ["authView", "uploadView", "processView", "resultView"]) $(v).hidden = v !== id;
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

  setupAiImages(c.ai_images || {});

  for (const [code, name] of Object.entries(c.languages)) {
    const o = new Option(name, code, false, code === c.default_language);
    $("languageSelect").add(o);
  }
  for (let n = 5; n <= 20; n++) $("slideCountSelect").add(new Option(`${n} slide`, String(n)));

  const wrap = $("styleOptions");
  for (const [key, label] of Object.entries(c.styles)) {
    const info = STYLE_INFO[key] || { desc: "", bg: "#fff", title: "#333", accent: "#888", border: "#ddd" };
    const el = document.createElement("label");
    el.className = "style-opt";
    el.innerHTML = `
      <input type="radio" name="style" value="${key}" ${key === c.default_style ? "checked" : ""}>
      <span class="style-card">
        <span class="swatch" style="background:${info.bg};border:1px solid ${info.border};--sw-title:${info.title};--sw-accent:${info.accent}"></span>
        <span class="style-name"></span>
        <span class="style-desc"></span>
      </span>`;
    el.querySelector(".style-name").textContent = label;
    el.querySelector(".style-desc").textContent = info.desc;
    wrap.appendChild(el);
  }

  if (c.password_required && !store.get("appPassword", true)) { showView("authView"); return; }
  resumeOrUpload();
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

function resumeOrUpload() {
  const hash = new URLSearchParams(location.hash.slice(1)).get("job");
  const jobId = hash || store.get("lastJob");
  if (jobId && /^[0-9a-f]{32}$/.test(jobId)) {
    state.jobId = jobId;
    showView("processView");
    renderStages("queued");
    poll();
  } else {
    showView("uploadView");
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
  $("submitBtn").disabled = true;
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
    $("submitBtn").disabled = false;
  };
  v.onerror = () => {
    URL.revokeObjectURL(url);
    // The browser may lack a codec; the server re-validates anyway.
    $("fileDetails").textContent = `${fmtSize(file.size)} · không đọc được thời lượng trên trình duyệt`;
    $("submitBtn").disabled = false;
  };
  v.src = url;
}

function clearFile() {
  state.file = null;
  $("fileInput").value = "";
  $("fileInfo").hidden = true;
  $("dropzone").hidden = false;
  $("submitBtn").disabled = true;
  $("slideHint").textContent = "";
  setFileError("");
}

// ------------------------------------------------------------------ upload
function upload(ev) {
  ev.preventDefault();
  if (!state.file) return;
  const fd = new FormData();
  fd.append("file", state.file);
  fd.append("title", $("titleInput").value.trim());
  fd.append("language", $("languageSelect").value);
  fd.append("style", (document.querySelector('input[name="style"]:checked') || {}).value || "professional");
  fd.append("slide_count", $("slideCountSelect").value);
  fd.append("ai_images", aiImagesChosen() ? "true" : "false");
  fd.append("image_density", $("imageDensity").value);
  useImageStages(aiImagesChosen());

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
    state.jobId = body.job_id;
    store.set("lastJob", body.job_id);
    history.replaceState(null, "", `#job=${body.job_id}`);
    poll();
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
    const li = document.createElement("li");
    li.className = "stage";
    li.dataset.stage = key;
    let mark = String(i + 1);
    if (current === "completed" || i < idx) { li.classList.add("done"); mark = "✓"; }
    else if (i === idx) { li.classList.add(failed ? "failed" : "active"); if (failed) mark = "!"; }
    if (key === "completed" && current === "completed") { li.classList.add("done"); }
    li.innerHTML = `<span class="dot"></span><span class="label"></span><span class="meta"></span>`;
    li.querySelector(".dot").textContent = li.classList.contains("active") ? "" : mark;
    li.querySelector(".label").textContent = label;
    const nextKey = STAGES[i + 1] && STAGES[i + 1][0];
    if (stageTimes[key] && stageTimes[nextKey]) {
      li.querySelector(".meta").textContent = `${Math.max(1, Math.round(stageTimes[nextKey] - stageTimes[key]))} giây`;
    }
    list.appendChild(li);
  });
}

function setStageMeta(stage, text) {
  const el = document.querySelector(`.stage[data-stage="${stage}"] .meta`);
  if (el) el.textContent = text;
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
  $("processFile").textContent = job.filename || "";
  useImageStages(Boolean(job.options && job.options.illustrations && job.options.illustrations.enabled));
  // "queued" maps to the step right after upload in the UI.
  const stage = job.stage === "queued" ? "extracting_audio" : job.stage;

  if (job.status === "done") {
    renderStages("completed");
    showResult(job);
    return;
  }
  if (job.status === "failed") {
    renderStages(stage, true, job.stage_times || {});
    showError(job.error || "Đã xảy ra lỗi.", true);
    return;
  }
  showView("processView");
  $("processError").hidden = true;
  renderStages(stage, false, job.stage_times || {});
  const ip = job.illustrations;
  if (stage === "generating_images" && ip && ip.total) {
    // Real counts reported by the GPU worker, not an estimated percentage.
    setStageMeta("generating_images", `${ip.done}/${ip.total} ảnh` + (ip.failed ? ` · ${ip.failed} lỗi` : ""));
  }
  const qn = $("queueNote");
  if (job.status === "queued" && job.queue_length > 1) {
    qn.textContent = `Đang chờ: có ${job.queue_length - 1} video khác đang xử lý trước.`;
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

function showError(message, canRetry) {
  showView("processView");
  $("processTitle").textContent = "Không thể hoàn tất";
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
  for (const l of [...new Set(r.layouts || [])]) {
    const s = document.createElement("span");
    s.textContent = LAYOUT_NAMES[l] || l;
    lw.appendChild(s);
  }
  $("transcriptCard").hidden = true;
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
    const card = document.createElement("div");
    card.className = "illus-item";
    const ph = document.createElement("div");
    ph.className = "ph";
    ph.textContent = it.has_image ? "Đang tải ảnh…" : (it.error || "Chưa có ảnh");
    const cap = document.createElement("div");
    cap.className = "cap";
    const title = document.createElement("span");
    title.textContent = it.slide_title || it.slide_id;
    title.title = title.textContent;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn ghost";
    btn.textContent = "Tạo lại";
    btn.disabled = left <= 0;
    btn.addEventListener("click", () => regenerateIllustration(it.slide_id, btn));
    cap.append(title, btn);
    card.append(ph, cap);
    grid.appendChild(card);
    if (it.has_image) {
      api(`/api/jobs/${state.jobId}/illustrations/${it.slide_id}.jpg`)
        .then((res) => res.blob())
        .then((blob) => {
          const img = document.createElement("img");
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

async function downloadPptx() {
  const btn = $("downloadBtn");
  btn.disabled = true;
  try {
    const res = await api(`/api/jobs/${state.jobId}/download`);
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd);
    const name = m ? decodeURIComponent(m[1] || m[2]) : "presentation.pptx";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  } catch (e) {
    $("resultNotice").textContent = e.message;
    $("resultNotice").hidden = false;
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
      const row = document.createElement("div");
      row.className = "seg";
      const t = document.createElement("time");
      t.textContent = fmtClock(seg.start);
      const p = document.createElement("span");
      p.textContent = seg.text;
      row.append(t, p);
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
  state.jobId = null;
  store.del("lastJob");
  history.replaceState(null, "", location.pathname);
  clearFile();
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

// ------------------------------------------------------------------ events
$("fileInput").addEventListener("change", (e) => selectFile(e.target.files[0]));
$("clearFile").addEventListener("click", clearFile);
const dz = $("dropzone");
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => selectFile(e.dataTransfer.files[0]));
$("uploadForm").addEventListener("submit", upload);
$("downloadBtn").addEventListener("click", downloadPptx);
$("transcriptBtn").addEventListener("click", toggleTranscript);
$("newBtn").addEventListener("click", startOver);
$("restartBtn").addEventListener("click", startOver);
$("retryBtn").addEventListener("click", retry);
$("authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  store.set("appPassword", $("authInput").value, true);
  try {
    await api("/api/jobs/" + "0".repeat(32));
  } catch (err) {
    if (/mật khẩu/.test(err.message)) { $("authError").textContent = "Sai mật khẩu."; $("authError").hidden = false; return; }
  }
  $("authError").hidden = true;
  resumeOrUpload();
});

window.addEventListener("hashchange", () => {
  if (state.config && new URLSearchParams(location.hash.slice(1)).get("job") !== state.jobId) resumeOrUpload();
});

init();
