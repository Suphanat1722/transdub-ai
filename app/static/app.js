const $ = (selector) => document.querySelector(selector);
const state = { jobs: [], current: null, layer: "source", offset: 0, limit: 100, events: null, voices: [], defaultVoice: "" };
const ACTIVE_STATUSES = ["queued", "running", "extracting", "separating", "transcribing", "translating", "synthesizing", "muxing", "waiting_quota"];

const stageNames = {
  uploaded: "ดาวน์โหลด", extracted: "แยกแทร็ก", separated: "ตัดเสียงพูด",
  transcribed: "ตรวจคำบรรยาย", translated: "แปลไทย", synthesizing: "สร้างเสียง",
  synthesized: "พร้อมรวม", completed: "เสร็จแล้ว",
};
const statusNames = {
  queued: "รอคิว", running: "กําลังเดินงาน", downloading: "กำลังดาวน์โหลด",
  extracting: "กําลังแยกเสียง",
  separating: "กําลังตัดเสียงพูด",
  reviewing_transcript: "รอตรวจคำบรรยาย", translating: "กําลังแปล",
  reviewing_translation: "รอตรวจคําแปล", synthesizing: "กําลังสร้างเสียง",
  needs_review: "ต้องแก้ก่อนรวม", muxing: "กําลังประกอบวิดีโอ",
  waiting_quota: "กําลังรอลองใหม่", paused: "พักอยู่", failed: "ไม่สําเร็จ",
  cancelled: "ยกเลิกแล้ว", completed: "เสร็จแล้ว",
};

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || body.error || `HTTP ${response.status}`);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(message, bad = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast ${bad ? "bad" : ""}`;
  element.hidden = false;
  element.title = "คลิกเพื่อคัดลอก";
  element.onclick = async () => {
    try { await navigator.clipboard.writeText(message); } catch { /* clipboard ไม่พร้อม */ }
  };
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; }, 6000);
  toast.history = toast.history || [];
  toast.history.unshift({ at: new Date().toLocaleString("th-TH"), message, bad });
  if (toast.history.length > 30) toast.history.length = 30;
  renderToastLog();
}

function renderToastLog() {
  const log = $("#toast-log");
  if (!log) return;
  const items = toast.history || [];
  log.innerHTML = items.length
    ? items.map((item) => `<p class="${item.bad ? "bad" : ""}"><span>${escapeHtml(item.at)}</span> ${escapeHtml(item.message)}</p>`).join("")
    : '<p class="empty">ยังไม่มีแจ้งเตือน</p>';
}

function confirmModal({ title = "ยืนยัน?", message = "", okLabel = "ยืนยัน" } = {}) {
  const modal = $("#confirm-modal");
  $("#confirm-title").textContent = title;
  $("#confirm-msg").textContent = message;
  $("#confirm-ok").textContent = okLabel;
  modal.hidden = false;
  return new Promise((resolve) => {
    const done = (value) => {
      modal.hidden = true;
      $("#confirm-ok").onclick = null;
      $("#confirm-cancel").onclick = null;
      modal.onclick = null;
      document.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onKey = (event) => { if (event.key === "Escape") done(false); };
    $("#confirm-ok").onclick = () => done(true);
    $("#confirm-cancel").onclick = () => done(false);
    modal.onclick = (event) => { if (event.target === modal) done(false); };
    document.addEventListener("keydown", onKey);
    $("#confirm-ok").focus();
  });
}

function parseTimestamp(value) {
  // รับทั้ง ms (ตัวเลข) และ HH:MM:SS,mmm / MM:SS.mmm
  if (/^\d+$/.test(value.trim())) return Number(value.trim());
  const match = value.trim().match(/^(?:(\d+):)?([0-5]?\d):([0-5]?\d)[,.](\d{1,3})$/);
  if (!match) return NaN;
  const [, h = "0", m, s, ms] = match;
  return Number(h) * 3_600_000 + Number(m) * 60_000 + Number(s) * 1000 + Number(`${ms}`.padEnd(3, "0"));
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatMs(ms) {
  const value = Math.max(0, Math.round(Number(ms) || 0));
  const h = Math.floor(value / 3_600_000), m = Math.floor((value % 3_600_000) / 60_000);
  const s = Math.floor((value % 60_000) / 1000), rest = value % 1000;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")},${String(rest).padStart(3, "0")}`;
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    const ready = health.ffmpeg_available && health.edge_tts_available && health.gemini_configured;
    $("#health").textContent = ready ? "● ระบบพร้อม" : "● ต้องตั้งค่าระบบ";
    $("#health").classList.toggle("ok", ready);
    const issues = [];
    if (!health.ffmpeg_available) issues.push("ไม่พบ FFmpeg");
    if (!health.edge_tts_available) issues.push("Edge TTS เชื่อมต่อไม่ได้");
    if (!health.gemini_configured) issues.push("ยังไม่มี GEMINI_API_KEY");
    $("#system-warning").hidden = !issues.length;
    $("#system-warning").textContent = issues.join(" · ");
    // Show the key card on the landing page when Gemini is not configured yet,
    // exactly where the user notices the missing key in the warning.
    if (!health.gemini_configured) {
      const card = $("#api-key-card");
      // job-panel ใช้ hidden เป็น source of truth (ไม่มี class visible)
      if (card && $("#job-panel").hidden) card.hidden = false;
    } else {
      $("#api-key-card").hidden = true;
    }
  } catch (error) { $("#health").textContent = "● เชื่อมต่อ backend ไม่ได้"; }
}

async function loadVoices() {
  try {
    const [voices, defaults] = await Promise.all([api("/api/voices"), api("/api/voices/default")]);
    state.voices = voices;
    state.defaultVoice = defaults.voice || "";
  } catch (error) { state.voices = []; state.defaultVoice = ""; }
  renderVoiceSelects();
  const select = $("#voice-select");
  if (state.defaultVoice && [...select.options].some((option) => option.value === state.defaultVoice)) {
    select.value = state.defaultVoice;
  }
  const hint = $("#voice-hint");
  if (hint && !state.voices.length) {
    hint.textContent = "ดึงรายชื่อเสียงจาก Edge TTS ไม่ได้ (ต้องต่อเน็ต) — ใส่ชื่อเสียงเองได้ เช่น th-TH-NiwatNeural แล้วกดเริ่มงาน";
  }
  if (state.current) fillJobSettingsInputs(state.current);
}

