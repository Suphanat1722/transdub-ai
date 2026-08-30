# TransDub AI

TransDub AI เป็นเว็บแอปบน Windows สำหรับพากย์วิดีโอเป็นภาษาไทยตั้งแต่ต้นจนจบในงานเดียว:

1. ดึงเสียงจากวิดีโอด้วย FFmpeg
2. แยกเสียงพูดออกจากเพลง/บรรยากาศด้วย Demucs
3. ถอดเสียงต้นฉบับด้วย Gemini 3.5 Transcribe พร้อม word timestamps
4. แปลและเรียบเรียงเป็น SRT ภาษาไทยสำหรับ TTS
5. สร้างเสียงด้วย JaiTTS-F5TTS และ voice profile ที่เลือก
6. ผสมเสียงพากย์กับ background stem แล้วประกอบกลับเป็น MP4

แอป bind เฉพาะ `127.0.0.1`, เก็บงานใน SQLite และ checkpoint รายขั้น/ราย chunk เพื่อทำต่อหลังปิดโปรแกรมได้ ข้อความและเสียงสำหรับ ASR ถูกส่งไป Gemini; JaiTTS, Demucs และการประกอบวิดีโอทำบนเครื่อง

## เริ่มใช้งาน

ต้องมี Windows, Python 3.12, Git, FFmpeg/FFprobe และ NVIDIA GPU ที่รองรับ CUDA (ใช้ CPU ได้แต่ช้ามาก)

1. รัน `Setup.ps1` หรือดับเบิลคลิก `Start TransDub AI.bat` เพื่อให้ติดตั้งอัตโนมัติ
2. ใส่ `GEMINI_API_KEY` ใน `.env`
3. ดับเบิลคลิก `Start TransDub AI.bat`
4. เปิด <http://127.0.0.1:8765>

โมเดล JaiTTS, Demucs checkpoint และ voice profiles เดิมถูกนำมาไว้ในโปรเจกต์นี้แล้ว สคริปต์ setup จะนำข้อมูลโปรไฟล์ “พอตแคส” และ “พี่นิว” เข้า SQLite ใหม่โดยไม่ย้ายประวัติงานเก่า

บน GitHub ไฟล์โมเดล JaiTTS ขนาดใหญ่ถูกแนบไว้เป็น release asset แยกจาก source code ให้ดาวน์โหลด `model.pt` จาก release ล่าสุดแล้ววางไว้ที่ `models/JaiTTS-F5TTS/model.pt` ก่อนรัน setup หาก clone จาก source ที่ไม่มีไฟล์โมเดล

## การทำงานและไฟล์

- ค่าเริ่มต้นทำงานอัตโนมัติจนได้ MP4; เลือกพักหลัง transcript หรือ translation ได้
- แก้ข้อความ/timecode ในหน้าเว็บได้ ผลลัพธ์ถัดไปที่เกี่ยวข้องจะถูก invalidate และสร้างใหม่
- ถ้าเสียงพากย์ยาวเกินวิดีโอ ระบบหยุดที่ `needs_review` และไม่ตัดคำพูดทิ้ง
- เก็บ source/translated SRT, background FLAC, dub WAV/MP3, report และ final MP4
- ไฟล์ชั่วคราวอยู่ใน `data/jobs/<UUID>/work` และถูกลบหลังงานสำเร็จ

## ทดสอบ

```powershell
.\Setup.ps1 -SkipTorch -Dev
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app scripts tests
.\.venv\Scripts\python.exe -m mypy app
```

## สิทธิ์ใช้งาน

Application code ใช้ MIT License แต่ JaiTTS checkpoint ใช้ CC BY-NC 4.0 และ FlowTTS มี license ของโครงการต้นทาง ระบบนี้จึงตั้งใจใช้ส่วนตัว/วิจัยที่ไม่ใช่เชิงพาณิชย์ โปรดอ่าน `THIRD_PARTY_NOTICES.md` ก่อนแจกจ่าย
