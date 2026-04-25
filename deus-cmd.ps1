#Requires -Version 5.1
<#
.SYNOPSIS
    Deus CLI for Windows - mirrors deus-cmd.sh on macOS/Linux.

.DESCRIPTION
    Usage:
      deus              Launch Claude Code in current directory (external project mode)
      deus home         Launch Claude Code in home mode (~\deus)
      deus auth         Rebuild dist/ and restart the background service
      deus status       Show service status
      deus backend      Manage default AI backend and model (show|set|model|list)
      deus logs         Review system health logs (rotate|review|summary|pinned)

    The Deus background service runs under NSSM or Servy.
    Credential proxy reads ~/.claude/.credentials.json directly - do NOT
    write CLAUDE_CODE_OAUTH_TOKEN to .env (causes login loop on token rotation).
#>

param(
    [Parameter(Position = 0)]
    [string]$Command = ""
)

# Resolve DeusHome from script location (works regardless of clone path)
$DeusHome = if ($env:DEUS_HOME) { $env:DEUS_HOME } else { Split-Path -Parent $PSCommandPath }
$ServiceName = "deus"
$LogFile = "$DeusHome\logs\deus.log"
$ErrorLog = "$DeusHome\logs\deus.error.log"

# -- Helpers ------------------------------------------------------------------

function Invoke-Claude {
    param([string[]]$ExtraArgs)
    $prefs = Get-DeusPreferences
    if ($prefs.bypass_permissions) {
        & claude --dangerously-skip-permissions @ExtraArgs
        if ($LASTEXITCODE -ne 0) {
            & claude @ExtraArgs
        }
    } else {
        & claude @ExtraArgs
    }
}

function Read-ConfigKey {
    param([string]$Key)
    $configPath = "$env:USERPROFILE\.config\deus\config.json"
    if (-not (Test-Path $configPath)) { return "" }
    try {
        $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
        $val = $cfg.$Key
        if ($val) { return $val } else { return "" }
    } catch { return "" }
}

function Write-ConfigKey {
    param([string]$Key, [string]$Value)
    $configPath = "$env:USERPROFILE\.config\deus\config.json"
    $configDir = Split-Path -Parent $configPath
    if (-not (Test-Path $configDir)) { New-Item -ItemType Directory -Path $configDir -Force | Out-Null }
    $cfg = @{}
    if (Test-Path $configPath) {
        try { $cfg = Get-Content $configPath -Raw | ConvertFrom-Json -AsHashtable } catch { $cfg = @{} }
    }
    $cfg[$Key] = $Value
    $cfg | ConvertTo-Json -Depth 10 | Set-Content $configPath
}

function Write-EnvKey {
    param([string]$Key, [string]$Value)
    $envFile = Join-Path $DeusHome ".env"
    if (-not (Test-Path $envFile)) { return }
    $lines = Get-Content $envFile
    $found = $false
    $newLines = $lines | ForEach-Object {
        if ($_ -match "^$Key=") { $found = $true; "$Key=$Value" } else { $_ }
    }
    if ($found) { $newLines | Set-Content $envFile }
}

function Get-DeusCliAgent {
    $agent = if ($env:DEUS_CLI_AGENT) { $env:DEUS_CLI_AGENT }
             elseif ($env:DEUS_AGENT_BACKEND) { $env:DEUS_AGENT_BACKEND }
             else { Read-ConfigKey "agent_backend" }
    if (-not $agent) { $agent = "claude" }
    switch ($agent.ToLower()) {
        "openai" { return "codex" }
        "codex"  { return "codex" }
        "ollama" { return "ollama" }
        default  { return "claude" }
    }
}