function voiceGroups(showAll, filter) {
  const needle = (filter || "").trim().toLowerCase();
  const matches = (voice) => !needle
    || voice.short_name.toLowerCase().includes(needle)
    || (voice.locale || "").toLowerCase().includes(needle);
  const thai = state.voices.filter((voice) => (voice.locale || "").toLowerCase().startsWith("th") && matches(voice));
  const others = showAll
    ? state.voices.filter((voice) => !(voice.locale || "").toLowerCase().startsWith("th") && matches(voice))
    : [];
  return { thai, others };
}

function populateVoiceSelect(select, showAll, filter) {
  if (!select) return;
  const { thai, others } = voiceGroups(showAll, filter);
  if (!thai.length && !others.length) {
    if (!state.voices.length) {
      // Fallback เสียงไทยยอดนิยมเมื่อ Edge TTS เข้าถึงไม่ได้ — ยังส่งงานต่อได้
      const fallback = ["th-TH-NiwatNeural", "th-TH-PremwadeeNeural", "th-TH-AcharaNeural"];
      select.innerHTML = fallback.map((name) => `<option value="${name}">${name} (ออฟไลน์)</option>`).join("");
      return;
    }
    select.innerHTML = `<option value="">ไม่พบเสียงที่ค้นหา</option>`;
    return;
  }
  const option = (voice) => `<option value="${escapeHtml(voice.short_name)}">${escapeHtml(voice.label)}</option>`;
  select.innerHTML =
    `<optgroup label="เสียงไทย">${thai.map(option).join("")}</optgroup>` +
    (others.length ? `<optgroup label="เสียงภาษาอื่น">${others.map(option).join("")}</optgroup>` : "");
}

function renderVoiceSelects() {
  const showAll = $("#show-all-voices")?.checked;
  const filter = $("#voice-search")?.value;
  populateVoiceSelect($("#voice-select"), showAll, filter);
  populateVoiceSelect($("#job-voice"), true, "");
  if (state.current) fillJobSettingsInputs(state.current);
}

async function loadJobs() {
  state.jobs = await api("/api/jobs");
  renderJobList();
}

function jobMatchesFilter(job, filter) {
  if (filter === "active") return ACTIVE_STATUSES.includes(job.status);
  if (filter === "review") return ["reviewing_transcript", "reviewing_translation", "needs_review", "paused", "failed", "waiting_quota"].includes(job.status);
  if (filter === "done") return ["completed", "cancelled"].includes(job.status);
  return true;
}

function renderJobList() {
  const filter = document.querySelector(".job-filters .active")?.dataset.filter || "all";
  const needle = ($("#job-search")?.value || "").trim().toLowerCase();
  const counts = { all: state.jobs.length, active: 0, review: 0, done: 0 };
  state.jobs.forEach((job) => {
    if (jobMatchesFilter(job, "active")) counts.active += 1;
    if (jobMatchesFilter(job, "review")) counts.review += 1;
    if (jobMatchesFilter(job, "done")) counts.done += 1;
  });
  const countsEl = $("#job-counts");
  if (countsEl) countsEl.textContent = `ทั้งหมด ${counts.all} · กำลังทำ ${counts.active} · รอตรวจ ${counts.review} · เสร็จ ${counts.done}`;
  const visible = state.jobs.filter((job) => jobMatchesFilter(job, filter)
    && (!needle || (job.filename || "").toLowerCase().includes(needle) || job.id.toLowerCase().includes(needle)));
  $("#job-list").innerHTML = visible.length ? visible.map((job) => `
    <button class="job-item ${state.current?.id === job.id ? "active" : ""}" data-id="${job.id}">
      <strong>${escapeHtml(job.filename)}</strong>
      <span>${statusNames[job.status] || job.status}</span>
      <i><b style="width:${Number(job.progress || 0)}%"></b></i>
    </button>`).join("") : '<p class="empty">ไม่พบงานตามเงื่อนไข</p>';
  document.querySelectorAll(".job-item").forEach((button) => button.onclick = () => openJob(button.dataset.id));
  const bulk = $("#bulk-delete-btn");
  if (bulk) {
    const finished = state.jobs.filter((job) => ["completed", "cancelled", "failed"].includes(job.status) && !ACTIVE_STATUSES.includes(job.status));
    bulk.hidden = !finished.length;
    bulk.textContent = finished.length ? `ลบงานที่จบแล้วทั้งหมด (${finished.length})` : "ลบงานที่จบแล้วทั้งหมด";
    bulk.onclick = async () => {
      const ok = await confirmModal({ title: "ลบงานที่จบแล้ว?", message: `ลบ ${finished.length} งานพร้อมไฟล์ทั้งหมด? งานที่กำลังทำจะไม่ถูกลบ`, okLabel: "ลบทั้งหมด" });
      if (!ok) return;
      let failed = 0;
      for (const job of finished) {
        try { await api(`/api/jobs/${job.id}`, { method: "DELETE" }); } catch { failed += 1; }
      }
      await loadJobs();
      if (state.current && finished.some((job) => job.id === state.current.id)) showCreate();
      toast(failed ? `ลบเสร็จ มีพลาด ${failed} งาน` : `ลบ ${finished.length - failed} งานแล้ว`, !!failed);
    };
  }
}

function escapeHtml(value = "") {
  const span = document.createElement("span"); span.textContent = value; return span.innerHTML;
}

function withPreservedCues(previous, next) {
  // Actions/SSE ตอบกลับแบบไม่มี cues (include_cues=False) ถ้าเขียนทับดื้อ ๆ
  // state.current.cues จะหาย ทำให้กล่องยืนยันแก้ต้นฉบับไม่ขึ้นและปุ่มฟัง
  // ไม่อัปเดต — คงของเดิมไว้เมื่อ payload ใหม่ไม่มีมา
  if (next && previous) {
    if (next.cues === undefined) next.cues = previous.cues;
    if (next.source_cues === undefined) next.source_cues = previous.source_cues;
  }
  return next;
}

