# Architecture

TransDub AI เป็น local-first single-user application ตัว FastAPI, SQLite และ queue worker ทํางานในเครื่องเดียวกัน การสร้างเสียงใช้ Microsoft Edge TTS ซึ่งเป็นบริการคลาวด์ ไม่ต้องมี GPU หรือโมเดล TTS ในเครื่อง

## Packages

- `app/api` — HTTP routing, request validation และ response files
- `app/core` — paths, constants และ runtime configuration
- `app/repositories` — SQLite access และ data migrations
- `app/services` — YouTube download/subtitles, SRT parsing, audio processing, queue worker และ Edge TTS synthesis
- `app/static` — dependency-free browser interface
- `migrations` — Alembic schema history

## Runtime flow

1. FastAPI เริ่มระบบและอัปเกรด SQLite schema
2. งานถูกสร้างจากลิงก์ YouTube (`mode="youtube"`); worker ขั้น `downloaded` ใช้ yt-dlp ดาวน์โหลดวิดีโอ (`app/services/youtube.py`) แล้วดึง subtitle จาก YouTube ด้วย youtube-transcript-api -- ซับภาษาไทยกลายเป็น source+translation (`mode="import"`, ข้าม Gemini ทั้งหมด) ส่วนซับภาษาอื่นกลายเป็น source แล้วเลื่อนเข้า Gemini แปล (`mode="import_pending"`)
3. worker เคลม cue ทีละชุด (`TTS_SYNTH_WORKERS`, ค่าเริ่มต้น 4) แล้วสังเคราะห์พร้อมกันด้วย Edge TTS (`edge_tts.Communicate`) ตามเสียงและอัตราการพูดของงาน แต่ละ cue เก็บเสียงตามจังหวะธรรมชาติ แล้วขั้นตอนประกอบค่อยจัดกลุ่ม/เร่งทั้งก้อนให้พอดีช่องเวลาโดยไม่ตัดคํา
4. Edge TTS คืนผลเป็น MP3 จากบริการ `speech.platform.bing.com`; ระบบแปลงเป็น WAV 24 kHz mono ด้วย FFmpeg แล้วเก็บเข้าตําแหน่ง cue พร้อม cache ตาม (text + voice + rate)
5. เมื่อครบทุก cue ระบบวางเสียงตาม start time โดยไม่ยืดหรือบังคับจบที่ end time; cue ที่เสียงพันกันถูกจัดเป็นกลุ่ม (overlap group) วางต่อกันแบบไม่มีทับ แล้วเร่งทั้งก้อนด้วย `atempo` เดียว (สูงสุด 1.25x) ให้พอดีซับ cue สุดท้ายของกลุ่ม — ทุก cue ในกลุ่มจึงจังหวะเท่ากัน แต่ละกลุ่มยึดที่เวลาเริ่มซับของตัวเองหรือเสียงกลุ่มก่อนหน้าที่เร่งแล้ว (อันไหนหลังกว่า) จึงไม่มีช่องเงียบหรือเร่งเกินหลอก หากเร่งสุดแล้วยังล้นจะเก็บเสียงเต็มและแจ้งเตือน “กลุ่ม cue …” พร้อมหยุดที่ `needs_review` เมื่อเสียงรวมยาวเกินวิดีโอ
6. FFmpeg ประกอบแบบ batch ละไม่เกิน 64 cue แล้ว mix พร้อม limiter ครั้งเดียว ทุกคําสั่งสั้นกว่า 20,000 ตัวอักษร
7. WAV/MP3/JSON/CSV ถูกเขียนใน temporary output revision และเปิดใช้งานแบบ atomic เมื่อครบทั้งหมด โดย master ยาวอย่างน้อยถึง subtitle end สุดท้าย

## Job settings after creation

`PATCH /api/jobs/{id}` แก้เสียง/อัตราการพูด/ระดับเสียง/โฟลเดอร์ส่งออกของงานที่สร้างแล้ว การเปลี่ยนเสียงหรืออัตราการพูดจะลบเสียง cue ที่สร้างไว้และเพิ่ม `generation_revision` ของทุก cue (ซึ่งเข้าร่วม cache key) เพื่อให้ worker สร้างเสียงใหม่ทั้งหมดโดยไม่แตะ transcript/คำแปล ส่วนระดับเสียงและโฟลเดอร์ส่งออกมีผลตอนมิกซ์ครั้งถัดไป `GET /api/jobs/{id}/cues/{cue_id}/preview` ผสมเสียง cue กับเสียงพื้นหลังช่วงเวลาเดียวกันด้วย FFmpeg เพื่อให้ฟังตรวจก่อนยืนยันคำแปล

## Job state machine