function Invoke-Codex {
    param([string[]]$ExtraArgs)
    if (-not (Get-Command "codex" -ErrorAction SilentlyContinue)) {
        Write-Error "Codex CLI not found. Install/login to Codex, or use DEUS_CLI_AGENT=claude."
        return
    }

    $systemPrompt = ""
    $userPromptParts = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $ExtraArgs.Count; $i++) {
        if ($ExtraArgs[$i] -eq "--append-system-prompt" -and $i + 1 -lt $ExtraArgs.Count) {
            $systemPrompt = $ExtraArgs[$i + 1]
            $i++
        } else {
            $userPromptParts.Add($ExtraArgs[$i])
        }
    }

    $prompt = $systemPrompt
    if ($userPromptParts.Count -gt 0) {
        $prompt += "`n`nUSER REQUEST:`n" + ($userPromptParts -join "`n")
    }

    $codexArgs = @()
    $codexModel = if ($env:DEUS_CODEX_MODEL) { $env:DEUS_CODEX_MODEL } elseif ($env:DEUS_OPENAI_MODEL) { $env:DEUS_OPENAI_MODEL } else { Read-ConfigKey "agent_backend_model" }
    if ($codexModel) {
        $codexArgs += @("--model", $codexModel)
    }

    $prefs = Get-DeusPreferences
    if ($prefs.bypass_permissions) {
        & codex @codexArgs --dangerously-bypass-approvals-and-sandbox $prompt
        if ($LASTEXITCODE -ne 0) {
            & codex @codexArgs $prompt
        }
    } else {
        & codex @codexArgs $prompt
    }
}

function Invoke-Agent {
    param([string[]]$ExtraArgs)
    $cliAgent = Get-DeusCliAgent
    switch ($cliAgent) {
        "ollama" {
            Write-Error "Ollama backend is not yet available as a CLI agent. Use 'deus backend set claude' or 'deus backend set openai' instead."
            return
        }
        "codex" { Invoke-Codex $ExtraArgs }
        default { Invoke-Claude $ExtraArgs }
    }
}

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
            Write-Error "Build failed - not restarting."
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

function Write-Status {
    param([string]$Message)
    Write-Host "`r  $Message$(' ' * (40 - $Message.Length))" -NoNewline
}

function Get-DeusIdentity {
    return @"
You are Deus - the user's personal AI assistant. You are not a generic coding tool. You collaborate on everything: coding, studies, life decisions, recommendations, brainstorming, and anything the user brings to you.

Key capabilities you have:
- Memory: you remember context across conversations. If a vault is configured, you have access to session logs, preferences, and project history.
- Channels: WhatsApp, Telegram, Slack, Discord, Gmail - the user may talk to you through any of these.
- Vision and voice: you can see images and transcribe voice messages.
- Calendar: you can read and create Google Calendar events.
- Self-improvement: you score your own responses and learn from both successes and failures over time.

Your personality:
- Concise and direct. No filler, no fluff.
- You run commands directly - never ask the user to run things manually.
- You prefer long-term scalable solutions over quick fixes.
- Security-conscious: never commit credentials, design as if the repo is public.

This repo (~/deus) is the infrastructure that powers you. See README.md for philosophy and CLAUDE.md for development rules.
"@
}

function Get-DeusPreferences {
    $configPath = "$env:USERPROFILE\.config\deus\config.json"
    $defaults = @{
        name = ""
        catch_me_up = $true
        bypass_permissions = $true
        persona = ""
    }
    if (-not (Test-Path $configPath)) { return $defaults }
    try {
        $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
        if ($cfg.name) { $defaults.name = $cfg.name }
        if ($null -ne $cfg.catch_me_up) { $defaults.catch_me_up = [bool]$cfg.catch_me_up }
        if ($null -ne $cfg.bypass_permissions) { $defaults.bypass_permissions = [bool]$cfg.bypass_permissions }
        if ($cfg.persona) { $defaults.persona = $cfg.persona }
    } catch { }
    return $defaults
}

function Get-ProjectMemorySettings {
    param([string]$WorkDir)
    $defaults = @{
        memory_level = "standard"
        save_summaries = $true
    }

    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($WorkDir)
        $md5 = [System.Security.Cryptography.MD5]::Create()
        $hashBytes = $md5.ComputeHash($bytes)
        $hash = -join ($hashBytes | ForEach-Object { $_.ToString("x2") })
        $projectConfigPath = Join-Path $env:USERPROFILE ".config\deus\projects\$hash.json"
        if (-not (Test-Path $projectConfigPath)) { return $defaults }

        $cfg = Get-Content $projectConfigPath -Raw | ConvertFrom-Json
        if ($cfg.memory_level) { $defaults.memory_level = [string]$cfg.memory_level }
        if ($null -ne $cfg.save_summaries) { $defaults.save_summaries = [bool]$cfg.save_summaries }
    } catch { }

    return $defaults
}