function showCreate() {
  if (state.events) state.events.close();
  state.current = null;
  $("#create-panel").hidden = false; $("#job-panel").hidden = true;
  const btn = $("#delete-job-btn");
  if (btn) btn.onclick = null;
  renderJobList();
  showStep(1);
}

function showStep(n) {
  state.step = n;
  document.querySelectorAll(".wizard-step").forEach((step) => { step.hidden = Number(step.dataset.step) !== n; });
  document.querySelectorAll("#create-steps li").forEach((item) => item.classList.toggle("active", Number(item.dataset.step) === n));
  if (n === 3) {
    const url = $("#youtube-url").value.trim();
    const voice = $("#voice-select").value || "(ยังไม่เลือกเสียง)";
    const fast = document.querySelector('input[name="separation_mode"]')?.checked ? " · โหมดเร็ว (ข้าม Demucs)" : "";
    $("#create-summary").textContent = `พร้อมเริ่ม: ${url} · เสียง ${voice}${fast}`;
  }
}

function validYoutubeUrl(value) {
  const url = (value || "").trim().toLowerCase();
  return url.includes("youtube.com/") || url.includes("youtu.be/");
}

async function openJob(id) {
  state.current = await api(`/api/jobs/${id}`);
  $("#create-panel").hidden = true; $("#job-panel").hidden = false;
  delete $("#translation-prompt").dataset["touched"];
  renderJob(); renderJobList();
  bindDeleteButton();
  state.offset = 0; await loadCues();
  $("#logs-card").hidden = false;
  if (!$("#logs-body").hidden) await loadLogs();
  if (state.events) state.events.close();
  if (!["completed", "failed", "cancelled", "needs_review"].includes(state.current.status)) {
    connectEvents(id);
  }
}

function connectEvents(id) {
  if (state.events) { try { state.events.close(); } catch { /* ignore */ } }
  const source = new EventSource(`/api/jobs/${id}/events`);
  state.events = source;
  source.onmessage = async (event) => {
      const previous = state.current;
      state.current = withPreservedCues(previous, JSON.parse(event.data));
      // Close synchronously for terminal jobs: the server already ended the
      // stream, and deferring close past the awaits below lets a stray error
      // win the race and flap the "reconnecting" message forever.
      if (["completed", "failed", "cancelled", "needs_review"].includes(state.current.status)) source.close();
      renderJob(); await loadJobs();
      if (!$("#logs-body").hidden) loadLogs();
      // Reload the cue list whenever the visible layer gains (or loses) cues,
      // so fresh subtitles/translations appear without a manual refresh.
      const countOf = (job) => state.layer === "source" ? job?.total_source_cues : job?.total_cues;
      if (previous && countOf(previous) !== countOf(state.current)) {
        state.offset = 0;
        await loadCues();       // the visible layer was (re)built: re-render the page
      } else {
        syncCueRows();          // same set of cues: patch status bits in place
      }
    };
  source.onerror = () => {
    // Server restarts kill the stream; keep retrying forever (5s) instead of
    // going silent — the loop stops itself on terminal statuses or job switch.
    $("#job-message").textContent = "การเชื่อมต่อขาดหาย กำลังลองเชื่อมต่อใหม่…";
    source.close();
    if (state.current && state.current.id === id) {
      setTimeout(() => {
        if (state.current && state.current.id === id && !["completed", "failed", "cancelled", "needs_review"].includes(state.current.status)) {
          connectEvents(id);
        }
      }, 5000);
    }
  };
}

function renderJob() {
  const job = state.current; if (!job) return;
  $("#job-id").textContent = job.id;
  $("#job-name").textContent = job.filename;
  $("#job-status").textContent = statusNames[job.status] || job.status;
  $("#job-status").className = `badge ${job.status}`;
  const progressBar = $("#job-progress");
  progressBar.style.width = `${Number(job.progress || 0)}%`;
  progressBar.parentElement.title = `ขั้น ${stageNames[job.stage] || job.stage} · ${Number(job.progress || 0).toFixed(0)}%`;
  renderTranslationProgress(job);
  let message = job.error || job.wait_reason || `คืบหน้า ${Number(job.progress || 0).toFixed(0)}% · ขั้น ${stageNames[job.stage] || job.stage}`;
  const tp = job.translation_progress;
  if (tp && tp.chunks_total && ["translating", "waiting_quota"].includes(job.status)) {
    message += ` · แปลช่วง ${tp.current_chunk || 0}/${tp.chunks_total}`;
    if (job.translation_model) message += ` (${job.translation_model})`;
  }
  if (job.stage === "synthesizing" && job.total_cues) {
    const done = job.completed_cues || 0;
    const total = job.total_cues;
    const cueNo = done < total ? done + 1 : total;
    message = `กําลังสร้างเสียง cue ${cueNo}/${total} (${done} เสร็จ) · ${Number(job.progress || 0).toFixed(0)}%`;
  }
  $("#job-message").textContent = message;
  // รวม synthesizing+synthesized เป็นขั้นเดียว "สร้างเสียง" ให้ตรงความรู้สึกผู้ใช้
  const order = ["uploaded", "extracted", "separated", "transcribed", "translated", "synthesizing", "completed"];
  const stageKey = job.stage === "synthesized" ? "synthesizing" : job.stage;
  const current = Math.max(0, order.indexOf(stageKey));
  $("#stage-track").innerHTML = order.map((stage, index) => `<span class="${index < current ? "done" : index === current ? "active" : ""}">${stageNames[stage]}</span>`).join("");
  const warnings = job.warnings || [];
  $("#job-warnings").innerHTML = warnings.length > 5
    ? warnings.slice(0, 5).map((warning) => `<p>⚠ ${escapeHtml(warning)}</p>`).join("")
      + `<p><button class="secondary small" id="warnings-more">แสดงทั้งหมด (${warnings.length})</button></p>`
    : warnings.map((warning) => `<p>⚠ ${escapeHtml(warning)}</p>`).join("");
  $("#warnings-more") && ($("#warnings-more").onclick = () => {
    $("#job-warnings").innerHTML = warnings.map((warning) => `<p>⚠ ${escapeHtml(warning)}</p>`).join("");
  });
  renderActions(job); renderArtifacts(job.artifacts || []); renderTranslationTools(job); renderJobSettings(job);
  if (job.status === "queued") loadQueue(job);
}

