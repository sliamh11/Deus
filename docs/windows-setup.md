# Windows Setup Guide

Deus runs on Windows via WSL2 + Docker Desktop. The host process is native Node.js on Windows; agent containers run inside the WSL2 Linux environment.

Estimated time: 15 minutes.

---

## 1. Prerequisites

### Node.js 20+

```powershell
winget install --id OpenJS.NodeJS.LTS -e
```

Verify: `node --version` (must be 20+).

### Docker Desktop (WSL2 backend)

Download from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop). Since Docker Desktop v4.19, WSL2 is installed automatically — no manual setup required.

After installation, open Docker Desktop and confirm the engine is running before continuing.

### Service manager (pick one)

**NSSM** (recommended):

```powershell
# via Chocolatey
choco install nssm

# via Scoop
scoop install nssm

# or download the binary directly from https://nssm.cc/download
```

**Servy-CLI** (alternative):

```powershell
winget install --id aelassas.Servy -e
```

Note: the command is `servy-cli`, not `servy`.

### Clone and install dependencies

```powershell
git clone https://github.com/your-org/deus.git
cd deus
npm install
```

---

## 2. Recommended Configuration

### .wslconfig

Create or edit `C:\Users\<YourUsername>\.wslconfig` to keep idle memory usage low:

```ini
[wsl2]
autoMemoryReclaim=gradual
```

Apply it:

```powershell
wsl --shutdown
```

Docker Desktop restarts WSL2 automatically on next use.

### Windows Defender exclusions

Defender scans the WSL2 virtual disk on every I/O, which causes noticeable slowdowns. Exclude the VHD path:

1. Open **Windows Security** > **Virus & threat protection** > **Manage settings** > **Add or remove exclusions**.
2. Add a **Folder** exclusion for:
   ```
   %LOCALAPPDATA%\Docker\wsl
   ```

Alternatively, from an elevated PowerShell session:

```powershell
Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\Docker\wsl"
```

### Keep state in Docker named volumes

Never bind-mount a Windows NTFS path (e.g. `C:\Users\...`) into a container. All persistent state — SQLite databases, session files — must live in Docker named volumes, which reside on the WSL2 ext4 VHD. Bind-mounting NTFS paths causes permission errors and corrupts SQLite WAL files.

---

## 3. Setup

Setup is identical to Linux:

```powershell
npm run setup
```

The setup wizard handles OAuth authentication (reads from `~/.claude/.credentials.json`), container image builds, and service registration. Follow the prompts.

---

## 4. Service Management

After setup, Deus is registered as a Windows service named `deus` that auto-starts on boot.

### With NSSM

```powershell
nssm start deus
nssm stop deus
nssm status deus
nssm restart deus
```

### With Servy-CLI

```powershell
servy-cli start deus
servy-cli stop deus
servy-cli status deus
```

### Logs

Logs are written to the project directory:

| File | Contents |
|------|----------|
| `logs/deus.log` | Standard output |
| `logs/deus.error.log` | Errors and crashes |

---

## 5. Troubleshooting

### Docker not starting

- Open Docker Desktop and check the status indicator. If it shows an error, restart it from the system tray.
- Run `wsl --status` in PowerShell. If WSL2 is not the default, run:
  ```powershell
  wsl --set-default-version 2
  ```
- If the Docker engine is stuck, run `wsl --shutdown`, then restart Docker Desktop.

### NSSM not found after install

Chocolatey and Scoop install to different locations. After installing NSSM, open a new terminal session so the updated `PATH` takes effect. To verify:

```powershell
where.exe nssm
```

If still not found, add the NSSM binary directory to your `PATH` manually, or use the full path to `nssm.exe`.

### I/O is slow inside containers

This is almost always Defender scanning the WSL2 VHD. Confirm the exclusion is active:

```powershell
Get-MpPreference | Select-Object -ExpandProperty ExclusionPath
```

The `%LOCALAPPDATA%\Docker\wsl` path should appear in the list. If not, re-add it (see section 2).

Also confirm you are not bind-mounting any NTFS paths — all volume mounts should be Docker named volumes.

### OAuth credentials not found

The setup wizard reads `~/.claude/.credentials.json`. On Windows, `~` resolves to `C:\Users\<YourUsername>`. If you authenticated on another machine, copy the credentials file to that location before running setup.
