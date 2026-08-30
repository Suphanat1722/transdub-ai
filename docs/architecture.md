# Architecture

JaiCue Studio เป็น local-first single-user application ตัว FastAPI, SQLite และ inference process ทำงานในเครื่องเดียวกัน

## Packages

- `app/api` — HTTP routing, request validation และ response files
- `app/core` — paths, constants และ runtime configuration
- `app/repositories` — SQLite access และ data migrations
- `app/services` — SRT parsing, audio processing, queue worker และ model inference
- `app/static` — dependency-free browser interface
- `migrations` — Alembic schema history

## Runtime flow

1. FastAPI เริ่มระบบและอัปเกรด SQLite schema
2. inference process โหลด JaiTTS และ Vocos บน CUDA หรือ CPU หนึ่งครั้ง
3. queue worker ใช้ glossary ที่ผู้ใช้กำหนด ประเมินเวลาจากอักขระ Unicode และสร้างเสียงทีละ cue รอบแรกใช้ multiplier 1.10; รอบ 1.35 จะเกิดเฉพาะเมื่อ tail 80 ms ดังกว่า -31 dB, silence ท้ายต่ำกว่า 40 ms และระดับลดลงน้อยกว่า 6 dB
4. ระบบเลือก candidate ที่จบปลอดภัยกว่าและ cache พร้อม quality metadata หลัง cue สำเร็จ ก่อนเริ่ม cue ถัดไป
5. เมื่อครบทุก cue ระบบวางเสียงตาม start time โดยไม่ยืดหรือบังคับจบที่ end time หากเสียงชน cue ถัดไปจะเร่งไม่เกิน 1.25x แล้วเลื่อน cue ถัดไปตามเพดานที่ตั้งไว้ ส่วนที่ยังเกินเพดานจึง mix ซ้อนกัน
6. FFmpeg แบ่งประกอบเป็น stem ละไม่เกิน 64 cue แล้ว mix stem พร้อม limiter ครั้งเดียว ทุกคำสั่งสั้นกว่า 20,000 ตัวอักษร
7. WAV/MP3/JSON/CSV ถูกเขียนใน temporary output revision และเปิดใช้งานแบบ atomic เมื่อครบทั้งหมด โดย master ยาวอย่างน้อยถึง subtitle end สุดท้าย

## Job state machine

`queued → running → queued` ทำซ้ำต่อ cue เมื่อกด Pause ระหว่าง inference จะเป็น `pausing` จน worker บันทึก cue ปัจจุบันแล้ว acknowledge เป็น `paused` ส่วน Cancel ใช้ `cancelling` เช่นเดียวกัน การประกอบใช้ `assembling` และห้ามลบโปรเจกต์ขณะมี cue `processing` หรืออยู่ใน active state

## Persistence boundaries

`data/` เป็น runtime stateและไม่อยู่ใน Git งานแต่ละงานมี UUID directory ของตัวเอง Path ใน SQLite เป็น relative ต่อ `data/` เพื่อย้าย workspace ได้ ส่วน `audio_cache` เป็นดัชนีไปยัง WAV ที่สร้างแล้วและถูกตรวจ/เก็บกวาดเมื่อ startup

ไฟล์เสียงราย cue และ profile ถูกเสิร์ฟจาก path ที่ตรวจว่าอยู่ใต้ directory ที่กำหนดเท่านั้น Mutation API ตรวจ Host/Origin หน้าเว็บอ่าน cue แบบ pagination 100 แถวและ poll เฉพาะ status เพื่อลด DOM/SQLite load

ก่อน inference ระบบ preprocess reference เพียงครั้งเดียว แล้วใช้ไฟล์และ transcript ที่ได้ชุดเดียวกันในการคำนวณ fixed duration และเป็น conditioning input โดยตรง จึงตัดช่วง reference ออกจาก mel output ที่ตำแหน่งเดียวกับที่ใช้สร้างจริง

ระบบปิด `clip_short` ของ FlowTTS เพราะการตัดเสียงโดยไม่ตัด transcript ที่ผู้ใช้กรอกให้ตรงกันจะทำให้อัตราพูดที่ประมาณได้เร็วเกินจริง Reference ที่ normalize แล้วจึงถูกเก็บครบทั้งเสียงและข้อความตลอด inference

ตัวประมาณ duration เพิ่ม articulation margin 250 ms หลังเวลาที่คำนวณตามสัดส่วนตัวอักษร โดยหารตาม speech speed เดียวกัน เพื่อให้คำสั้นมีพื้นที่ปิดพยางค์ครบโดยไม่แก้ waveform ภายหลัง

## Model boundary

JaiTTS weights ไม่รวมอยู่ใน repository ระบบค้นหา `model.pt` และ `vocab.txt` จาก `models/JaiTTS-F5TTS` หรือรูปแบบ Hugging Face cache ที่วางใน workspace เท่านั้น ส่วน Vocos ถูก cache แยกใน `data/hf-cache`