async function loadQueue(job) {
  const now = Date.now();
  if (loadQueue.at && now - loadQueue.at < 5000 && loadQueue.for === job.id) return;
  loadQueue.at = now; loadQueue.for = job.id;
  try {
    const queue = await api(`/api/jobs/${job.id}/queue`);
    if (state.current && state.current.id === job.id && queue.position > 0) {
      $("#job-message").textContent += ` · คิวที่ ${queue.position} จาก ${queue.queued} งานรอ`;
    }
  } catch { /* queue ไม่พร้อม ไม่ต้องบล็อก UI */ }
}

function renderTranslationTools(job) {
  // Show the Gemini system-prompt box only while reviewing the transcript in
  // the normal (Gemini) mode, so the user sets the prompt before translating.
  const tools = $("#translation-tools");
  tools.hidden = !(job.status === "reviewing_transcript" && job.mode !== "import");
  const box = $("#translation-prompt");
  if (!tools.hidden && !box.dataset.touched) box.value = job.translation_prompt || "";
}

async function savePrompt() {
  const value = $("#translation-prompt").value;
  try {
    state.current = withPreservedCues(state.current, await api(`/api/jobs/${state.current.id}/translation-prompt`, {
      method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ prompt: value }),
    }));
    toast("บันทึก prompt แล้ว");
  } catch (error) { toast(error.message, true); }
}

function renderJobSettings(job) {
  const card = $("#job-settings");
  card.hidden = false;
  const editable = !ACTIVE_STATUSES.includes(job.status);
  $("#save-job-settings").disabled = !editable;
  $("#save-job-settings").textContent = editable ? "บันทึกการตั้งค่า" : "งานกำลังเดิน หยุดก่อนแก้ตั้งค่า";
  // Don't clobber inputs the user is currently editing (SSE fires every second).
  if (card.contains(document.activeElement) && document.activeElement !== card) return;
  fillJobSettingsInputs(job);
}

function fillJobSettingsInputs(job) {
  const select = $("#job-voice");
  populateVoiceSelect(select, true, "");
  const currentVoice = job.voice || state.defaultVoice || "";
  if (currentVoice && ![...select.options].some((option) => option.value === currentVoice)) {
    const option = document.createElement("option");
    option.value = currentVoice; option.textContent = currentVoice;
    select.appendChild(option);
  }
  select.value = currentVoice;
  $("#job-rate").value = job.tts_rate || 0;
  $("#job-rate-value").textContent = `${job.tts_rate || 0}%`;
  $("#job-bg").value = job.background_volume ?? 100;
  $("#job-bg-value").textContent = `${job.background_volume ?? 100}%`;
  $("#job-voice-vol").value = job.voice_volume ?? 100;
  $("#job-voice-vol-value").textContent = `${job.voice_volume ?? 100}%`;
  $("#job-output-dir").value = job.output_dir || "";
}

async function saveJobSettings() {
  const job = state.current; if (!job) return;
  const body = {};
  const voice = $("#job-voice").value;
  const rate = Number($("#job-rate").value);
  const bg = Number($("#job-bg").value);
  const voiceVolume = Number($("#job-voice-vol").value);
  const outputDir = $("#job-output-dir").value.trim();
  if (voice && voice !== job.voice) body.voice = voice;
  if (rate !== Number(job.tts_rate || 0)) body.tts_rate = rate;
  if (bg !== Number(job.background_volume ?? 100)) body.background_volume = bg;
  if (voiceVolume !== Number(job.voice_volume ?? 100)) body.voice_volume = voiceVolume;
  if (outputDir !== (job.output_dir || "")) body.output_dir = outputDir;
  if (!Object.keys(body).length) { toast("ยังไม่มีอะไรเปลี่ยน"); return; }
  if ((body.voice || body.tts_rate !== undefined) && job.total_cues) {
    const ok = await confirmModal({ title: "สร้างเสียงใหม่ทั้งหมด?", message: "เปลี่ยนเสียง/อัตราการพูดจะลบเสียงที่สร้างไว้และสร้างใหม่ทั้งหมด (ข้อความและคำแปลคงเดิม) ทำต่อไหม?", okLabel: "สร้างใหม่" });
    if (!ok) return;
  }
  try {
    state.current = withPreservedCues(state.current, await api(`/api/jobs/${job.id}`, {
      method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    }));
    toast("บันทึกการตั้งค่าแล้ว");
    renderJob(); renderJobList();
    if (body.voice || body.tts_rate !== undefined) syncCueRows();
  } catch (error) { toast(error.message, true); }
}

function renderTranslationProgress(job) {
  const box = $("#translate-progress");
  if (!box) return;
  const tp = job.translation_progress;
  const translating = ["translating", "waiting_quota"].includes(job.status);
  if (!tp || !tp.chunks_total || !translating) { box.hidden = true; return; }
  box.hidden = false;
  $("#translate-progress-bar").style.width = `${tp.progress}%`;
  const current = tp.current_chunk || 0;
  let hint = `แปลช่วง ${current}/${tp.chunks_total}`;
  if (job.translation_model) hint += ` · ใช้ ${job.translation_model}`;
  if (tp.chunks_failed > 0) hint += ` · พลาด ${tp.chunks_failed} ช่วง`;
  if (job.status === "waiting_quota") hint += " · รอ quota อยู่";
  $("#translate-hint").textContent = hint;
}

