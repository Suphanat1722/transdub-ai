# TransDub AI

TransDub AI เป็นเว็บแอปบน Windows สําหรับพากย์วิดีโอเป็นภาษาไทยตั้งแต่ต้นจนจบในงานเดียว:

1. ดึงเสียงจากวิดีโอด้วย FFmpeg
2. แยกเสียงพูดออกจากเพลง/บรรยากาศด้วย Demucs
3. ถอดเสียงต้นฉบับด้วย Gemini 3.5 Transcribe พร้อม word timestamps
4. แปลและเรียบเรียงเป็น SRT ภาษาไทยสําหรับ TTS
5. สร้างเสียงด้วย Microsoft Edge TTS (เสียงพากย์สำเร็จรูป เลือกได้หลายเสียง)
6. ผสมเสียงพากย์กับ background stem แล้วประกอบกลับเป็น MP4

แอป bind เฉพาะ `127.0.0.1`, เก็บงานใน SQLite และ checkpoint รายขั้น/ราย chunk เพื่อทําต่อหลังปิดโปรแกรมได้ ข้อความและเสียงสําหรับ ASR ถูกส่งไป Gemini; Edge TTS, Demucs และการประกอบวิดีโอทําผ่านบริการ/บนเครื่อง

> **เสียงและสถาปัตยกรรม**: เวอร์ชันปัจจุบันเปลี่ยนมาใช้ **Microsoft Edge TTS** แทนการโคลนเสียงท้องถิ่น (เดิมคือ JaiTTS-F5TTS)
> Edge TTS **ไม่มี voice cloning** — ใช้เสียงพากย์สำเร็จรูป (preset voices) ที่ Microsoft ให้บริการเท่านั้น
> ระบบ voice profile / เสียงอ้างอิง / GPU inference ของเดิมจึงถูกถอดออกทั้งหมด ไม่ต้องใช้ GPU หรือดาวน์โหลดโมเดล TTS
> Edge TTS ต้องเชื่อมต่ออินเทอร์เน็ตเพื่อเข้าถึงบริการ `speech.platform.bing.com`

## เริ่มใช้งาน

ต้องมี Windows, Python 3.12, Git, FFmpeg/FFprobe และอินเทอร์เน็ต (Edge TTS เข้าถึงได้)

1. รัน `Setup.ps1` หรือดับเบิลคลิก `Start TransDub AI.bat` เพื่อให้ติดตั้งอัตโนมัติ
2. ใส่ `GEMINI_API_KEY` ใน `.env`
3. ดับเบิลคลิก `Start TransDub AI.bat`
4. เปิด <http://127.0.0.1:8765>

## การทํางานและไฟล์

- ค่าเริ่มต้นพักเพื่อตรวจ transcript และคําแปล (`pause_after_transcription`/`pause_after_translation` เป็น on) และเลือกเสียงผู้ชาย `th-TH-NiwatNeural` เป็นเสียงเริ่มต้น; เปลี่ยนเป็นเสียง/ปิดพักได้ในหน้าเว็บ
- หน้าเว็บให้เลือกรายการเสียง Edge TTS และอัตราการพูด (rate) ได้
- เลือกโฟลเดอร์ส่งออกวิดีโอเสร็จได้ ถ้าไม่เลือก ระบบบันทึกไฟล์ `.th-dub.mp4` ไปที่โฟลเดอร์เดียวกับวิดีโอต้นฉบับ
- หาก Gemini รวม cue สั้นหรือส่ง timecode เหลื่อม ระบบจะแบ่ง chunk แล้วลองใหม่อัตโนมัติ พร้อมบันทึกคําเตือนการจับคู่ไว้ให้ตรวจ
- แก้ข้อความ/timecode ในหน้าเว็บได้ ผลลัพธ์ถัดไปที่เกี่ยวข้องจะถูก invalidate และสร้างใหม่
- **ไม่ตัดคําท้ายเสียงที่ยาวชน**: ระบบจะสร้างเสียงใหม่ด้วยอัตราเร็ว (rate) สูงขึ้นทีละขั้นจนพอดีช่องเวลา ถ้าเร่งสุดแล้วยังยาวเกิน ผลลัพธ์จะหยุดที่ `needs_review` ให้ตรวจ/ย่อข้อความแทนการตัดคําพูดทิ้ง
- เก็บ source/translated SRT, background FLAC, dub WAV/MP3, report และ final MP4
- ไฟล์ชั่วคราวอยู่ใน `data/jobs/<UUID>/work` และถูกลบหลังงานสําเร็จ

## ทดสอบ

```powershell
.\Setup.ps1 -Dev
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app scripts tests
.\.venv\Scripts\python.exe -m mypy app
```

การทดสอบที่เรียก FFmpeg (การประกอบเสียง/วิดีโอ) ต้องมี FFmpeg ใน PATH จึงจะรันได้

## สิทธิ์ใช้งาน

Application code ใช้ MIT License ส่วนเสียง Edge TTS เป็นบริการของ Microsoft และมีข้อกํากับการใช้งานของตนเอง โปรดอ่าน `THIRD_PARTY_NOTICES.md` ก่อนแจกจ่าย