# StockLens Installation Guide

[🇰🇷 한국어](../ko/INSTALL.md) | [TOOLS](TOOLS.md) | [USAGE](USAGE.md)

---

## Distribution Status

Public installation instructions for StockLens ended on 2026-06-01.

New installations now follow the buyer guide and installer command provided after purchase.

## What the install script does

1. **Check / install uv** — runs the official [astral.sh](https://astral.sh/uv/) installer if missing (Python runtime included)
2. **Install StockLens MCP** — installs into an isolated environment to avoid polluting system Python
3. **Register Claude Desktop config** — adds an absolute-path entry to `claude_desktop_config.json` (no PATH dependency)
4. **Verify** — runs `stocklens-doctor`, exits non-zero on critical issues

---

## Restart Claude Desktop

**Not just closing the window — fully quit and relaunch.**

- **Windows**: System tray (bottom-right) → right-click Claude → **Quit**
- **macOS**: Menu bar → Claude → **Quit** (or `Cmd + Q`)
- **Linux**: Tray → Quit

Then launch Claude Desktop again.

---

## Verify it works

In Claude:
```
Show me Samsung Electronics (005930) current price
```

If you see the stock name, price, and volume, you're done.

<img width="850" height="415" alt="result" src="https://github.com/user-attachments/assets/ac50dd95-85b8-4471-a79c-6aa196f62af4" />

---

## Update

Updates follow the buyer guide.

---

## Diagnose

If install/config seems off:

```bash
stocklens-doctor
```

4-step health check (uv / package / command / config). Each item shows status + fix command.

---

## Troubleshooting

### `uv: command not found`

The uv installer adds `~/.local/bin` to PATH for **new shells**. To keep using the same shell:

**Windows PowerShell:**
```powershell
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
```

**macOS/Linux:**
```bash
export PATH="$HOME/.local/bin:$PATH"
```

---

### `stocklens-setup: command not found`

Same root cause — PATH not reloaded. Either add `~/.local/bin` to PATH (above) or call by absolute path:

**Windows:**
```powershell
& "$env:USERPROFILE\.local\bin\stocklens-setup.exe"
```

**macOS/Linux:**
```bash
~/.local/bin/stocklens-setup
```

---

### StockLens tools not visible in Claude Desktop

1. Confirm Claude Desktop is fully quit (tray → Quit)
2. Run `stocklens-doctor` to validate the config entry
3. Inspect the config file directly:

**Windows**: File Explorer address bar → `%APPDATA%\Claude`
**macOS**: Finder → `Cmd + Shift + G` → `~/Library/Application Support/Claude`

`claude_desktop_config.json` should contain:

```json
{
  "mcpServers": {
    "stocklens": {
      "command": "C:\\Users\\<you>\\.local\\bin\\stocklens.exe"
    }
  }
}
```

If missing or wrong path:
```bash
stocklens-setup
```

---

### Tools visible but error on invocation

Likely Naver Finance / Yahoo Finance connectivity issue:

1. Check https://finance.naver.com loads in browser
2. Check corporate/school firewall
3. Restart Claude Desktop

---

### Migrating from `naver-stock-mcp` / pip-installed stocklens

Clean up the old pip install first:

```bash
# Windows
py -m pip uninstall naver-stock-mcp stocklens-mcp -y

# macOS/Linux
python3 -m pip uninstall naver-stock-mcp stocklens-mcp -y
```

Then follow the buyer installation guide. `stocklens-setup` updates the existing Claude config entry to the new absolute path.

---

## Broker connection (optional, intraday bars)

Connect a broker Open API to unlock Korean and US intraday bars plus
detailed investor flow. **One broker is enough** - pick Korea Investment
& Securities (KIS) or Kiwoom Securities.

| | KIS | Kiwoom |
|---|---|---|
| KR / US intraday bars | yes | yes |
| Detailed flow investor kinds | 3 (retail, foreign, institution total) | 13 (institution broken out) |
| Buy / sell split in flow | yes | no (net only) |
| Lending, credit, foreign holding | no | yes |
| Where you can use it | no IP restriction; pick this for a laptop | only from the IP you registered |
| Key issuance | https://apiportal.koreainvestment.com | https://openapi.kiwoom.com |

Neither is a superset of the other. Choose Kiwoom to break institutions
apart, KIS to separate buys from sells.

1. Issue a quotation-scope App Key and App Secret at the portal above
2. Open LeetKit Manager and click the broker connection button on the
   StockLens card
3. Pick the broker tab, keep the default live (real) profile, paste the
   App Key and App Secret, then run the connection test and save
4. Done when Korean and US intraday availability is shown

Connecting both still serves data from **one primary broker only**. It
never switches to the other broker silently when one fails.

Notes:

- No account number or order password is ever requested. This is a
  quotation-only connection; trading features are not supported
- Keys are stored only in the OS credential store, never in files
- Two levels of disconnect: current-profile disconnect removes only the
  active environment's keys; full provider disconnect removes all keys and
  that broker's intraday cache. Neither touches the package, license, MCP
  registrations, or the Naver/Yahoo features
- Uninstalling the package alone keeps the broker profiles; reinstalling
  picks them up again. Use the Manager's full cleanup to wipe everything
- If anything misbehaves, switch the data source mode to legacy in
  Manager: zero broker calls, identical to pre-connection behavior

---

## Still stuck?

Use the support channel included in the buyer guide.

Please include:
- OS (Windows/macOS/Linux + version)
- Full `stocklens-doctor` output
- Which step failed
