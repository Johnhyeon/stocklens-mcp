# StockLens Installation Guide

Detailed installation for both developers and non-developers.

[🇰🇷 한국어](../ko/INSTALL.md) | [TOOLS](TOOLS.md) | [USAGE](USAGE.md)

---

## Prerequisites

1. **Python 3.11+**
2. **Claude Desktop app**
3. **Internet connection**

No API keys or brokerage accounts required.

---

## Step 1. Install Python

### Windows

1. Visit https://www.python.org/downloads/
2. Click the yellow **"Download Python 3.x.x"** button
3. Run the downloaded installer (`python-3.12.x-amd64.exe`)

<img width="653" height="401" alt="python_path" src="https://github.com/user-attachments/assets/acdbe3a9-82cb-484f-b4cf-5fda4a6829c9" />

**⚠️ IMPORTANT: Check the checkbox at the bottom of the installer window**

```
☑ Add python.exe to PATH  ← Check this!
```

This checkbox is at the **very bottom** of the first installer screen,
right above the "Install Now" button.
Without checking, the `python` command won't work in terminal.

4. Click **Install Now**
5. Wait for installation, then **Close**

### Verify Installation

Open **PowerShell** or **Command Prompt (cmd)**:

<img width="394" height="202" alt="image" src="https://github.com/user-attachments/assets/8a9a020c-8fcf-4fd2-9ad1-5413df43f311" />

```powershell
py --version
```
<img width="526" height="279" alt="image" src="https://github.com/user-attachments/assets/4e9f01bd-1146-4b66-bdce-b5e06d3952aa" />

You should see `Python 3.12.x` or similar.

If you get an error → **Restart your computer and try again**.

### macOS

Terminal:
```bash
brew install python@3.12
```

(If Homebrew is not installed: https://brew.sh/)

### Linux

```bash
sudo apt update
sudo apt install python3 python3-pip  # Ubuntu/Debian
```

---

## Step 2. Install Claude Desktop

<img width="458" height="644" alt="image" src="https://github.com/user-attachments/assets/5cb8847a-b1bf-4125-a2f0-d7e763234efe" />


Download from https://claude.ai/download — select your OS, install, sign in.

---

## Step 3. Install StockLens (3 commands)

No file download needed. Paste into terminal. Works on Windows/macOS/Linux.

**Windows** — PowerShell or Command Prompt (cmd):
```powershell
py -m pip install stocklens-mcp
py -m stock_mcp_server.setup_claude
py -m stock_mcp_server.doctor
```

**macOS/Linux** — Terminal:
```bash
python3 -m pip install stocklens-mcp
python3 -m stock_mcp_server.setup_claude
python3 -m stock_mcp_server.doctor
```

### What each line does
1. **Install** — Install stocklens-mcp package from PyPI
2. **Configure** — Add stocklens entry to Claude Desktop config (absolute path, auto-detects Microsoft Store version)
3. **Verify** — 4-step health check (Python / package / command / config). Shows fix commands if anything fails.

### If `py` / `python3` is not recognized
Python isn't in PATH or not installed. Go back to Step 1 and install Python 3.11+ with "Add Python to PATH" checked.

### Multiple Python versions installed
`py` (Windows Launcher) or `python3` (mac/Linux) automatically picks the latest version. Using `py -m pip install` is safer than plain `pip install` which may pick an older Python.

---

## Step 4. Restart Claude Desktop

**Important**: Not just closing the window — **fully quit the app**.

- **Windows**: Right-click Claude icon in system tray (bottom-right) → **Quit**
- **macOS**: Menu bar → Claude → **Quit** or `Cmd + Q`
- **Linux**: Tray → Quit

Then launch Claude Desktop again.

---

## Step 5. Verify It Works

<img width="850" height="415" alt="image" src="https://github.com/user-attachments/assets/ac50dd95-85b8-4471-a79c-6aa196f62af4" />


In Claude:
```
Show me Samsung Electronics (005930) current price
```

<img width="797" height="948" alt="image" src="https://github.com/user-attachments/assets/1daa0535-4ab5-480c-b70f-dcfdb5c5c864" />


Expected response:
```
Stock: Samsung Electronics (005930)
Price: 206,000 KRW
Change: +2,000 KRW
Volume: 18,229,163
...
```

---

## Troubleshooting

### "python is not recognized as an internal command"

Python PATH is not set. Fix:

1. Run Python installer (`python-*.exe`) again
2. Click **"Modify"**
3. Check **"Add Python to environment variables"**
4. Next → Install
5. Restart computer

---

### SSL errors during `pip install`

```bash
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org stocklens-mcp
```

---

### StockLens tools not visible in Claude Desktop

1. Make sure Claude Desktop is fully quit (tray → Quit)
2. Verify `stocklens-setup` completed successfully
3. Check config file:

**Windows**: File Explorer address bar → `%APPDATA%\Claude`

**macOS**: Finder → `Cmd + Shift + G` → `~/Library/Application Support/Claude`

Open `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "stocklens": {
      "command": "stocklens"
    }
  }
}
```

If missing, run:
```bash
stocklens-setup
```

---

### "command not found: stocklens"

Python Scripts folder not in PATH. Workaround:

**Windows PowerShell**:
```powershell
python -m stock_mcp_server.setup_claude stocklens
```

Then edit `claude_desktop_config.json` to use:
```json
"command": "python",
"args": ["-m", "stock_mcp_server.server"]
```

---

### Tools visible but error on invocation

Likely Naver Finance connectivity issue:

1. Verify https://finance.naver.com loads in browser
2. Check if corporate/school firewall is blocking
3. Restart Claude Desktop

---

### Update

```bash
pip install --upgrade stocklens-mcp
```

Or run `update.bat` / `update.sh`.

---

### Migrating from `naver-stock-mcp`

The package was renamed from `naver-stock-mcp` to `stocklens-mcp`.

```bash
# Uninstall old
pip uninstall naver-stock-mcp

# Install new
pip install stocklens-mcp
stocklens-setup
```

Then restart Claude Desktop.

---

## Still Having Issues?

Open an issue:
https://github.com/Johnhyeon/stocklens-mcp/issues

Please include:
- OS (Windows/macOS/Linux + version)
- Python version (`python --version`)
- Full error message (screenshot or text)
- Which step failed
