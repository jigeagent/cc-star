# Windows Installation Guide

> **Filed under:** [CC-STAR-2026-06-07] Windows production environment pitfall log.
> **Author:** leopard@jige

cc-star works on Windows, but Claude Code has several Windows-specific quirks you need to know. This guide covers every pitfall we've encountered in production.

---

## Quick Diagnosis

Run this PowerShell script to check your installation:

```powershell
Write-Host "=== cc-star Quick Diagnosis ===" -ForegroundColor Cyan

# 1. settings.json path format (P0)
$settings = Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw
$backslash = [regex]::Matches($settings, '\\\\[^/]').Count
if ($backslash -gt 0) {
    Write-Host "[FAIL] settings.json has $backslash backslash paths — must use forward slashes" -ForegroundColor Red
} else {
    Write-Host "[PASS] settings.json path format OK" -ForegroundColor Green
}

# 2. Environment variables (P1)
$token = [Environment]::GetEnvironmentVariable("ANTHROPIC_AUTH_TOKEN", "User")
if ($token) {
    Write-Host "[PASS] ANTHROPIC_AUTH_TOKEN set ($($token.Substring(0,8))...)" -ForegroundColor Green
} else {
    Write-Host "[FAIL] ANTHROPIC_AUTH_TOKEN not set" -ForegroundColor Red
}

# 3. cc-star hooks directory
$hooksDir = "$env:USERPROFILE\.cc-star\hooks"
$requiredFiles = @("inject.py", "store.py", "summary.py", "compact.py", "session_start.py")
$missing = @()
foreach ($f in $requiredFiles) {
    if (-not (Test-Path "$hooksDir\$f")) { $missing += $f }
}
if ($missing.Count -eq 0) {
    Write-Host "[PASS] cc-star hooks all present" -ForegroundColor Green
} else {
    Write-Host "[FAIL] Missing hooks: $($missing -join ', ')" -ForegroundColor Red
}

# 4. Node.js spawn check (P1 — subagent ENOENT)
try {
    $nodeTest = node -e "require('child_process').spawn('claude',['--version'],{stdio:'pipe'}).on('error',()=>process.exit(1)).on('exit',()=>process.exit(0))" 2>&1
    Write-Host "[PASS] Node.js can spawn claude subprocess" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Node.js cannot spawn claude — subagent/Workflow will fail" -ForegroundColor Red
    Write-Host "       Fix: see 'P1 - claude not found by Node.js spawn' section" -ForegroundColor Yellow
}

# 5. OpenViking server (if used)
try {
    $ov = (Invoke-WebRequest http://localhost:1933/health -TimeoutSec 3).Content
    Write-Host "[PASS] OpenViking server running" -ForegroundColor Green
} catch {
    Write-Host "[INFO] OpenViking not running (port 1933) — skip if not using OV" -ForegroundColor Yellow
}

Write-Host "`nDiagnosis complete." -ForegroundColor Cyan
```

---

## 🔴 P0 — Hook path backslash escape (CRITICAL)

### Symptom

```
[python C:\Users\Administrator\.cc-star\hooks\inject.py]:
C:\Program Files\Python312\python.exe: can't open file
'C:\UsersAdministrator.cc-starhooksinject.py': [Errno 2] No such file or directory
```

The backslashes `\` are re-interpreted by the Claude Code hook execution layer:
- `\U` → Unicode escape → consumed
- `\A` → ASCII bell `\a` → consumed
- `\h` → escape sequence → consumed
- Result: `C:\Users\Administrator\.cc-star\hooks\inject.py` → `C:UsersAdministrator.cc-starhooksinject.py`

### Fix

**All hook command paths in `settings.json` MUST use forward slashes:**

```json
// ❌ WRONG — backslashes will be re-escaped
{ "command": "python C:\\Users\\Administrator\\.cc-star\\hooks\\inject.py" }

// ✅ CORRECT — forward slashes work on both Windows and Claude Code
{ "command": "python C:/Users/Administrator/.cc-star/hooks/inject.py" }
```

### Root cause

`cc-star init` used `str(Path(...))` which produces backslashes on Windows.
**Fixed in v0.2.4+** — `installer.py` now uses `Path.as_posix()` to always emit forward slashes.

If you're on an older version, run this PowerShell fix:

```powershell
$settingsPath = "$env:USERPROFILE\.claude\settings.json"
Copy-Item $settingsPath "$settingsPath.bak" -Force  # backup
$raw = Get-Content $settingsPath -Raw
$fixed = $raw -replace '\\\\', '/'  # replace \\ with /
$fixed | Set-Content $settingsPath -NoNewline -Encoding UTF8
```

### Rule

> **No backslashes `\` in `settings.json` → `hooks` → `command` paths. Ever.**
> Affects: SessionStart / UserPromptSubmit / Stop / SessionEnd / PreCompact / PostCompact

---

## 🟡 P1 — `settings.json` `env` not loaded in `--print` mode

### Symptom

```
Not logged in · Please run /login
```
But `claude auth status` shows `loggedIn: true` and `ANTHROPIC_AUTH_TOKEN` is set in `settings.json` → `env`.

### Root cause

Claude Code 2.1.x Windows bug — `settings.json` `env` is **not loaded** when Claude Code starts in `--print` mode (used by some automation/integration scenarios). Interactive REPL mode is unaffected.

### Fix

Write API keys to **system/user environment variables** instead of `settings.json` `env`:

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_AUTH_TOKEN", "sk-...", "Machine")
[Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", "https://api.xxx.com/anthropic", "Machine")
[Environment]::SetEnvironmentVariable("ANTHROPIC_MODEL", "deepseek-v4-flash", "Machine")
```

