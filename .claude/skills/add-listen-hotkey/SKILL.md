# /add-listen-hotkey

Install a global hotkey that triggers `deus listen` from anywhere on the OS.
Also installs all required dependencies (sox, whisper-cli) and downloads the whisper model.

## Step 1 — Install dependencies and whisper model

Run these checks and installs **before** asking the user about hotkey preferences.

### Dependencies

**macOS:**
```bash
# Check sox
command -v sox || brew install sox

# Check whisper-cli (from whisper-cpp)
command -v whisper-cli || brew install whisper-cpp
```

**Linux:**
```bash
# Check sox
command -v sox || sudo apt install -y sox libsox-fmt-all

# Check whisper-cli — not in apt, build from source if missing
if ! command -v whisper-cli; then
  echo "whisper-cli not found. Build from: https://github.com/ggerganov/whisper.cpp"
  echo "After building, ensure 'whisper-cli' is on your PATH."
  # Pause and ask user to confirm before continuing
fi
```

**Windows:**
```powershell
# Check sox
if (-not (Get-Command sox -ErrorAction SilentlyContinue)) {
  winget install sharkdp.bat  # or: choco install sox.portable
}

# Check whisper-cli
if (-not (Get-Command whisper-cli -ErrorAction SilentlyContinue)) {
  Write-Host "Download whisper-cli from: https://github.com/ggerganov/whisper.cpp/releases"
  Write-Host "Add it to your PATH, then re-run this skill."
  # Stop and wait for user
}
```

### Whisper model

Resolve the model path from `WHISPER_MODEL` env var, or default to
`~/deus/data/models/ggml-large-v3-turbo.bin` (Liam's personal config) or
`~/deus/data/models/ggml-base.bin` (public default).

If the model file doesn't exist, download it:
```bash
MODEL_PATH="${WHISPER_MODEL:-$HOME/deus/data/models/ggml-base.bin}"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"

if [ ! -f "$MODEL_PATH" ]; then
  mkdir -p "$(dirname "$MODEL_PATH")"
  echo "Downloading whisper model (148 MB)..."
  curl -L --progress-bar -o "$MODEL_PATH" "$MODEL_URL"
  echo "Model saved to: $MODEL_PATH"
fi
```

For `ggml-large-v3-turbo.bin` (higher accuracy, 1.5 GB):
```
https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

Ask the user which model they want if `WHISPER_MODEL` is not already set:
- `base` (148 MB, fast, good for English) — default
- `large-v3-turbo` (1.5 GB, best accuracy, recommended for Hebrew)

Set `WHISPER_MODEL` in `~/.zshrc` / `~/.bashrc` / Windows user environment variables
so `deus listen` always finds the model without specifying it each time.

### Verify end-to-end

Run a quick smoke test before installing the hotkey:
```bash
DEUS_LISTEN_NO_CLIPBOARD=1 node ~/deus/dist/deus-listen.js --help 2>/dev/null || \
  echo "Build needed — run: cd ~/deus && npm run build"
```

If the build is missing, run `npm run build` in `~/deus` before proceeding.

## Step 2 — Detect OS and ask for hotkey preferences

Detect the OS (`IS_MACOS` / `IS_LINUX` / `IS_WINDOWS` from platform.ts context, or run `uname -s`).

Ask the user:
1. **Hotkey** — default `Cmd+Option+V` (macOS) / `Super+Alt+V` (Linux) / `Ctrl+Alt+V` (Windows)
2. **Mode**:
   - `silent` (default) — runs `deus listen` in background, notification on completion
   - `terminal` — opens a new terminal window with the VU meter visible
3. **Stream mode?** — if yes, uses `deus listen --stream` instead of single-shot

## Step 3 — Install per OS

### macOS — Hammerspoon

Check if Hammerspoon is installed: `ls /Applications/Hammerspoon.app 2>/dev/null`
If missing: `brew install --cask hammerspoon` (ask user to approve).

Write `~/.hammerspoon/deus-listen.lua`:

```lua
-- deus listen hotkey (managed by Deus /add-listen-hotkey)
local mods = {"cmd", "alt"}
local key  = "v"

hs.hotkey.bind(mods, key, function()
  -- SILENT MODE: run headless, notify on completion
  local task = hs.task.new("/bin/zsh", function(code, stdout, stderr)
    local msg = code == 0 and "Copied to clipboard" or "Transcription failed"
    hs.notify.new({title = "Deus", informativeText = msg}):send()
  end, {"-lc", "deus listen --no-clipboard=false"})
  task:start()
  hs.notify.new({title = "Deus", informativeText = "Listening…"}):send()
end)
```

For **terminal mode** replace the task body with:
```lua
  hs.execute("open -a Ghostty --args -e 'deus listen'")
```
(or `iTerm2` / `Terminal.app` if Ghostty is absent — detect with `ls /Applications/Ghostty.app`).

Source the file from `~/.hammerspoon/init.lua`:
```lua
-- Auto-appended by Deus /add-listen-hotkey
require("deus-listen")
```

Reload Hammerspoon: `open -g hammerspoon://reloadConfig`

### Linux — sxhkd

Check if sxhkd is running: `pgrep sxhkd`
If missing: `sudo apt install sxhkd` (or pacman/dnf equivalent).

Append to `~/.config/sxhkd/sxhkdrc` (create if missing):
```
# deus listen (managed by Deus /add-listen-hotkey)
super + alt + v
    deus listen
```

For **stream mode**: replace `deus listen` with `deus listen --stream`.

Reload: `pkill -USR1 sxhkd`

For **terminal mode** (VU meter visible):
```
super + alt + v
    ghostty -e deus listen
```

### Windows — AutoHotkey v2

Check if AHK is installed: `Get-Command autohotkey.exe -ErrorAction SilentlyContinue`
If missing: `winget install AutoHotkey.AutoHotkey`

Write `%APPDATA%\deus\deus-listen.ahk`:
```ahk
; deus listen hotkey (managed by Deus /add-listen-hotkey)
^!v:: {  ; Ctrl+Alt+V
    Run "deus listen", , "Hide"
}
```

For **terminal mode**: `Run "wt.exe deus listen"` (Windows Terminal).

Add to startup: create a shortcut in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`.

Run immediately: `Start-Process autohotkey.exe "$env:APPDATA\deus\deus-listen.ahk"`

## Step 4 — Verify

Test the hotkey:
- macOS: trigger it, wait for "Listening…" notification, speak, check clipboard.
- Linux: trigger, speak, check `xclip -o -selection clipboard`.
- Windows: trigger, speak, check `Get-Clipboard`.

## Step 5 — Uninstall instructions (show to user)

- **macOS**: delete `~/.hammerspoon/deus-listen.lua`, remove the `require` line from `init.lua`, reload Hammerspoon.
- **Linux**: remove the appended block from `~/.config/sxhkd/sxhkdrc`, `pkill -USR1 sxhkd`.
- **Windows**: delete `%APPDATA%\deus\deus-listen.ahk` and the startup shortcut.
