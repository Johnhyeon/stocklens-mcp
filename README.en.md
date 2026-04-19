<div align="center">

<img src="assets/logo.svg" width="120" height="120" alt="StockLens logo">

# StockLens

**AI-powered Korean stock analysis with real data**

[![PyPI](https://img.shields.io/pypi/v/stocklens-mcp.svg)](https://pypi.org/project/stocklens-mcp/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

[🇰🇷 한국어](README.md) | 🇺🇸 **English**

</div>

---

## Why StockLens

When you show AI a chart image, it **guesses the numbers and often gets them wrong** (hallucination).

**StockLens** connects Claude directly to live data from Naver Finance (Korea's largest stock portal), so AI **reads real numbers instead of guessing**.

```
❌ "Samsung Electronics is around 80,000 KRW" (guess, wrong)
✅ "Samsung Electronics at 206,000 KRW, +5.3% vs 20-day MA" (real data)
```

## Features

- 📊 **19 tools** — Prices, charts, investor flows, financials, screening, Excel export
- 🔑 **No API key required** — Uses public Naver Finance data
- 🚀 **Fast responses** — TTL cache + Semaphore optimization
- 📁 **Excel snapshots** — Scan once, query instantly
- 🤖 **Gemini/GPT compatible** — Export to Excel for use with other AIs

## Quick Start (`.mcpb`, recommended)

Install as a Claude Desktop extension in a few clicks — **Python and dependencies bundled, no prior setup needed**.

<!-- TODO: 30-second install demo GIF — download → settings → install extension → approve -->
![Install demo](assets/setup.gif)

**Steps**
1. [Download Claude Desktop](https://claude.ai/download) → install → sign in
2. Top-left menu → **Settings → Developer → Install Extension**
3. Grab `stocklens-mcp-*.mcpb` from the [latest Releases page](https://github.com/Johnhyeon/stocklens-mcp/releases/latest)
4. Pick the `.mcpb` → **Approve all**

> 💡 **Response timing**
> - **Installation may take a few moments.**
> - **Permissions**: For smoother use, set the extension to **Allow all** after installing.
> - **First call after install**: may take 1~5 minutes — Claude Desktop auto-downloads Python + dependencies.
    No progress indicator, so if it looks stuck, **retry the same query once**.
> - **Subsequent calls**: 1~2 seconds for first request, instant on repeat (built-in cache)

> ⚠️ **Do not register pip + `.mcpb` simultaneously** — they conflict and can stall responses. Existing pip users, see below

---

### 🔄 Existing pip users

**Switch to `.mcpb` (recommended)**:
```bash
py -m pip uninstall stocklens-mcp
```
Then delete the `"stocklens"` entry in `%APPDATA%\Claude\claude_desktop_config.json` → follow the `.mcpb` flow above.

**Stay on pip (upgrade only)**:
```bash
py -m pip install --upgrade stocklens-mcp
```

> 📌 Full pip install / troubleshooting: [Install guide](guides/en/INSTALL.md)

## Verify Installation

In Claude:
```
Show me Samsung Electronics (005930) current price
```

If you see the stock name, price, and volume, you're all set.

<!-- TODO: screenshot — Claude response example -->
<img width="850" height="415" alt="image" src="https://github.com/user-attachments/assets/ac50dd95-85b8-4471-a79c-6aa196f62af4" />

<img width="797" height="948" alt="image" src="https://github.com/user-attachments/assets/1daa0535-4ab5-480c-b70f-dcfdb5c5c864" />

## Installation Diagnosis (pip users only)

With `.mcpb`, Claude Desktop handles everything automatically. On the pip path, if MCP doesn't appear:

```bash
stocklens-doctor
```

Auto-checks Python / package / command / config in 4 steps. Shows the exact fix command. Send this to anyone having install trouble.

## Example Queries

```
"Analyze SK Hynix 120-day candles using the 20-day MA trend"
"Check Kakao's foreign/institutional investor flow for the last 20 days"
"Find stocks in top-100 market cap with PER under 15"
"Show today's strongest 3 themes and analyze the leader of each"
```

> ✅ Only builds that pass full-tool QA and load tests ship to release. ([details](QUALITY.md))

## Learn More

- [📘 **All 19 Tools** →](guides/en/TOOLS.md)
- [💡 **50 Prompt Examples** →](guides/en/USAGE.md)
- [🔧 **Installation & Troubleshooting** →](guides/en/INSTALL.md)

## Supported Environments

| Environment | Support |
|-------------|---------|
| Claude Desktop (app) | ✅ Main target |
| Claude Code (CLI) | ✅ |
| Claude.ai (web) | ❌ Local MCP not supported |
| ChatGPT / Gemini | Via Excel export workaround |

## Important Note for International Users

StockLens is designed for **Korean stock market (KOSPI/KOSDAQ)** data from Naver Finance. Stock codes are 6-digit Korean tickers (e.g., `005930` for Samsung Electronics, `000660` for SK Hynix). US/global stock support is planned for a future version.

## Contributing

Issues and PRs are welcome. Please open an [Issue](https://github.com/Johnhyeon/stocklens-mcp/issues) for bugs or feature requests.

## License

MIT License
