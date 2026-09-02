const $ = (selector) => document.querySelector(selector);
const state = { jobs: [], current: null, layer: "source", offset: 0, limit: 100, events: null };

const stageNames = {
  uploaded: "รับไฟล์", extracted: "แยกแทร็ก", separated: "ตัดเสียงพูด",
  transcribed: "ถอดข้อความ", translated: "แปลไทย", synthesizing: "สร้างเสียง",
  synthesized: "พร้อมรวม", completed: "เสร็จแล้ว",
};
const statusNames = {
  queued: "รอคิว", running: "กำลังเดินงาน", extracting: "กำลังแยกเสียง",
  separating: "กำลังตัดเสียงพูด", transcribing: "กำลังถอดข้อความ",
  reviewing_transcript: "รอตรวจ transcript", translating: "กำลังแปล",
  reviewing_translation: "รอตรวจคำแปล", synthesizing: "กำลังสร้างเสียง",
  needs_review: "ต้องแก้ก่อนรวม", muxing: "กำลังประกอบวิดีโอ",
  waiting_quota: "กำลังรอลองใหม่", paused: "พักอยู่", failed: "ไม่สำเร็จ",
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
    const voices = await api("/api/voices");
    const defaultSettings = await api("/api/voices/default");
    const select = $("#voice-select");
    if (!voices.length) {
      select.innerHTML = '<option value="">ไม่พบเสียง (Edge TTS เข้าถึงไม่ได้)</option>';
      return;
    }
    select.innerHTML = voices.map((voice) =>
      `<option value="${escapeHtml(voice.short_name)}">${escapeHtml(voice.label)}</option>`
    ).join("");
    const preferred = defaultSettings.voice;
    if (preferred) select.value = preferred;
  } catch (error) { $("#voice-select").innerHTML = '<option value="">ไม่สามารถโหลดเสียงได้</option>'; }
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
  $("#translation-srt").value = "";
  renderJob(); renderJobList();
  bindDeleteButton();
  state.offset = 0; await loadCues();
  if (state.events) state.events.close();
  if (!["completed", "failed", "cancelled", "needs_review"].includes(state.current.status)) {
    state.events = new EventSource(`/api/jobs/${id}/events`);
    state.events.onmessage = async (event) => {
      state.current = JSON.parse(event.data); renderJob(); await loadJobs(); await loadCues();
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
  renderActions(job); renderArtifacts(job.artifacts || []); renderTranslationTools(job);
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
  const activeList = ["queued", "running", "extracting", "separating", "transcribing", "translating", "synthesizing", "muxing", "waiting_quota"];
  const active = activeList.includes(job.status);
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
      <div class="cue-meta"><b>#${cue.position}</b><input class="start" type="number" value="${cue.start_ms}" min="0"><span>→</span><input class="end" type="number" value="${cue.end_ms}" min="1"><small>ms</small></div>
      <textarea>${escapeHtml(cue.text)}</textarea>
      <div class="cue-foot"><span>${(cue.warnings || []).map(escapeHtml).join(" · ")}</span>
        <span class="cue-btns">
          ${cue.status === "completed" && cue.audio_path ? `<button class="cue-play" title="ฟังเสียง">▶</button>` : ""}
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
  document.querySelectorAll(".cue-regenerate").forEach((button) => button.onclick = () => regenerateCue(button.closest(".cue")));
}

async function playCueAudio(element) {
  const id = element.dataset.id;
  const url = `/api/jobs/${state.current.id}/cues/${id}/audio`;
  const audio = new Audio(url);
  audio.play().catch(() => toast("ไม่สามารถเล่นเสียงได้", true));
}

async function regenerateCue(element) {
  const id = element.dataset.id;
  if (!confirm("สร้างเสียงของ cue นี้ใหม่?")) return;
  try {
    await api(`/api/jobs/${state.current.id}/actions`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ action: "regenerate_cue", cue_id: Number(id) }),
    });
    toast("กําลังสร้างเสียง cue ใหม่"); await openJob(state.current.id);
  } catch (error) { toast(error.message, true); }
}

async function saveCue(element) {
  try {
    await api(`/api/jobs/${state.current.id}/cues/${element.dataset.id}`, {
      method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        layer: state.layer, text: element.querySelector("textarea").value,
        start_ms: Number(element.querySelector(".start").value), end_ms: Number(element.querySelector(".end").value),
      }),
    });
    toast("บันทึก cue แล้ว และล้างผลลัพธ์ถัดไปที่เกี่ยวข้อง"); await openJob(state.current.id);
  } catch (error) { toast(error.message, true); }
}

$("#job-form").onsubmit = (event) => {
  event.preventDefault(); const form = new FormData(event.currentTarget); const request = new XMLHttpRequest();
  $("#upload-progress").hidden = false;
  request.open("POST", "/api/jobs"); request.responseType = "json";
  request.upload.onprogress = (e) => { if (e.lengthComputable) $("#upload-progress span").style.width = `${e.loaded / e.total * 100}%`; };
  request.onload = async () => { if (request.status < 300) { await loadJobs(); openJob(request.response.id); } else toast(request.response?.detail || "สร้างงานไม่สำเร็จ", true); $("#upload-progress").hidden = true; };
  request.onerror = () => toast("อัปโหลดไม่สำเร็จ", true); request.send(form);
};

$("#video").onchange = (event) => { const file = event.target.files[0]; $("#video-name").textContent = file ? `${file.name} · ${formatSize(file.size)}` : "รองรับไฟล์ที่ FFmpeg อ่านได้ สูงสุด 8 GB"; };
function bindRanges() {
  document.querySelectorAll('input[type="range"]').forEach((input) => input.oninput = () => {
    const label = input.name === "voice_volume" ? "voice" : input.name === "tts_rate" ? "tts-rate" : "background";
    $(`#${label}-value`).textContent = `${input.value}%`;
  });
}
bindRanges();
document.querySelectorAll(".tabs button").forEach((button) => button.onclick = async () => { document.querySelectorAll(".tabs button").forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.layer = button.dataset.layer; state.offset = 0; await loadCues(); });
$("#prev-page").onclick = () => { state.offset = Math.max(0, state.offset - state.limit); loadCues(); };
$("#next-page").onclick = () => { state.offset += state.limit; loadCues(); };
$("#new-job-tab").onclick = showCreate; $("#refresh-jobs").onclick = loadJobs;
$("#translation-prompt").addEventListener("input", () => { $("#translation-prompt").dataset.touched = "1"; });
$("#save-prompt").onclick = savePrompt;
$("#use-srt").addEventListener("change", () => {
  const on = $("#use-srt").checked;
  $("#srt-upload-box").hidden = !on;
  $("#srt-file").required = on;
  // When importing our own SRT, the review pauses are not needed.
  document.querySelectorAll('input[name="pause_after_transcription"], input[name="pause_after_translation"]').forEach((cb) => { cb.checked = !on; cb.disabled = on; });
});

await Promise.all([loadHealth(), loadVoices(), loadJobs()]);