function Invoke-ClaudeWithContext {
    param([string]$WorkDir)

    # Load preferences
    Write-Status "Loading preferences..."
    $prefs = Get-DeusPreferences

    # Load token for current shell session (service uses credentials.json directly)
    Write-Status "Loading credentials..."
    $credPath = "$env:USERPROFILE\.claude\.credentials.json"
    if (Test-Path $credPath) {
        try {
            $creds = Get-Content $credPath -Raw | ConvertFrom-Json
            $token = $creds.claudeAiOauth.accessToken
            if ($token) { $env:CLAUDE_CODE_OAUTH_TOKEN = $token }
        } catch { }
    }

    $vault = Get-VaultPath
    $isHomeMode = ($WorkDir -eq $DeusHome)
    $projectMemory = if ($isHomeMode) {
        @{ memory_level = "home"; save_summaries = $true }
    } else {
        Get-ProjectMemorySettings -WorkDir $WorkDir
    }
    $skipVaultRecall = (-not $isHomeMode -and $projectMemory.memory_level -eq "restricted")

    # Git context
    Write-Status "Loading git status..."
    $gitContext = ""
    Push-Location $WorkDir
    try {
        $branch    = & git rev-parse --abbrev-ref HEAD 2>$null
        $mainBranch = "main"
        $gitStatus = & git status --short 2>$null
        $gitLog    = & git log --oneline -5 2>$null
        if ($branch) {
            $gitContext  = "Current branch: $branch`nMain branch: $mainBranch`n"
            $gitContext += "Status:`n$(if ($gitStatus) { $gitStatus } else { '(clean)' })`n`nRecent commits:`n$($gitLog -join "`n")"
        }
    } catch { } finally { Pop-Location }

    # Build startup instruction
    $identity = Get-DeusIdentity
    if ($prefs.name) { $identity += "`n`nThe user's name is $($prefs.name)." }
    if ($prefs.persona) { $identity += "`n`nAdditional instructions from the user: $($prefs.persona)" }

    if (-not $vault) {
        Write-Host "`r  Ready.                                " -ForegroundColor Green
        Write-Host ""
        $prompt = "STARTUP INSTRUCTION:`n`n$identity"
        if ($gitContext) { $prompt += "`n`ngitStatus:`n$gitContext" }
        Write-Host "Warning: No vault configured. Set DEUS_VAULT_PATH or vault_path in ~/.config/deus/config.json" -ForegroundColor Yellow
        Set-Location $WorkDir
        Invoke-Agent @("--append-system-prompt", $prompt)
        return
    }

    $context = ""
    if ($skipVaultRecall) {
        Write-Status "Restricted memory: skipping vault recall..."
    } else {
        # -- Load context (mirrors deus-cmd.sh context loading) ------------------
        # Auto-load CLAUDE.md plus the future-neutral AGENTS.md alias, and STATE.md
        # (churny previous/pending written by /compress). Other leaves load on
        # demand via memory_tree. Missing files silent-skip for legacy vaults.
        Write-Status "Reading vault..."
        $claudeMd  = Read-VaultFile "$vault\CLAUDE.md"
        $agentsMd  = Read-VaultFile "$vault\AGENTS.md"
        $stateMd   = Read-VaultFile "$vault\STATE.md"

        if ($claudeMd) { $context += "=== VAULT: CLAUDE.md ===`n$claudeMd" }
        if ($agentsMd) { $context += "`n`n=== VAULT: AGENTS.md ===`n$agentsMd" }
        if ($stateMd)  { $context += "`n`n=== VAULT: STATE.md ===`n$stateMd" }

        # Memory tree (Phase 4, gated by DEUS_MEMORY_TREE=1 during dogfood).
        if ($env:DEUS_MEMORY_TREE -eq "1") {
            $memoryTreeMd = Read-VaultFile "$vault\MEMORY_TREE.md"
            if ($memoryTreeMd) {
                $context += "`n`n=== VAULT: MEMORY_TREE.md ===`n$memoryTreeMd`n`n=== MEMORY TREE USAGE ===`nFor factual personal questions (identity, household, preferences, cross-branch), call:`n  python3 `$HOME/deus/scripts/memory_tree.py query `"<question>`"`nThe top result's path is the vault file to Read. On abstained:true or low confidence, fall back to Persona/INDEX.md. Prefer this over guessing from CLAUDE.md hints."
            }
        }

        # Checkpoint (today's)
        Write-Status "Checking checkpoints..."
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
        Write-Status "Loading recent sessions..."
        $pythonCmd = if (Get-Command "python3" -ErrorAction SilentlyContinue) { "python3" } else { "python" }
        $indexerPath = "$DeusHome\scripts\memory_indexer.py"
        if ((Get-Command $pythonCmd -ErrorAction SilentlyContinue) -and (Test-Path $indexerPath)) {
            $recent = & $pythonCmd $indexerPath --recent 3 2>$null
            if ($recent) { $context += "`n`n=== RECENT SESSIONS ===`n$recent" }
        }

        # Query memory for related sessions
        Write-Status "Retrieving relevant context..."
        if ((Get-Command $pythonCmd -ErrorAction SilentlyContinue) -and (Test-Path $indexerPath)) {
            $related = & $pythonCmd $indexerPath --query --top 2 --recency-boost 2>$null
            if ($related) { $context += "`n`n=== RELATED SESSIONS ===`n$related" }
        }
    }

    # Git status for context
    if ($gitContext) { $context += "`n`n=== GIT STATUS ===`n$gitContext" }

    Write-Host "`r  Ready.                                " -ForegroundColor Green
    Write-Host ""

    # Build startup instruction with optional catch-me-up greeting
    if ($isHomeMode -and $prefs.catch_me_up) {
        $startupInstruction = @"
STARTUP INSTRUCTION:

$identity

Context from the memory vault has been pre-loaded above. Catch the user up using exactly this format:

* Previous session: [1-2 lines of ongoing context and last session topic]
* Pending: [bullet list of pending tasks, max 3 items]

Then stop and wait for the user.
"@
    } elseif ($isHomeMode) {
        $startupInstruction = @"
STARTUP INSTRUCTION:

$identity

Context from the memory vault has been pre-loaded above. Wait for the user's instructions.
"@
    } else {
        $memoryScope = if ($skipVaultRecall) {
            "Saved vault/session memory was intentionally not preloaded for this project. Use Deus core behavior, live repo state, and live tools only."
        } else {
            "You have your full memory, preferences, and capabilities."
        }
        $startupInstruction = @"
STARTUP INSTRUCTION:

$identity

You are operating in EXTERNAL PROJECT MODE. The current directory is an external codebase at $WorkDir - not the Deus project. $memoryScope Focus on this codebase while applying all your behavioral rules and knowledge.

gitStatus:
$gitContext
"@
    }

    # Combine context + startup instruction
    $fullPrompt = ""
    if ($context) { $fullPrompt = $context + "`n`n" }
    $fullPrompt += $startupInstruction

    Set-Location $WorkDir
    if ($isHomeMode -and $prefs.catch_me_up) {
        Invoke-Agent @("--append-system-prompt", $fullPrompt, "Catch me up.")
    } else {
        Invoke-Agent @("--append-system-prompt", $fullPrompt)
    }
}

# -- Commands -----------------------------------------------------------------

if ($Command.ToLower() -in @("codex", "claude")) {
    if ($Command.ToLower() -eq "claude") {
        $env:DEUS_CLI_AGENT = "claude"
        $env:DEUS_AGENT_BACKEND = "claude"
    } else {
        $env:DEUS_CLI_AGENT = "codex"
        $env:DEUS_AGENT_BACKEND = "openai"
    }
    if ($args.Count -gt 0) {
        $Command = $args[0]
        $args = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }
    } else {
        $Command = ""
    }
}

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

    "backend" {
        $currentBackend = Read-ConfigKey "agent_backend"
        if (-not $currentBackend) { $currentBackend = if ($env:DEUS_AGENT_BACKEND) { $env:DEUS_AGENT_BACKEND } else { "claude" } }
        $displayMap = @{ "openai" = "codex" }
        $currentDisplay = if ($displayMap.ContainsKey($currentBackend)) { $displayMap[$currentBackend] } else { $currentBackend }
        $currentModel = Read-ConfigKey "agent_backend_model"

        $sub = if ($args.Count -gt 0) { $args[0] } else { "show" }
        switch ($sub.ToLower()) {
            "show" {
                Write-Host "Backend: $currentDisplay"
                if ($currentModel) { Write-Host "Model:   $currentModel" }
                if ($env:DEUS_AGENT_BACKEND) { Write-Host "(env override: DEUS_AGENT_BACKEND=$($env:DEUS_AGENT_BACKEND))" }
            }
            "list" {
                foreach ($b in @("claude", "codex", "ollama")) {
                    if ($b -eq $currentDisplay) {
                        Write-Host "* $b (active)"
                    } else {
                        Write-Host "  $b"
                    }
                }
            }
            "set" {
                if ($args.Count -lt 2) {
                    Write-Host "Usage: deus backend set <claude|codex|ollama>"
                    exit 1
                }
                $input = $args[1].ToLower()
                if ($input -notin @("claude", "codex", "ollama")) {
                    Write-Host "Unknown backend: $($args[1])"
                    Write-Host "Available: claude, codex, ollama"
                    exit 1
                }
                $internalMap = @{ "codex" = "openai" }
                $internalVal = if ($internalMap.ContainsKey($input)) { $internalMap[$input] } else { $input }
                Write-ConfigKey "agent_backend" $internalVal
                Write-EnvKey "DEUS_AGENT_BACKEND" $internalVal
                Write-Host "Default backend set to: $input"
                Write-Host "Takes effect on next 'deus' launch. Background service uses .env."
            }
            "model" {
                if ($args.Count -lt 2) {
                    if ($currentModel) {
                        Write-Host "Current model: $currentModel (backend: $currentDisplay)"
                    } else {
                        Write-Host "No model override set (using backend default)"
                    }
                    return
                }
                $newModel = $args[1]
                Write-ConfigKey "agent_backend_model" $newModel
                if ($currentBackend -eq "openai") {
                    Write-EnvKey "DEUS_OPENAI_MODEL" $newModel
                    Write-EnvKey "DEUS_CODEX_MODEL" $newModel
                }
                Write-Host "Model set to: $newModel (backend: $currentDisplay)"
                Write-Host "Takes effect on next 'deus' launch."
            }
            default {
                Write-Host "Usage: deus backend [show|set|model|list]"
                Write-Host ""
                Write-Host "  deus backend           Show current backend and model"
                Write-Host "  deus backend set <be>  Set default backend (claude|codex|ollama)"
                Write-Host "  deus backend model <m> Set model for current backend (e.g. gpt-4o)"
                Write-Host "  deus backend list      List available backends"
            }
        }
    }

    "status" {
        Get-DeusServiceStatus
    }

    "logs" {
        $sub = if ($args.Count -gt 0) { $args[0] } else { "" }
        $reviewScript = Join-Path $DeusHome "scripts\log_review.py"
        switch ($sub.ToLower()) {
            "summary" { & python3 $reviewScript --summary }
            "pinned"  { & python3 $reviewScript --pinned }
            "rotate"  { & python3 $reviewScript --rotate-only }
            "review"  { & python3 $reviewScript --review-only }
            ""        { & python3 $reviewScript }
            default {
                Write-Host "Usage: deus logs [summary|pinned|rotate|review]"
                Write-Host ""
                Write-Host "  deus logs           Rotate old logs + run Ollama health review"
                Write-Host "  deus logs summary   Print last saved daily report"
                Write-Host "  deus logs pinned    Print pinned issues needing attention"
                Write-Host "  deus logs rotate    Rotation only"
                Write-Host "  deus logs review    Health review only"
            }
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
        # Phase 2+: Node.js with live VU meter. Use --stream for continuous dictation.
        $remaining = $Args[1..($Args.Length - 1)]
        & node "$DeusHome\dist\deus-listen.js" @remaining
    }

    default {
        Write-Host "Usage: deus [claude|codex] [home|auth|status|backend|logs|listen]"
        Write-Host ""
        Write-Host "  deus            Launch in current directory (external project mode if not ~\deus)"
        Write-Host "  deus codex      Launch with Codex (OpenAI) for this session"
        Write-Host "  deus home       Launch in home mode (~\deus) regardless of current directory"
        Write-Host "  deus auth       Rebuild dist/ and restart background service"
        Write-Host "  deus status     Show service status (NSSM or Servy)"
        Write-Host "  deus backend    Manage default AI backend and model (show|set|model|list)"
        Write-Host "  deus logs       Review system health logs (rotate|review|summary|pinned)"
        Write-Host "  deus listen     Record from mic, transcribe, and copy to clipboard"
    }
}
