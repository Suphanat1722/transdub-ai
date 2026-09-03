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
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; }, 4000);
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
    select.innerHTML = `<option value="">${state.voices.length ? "ไม่พบเสียงที่ค้นหา" : "ไม่พบเสียง (Edge TTS เข้าถึงไม่ได้)"}</option>`;
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

function renderJobList() {
  $("#job-list").innerHTML = state.jobs.length ? state.jobs.map((job) => `
    <button class="job-item ${state.current?.id === job.id ? "active" : ""}" data-id="${job.id}">
      <strong>${escapeHtml(job.filename)}</strong>
      <span>${statusNames[job.status] || job.status}</span>
      <i><b style="width:${Number(job.progress || 0)}%"></b></i>
    </button>`).join("") : '<p class="empty">ยังไม่มีงาน</p>';
  document.querySelectorAll(".job-item").forEach((button) => button.onclick = () => openJob(button.dataset.id));
}

function escapeHtml(value = "") {
  const span = document.createElement("span"); span.textContent = value; return span.innerHTML;
}

function showCreate() {
  if (state.events) state.events.close();
  state.current = null;
  $("#create-panel").hidden = false; $("#job-panel").hidden = true;
  const btn = $("#delete-job-btn");
  if (btn) btn.onclick = null;
  renderJobList();
}

async function openJob(id) {
  state.current = await api(`/api/jobs/${id}`);
  $("#create-panel").hidden = true; $("#job-panel").hidden = false;
  delete $("#translation-prompt").dataset["touched"];
  renderJob(); renderJobList();
  bindDeleteButton();
  state.offset = 0; await loadCues();
  if (state.events) state.events.close();
  if (!["completed", "failed", "cancelled", "needs_review"].includes(state.current.status)) {
    state.events = new EventSource(`/api/jobs/${id}/events`);
    state.events.onmessage = async (event) => {
      const previous = state.current;
      state.current = JSON.parse(event.data);
      renderJob(); await loadJobs();
      if (state.layer === "translation" && previous && state.current.total_cues !== previous.total_cues) {
        await loadCues();       // the translation layer was (re)built: re-render the page
      } else {
        syncCueRows();          // same set of cues: patch status bits in place
      }
      if (["completed", "failed", "cancelled", "needs_review"].includes(state.current.status)) state.events.close();
    };
  }
}

function renderJob() {
  const job = state.current; if (!job) return;
  $("#job-id").textContent = job.id;
  $("#job-name").textContent = job.filename;
  $("#job-status").textContent = statusNames[job.status] || job.status;
  $("#job-status").className = `badge ${job.status}`;
  $("#job-progress").style.width = `${Number(job.progress || 0)}%`;
  renderTranslationProgress(job);
  let message = job.error || job.wait_reason || `คืบหน้า ${Number(job.progress || 0).toFixed(0)}%`;
  if (job.stage === "synthesizing" && job.total_cues) {
    const done = job.completed_cues || 0;
    const total = job.total_cues;
    const cueNo = done < total ? done + 1 : total;
    message = `กําลังสร้างเสียง cue ${cueNo}/${total} (${done} เสร็จ)`;
  }
  $("#job-message").textContent = message;
  const order = ["uploaded", "extracted", "separated", "transcribed", "translated", "synthesizing", "synthesized", "completed"];
  const current = Math.max(0, order.indexOf(job.stage));
  $("#stage-track").innerHTML = order.map((stage, index) => `<span class="${index < current ? "done" : index === current ? "active" : ""}">${stageNames[stage]}</span>`).join("");
  $("#job-warnings").innerHTML = (job.warnings || []).map((warning) => `<p>⚠ ${escapeHtml(warning)}</p>`).join("");
  renderActions(job); renderArtifacts(job.artifacts || []); renderTranslationTools(job); renderJobSettings(job);
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
    state.current = await api(`/api/jobs/${state.current.id}/translation-prompt`, {
      method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ prompt: value }),
    });
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
    if (!confirm("เปลี่ยนเสียง/อัตราการพูดจะลบเสียงที่สร้างไว้และสร้างใหม่ทั้งหมด (ข้อความและคำแปลคงเดิม) ทำต่อไหม?")) return;
  }
  try {
    state.current = await api(`/api/jobs/${job.id}`, {
      method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
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
  if (job.status === "completed" && job.artifacts?.some((item) => item.kind === "dub_wav")) actions.push(["remux", "มิกซ์ MP4 ใหม่"]);
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
    if (!confirm("ลบงานนี้พร้อมไฟล์ผลลัพธ์ทั้งหมด? การกระทํานี้ย้อนกลับไม่ได้")) return;
    try {
      await api(`/api/jobs/${state.current.id}`, { method: "DELETE" });
      toast("ลบงานแล้ว"); state.events?.close(); await loadJobs(); showCreate();
    } catch (error) { toast(error.message, true); }
    return;
  }
  try {
    state.current = await api(`/api/jobs/${state.current.id}/actions`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ action }) });
    renderJob(); await loadJobs();
    if (["resume", "retry", "approve_transcript", "approve_translation", "remux", "retranslate"].includes(action)) openJob(state.current.id);
  } catch (error) { toast(error.message, true); }
}