function renderActions(job) {
  const actions = [];
  const active = ACTIVE_STATUSES.includes(job.status);
  if (active) actions.push(["pause", "พัก"], ["cancel", "ยกเลิก"]);
  if (["paused", "waiting_quota"].includes(job.status)) actions.push(["resume", "ทําต่อ"]);
  if (["failed", "needs_review"].includes(job.status)) actions.push(["retry", "ลองต่อ"]);
  if (job.status === "reviewing_transcript") actions.push(["approve_transcript", "ยืนยัน transcript และแปลต่อ"]);
  if (job.status === "reviewing_translation") actions.push(["approve_translation", "ยืนยันคําแปลและสร้างเสียง"], ["retranslate", "แปลใหม่"]);
  if (job.status === "needs_review") actions.push(["retranslate", "แปลใหม่"]);
  if (job.status === "completed" && job.artifacts?.some((item) => item.kind === "dub_wav")) actions.push(["remux", "มิกซ์ MP4 ใหม่"]);
  if (job.status === "completed") actions.push(["reassemble", "ประกอบเสียงใหม่"]);
  $("#actions").innerHTML = actions.map(([action, label], index) => {
    const cls = index === 0 ? "primary" : "secondary";
    return `<button data-action="${action}" class="${cls}">${label}</button>`;
  }).join("");
  document.querySelectorAll("#actions button").forEach((button) => button.onclick = () => runAction(button.dataset.action));
}

function bindDeleteButton() {
  const button = $("#delete-job-btn");
  button.onclick = () => runAction("delete");
}

async function runAction(action) {
  if (action === "delete") {
    const ok = await confirmModal({ title: "ลบงานนี้?", message: "ลบงานนี้พร้อมไฟล์ผลลัพธ์ทั้งหมด? การกระทำนี้ย้อนกลับไม่ได้", okLabel: "ลบงาน" });
    if (!ok) return;
    try {
      await api(`/api/jobs/${state.current.id}`, { method: "DELETE" });
      toast("ลบงานแล้ว"); state.events?.close(); await loadJobs(); showCreate();
    } catch (error) { toast(error.message, true); }
    return;
  }
  try {
    state.current = withPreservedCues(state.current, await api(`/api/jobs/${state.current.id}/actions`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ action }) }));
    renderJob(); await loadJobs();
    if (["resume", "retry", "approve_transcript", "approve_translation", "remux", "reassemble", "retranslate"].includes(action)) openJob(state.current.id);
  } catch (error) { toast(error.message, true); }
}

function renderArtifacts(items) {
  const labels = { background: "เสียงพื้นหลัง", source_srt: "SRT ต้นฉบับ", translated_srt: "SRT ภาษาไทย", dub_wav: "เสียงพากย์ WAV", dub_mp3: "เสียงพากย์ MP3", report_json: "รายงาน JSON", report_csv: "รายงาน CSV", final_video: "วิดีโอพากย์ไทย" };
  $("#artifacts").innerHTML = items.length ? items.map((item) => `<a href="${item.download_url}"><span>↓</span>${labels[item.kind] || item.kind}</a>`).join("") : '<p class="empty">ไฟล์จะปรากฏเมื่อแต่ละขั้นเสร็จ</p>';
  const player = $("#preview-player");
  if (!player) return;
  const video = items.find((item) => item.kind === "final_video");
  const audio = items.find((item) => item.kind === "dub_mp3") || items.find((item) => item.kind === "dub_wav");
  if (video) {
    player.hidden = false;
    player.innerHTML = `<video controls preload="metadata" src="${video.download_url}"></video><p class="muted-note">ตัวอย่างวิดีโอสุดท้าย — ถ้าเล่นไม่ได้ให้กด ↓ ดาวน์โหลดแทน</p>`;
  } else if (audio) {
    player.hidden = false;
    player.innerHTML = `<audio controls preload="metadata" src="${audio.download_url}"></audio><p class="muted-note">ฟังเสียงพากย์ฉบับเต็มก่อนรวมวิดีโอ</p>`;
  } else {
    player.hidden = true;
    player.innerHTML = "";
  }
}

async function loadCues() {
  if (!state.current) return;
  let data = await api(`/api/jobs/${state.current.id}/cues?layer=${state.layer}&offset=${state.offset}&limit=${state.limit}`);
  if (data.total > 0 && state.offset >= data.total) {
    state.offset = 0;
    data = await api(`/api/jobs/${state.current.id}/cues?layer=${state.layer}&offset=0&limit=${state.limit}`);
  }
  state.cueTotal = data.total;
  $("#cue-list").innerHTML = data.items.length ? data.items.map((cue) => `
    <article class="cue" data-id="${cue.id}">
      <div class="cue-meta"><b>#${cue.position}</b><input class="start" type="number" value="${cue.start_ms}" min="0" title="เวลาเริ่ม (ms)"><span>→</span><input class="end" type="number" value="${cue.end_ms}" min="1" title="เวลาจบ (ms)"><small>ms</small><small class="t-read"></small></div>
      <div class="cue-ts"><input class="start-ts" value="${formatMs(cue.start_ms)}" title="เวลาเริ่ม HH:MM:SS,mmm"><span>→</span><input class="end-ts" value="${formatMs(cue.end_ms)}" title="เวลาจบ HH:MM:SS,mmm"><small class="cue-err" hidden></small></div>
      <textarea>${escapeHtml(cue.text)}</textarea>
      <div class="cue-foot"><span>${(cue.warnings || []).map(escapeHtml).join(" · ")}</span>
        <span class="cue-btns">
          ${cue.status === "completed" && cue.audio_path ? `<button class="cue-play" title="ฟังเสียง">▶</button>` : ""}
          ${hasBackground() && state.layer === "translation" && cue.status === "completed" && cue.audio_path ? `<button class="cue-preview" title="ฟังเสียงพูดผสมเสียงพื้นหลัง">🎚</button>` : ""}
          ${state.layer === "translation" ? `<button class="cue-regenerate" title="สร้างเสียงใหม่">↻</button>` : ""}
          <button class="save-cue">บันทึก</button>
        </span>
      </div>
    </article>`).join("") : '<p class="empty">ขั้นนี้ยังไม่มี cue</p>';
  const page = Math.floor(data.offset / data.limit) + 1, pages = Math.max(1, Math.ceil(data.total / data.limit));
  $("#page-info").textContent = `หน้า ${page}/${pages} · ${data.total} cues`;
  $("#prev-page").disabled = data.offset === 0; $("#next-page").disabled = data.offset + data.limit >= data.total;
  document.querySelectorAll(".save-cue").forEach((button) => button.onclick = () => saveCue(button.closest(".cue")));
  document.querySelectorAll(".cue-play").forEach((button) => button.onclick = () => playCueAudio(button.closest(".cue")));
  document.querySelectorAll(".cue-preview").forEach((button) => button.onclick = () => playCuePreview(button.closest(".cue")));
  document.querySelectorAll(".cue-regenerate").forEach((button) => button.onclick = () => regenerateCue(button.closest(".cue")));
  document.querySelectorAll(".cue").forEach((article) => {
    const start = article.querySelector(".start"), end = article.querySelector(".end");
    const startTs = article.querySelector(".start-ts"), endTs = article.querySelector(".end-ts");
    const readout = article.querySelector(".t-read"), err = article.querySelector(".cue-err");
    const saveBtn = article.querySelector(".save-cue");
    const syncFromMs = () => {
      startTs.value = formatMs(Number(start.value || 0));
      endTs.value = formatMs(Number(end.value || 0));
      readout.textContent = "";
      validateCueRow(article);
    };
    const syncFromTs = (which) => {
      const parsed = parseTimestamp((which === "start" ? startTs : endTs).value);
      if (!Number.isNaN(parsed)) (which === "start" ? start : end).value = parsed;
      validateCueRow(article);
    };
    start.oninput = syncFromMs; end.oninput = syncFromMs;
    startTs.oninput = () => syncFromTs("start"); endTs.oninput = () => syncFromTs("end");
    article.querySelector("textarea").oninput = () => { article.classList.add("dirty"); updateSaveAll(); validateCueRow(article); };
    start.oninput = (e) => { article.classList.add("dirty"); updateSaveAll(); syncFromMs(); };
    end.oninput = (e) => { article.classList.add("dirty"); updateSaveAll(); syncFromMs(); };
    err.hidden = true;
    saveBtn.disabled = false;
  });
  applyCueSearch();
  updateSaveAll();
  validateAllCueRows();
}

