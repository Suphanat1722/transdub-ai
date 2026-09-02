param([switch]$Dev)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
Set-Location -LiteralPath $ProjectRoot

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "FFmpeg was not found in PATH" }
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { throw "FFprobe was not found in PATH" }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python Launcher (py) was not found" }

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "[1/4] Creating Python 3.12 environment..." -ForegroundColor Cyan
    py -3.12 -m venv .venv
}

Write-Host "[2/4] Updating installation tools..." -ForegroundColor Cyan
& $Python -m pip install --upgrade pip setuptools wheel

Write-Host "[3/4] Installing TransDub AI, Edge TTS and Demucs..." -ForegroundColor Cyan
$Extras = if ($Dev) { ".[inference,dev]" } else { ".[inference]" }
& $Python -m pip install -e $Extras

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "Created .env - add GEMINI_API_KEY before starting." -ForegroundColor Yellow
}

$env:PYTHONUTF8 = "1"
$env:TORCH_HOME = Join-Path $ProjectRoot "models\demucs"
Write-Host "[4/4] Initializing database..." -ForegroundColor Cyan
& $Python -m alembic upgrade head
& $Python -c "from app.services import edge_tts_synth; print('Edge TTS voices:', len(edge_tts_synth.list_voices()))" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "Note: Edge TTS could not be reached now; it will work once online." -ForegroundColor Yellow }

Write-Host "`nTransDub AI is ready. Double-click 'Start TransDub AI.bat'." -ForegroundColor Green