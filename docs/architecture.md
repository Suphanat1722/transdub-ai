# Architecture

TransDub AI เป็น local-first single-user application ตัว FastAPI, SQLite และ queue worker ทํางานในเครื่องเดียวกัน การสร้างเสียงใช้ Microsoft Edge TTS ซึ่งเป็นบริการคลาวด์ ไม่ต้องมี GPU หรือโมเดล TTS ในเครื่อง

## Packages

- `app/api` — HTTP routing, request validation และ response files
- `app/core` — paths, constants และ runtime configuration
- `app/repositories` — SQLite access และ data migrations
- `app/services` — SRT parsing, audio processing, queue worker และ Edge TTS synthesis
- `app/static` — dependency-free browser interface
- `migrations` — Alembic schema history

## Runtime flow

1. FastAPI เริ่มระบบและอัปเกรด SQLite schema
2. queue worker ใช้ glossary ที่ผู้ใช้กําหนด แล้วสังเคราะห์ทีละ cue ด้วย Edge TTS (`edge_tts.Communicate`) ตามเสียงและอัตราการพูดที่บันทึกไว้ในแต่ละงาน เพื่อให้เสียงพอดีช่องเวลาโดยไม่ตัดคํา ระบบจะลองเพิ่มอัตราเร็ว (rate) ขึ้นทีละขั้น (+10% จนถึงสูงสุด +50%) สร้างใหม่จนกว่าจะพอดี; ถ้าเร่งสุดแล้วยังยาวเกินจะเก็บเต็มเสียงและแจ้งเตือนให้ตรวจ
3. Edge TTS คืนผลเป็น MP3 จากบริการ `speech.platform.bing.com`; ระบบแปลงเป็น WAV 24 kHz mono ด้วย FFmpeg แล้วเก็บเข้าตําแหน่ง cue พร้อม cache ตาม (text + voice + rate)
4. เมื่อครบทุก cue ระบบวางเสียงตาม start time โดยไม่ยืดหรือบังคับจบที่ end time; ถ้าเสียงยังชน cue ถัดไปจะเลื่อน cue ถัดไปตามเพดานที่ตั้งไว้ หากเสียงยาวเกินวิดีโอระบบจะหยุดที่ `needs_review` ให้แก้/ย่อข้อความแทนการตัดคําทิ้ง
5. FFmpeg แบ่งประกอบเป็น stem ละไม่เกิน 64 cue แล้ว mix stem พร้อม limiter ครั้งเดียว ทุกคําสั่งสั้นกว่า 20,000 ตัวอักษร
6. WAV/MP3/JSON/CSV ถูกเขียนใน temporary output revision และเปิดใช้งานแบบ atomic เมื่อครบทั้งหมด โดย master ยาวอย่างน้อยถึง subtitle end สุดท้าย

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
- ข้อความ/เสียงสําหรับถอดความและแปลถูกส่งไป Gemini; เสียงพูดพากย์เองถูกสร้างผ่าน Edge TTS