#Requires -Version 5.1
<#
.SYNOPSIS
    Deus CLI for Windows — mirrors deus-cmd.sh on macOS/Linux.

.DESCRIPTION
    Usage:
      deus              Launch Claude Code in current directory (external project mode)
      deus home         Launch Claude Code in home mode (~\deus)
      deus auth         Rebuild dist/ and restart the background service
      deus status       Show service status
      deus logs         Tail the Deus service log

    The Deus background service runs under NSSM or Servy.
    Credential proxy reads ~/.claude/.credentials.json directly — do NOT
    write CLAUDE_CODE_OAUTH_TOKEN to .env (causes login loop on token rotation).
#>

param(
    [Parameter(Position = 0)]
    [string]$Command = ""
)

$DeusHome = "$env:USERPROFILE\deus"
$ServiceName = "deus"
$LogFile = "$DeusHome\logs\deus.log"
$ErrorLog = "$DeusHome\logs\deus.error.log"

# ── Helpers ──────────────────────────────────────────────────────────────────

function Get-ServiceManager {
    if (Get-Command "servy-cli" -ErrorAction SilentlyContinue) { return "servy" }
    if (Get-Command "nssm" -ErrorAction SilentlyContinue) { return "nssm" }
    return "none"
}

function Restart-DeusService {
    $mgr = Get-ServiceManager
    switch ($mgr) {
        "servy" {
            & servy-cli restart --name=$ServiceName
        }
        "nssm" {
            & nssm restart $ServiceName
        }
        "none" {
            Write-Host "No service manager found. Start Deus manually: node $DeusHome\dist\index.js" -ForegroundColor Yellow
            Write-Host "Install NSSM: choco install nssm" -ForegroundColor Yellow
        }
    }
}

function Get-DeusServiceStatus {
    $mgr = Get-ServiceManager
    switch ($mgr) {
        "servy" {
            & servy-cli status --name=$ServiceName
        }
        "nssm" {
            & nssm status $ServiceName
        }
        "none" {
            # Fall back to checking if the process is running
            $proc = Get-Process -Name "node" -ErrorAction SilentlyContinue |
                Where-Object { $_.MainModule.FileName -like "*\deus\*" }
            if ($proc) {
                Write-Host "Deus is running (PID $($proc.Id))" -ForegroundColor Green
            } else {
                Write-Host "Deus does not appear to be running." -ForegroundColor Yellow
            }
        }
    }
}

function Build-Deus {
    Write-Host "  Building..." -NoNewline
    Push-Location $DeusHome
    try {
        & npm run build --silent
        if ($LASTEXITCODE -ne 0) {
            Write-Host " FAILED" -ForegroundColor Red
            Write-Error "Build failed — not restarting."
            return $false
        }
    } finally {
        Pop-Location
    }
    Write-Host " OK" -ForegroundColor Green
    return $true
}

function Read-VaultFile {
    param([string]$Path)
    if (Test-Path $Path) { return Get-Content $Path -Raw } else { return "" }
}

function Get-VaultPath {
    $envVault = $env:DEUS_VAULT_PATH
    if ($envVault) { return $envVault }
    $configPath = "$env:USERPROFILE\.config\deus\config.json"
    if (Test-Path $configPath) {
        try {
            $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
            return $cfg.vault_path
        } catch { }
    }
    return ""
}

