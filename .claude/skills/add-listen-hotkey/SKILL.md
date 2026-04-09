# /add-listen-hotkey

Install a global hotkey that triggers `deus listen` from anywhere on the OS.

## Prerequisites

- `deus listen` must work from terminal (`npm run build` must have been run).
- `deus` must be on `$PATH` (run `deus auth` once to ensure the symlink is in place).

## Step 1 — Detect OS and ask for preferences

Detect the OS (`IS_MACOS` / `IS_LINUX` / `IS_WINDOWS` from platform.ts context, or run `uname -s`).

Ask the user:
1. **Hotkey** — default `Cmd+Option+V` (macOS) / `Super+Alt+V` (Linux) / `Ctrl+Alt+V` (Windows)
2. **Mode**:
   - `silent` (default) — runs `deus listen` in background, notification on completion
   - `terminal` — opens a new terminal window with the VU meter visible
3. **Stream mode?** — if yes, uses `deus listen --stream` instead of single-shot

## Step 2 — Install per OS

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

## Step 3 — Verify

Test the hotkey:
- macOS: trigger it, wait for "Listening…" notification, speak, check clipboard.
- Linux: trigger, speak, check `xclip -o -selection clipboard`.
- Windows: trigger, speak, check `Get-Clipboard`.

## Step 4 — Uninstall instructions (show to user)

- **macOS**: delete `~/.hammerspoon/deus-listen.lua`, remove the `require` line from `init.lua`, reload Hammerspoon.
- **Linux**: remove the appended block from `~/.config/sxhkd/sxhkdrc`, `pkill -USR1 sxhkd`.
- **Windows**: delete `%APPDATA%\deus\deus-listen.ahk` and the startup shortcut.