`queued → running → queued` ทําซ้ําต่อ cue เมื่อกด Pause จะตั้ง `control_requested=pause` และ worker acknowledge เป็น `paused` หลังจบ cue ปัจจุบัน Cancel ใช้กลไกเดียวกัน การประกอบใช้ `muxing` และห้ามลบโปรเจกต์ขณะมี cue `processing` หรืออยู่ใน active state

## Persistence boundaries

`data/` เป็น runtime stateและไม่อยู่ใน Git งานแต่ละงานมี UUID directory ของตัวเอง Path ใน SQLite เป็น relative ต่อ `data/` เพื่อย้าย workspace ได้ ส่วน `audio_cache` เป็นดัชนีไปยัง WAV ที่สร้างแล้วและถูกตรวจ/เก็บกวาดเมื่อ startup

ไฟล์เสียงราย cue ถูกเสิร์ฟจาก path ที่ตรวจว่าอยู่ใต้ directory ที่กําหนดเท่านั้น Mutation API ตรวจ Host/Origin หน้าเว็บอ่าน cue แบบ pagination 100 แถวและ poll เฉพาะ status เพื่อลด DOM/SQLite load

## Model / service boundary

- **Edge TTS** เป็นบริการคลาวด์ของ Microsoft ไม่มี weight ใน repository และไม่มี voice cloning ใช้เสียงพากย์สำเร็จรูปตาม `voice` ที่เลือกในงานเท่านั้น (ค่าเริ่มต้น `th-TH-NiwatNeural`) รายการเสียงถูก cache ไว้ใน process 10 นาที เพื่อไม่ให้ `/api/health` และ `/api/voices` เรียก network ทุกครั้ง
- Edge TTS ต้องเข้าถึงอินเทอร์เน็ตได้ ณ เวลาที่สร้างเสียง หากเข้าถึงไม่ได้ worker หน่วงและลองใหม่ (`waiting_quota`)
- **Demucs** ยังทําบนเครื่องเพื่อแยก background stem ใช้ CUDA ได้ถ้ามี ไม่งั้น CPU
- ข้อความคําบรรยายจาก YouTube ที่ไม่ใช่ไทยถูกส่งไป Gemini แปลเป็นไทย; เสียงพากย์เองสร้างผ่าน Edge TTS (ดาวน์โหลดวิดีโอ/ดึงซับทําผ่าน yt-dlp + youtube-transcript-api ยังต้องอินเทอร์เน็ต)

## Separation modes

`jobs.separation_mode` (`demucs` default, `fast` opt-in): `fast` ข้าม Demucs แล้วใช้เสียงต้นฉบับเป็น
background stem ตรง ๆ (เร็วหลายเท่า ไม่ต้องมี torch/CUDA เหมาะกับเครื่องช้าหรือวิดีโอพูดชัด)
ตั้งตอนสร้างงาน (`separation_mode=fast`) ดูได้จาก `GET /api/jobs/{id}/logs`

## YouTube download quality and outputs

ดาวน์โหลดด้วย `bv*+ba/b` ลอง client ตามลำดับ TV → web → iOS → Android โดยอันแรกที่สำเร็จชนะ (client มือถือให้คุณภาพลดจึงไว้ท้าย) และรวมเป็น MKV เพื่อไม่ทิ้งสตรีม VP9/AV1/Opus วิดีโอจบยัง mux เป็น MP4 เหมือนเดิม ชื่องานและไฟล์ใช้ชื่อคลิป YouTube (sanitize สำหรับ Windows) ไฟล์เสร็จไปโฟลเดอร์ที่เลือก หรือ `data/outputs` (ชื่อ `{ชื่อคลิป}.th-dub.mp4`) เมื่อไม่เลือก

## Logs

`GET /api/jobs/{id}/logs` รวม `error/warnings` + `stage_attempts` 100 รายการล่าสุด +
`api_usage` หน้าเว็บมีปุ่มแสดง/ซ่อนที่การ์ด “บันทึกงาน” ใช้ตรวจ quota/model/เวลาก่อน retry

## Phase-3 additions

- สร้างงานเป็น wizard 3 ขั้น (วิดีโอ → เสียง → เริ่ม) validate URL ฝั่ง client ก่อนส่ง
- แก้ source cue แบบ text-only ลบ checkpoint เฉพาะ chunk ที่ทับ (`affected_chunk_ids_for_source_index`)
  chunk อื่น reuse checkpoint ไม่ยิง Gemini ซ้ำ (แก้เวลายังล้างทั้งงานเหมือนเดิม)
- `GET /api/jobs/{id}/queue` บอกคิวที่/งานรอทั้งหมด (worker ยังรันทีละ 1 job เท่าเดิม)
- `init_db()` เลิก upgrade Alembic ซ้ำบน fresh DB; `tests/test_e2e_smoke.py` คุม ready/validate/queue/logs/wizard