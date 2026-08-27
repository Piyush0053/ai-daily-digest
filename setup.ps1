<#
.SYNOPSIS
    Setup script for AI Daily Digest agent.
.DESCRIPTION
    - Creates a GitHub repo
    - Initializes git
    - Installs Python dependencies
    - Registers Windows Task Scheduler tasks (daily + on network connect)
#>

$ErrorActionPreference = "Continue"
$PROJECT_DIR = "C:\Users\mitta\ai-daily-digest"
$REPO_NAME = "ai-daily-digest"
$TASK_NAME_DAILY = "AI-Daily-Digest-Daily"
$TASK_NAME_NETWORK = "AI-Daily-Digest-OnNetwork"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "      AI Daily Digest - Setup Wizard            " -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# --- Step 1: Install Python dependencies ---
Write-Host "[Step 1] Installing Python dependencies..." -ForegroundColor Yellow
pip install -r "$PROJECT_DIR\requirements.txt" --quiet
Write-Host "  [OK] Dependencies installed" -ForegroundColor Green

# --- Step 2: Authenticate GitHub CLI ---
Write-Host ""
Write-Host "[Step 2] GitHub CLI Authentication..." -ForegroundColor Yellow

try {
    $ghPath = Get-Command gh -ErrorAction Stop | Select-Object -ExpandProperty Source
    Write-Host "  Found gh at: $ghPath" -ForegroundColor Gray
} catch {
    # Try to find gh in default install locations
    $possiblePaths = @(
        "C:\Program Files\GitHub CLI\gh.exe",
        "C:\Program Files (x86)\GitHub CLI\gh.exe",
        "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe"
    )
    $ghPath = $null
    foreach ($p in $possiblePaths) {
        if (Test-Path $p) {
            $ghPath = $p
            break
        }
    }
    if ($ghPath) {
        Write-Host "  Found gh at: $ghPath (not on PATH)" -ForegroundColor Yellow
        Write-Host "  Adding to PATH for this session..." -ForegroundColor Yellow
        $ghDir = Split-Path $ghPath
        $env:PATH = "$ghDir;$env:PATH"
    } else {
        Write-Host "  [ERROR] GitHub CLI (gh) not found. Please install it first." -ForegroundColor Red
        Write-Host "  Run: winget install --id GitHub.cli" -ForegroundColor Yellow
        exit 1
    }
}

$ghStatus = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  You need to log in to GitHub CLI." -ForegroundColor Red
    Write-Host "  Running 'gh auth login'... Follow the prompts." -ForegroundColor Yellow
    gh auth login
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] GitHub authentication failed. Please run 'gh auth login' manually." -ForegroundColor Red
        exit 1
    }
}
Write-Host "  [OK] GitHub CLI authenticated" -ForegroundColor Green

# --- Step 3: Create GitHub repo ---
Write-Host ""
Write-Host "[Step 3] Creating GitHub repository..." -ForegroundColor Yellow

Set-Location $PROJECT_DIR

# Initialize git if needed
if (-not (Test-Path ".git")) {
    git init
    git branch -M main
}

# Create repo on GitHub (will skip if it already exists)
$repoCheck = gh repo view $REPO_NAME 2>&1
if ($LASTEXITCODE -ne 0) {
    gh repo create $REPO_NAME --public --description "Automated daily AI news digest and trending agent repos" --source . --remote origin --push
    $username = gh api user -q .login
    Write-Host "  [OK] Repository created: https://github.com/$username/$REPO_NAME" -ForegroundColor Green
} else {
    Write-Host "  [OK] Repository already exists" -ForegroundColor Green
    # Make sure remote is set
    $remoteExists = git remote 2>&1
    if ($remoteExists -notcontains "origin") {
        $username = gh api user -q .login
        git remote add origin "https://github.com/$username/$REPO_NAME.git"
    }
}

# Initial commit
git add -A
git commit -m "Initial commit - AI Daily Digest agent" --allow-empty
git push -u origin main 2>$null

Write-Host "  [OK] Initial commit pushed" -ForegroundColor Green

# --- Step 4: Register Task Scheduler tasks ---
Write-Host ""
Write-Host "[Step 4] Setting up Windows Task Scheduler..." -ForegroundColor Yellow

$scriptPath = "$PROJECT_DIR\run_agent.ps1"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`""

# --- Daily trigger at 9:00 AM ---
$triggerDaily = New-ScheduledTaskTrigger -Daily -At "09:00AM"
$settingsDaily = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TASK_NAME_DAILY -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $TASK_NAME_DAILY `
    -Action $action `
    -Trigger $triggerDaily `
    -Settings $settingsDaily `
    -Description "Runs AI Daily Digest every day at 9 AM" `
    -RunLevel Limited

Write-Host "  [OK] Daily task registered (9:00 AM)" -ForegroundColor Green

# --- Network connection trigger (Event-based) ---
$triggerXml = @"
<QueryList>
  <Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational">
    <Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[EventID=10000]]</Select>
  </Query>
</QueryList>
"@

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>$triggerXml</Subscription>
      <Delay>PT30S</Delay>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$scriptPath"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

# Save XML and register
$xmlPath = "$PROJECT_DIR\task_network.xml"
$taskXml | Out-File -FilePath $xmlPath -Encoding Unicode
Unregister-ScheduledTask -TaskName $TASK_NAME_NETWORK -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TASK_NAME_NETWORK -Xml (Get-Content $xmlPath -Raw)
Remove-Item $xmlPath -Force

Write-Host "  [OK] Network trigger task registered (runs on internet connect)" -ForegroundColor Green

# --- Done ---
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "      Setup Complete!                           " -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Your agent will:" -ForegroundColor White
Write-Host "  - Run every day at 9:00 AM" -ForegroundColor White
Write-Host "  - Run when your PC connects to the internet" -ForegroundColor White
Write-Host "  - Fetch latest AI news and trending repos" -ForegroundColor White
Write-Host "  - Auto-commit and push to GitHub" -ForegroundColor White
Write-Host ""
Write-Host "To run it manually right now:" -ForegroundColor Yellow
Write-Host ('  powershell -File "' + $scriptPath + '"') -ForegroundColor Cyan
Write-Host ""
Write-Host "To check logs:" -ForegroundColor Yellow
Write-Host ('  Get-Content "' + $PROJECT_DIR + '\agent.log" -Tail 20') -ForegroundColor Cyan
Write-Host ""