function cueRows() { return [...document.querySelectorAll(".cue")]; }

function validateCueRow(article) {
  const rows = cueRows();
  const index = rows.indexOf(article);
  const err = article.querySelector(".cue-err");
  const saveBtn = article.querySelector(".save-cue");
  const start = Number(article.querySelector(".start").value || 0);
  const end = Number(article.querySelector(".end").value || 0);
  let message = "";
  if (!(end > start)) message = "เวลาจบต้องมากกว่าเวลาเริ่ม";
  else if (index > 0 && start < Number(rows[index - 1].querySelector(".end").value || 0)) message = "เวลาเริ่มทับ cue ก่อนหน้า";
  else if (index + 1 < rows.length && end > Number(rows[index + 1].querySelector(".start").value || 0)) message = "เวลาจบทับ cue ถัดไป";
  err.textContent = message;
  err.hidden = !message;
  article.classList.toggle("invalid", !!message);
  saveBtn.disabled = !!message;
  return !message;
}

function validateAllCueRows() { cueRows().forEach(validateCueRow); }

function applyCueSearch() {
  const needle = ($("#cue-search")?.value || "").trim().toLowerCase();
  cueRows().forEach((article) => {
    const text = article.querySelector("textarea").value.toLowerCase();
    const id = article.querySelector("b")?.textContent.toLowerCase() || "";
    article.style.display = !needle || text.includes(needle) || id.includes(needle) ? "" : "none";
  });
}

function updateSaveAll() {
  const btn = $("#save-all-cues");
  if (!btn) return;
  const dirty = document.querySelectorAll(".cue.dirty").length;
  btn.textContent = dirty ? `บันทึกทั้งหมด (${dirty})` : "บันทึกทั้งหมด";
  btn.disabled = !dirty;
}

async function saveAllCues() {
  const dirty = cueRows().filter((article) => article.classList.contains("dirty") && article.style.display !== "none");
  if (!dirty.length) { toast("ยังไม่มีอะไรเปลี่ยน"); return; }
  if (dirty.some((article) => article.classList.contains("invalid"))) { toast("มี cue เวลาทับกัน — แก้ก่อนบันทึก", true); return; }
  // ถามครั้งเดียวแม้มี source-layer (จะล้างคำแปลทั้งงาน)
  if (state.layer === "source" && state.current.cues?.length) {
    const ok = await confirmModal({ title: "แก้ต้นฉบับล้างคำแปล?", message: `บันทึก ${dirty.length} cue ต้นฉบับจะลบคำแปลและเสียงทั้งหมด บันทึกต่อไหม?`, okLabel: "บันทึกต่อ" });
    if (!ok) return;
    for (const article of dirty) {
      await saveCue(article, { skipConfirm: true });
      article.classList.remove("dirty");
    }
  } else {
    let failed = 0;
    for (const article of dirty) {
      try { await saveCue(article, { skipConfirm: true }); article.classList.remove("dirty"); }
      catch { failed += 1; }
    }
    toast(failed ? `บันทึกเสร็จ มีพลาด ${failed} cue` : `บันทึก ${dirty.length} cue แล้ว`, !!failed);
  }
  updateSaveAll();
}

function hasBackground() {
  return (state.current?.artifacts || []).some((item) => item.kind === "background");
}

function makeCueButton(article, className, title, text, handler) {
  const button = document.createElement("button");
  button.className = className; button.title = title; button.textContent = text;
  button.onclick = () => handler(button.closest(".cue"));
  article.querySelector(".cue-btns").prepend(button);
}

