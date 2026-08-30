param([switch]$SkipTorch, [switch]$Dev)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
Set-Location -LiteralPath $ProjectRoot

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "FFmpeg was not found in PATH" }
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { throw "FFprobe was not found in PATH" }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python Launcher (py) was not found" }

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "[1/5] Creating Python 3.12 environment..." -ForegroundColor Cyan
    py -3.12 -m venv .venv
}

Write-Host "[2/5] Updating installation tools..." -ForegroundColor Cyan
& $Python -m pip install --upgrade pip setuptools wheel

if (-not $SkipTorch) {
    Write-Host "[3/5] Installing PyTorch CUDA 12.6..." -ForegroundColor Cyan
    & $Python -m pip install torch==2.11.0+cu126 torchvision==0.26.0+cu126 torchaudio==2.11.0+cu126 --index-url https://download.pytorch.org/whl/cu126
} else {
    Write-Host "[3/5] Keeping the existing PyTorch installation." -ForegroundColor Yellow
}

Write-Host "[4/5] Installing TransDub AI, JaiTTS and Demucs..." -ForegroundColor Cyan
$Extras = if ($Dev) { ".[inference,dev]" } else { ".[inference]" }
& $Python -m pip install -e $Extras

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "Created .env - add GEMINI_API_KEY before starting." -ForegroundColor Yellow
}

$env:PYTHONUTF8 = "1"
$env:TORCH_HOME = Join-Path $ProjectRoot "models\demucs"
Write-Host "[5/5] Initializing database and importing reusable voice profiles..." -ForegroundColor Cyan
& $Python -m alembic upgrade head
& $Python scripts\import_legacy_profiles.py
& $Python -c "import torch; print('CUDA:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"

Write-Host "`nTransDub AI is ready. Double-click 'Start TransDub AI.bat'." -ForegroundColor Green