**You must open a new terminal window** for the changes to take effect.

### Fallback: launch script

Create a batch file that sets env vars explicitly:

```bat
@echo off
set ANTHROPIC_AUTH_TOKEN=sk-...
set ANTHROPIC_BASE_URL=https://api.xxx.com/anthropic
set ANTHROPIC_MODEL=deepseek-v4-flash
cd /d D:\WorkBuddy
claude %*
```

---

## 🟡 P1 — `mcp.json` commands must be real

### Symptom

```
1 MCP server(s) not connected — run /mcp to authenticate, retry, or see details:
openviking: failed — MCP error -32000: Connection closed
```

### Fix

**Before adding a server to `mcp.json`, verify the command works standalone:**

```powershell
# Verify the Python script exists
python C:/path/to/mcp-server.py --help

# Or verify the Python module is installed
python -c "import mcp_module; print('OK')"
```

Don't leave dead MCP entries in config — Claude Code tries to connect on every startup, slowing down initialization.

---

## 🟡 P1 — OpenViking server must be started first

### Symptom

MCP connection fails or `curl http://localhost:1933/health` returns connection refused.

### Fix

```powershell
Start-Process -FilePath "openviking-server.exe" `
    -ArgumentList "--host", "127.0.0.1", "--port", "1933", `
                  "--config", "$env:USERPROFILE\.openviking\ov.conf" `
    -WorkingDirectory "D:\OVData" -WindowStyle Hidden

# Verify
curl http://localhost:1933/health
# Expected: {"status":"ok","healthy":true}
```

### Auto-start on boot

Configure a Windows scheduled task `OpenViking-AutoStart` to launch the server at startup.

---

## 🟡 P1 — `claude` not found by Node.js `spawn()` (subagent ENOENT)

### Symptom

```
Error: spawn claude ENOENT
```
or
```
agent failed: failed to spawn claude: spawn claude ENOENT
```

`which claude` works in bash, but Claude Code multi-agent features fail.

### Root cause

Node.js on Windows uses the **native Windows PATH** (not the Git Bash PATH) when spawning subprocesses without `shell: true`. If `claude` resolves to a `.cmd` shim (which can't be spawned directly) or the `claude.exe` directory isn't in the Windows environment PATH, you get ENOENT.

### Fix

**Step 1** — Ensure `claude.exe` is findable via Windows native PATH:

```powershell
# Option A: Add npm global bin dir to User PATH (permanent, needs new terminal)
[Environment]::SetEnvironmentVariable(
  'PATH',
  [Environment]::GetEnvironmentVariable('PATH', 'User') +
    ';D:\npm-global\node_modules\@anthropic-ai\claude-code\bin',
  'User'
)

# Option B: Create a symbolic link in a directory already in Windows PATH
# (works immediately, survives npm updates)
cd "C:\Program Files\Python312\Scripts"
New-Item -ItemType SymbolicLink -Name claude.exe `
  -Target "D:\npm-global\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
```

**Step 2** — Verify:

```powershell
# From a NEW terminal:
node -e "require('child_process').spawn('claude', ['--version'], { stdio: 'pipe' })"
# Should output: 2.1.x (Claude Code)
```

### Why symbolic link?

- Survives `npm update -g @anthropic-ai/claude-code`
- No stale copies
- Works immediately without terminal restart (if using an already-in-PATH directory)

### Symptom

```
claude doctor:
  ✗ claude.ai OAuth token present
  ✗ claude.ai subscriber auth active
  ✗ OAuth token has user:profile scope
  ...
```

### Explanation

**This is normal when using API Key mode.** Remote Control checks are for claude.ai OAuth subscription authentication. They are irrelevant to API Key mode. As long as you see:

```
✓ First-party provider (api.anthropic.com)
```

Your authentication is fine.

---

## Installation Checklist

Use this for fresh installs or after reinstalling Windows:

- [ ] `cc-star init` completed without errors
- [ ] `settings.json` hooks → command paths all use `/` (forward slashes)
- [ ] API keys set in system/user environment variables (not just `settings.json` `env`)
- [ ] Opened a **new terminal** and verified `claude` starts without "Not logged in"
- [ ] `claude doctor` shows ✓ for First-party provider
- [ ] **Node.js `spawn('claude')` works** (needed for multi-agent/Workflow):
  ```powershell
  node -e "require('child_process').spawn('claude', ['--version'], {stdio:'pipe'}).on('error',e=>console.log('FAIL:',e.message)).on('exit',c=>console.log('OK'))"
  ```
  Should print `OK`, not `FAIL`.
- [ ] cc-star hook files exist: `~/.cc-star/hooks/{inject,store,summary,compact,session_start}.py`
- [ ] `cc-star status` reports healthy
- [ ] (Optional) OpenViking server running: `curl http://localhost:1933/health` → 200
- [ ] (If using MCP) Each `mcp.json` entry verified — command exists and runs

---

## Version History

| Version | Status | Windows notes |
|---------|--------|---------------|
| v0.2.3 | ⚠️ Backslash bug | `installer.py` uses `str(Path)` — produces `\\` paths |
| v0.2.4+ | ✅ Fixed | `installer.py` uses `Path.as_posix()` — produces `/` paths |