function syncCueRows() {
  // Patch status-dependent bits of the rendered cue rows (warnings, play
  // buttons) without re-rendering the whole list, so scroll and edits survive.
  if (!state.current) return;
  const cuesById = new Map((state.current.cues || []).map((cue) => [String(cue.id), cue]));
  document.querySelectorAll(".cue").forEach((article) => {
    const cue = cuesById.get(article.dataset.id);
    if (!cue) return;
    article.querySelector(".cue-foot span:first-child").textContent = (cue.warnings || []).join(" · ");
    const playable = cue.status === "completed" && cue.audio_path;
    if (playable && !article.querySelector(".cue-play")) {
      makeCueButton(article, "cue-play", "ฟังเสียง", "▶", playCueAudio);
    }
    if (!playable) article.querySelector(".cue-play")?.remove();
    if (playable && hasBackground() && state.layer === "translation" && !article.querySelector(".cue-preview")) {
      makeCueButton(article, "cue-preview", "ฟังเสียงพูดผสมเสียงพื้นหลัง", "🎚", playCuePreview);
    }
    if (!playable || !hasBackground() || state.layer !== "translation") {
      article.querySelector(".cue-preview")?.remove();
    }
  });
}

async function refreshJob() {
  if (!state.current) return;
  state.current = await api(`/api/jobs/${state.current.id}`);
  renderJob(); renderJobList();
}

async function playCueAudio(element) {
  const url = `/api/jobs/${state.current.id}/cues/${element.dataset.id}/audio`;
  const audio = new Audio(url);
  audio.play().catch(() => toast("ไม่สามารถเล่นเสียงได้", true));
}

function playCuePreview(element) {
  const url = `/api/jobs/${state.current.id}/cues/${element.dataset.id}/preview`;
  const audio = new Audio(url);
  audio.play().catch(() => toast("ผสมเสียงตัวอย่างไม่สำเร็จ", true));
}

async function regenerateCue(element) {
  const id = element.dataset.id;
  const ok = await confirmModal({ title: "สร้างเสียง cue นี้ใหม่?", message: `สร้างเสียงของ cue #${element.querySelector("b")?.textContent || id} ใหม่?`, okLabel: "สร้างใหม่" });
  if (!ok) return;
  try {
    await api(`/api/jobs/${state.current.id}/actions`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ action: "regenerate_cue", cue_id: Number(id) }),
    });
    toast("กําลังสร้างเสียง cue ใหม่");
    await refreshJob(); syncCueRows();
  } catch (error) { toast(error.message, true); }
}

async function saveCue(element, options = {}) {
  // Editing the source layer discards the whole translation layer downstream;
  // say so before doing it.
  if (!options.skipConfirm && state.layer === "source" && state.current.cues?.length) {
    const ok = await confirmModal({ title: "แก้ต้นฉบับล้างคำแปล?", message: "การแก้ต้นฉบับจะลบคำแปลและเสียงที่สร้างไว้ทั้งหมด แล้วแปล/สร้างเสียงใหม่ บันทึกต่อไหม?", okLabel: "บันทึกต่อ" });
    if (!ok) return;
  }
  try {
    await api(`/api/jobs/${state.current.id}/cues/${element.dataset.id}`, {
      method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        layer: state.layer, text: element.querySelector("textarea").value,
        start_ms: Number(element.querySelector(".start").value), end_ms: Number(element.querySelector(".end").value),
      }),
    });
    toast("บันทึก cue แล้ว และล้างผลลัพธ์ถัดไปที่เกี่ยวข้อง");
    await refreshJob(); syncCueRows();
  } catch (error) { toast(error.message, true); }
}

$("#job-form").onsubmit = async (event) => {
  event.preventDefault();
  const urlInput = $("#youtube-url");
  if (!urlInput.value.trim()) {
    toast("กรุณากรอกลิงก์ YouTube ก่อนเริ่มพากย์", true);
    showStep(1); urlInput.focus();
    return;
  }
  if (!validYoutubeUrl(urlInput.value)) {
    toast("ลิงก์ต้องเป็น YouTube (youtube.com หรือ youtu.be)", true);
    showStep(1); urlInput.focus();
    return;
  }
  if (!$("#voice-select").value) {
    toast("กรุณาเลือกเสียงพากย์ก่อน", true);
    showStep(2);
    return;
  }
  const submit = $("#create-submit");
  const progress = $("#create-progress");
  submit.disabled = true;
  if (progress) { progress.hidden = false; progress.textContent = "กำลังสร้างงาน…"; }
  try {
    const form = new FormData(event.currentTarget);
    // checkbox ที่ไม่ติ๊กจะไม่ถูกส่ง — เติมให้ชัดว่า false
    if (!form.has("pause_after_transcription")) form.append("pause_after_transcription", "false");
    if (!form.has("pause_after_translation")) form.append("pause_after_translation", "false");
    const response = await fetch("/api/jobs", { method: "POST", body: form });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "สร้างงานไม่สําเร็จ");
    await loadJobs();
    await openJob(body.id);
    toast("สร้างงานแล้ว กำลังเริ่มพากย์อัตโนมัติ");
  } catch (error) {
    toast(error.message || "สร้างงานไม่สําเร็จ (ตรวจอินเทอร์เน็ต)", true);
  } finally {
    submit.disabled = false;
    if (progress) progress.hidden = true;
  }
};

