<#
.SYNOPSIS
    AI Daily Digest - Auto-commit agent
.DESCRIPTION
    Fetches the latest AI news/repos, generates a digest, commits, and pushes to GitHub.
    Designed to be triggered by Windows Task Scheduler on internet connection or daily.
#>

$ErrorActionPreference = "Continue"
$PROJECT_DIR = "C:\Users\mitta\ai-daily-digest"
$LOG_FILE = "$PROJECT_DIR\agent.log"
$LOCK_FILE = "$PROJECT_DIR\.running.lock"

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$timestamp | $Message" | Tee-Object -FilePath $LOG_FILE -Append
}

function Test-InternetConnection {
    try {
        $result = Test-Connection -ComputerName "github.com" -Count 1 -Quiet -TimeoutSeconds 5
        return $result
    } catch {
        return $false
    }
}

# ─── Main Logic ──────────────────────────────────────────────────────────────

Write-Log "🚀 AI Daily Digest agent started"

# Prevent duplicate runs
if (Test-Path $LOCK_FILE) {
    $lockAge = (Get-Date) - (Get-Item $LOCK_FILE).LastWriteTime
    if ($lockAge.TotalMinutes -lt 30) {
        Write-Log "⏭ Another instance is running (lock is $([int]$lockAge.TotalMinutes)m old). Exiting."
        exit 0
    }
    Write-Log "🔓 Stale lock found, removing."
    Remove-Item $LOCK_FILE -Force
}

New-Item -Path $LOCK_FILE -ItemType File -Force | Out-Null

try {
    # Wait for internet (up to 5 minutes)
    Write-Log "📡 Checking internet connection..."
    $retries = 0
    while (-not (Test-InternetConnection)) {
        $retries++
        if ($retries -ge 30) {
            Write-Log "❌ No internet after 5 minutes. Aborting."
            exit 1
        }
        Write-Log "  ⏳ No internet. Retry $retries/30..."
        Start-Sleep -Seconds 10
    }
    Write-Log "✅ Internet is available"

    # Navigate to project
    Set-Location $PROJECT_DIR

    # Check if today's digest already exists
    $today = Get-Date -Format "yyyy-MM-dd"
    $digestFile = "digests\$today.md"

    if (Test-Path $digestFile) {
        Write-Log "✅ Digest for $today already exists and committed. Skipping."
        # Still try to push in case a previous push failed
        git push origin main 2>&1 | ForEach-Object { Write-Log "  git: $_" }
        exit 0
    }

    # Run the Python fetcher
    Write-Log "📰 Running digest fetcher..."
    $pythonOutput = python "$PROJECT_DIR\fetch_digest.py" 2>&1
    $pythonOutput | ForEach-Object { Write-Log "  py: $_" }

    if (-not (Test-Path $digestFile)) {
        Write-Log "❌ Digest file was not created. Something went wrong."
        exit 1
    }

    # Git add, commit, push
    Write-Log "📤 Committing and pushing to GitHub..."

    git add -A 2>&1 | ForEach-Object { Write-Log "  git: $_" }

    $commitMsg = "📰 AI Digest for $today - Auto-generated"
    git commit -m $commitMsg 2>&1 | ForEach-Object { Write-Log "  git: $_" }

    git push origin main 2>&1 | ForEach-Object { Write-Log "  git: $_" }

    Write-Log "✅ Successfully committed and pushed digest for $today"

} catch {
    Write-Log "❌ Error: $_"
    exit 1
} finally {
    Remove-Item $LOCK_FILE -Force -ErrorAction SilentlyContinue
    Write-Log "🏁 Agent finished"
    Write-Log "────────────────────────────────────────"
}
