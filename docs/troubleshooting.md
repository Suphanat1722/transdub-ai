# Troubleshooting

## YouTube ถูกบล็อก / TooManyRequests / bot-check

- อาการ: งานค้างที่ `downloading` แล้ว `failed` ว่า sign-in / bot / TooManyRequests
- แอปไล่ client `android → ios → tv → default` พร้อม `impersonate=chrome` ให้อัตโนมัติแล้ว
- ถ้ายังโดนบล็อก: ตั้ง proxy ใน `.env` (`YOUTUBE_PROXY_WEBSHARE_USER/PASS` หรือ `YOUTUBE_PROXY_URL`)
  ถ้าใช้ Webshare ต้องเป็นแพ็กเกจ **Residential** เท่านั้น (ดู `.env.example`)
- ลองวิดีโออื่นก่อนเพื่อแยกปัญหาว่าเป็นที่ IP หรือวิดีโอนั้นไม่มีซับ

## ไม่มีคำบรรยาย / ซับว่าง

- แอปเป็น YouTube-only และใช้ซับที่มีอยู่บน YouTube เท่านั้น วิดีโอที่ไม่มีซับใช้ไม่ได้
- ซับไทยข้าม Gemini ทันที ซับภาษาอื่นต้องมี `GEMINI_API_KEY`

## Gemini quota / 429

- งานเข้า `waiting_quota` แล้วลองใหม่เอง ดูรายละเอียดที่การ์ด “บันทึกงาน” (`GET /api/jobs/{id}/logs`)
- เปลี่ยนรุ่นใน `.env` (`TRANSLATION_MODELS`) ถ้ารุ่น default โดน 404/quota

## Edge TTS ต่อไม่ได้

- ต้องต่อเน็ตถึง `speech.platform.bing.com` เสมอตอนสร้างเสียง
- หน้าเว็บมี fallback เสียงไทย 3 เสียงให้ส่งงานต่อได้แม้ list voices ล้มเหลว
- เช็คแบบเบาไม่ยิงเน็ต: `GET /api/ready` เช็คแบบเต็ม: `GET /api/health`

## FFmpeg / Demucs

- ต้องมี `ffmpeg` + `ffprobe` ใน PATH ก่อนรัน `Setup.ps1`
- Demucs หนัก (torch หลาย GB) ถ้าเครื่องช้าให้ติ๊ก “โหมดเร็ว: ข้าม Demucs” ตอนสร้างงาน
  (`separation_mode=fast` ใช้เสียงต้นฉบับเป็นพื้นหลังแทน)
- ระหว่างแยกเสียง หน้าเว็บแสดงเวลาที่ผ่านไปให้รู้ว่ายังรันอยู่ (ไม่ได้ค้าง) และกดพัก/ยกเลิกได้
  ระบบจะหยุด Demucs ให้แล้วทำต่อจากขั้นเดิมเมื่อกดทำต่อ

## งานค้าง / เปิดโปรแกรมใหม่

- `init_db()` กู้ job ที่ค้าง (`extracting/separating/translating/synthesizing/muxing/running`)
  กลับเป็น `queued` และ cue `processing` กลับเป็น `pending` อัตโนมัติ
- ดูสาเหตุที่การ์ด “บันทึกงาน” ก่อนกด retry/approve