function renderArtifacts(items) {
  const labels = { background: "เสียงพื้นหลัง", source_srt: "SRT ต้นฉบับ", translated_srt: "SRT ภาษาไทย", dub_wav: "เสียงพากย์ WAV", dub_mp3: "เสียงพากย์ MP3", report_json: "รายงาน JSON", report_csv: "รายงาน CSV", final_video: "วิดีโอพากย์ไทย" };
  $("#artifacts").innerHTML = items.length ? items.map((item) => `<a href="${item.download_url}"><span>↓</span>${labels[item.kind] || item.kind}</a>`).join("") : '<p class="empty">ไฟล์จะปรากฏเมื่อแต่ละขั้นเสร็จ</p>';
}

async function loadCues() {
  if (!state.current) return;
  const data = await api(`/api/jobs/${state.current.id}/cues?layer=${state.layer}&offset=${state.offset}&limit=${state.limit}`);
  $("#cue-list").innerHTML = data.items.length ? data.items.map((cue) => `
    <article class="cue" data-id="${cue.id}">
      <div class="cue-meta"><b>#${cue.position}</b><input class="start" type="number" value="${cue.start_ms}" min="0"><span>→</span><input class="end" type="number" value="${cue.end_ms}" min="1"><small>ms</small><small class="t-read"></small></div>
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
    const updateReadout = () => {
      const start = Number(article.querySelector(".start").value || 0);
      const end = Number(article.querySelector(".end").value || 0);
      article.querySelector(".t-read").textContent = `${formatMs(start)} → ${formatMs(end)}`;
    };
    article.querySelector(".start").oninput = updateReadout;
    article.querySelector(".end").oninput = updateReadout;
    updateReadout();
  });
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
  if (!confirm("สร้างเสียงของ cue นี้ใหม่?")) return;
  try {
    await api(`/api/jobs/${state.current.id}/actions`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ action: "regenerate_cue", cue_id: Number(id) }),
    });
    toast("กําลังสร้างเสียง cue ใหม่");
    await refreshJob(); syncCueRows();
  } catch (error) { toast(error.message, true); }
}

async function saveCue(element) {
  // Editing the source layer discards the whole translation layer downstream;
  // say so before doing it.
  if (state.layer === "source" && state.current.cues?.length) {
    if (!confirm("การแก้ต้นฉบับจะลบคำแปลและเสียงที่สร้างไว้ทั้งหมด แล้วแปล/สร้างเสียงใหม่ บันทึกต่อไหม?")) return;
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

$("#job-form").onsubmit = (event) => {
  event.preventDefault();
  if (!$("#youtube-url").value.trim()) {
    toast("กรุณากรอกลิงก์ YouTube ก่อนเริ่มพากย์", true);
    return;
  }
  const form = new FormData(event.currentTarget); const request = new XMLHttpRequest();
  $("#upload-progress").hidden = false;
  request.open("POST", "/api/jobs"); request.responseType = "json";
  request.upload.onprogress = (e) => { if (e.lengthComputable) $("#upload-progress span").style.width = `${e.loaded / e.total * 100}%`; };
  request.onload = async () => { if (request.status < 300) { await loadJobs(); openJob(request.response.id); } else toast(request.response?.detail || "สร้างงานไม่สําเร็จ", true); $("#upload-progress").hidden = true; };
  request.onerror = () => toast("สร้างงานไม่สําเร็จ (ตรวจอินเทอร์เน็ต)", true); $("#upload-progress").hidden = true; request.send(form);
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
$("#translation-prompt").addEventListener("input", () => { $("#translation-prompt").dataset.touched = "1"; });
$("#save-prompt").onclick = savePrompt;

$("#pick-folder-btn").onclick = async () => {
  try {
    const res = await api("/api/jobs/pick-folder", { method: "POST" });
    if (res && res.path) $("#output-dir").value = res.path;
  } catch (error) { toast(error.message, true); }
};

await Promise.all([loadHealth(), loadVoices(), loadJobs()]);
