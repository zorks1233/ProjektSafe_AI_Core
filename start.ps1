# ============================================================
# ProjektSafe AI Core – Start (Windows PowerShell)
# 1.  .\start.ps1            -> installiert Abhängigkeiten und startet das Backend
# 2.  Browser: http://127.0.0.1:8000/docs
# ============================================================

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "[1/3] Python-Version prüfen ..." -ForegroundColor Cyan
python --version
if ($LASTEXITCODE -ne 0) { throw "Python nicht gefunden. Bitte Python 3.11+ installieren." }

Write-Host "[2/3] Abhängigkeiten installieren ..." -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r "$Root\backend\requirements.txt"

Write-Host "[3/3] Backend starten -> http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "      API-Doku:        http://127.0.0.1:8000/docs" -ForegroundColor Gray
Write-Host "      Healthcheck:     http://127.0.0.1:8000/api/health" -ForegroundColor Gray
Set-Location $Root
python -m uvicorn ai_platform.api.main:app --app-dir backend --host 127.0.0.1 --port 8000
