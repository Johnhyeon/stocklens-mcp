<div align="center">

<img src="assets/logo.svg" width="120" height="120" alt="StockLens logo">

# StockLens

**AI-powered Korean stock analysis with real data**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

[🇰🇷 한국어](README.md) | 🇺🇸 **English**

</div>

---

## Distribution Status

Public installation instructions for StockLens ended on 2026-06-01.

New installations now follow the buyer guide and installer command provided after purchase. Existing users who already installed the public build may continue using the version they have, while new distribution, setup support, and usage templates are organized under the paid package.

## Why StockLens

When you show AI a chart image, it **guesses the numbers and often gets them wrong** (hallucination).

**StockLens** connects Claude directly to live data from Naver Finance (Korea's largest stock portal), so AI **reads real numbers instead of guessing**.

```
❌ "Samsung Electronics is around 80,000 KRW" (guess, wrong)
✅ "Samsung Electronics at 206,000 KRW, +5.3% vs 20-day MA" (real data)
```

## Features

- 📊 **48 tools** — Market calendar, prices, charts, investor flows, financials, screening, Excel export
- 🔑 **No API key required** — Uses public Naver Finance data
- 🚀 **Fast responses** — TTL cache + Semaphore optimization
- 📁 **Excel snapshots** — Scan once, query instantly
- 🤖 **Gemini/GPT compatible** — Export to Excel for use with other AIs
- 🕐 **Result metadata v3** — every response carries the requested range vs. what
  actually came back, whether the last bar is still forming, whether prices are
  split-adjusted, and whether financial periods are mixed. Ask for 60 days, get 20,
  and it says so. All v3 fields are **optional**, so existing consumers can ignore
  them ([TOOLS.md](guides/en/TOOLS.md))

## Installation

The buyer guide covers:

1. Checking or installing `uv`
2. Installing the StockLens MCP package
3. Registering it with Claude Desktop or Claude Code
4. Running diagnostics and the first verification query

Direct public installer commands are no longer published in this README.

## Verify Installation

In Claude:
```
Show me Samsung Electronics (005930) current price
```

If you see the stock name, price, and volume, you're all set.

<!-- TODO: screenshot — Claude response example -->
<img width="850" height="415" alt="image" src="https://github.com/user-attachments/assets/ac50dd95-85b8-4471-a79c-6aa196f62af4" />

<img width="797" height="948" alt="image" src="https://github.com/user-attachments/assets/1daa0535-4ab5-480c-b70f-dcfdb5c5c864" />

## Installation Diagnosis

```bash
stocklens-doctor
```

Auto-checks uv / package / command / config in 4 steps. Shows the exact fix command. Send this to anyone having install trouble.

## Example Queries

```
"Analyze SK Hynix 120-day candles using the 20-day MA trend"
"Check Kakao's foreign/institutional investor flow for the last 20 days"
"Find stocks in top-100 market cap with PER under 15"
"Show today's strongest 3 themes and analyze the leader of each"
```

> ✅ Only builds that pass full-tool QA and load tests ship to release. ([details](QUALITY.md))

## Learn More

- [📘 **All 48 Tools** →](guides/en/TOOLS.md)
- [💡 **50 Prompt Examples** →](guides/en/USAGE.md)

## Supported Environments

| Environment | Support |
|-------------|---------|
| Claude Desktop (app) | ✅ Main target |
| Claude Code (CLI) | ✅ |
| Claude.ai (web) | ❌ Local MCP not supported |
| ChatGPT / Gemini | Via Excel export workaround |

## Market Coverage

- **Korean market (KOSPI/KOSDAQ)** via Naver Finance — 6-digit tickers (`005930` = Samsung Electronics, `000660` = SK Hynix)
- **US market (NYSE/NASDAQ)** via Yahoo Finance — alphabet tickers (`AAPL`, `TSLA`, `BRK.B`)

Tickers are auto-detected; mix freely in natural language (e.g., "compare 005930 and AAPL"). Full tool list in [TOOLS.md](guides/en/TOOLS.md).

## Operating Principle

StockLens does not provide investment recommendations, buy/sell signals, automated trading, or return guarantees. It is a data connection tool that helps Claude read public market data.

## License

MIT License