function bindRanges() {
  document.querySelectorAll('#job-form input[type="range"]').forEach((input) => input.oninput = () => {
    const label = input.name === "voice_volume" ? "voice" : input.name === "tts_rate" ? "tts-rate" : "background";
    $(`#${label}-value`).textContent = `${input.value}%`;
  });
  $("#job-rate").oninput = () => { $("#job-rate-value").textContent = `${$("#job-rate").value}%`; };
  $("#job-bg").oninput = () => { $("#job-bg-value").textContent = `${$("#job-bg").value}%`; };
  $("#job-voice-vol").oninput = () => { $("#job-voice-vol-value").textContent = `${$("#job-voice-vol").value}%`; };
}
bindRanges();
$("#voice-search").oninput = renderVoiceSelects;
$("#show-all-voices").onchange = renderVoiceSelects;
$("#save-job-settings").onclick = saveJobSettings;
document.querySelectorAll(".tabs button").forEach((button) => button.onclick = async () => { document.querySelectorAll(".tabs button").forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.layer = button.dataset.layer; state.offset = 0; await loadCues(); });
$("#prev-page").onclick = () => { state.offset = Math.max(0, state.offset - state.limit); loadCues(); };
$("#next-page").onclick = () => { state.offset += state.limit; loadCues(); };
$("#new-job-tab").onclick = showCreate; $("#refresh-jobs").onclick = loadJobs;
$("#to-step-2").onclick = () => {
  const urlInput = $("#youtube-url");
  if (!urlInput.value.trim()) { toast("กรุณากรอกลิงก์ YouTube ก่อน", true); urlInput.focus(); return; }
  if (!validYoutubeUrl(urlInput.value)) {
    $("#url-msg").textContent = "ลิงก์ต้องเป็น YouTube (youtube.com หรือ youtu.be)";
    toast("ลิงก์ต้องเป็น YouTube", true); urlInput.focus(); return;
  }
  $("#url-msg").textContent = "";
  showStep(2);
};
$("#back-step-1").onclick = () => showStep(1);
$("#to-step-3").onclick = () => {
  if (!$("#voice-select").value) { toast("กรุณาเลือกเสียงพากย์ก่อน", true); return; }
  showStep(3);
};
$("#back-step-2").onclick = () => showStep(2);
document.querySelectorAll("#create-steps li").forEach((item) => item.onclick = () => {
  const target = Number(item.dataset.step);
  if (target < (state.step || 1)) showStep(target);
});
$("#translation-prompt").addEventListener("input", () => { $("#translation-prompt").dataset.touched = "1"; });
$("#save-prompt").onclick = savePrompt;

$("#pick-folder-btn").onclick = async () => {
  try {
    const res = await api("/api/jobs/pick-folder", { method: "POST" });
    if (res && res.path) {
      $("#output-dir").value = res.path;
      $("#folder-msg").textContent = "";
    } else {
      toast("ไม่ได้เลือกโฟลเดอร์ (พิมพ์ path เองหรือกดปุ่มตรวจ)");
    }
  } catch (error) { toast(`${error.message} — พิมพ์ path เองแล้วกดปุ่มตรวจ`, true); }
};

$("#check-folder-btn").onclick = async () => {
  const value = $("#output-dir").value.trim();
  const msg = $("#folder-msg");
  if (!value) { msg.textContent = "ว่าง = โฟลเดอร์ Output กลาง (ใช้ได้)"; return; }
  try {
    const res = await api("/api/jobs/validate-folder", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ path: value }),
    });
    msg.textContent = res.ok ? "✓ โฟลเดอร์ใช้ได้" : "โฟลเดอร์นี้ใช้ไม่ได้";
  } catch (error) { msg.textContent = error.message; }
};

async function loadLogs() {
  if (!state.current) return;
  const card = $("#logs-card");
  card.hidden = false;
  const body = $("#logs-body");
  try {
    const data = await api(`/api/jobs/${state.current.id}/logs`);
    const attempts = (data.attempts || []).slice(-20).reverse();
    body.innerHTML = `
      <p class="muted-note">โหมดแยกเสียง: <b>${escapeHtml(data.separation_mode || "demucs")}</b> · error: ${escapeHtml(data.error || "-")} · warnings: ${(data.warnings || []).length}</p>
      ${(data.warnings || []).map((w) => `<p>⚠ ${escapeHtml(w)}</p>`).join("")}
      ${attempts.length ? `<table class="logs-table"><thead><tr><th>เวลา</th><th>ขั้น</th><th>ผล</th><th>รายละเอียด</th></tr></thead><tbody>${attempts.map((a) => `<tr><td>${escapeHtml((a.created_at || "").slice(0, 19).replace("T", " "))}</td><td>${escapeHtml(a.stage || "")}</td><td>${escapeHtml(a.outcome || "")}</td><td>${escapeHtml((a.message || a.model || "").slice(0, 160))}</td></tr>`).join("")}</tbody></table>` : '<p class="empty">ยังไม่มีบันทึกขั้น (งานเพิ่งสร้าง)</p>'}`;
  } catch (error) {
    body.innerHTML = `<p class="empty">โหลดบันทึกไม่สำเร็จ: ${escapeHtml(error.message)}</p>`;
  }
}

$("#toast-history-toggle").onclick = () => {
  const log = $("#toast-log");
  log.hidden = !log.hidden;
  $("#toast-history-toggle").textContent = log.hidden ? "ประวัติ" : "ซ่อน";
};

$("#job-search").oninput = renderJobList;
document.querySelectorAll(".job-filters button").forEach((button) => button.onclick = () => {
  document.querySelectorAll(".job-filters button").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  renderJobList();
});
$("#cue-search").oninput = applyCueSearch;
$("#save-all-cues").onclick = saveAllCues;
$("#logs-toggle").onclick = async () => {
  const body = $("#logs-body");
  body.hidden = !body.hidden;
  $("#logs-toggle").textContent = body.hidden ? "แสดง" : "ซ่อน";
  if (!body.hidden) await loadLogs();
};

$("#api-key-toggle").onclick = () => {
  const box = $("#api-key-box");
  box.hidden = !box.hidden;
  $("#api-key-toggle").textContent = box.hidden ? "เปิด" : "ซ่อน";
  if (!box.hidden) $("#api-key-input").focus();
};
$("#api-key-cancel").onclick = () => {
  $("#api-key-box").hidden = true;
  $("#api-key-toggle").textContent = "เปิด";
  $("#api-key-msg").textContent = "";
};
$("#api-key-save").onclick = async () => {
  const value = $("#api-key-input").value.trim();
  if (!value) { $("#api-key-msg").textContent = "กรุณาใส่ API key"; return; }
  $("#api-key-msg").textContent = "กำลังบันทึก...";
  try {
    await api("/api/settings/local/api-key", {
      method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ api_key: value }),
    });
    $("#api-key-msg").textContent = "บันทึกแล้ว ✓";
    $("#api-key-box").hidden = true;
    $("#api-key-toggle").textContent = "เปิด";
    $("#api-key-input").value = "";
    await loadHealth();
  } catch (error) { $("#api-key-msg").textContent = error.message; }
};

await Promise.all([loadHealth(), loadVoices(), loadJobs()]);
