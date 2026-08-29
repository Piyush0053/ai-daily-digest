<#
.SYNOPSIS
    AI Daily Digest - Auto-commit agent
.DESCRIPTION
    Fetches the latest AI news/repos, generates a digest, commits, and pushes to GitHub.
    Triggered by Windows Task Scheduler (daily at 9 AM + on internet connect).

Exit codes from fetch_digest.py:
    0 = success or "already done today"
    1 = error / crash
    2 = skip commit (not enough articles found, will retry next trigger)
#>

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$PROJECT_DIR = "C:\Users\mitta\ai-daily-digest"
$LOG_FILE    = "$PROJECT_DIR\agent.log"
$LOCK_FILE   = "$PROJECT_DIR\.running.lock"

# Ensure gh is on PATH
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    if (Test-Path "C:\Program Files\GitHub CLI\gh.exe") {
        $env:PATH = "C:\Program Files\GitHub CLI;$env:PATH"
    }
}

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$timestamp | $Message" | Tee-Object -FilePath $LOG_FILE -Append
}

function Test-InternetConnection {
    try {
        Invoke-WebRequest -Uri "https://github.com" -Method Head `
            -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

# --- Main Logic ---

Write-Log "Agent started"

# Lock timeout: 10 min
if (Test-Path $LOCK_FILE) {
    $lockAge = (Get-Date) - (Get-Item $LOCK_FILE).LastWriteTime
    if ($lockAge.TotalMinutes -lt 10) {
        Write-Log "Another instance is running (lock is $([int]$lockAge.TotalMinutes)m old). Exiting."
        exit 0
    }
    Write-Log "Stale lock found ($([int]$lockAge.TotalMinutes)m old), removing."
    Remove-Item $LOCK_FILE -Force
}

New-Item -Path $LOCK_FILE -ItemType File -Force | Out-Null

try {
    # Wait for internet: up to 5 minutes with HTTP check
    Write-Log "Checking internet connection..."
    $retries = 0
    while (-not (Test-InternetConnection)) {
        $retries++
        if ($retries -ge 30) {
            Write-Log "No internet after 5 minutes. Aborting."
            exit 1
        }
        Write-Log "  No internet. Retry $retries/30 (waiting 10s)..."
        Start-Sleep -Seconds 10
    }
    Write-Log "Internet is available"

    Set-Location $PROJECT_DIR

    # Check if today's digest already exists
    $today      = Get-Date -Format "yyyy-MM-dd"
    $digestFile = "digests\$today.md"

    if (Test-Path $digestFile) {
        Write-Log "Digest for $today already exists. Pushing any pending commits..."
        git push origin main 2>&1 | ForEach-Object { Write-Log "  git: $_" }
        exit 0
    }

    # Run the Python fetcher
    Write-Log "Running digest fetcher..."
    $pythonOutput = python "$PROJECT_DIR\fetch_digest.py" 2>&1
    $exitCode     = $LASTEXITCODE
    $pythonOutput | ForEach-Object { Write-Log "  py: $_" }

    # Handle exit code 2 = skip commit (not enough articles)
    if ($exitCode -eq 2) {
        Write-Log "Skipping commit: not enough articles fetched. Will retry on next trigger."
        exit 0
    }

    if ($exitCode -ne 0) {
        Write-Log "ERROR: Python script failed with exit code $exitCode."
        exit 1
    }

    if (-not (Test-Path $digestFile)) {
        Write-Log "ERROR: Digest file was not created despite exit code 0."
        exit 1
    }

    # Git add, commit, push
    Write-Log "Committing and pushing to GitHub..."

    git add -A 2>&1 | ForEach-Object { Write-Log "  git: $_" }

    $commitMsg = "AI Digest for $today - Auto-generated"
    git commit -m $commitMsg 2>&1 | ForEach-Object { Write-Log "  git: $_" }

    $pushOutput = git push origin main 2>&1
    $pushExit   = $LASTEXITCODE
    $pushOutput | ForEach-Object { Write-Log "  git: $_" }

    if ($pushExit -eq 0) {
        Write-Log "Successfully committed and pushed digest for $today"
    } else {
        Write-Log "WARNING: Commit succeeded but push failed (exit $pushExit). Will retry on next trigger."
    }

} catch {
    Write-Log "ERROR: $_"
    exit 1
} finally {
    Remove-Item $LOCK_FILE -Force -ErrorAction SilentlyContinue
    Write-Log "Agent finished"
    Write-Log "----------------------------------------"
}