function Invoke-ClaudeWithContext {
    param([string]$WorkDir)

    # Load token for current shell session (service uses credentials.json directly)
    $credPath = "$env:USERPROFILE\.claude\.credentials.json"
    if (Test-Path $credPath) {
        try {
            $creds = Get-Content $credPath -Raw | ConvertFrom-Json
            $token = $creds.claudeAiOauth.accessToken
            if ($token) { $env:CLAUDE_CODE_OAUTH_TOKEN = $token }
        } catch { }
    }

    $vault = Get-VaultPath
    if (-not $vault) {
        Write-Host "Warning: No vault configured. Set DEUS_VAULT_PATH or vault_path in ~/.config/deus/config.json" -ForegroundColor Yellow
        Set-Location $WorkDir
        & claude --dangerously-skip-permissions
        return
    }

    # ── Load context (mirrors deus-cmd.sh context loading) ──────────────────
    Write-Host "  Reading vault...`r" -NoNewline
    $claudeMd  = Read-VaultFile "$vault\CLAUDE.md"
    $studyMd   = Read-VaultFile "$vault\STUDY.md"
    $infraMd   = Read-VaultFile "$vault\INFRA.md"

    $context = ""
    if ($claudeMd) { $context += "=== VAULT: CLAUDE.md ===`n$claudeMd" }
    if ($studyMd)  { $context += "`n`n=== VAULT: STUDY.md ===`n$studyMd" }
    if ($infraMd)  { $context += "`n`n=== VAULT: INFRA.md ===`n$infraMd" }

    # Checkpoint (today's)
    Write-Host "  Checking checkpoints...`r" -NoNewline
    $today = Get-Date -Format "yyyy-MM-dd"
    $checkpointDir = "$vault\Checkpoints"
    if (Test-Path $checkpointDir) {
        $cpFile = Get-ChildItem $checkpointDir -Filter "$today-*.md" |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($cpFile) {
            $cp = Get-Content $cpFile.FullName -Raw
            if ($cp) { $context += "`n`n=== MID-SESSION CHECKPOINT ===`n$cp" }
        }
    }

    # Recent sessions (via memory_indexer.py)
    Write-Host "  Loading recent sessions...`r" -NoNewline
    $pythonCmd = if (Get-Command "python3" -ErrorAction SilentlyContinue) { "python3" } else { "python" }
    $indexerPath = "$DeusHome\scripts\memory_indexer.py"
    if ((Get-Command $pythonCmd -ErrorAction SilentlyContinue) -and (Test-Path $indexerPath)) {
        $recent = & $pythonCmd $indexerPath --recent 3 2>$null
        if ($recent) { $context += "`n`n=== RECENT SESSIONS ===`n$recent" }
    }

    # Query memory for related sessions
    $related = ""
    if ((Get-Command $pythonCmd -ErrorAction SilentlyContinue) -and (Test-Path $indexerPath)) {
        $related = & $pythonCmd $indexerPath --query --top 2 --recency-boost 2>$null
        if ($related) { $context += "`n`n=== RELATED SESSIONS ===`n$related" }
    }

    # Git status for context
    Write-Host "  Loading git status...`r" -NoNewline
    $gitStatus = ""
    Push-Location $WorkDir
    try {
        $branch    = & git rev-parse --abbrev-ref HEAD 2>$null
        $mainBranch = "main"
        $status    = & git status --short 2>$null
        $log       = & git log --oneline -5 2>$null
        if ($branch) {
            $gitStatus  = "Current branch: $branch`n`nMain branch (you will usually use this for PRs): $mainBranch`n`n"
            $gitStatus += "Status:`n$(if ($status) { $status } else { '(clean)' })`n`nRecent commits:`n$($log -join "`n")"
        }
    } catch { } finally { Pop-Location }
    if ($gitStatus) { $context += "`n`n=== GIT STATUS ===`n$gitStatus" }

    Write-Host "  " + (" " * 40) + "`r" -NoNewline  # clear line

    # Launch Claude with system prompt
    $env:CLAUDE_SYSTEM_PROMPT = $context
    Set-Location $WorkDir
    & claude --dangerously-skip-permissions
}

# ── Commands ─────────────────────────────────────────────────────────────────

switch ($Command.ToLower()) {
    "auth" {
        # Validate credentials before restarting
        $credPath = "$env:USERPROFILE\.claude\.credentials.json"
        if (-not (Test-Path $credPath)) {
            Write-Error "Error: ~/.claude/.credentials.json not found. Run 'claude' to authenticate first."
            exit 1
        }
        try {
            $creds = Get-Content $credPath -Raw | ConvertFrom-Json
            if (-not $creds.claudeAiOauth.accessToken) { throw "no token" }
        } catch {
            Write-Error "Error: could not read token from ~/.claude/.credentials.json"
            exit 1
        }

        # Rebuild then restart
        if (-not (Build-Deus)) { exit 1 }
        Restart-DeusService
        Write-Host "Deus built and restarted."
    }

    "status" {
        Get-DeusServiceStatus
    }

    "logs" {
        if (Test-Path $LogFile) {
            Get-Content $LogFile -Wait -Tail 50
        } else {
            Write-Host "Log file not found: $LogFile" -ForegroundColor Yellow
        }
    }

    "home" {
        Invoke-ClaudeWithContext -WorkDir $DeusHome
    }

    { $_ -eq "" -or $_ -eq "." } {
        $currentDir = (Get-Location).Path
        if ($currentDir -eq $DeusHome) {
            Invoke-ClaudeWithContext -WorkDir $DeusHome
        } else {
            Invoke-ClaudeWithContext -WorkDir $currentDir
        }
    }

    "listen" {
        # Record from mic, transcribe with whisper.cpp, copy to clipboard.
        # Uses sox for recording and whisper-cli for transcription.
        $whisperBin = if ($env:WHISPER_BIN) { $env:WHISPER_BIN } else { "whisper-cli" }
        $whisperModel = if ($env:WHISPER_MODEL) { $env:WHISPER_MODEL } else { "$DeusHome\data\models\ggml-base.bin" }
        $whisperLang = if ($env:WHISPER_LANG) { $env:WHISPER_LANG } else { "en" }
        $modelUrl = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"

        # Check dependencies
        $deps = @("sox", $whisperBin, "ffmpeg")
        $missingDeps = $deps | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) }
        if ($missingDeps) {
            Write-Host "Missing dependencies: $($missingDeps -join ', ')" -ForegroundColor Red
            Write-Host "Install with: choco install sox.portable ffmpeg"
            Write-Host "whisper-cpp: download from https://github.com/ggerganov/whisper.cpp/releases"
            exit 1
        }

        # Auto-download model
        if (-not (Test-Path $whisperModel)) {
            Write-Host "Whisper model not found. Downloading ggml-base.bin (148 MB)..."
            $modelDir = Split-Path $whisperModel -Parent
            if (-not (Test-Path $modelDir)) { New-Item -ItemType Directory -Path $modelDir -Force | Out-Null }
            Invoke-WebRequest -Uri $modelUrl -OutFile $whisperModel -UseBasicParsing
            Write-Host "Download complete."
        }

        # Record
        $tmpFile = Join-Path $env:TEMP "deus-voice-$(Get-Date -Format 'yyyyMMddHHmmss').wav"
        Write-Host ""
        Write-Host "  Recording... (press Ctrl+C to stop)" -ForegroundColor Cyan
        Write-Host ""

        try {
            # rec from sox: 16kHz mono WAV
            & sox -d -q -r 16000 -c 1 -b 16 $tmpFile
        } catch { }

        if (-not (Test-Path $tmpFile) -or (Get-Item $tmpFile).Length -lt 16000) {
            Write-Host "  Recording too short or failed. Try again." -ForegroundColor Yellow
            if (Test-Path $tmpFile) { Remove-Item $tmpFile -Force }
            exit 1
        }

        # Transcribe
        Write-Host "  Transcribing..." -ForegroundColor Cyan
        $transcript = & $whisperBin -m $whisperModel -f $tmpFile --no-timestamps -nt -l $whisperLang 2>$null
        $transcript = ($transcript | Where-Object { $_.Trim() -ne "" }) -join " "
        $transcript = $transcript.Trim()

        # Cleanup
        Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue

        if (-not $transcript) {
            Write-Host "  Could not transcribe audio. Try speaking louder or longer." -ForegroundColor Yellow
            exit 1
        }

        Write-Host ""
        Write-Host "  $transcript"
        Write-Host ""
        Set-Clipboard -Value $transcript
        Write-Host "  Copied to clipboard. Paste with Ctrl+V." -ForegroundColor Green
        Write-Host ""
    }

    default {
        Write-Host "Usage: deus [home|auth|status|logs|listen]"
        Write-Host ""
        Write-Host "  deus        Launch in current directory (external project mode if not ~\deus)"
        Write-Host "  deus home   Launch in home mode (~\deus) regardless of current directory"
        Write-Host "  deus auth   Rebuild dist/ and restart background service"
        Write-Host "  deus status Show service status (NSSM or Servy)"
        Write-Host "  deus logs   Tail the Deus service log"
        Write-Host "  deus listen Record from mic, transcribe, and copy to clipboard"
    }
}
