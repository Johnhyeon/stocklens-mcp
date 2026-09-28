<div align="center">

<img src="https://raw.githubusercontent.com/Johnhyeon/stocklens-mcp/main/assets/logo.svg" width="120" height="120" alt="StockLens logo">

# StockLens

**Lets Claude and Codex look up stock data themselves before they answer**

[leetkey.kr/en](https://leetkey.kr/en/) | [한국어](https://github.com/Johnhyeon/stocklens-mcp/blob/main/README.md) | [Patch notes](https://github.com/Johnhyeon/stocklens-mcp/blob/main/PATCHNOTES.md)

</div>

---

Ask an AI about a stock and you get a plausible number. The trouble is you can't tell when that number is from or where it came from. Sometimes it is an old figure from training, stated as if it were today's.

With StockLens connected, the AI fetches the numbers on the spot from Naver Finance, Yahoo Finance and any brokerage account you connect. **Every figure carries the date it is from and its source.** No more screenshots of charts or copy-pasting numbers.

StockLens is the core Lens of [LeetKit](https://leetkey.kr/en/), used together with DartLens (Korean DART filings) and TelegramLens (Telegram stock chatter).

**Ask in English, get answers in English.** The installer and setup guide are in Korean; after that you just talk to your AI.

## A real answer

> Summarize investor flows for Samsung Electronics over the last 5 days

**Samsung Electronics: investor flows** `As of 2026-09-23 market close`

| Date | Institutions | Foreigners |
|---|---:|---:|
| Sep 23 | +1,346,883 sh | +4,513,767 sh |
| Sep 22 | +259,459 sh | +659,851 sh |

Note: since Sep 14, the closing price is the last KRX after-market trade at 20:00, not the regular-session close.

Asked right after a market holiday, it still said which trading day the numbers were from. It does not pass off an old number as today's. (Labels translated from the Korean output.)

## What you can ask

Plain words work, and you can keep narrowing down from the previous answer.

```
Among the top 300 by market cap, find stocks in a bullish moving-average alignment, above the 20-day line, with volume over 1.5x normal
Keep only the ones both foreigners and institutions bought over the last 5 days
Compare PER, ROE and debt ratio for what's left in one table and save it to Excel
```

```
Why did SK hynix rise today? Line up the articles and filings in time order
Summarize NVIDIA's last 4 quarters of earnings and analyst price targets
```

## What it covers

- **Korean stocks:** quotes, daily/weekly/monthly charts and indicators, investor flows, financial statements, consensus, broker reports, filings list, news, sectors and themes, ETFs
- **US stocks:** quotes, charts, financial statements, earnings, analyst views, insider trades, institutional holders, options, short interest
- **Chart screening:** write a condition in a sentence, such as "moving averages converging" or "golden cross with volume twice normal", and it scans the top KOSPI and KOSDAQ stocks by market cap at once. Definitions match Korean brokerage condition search (Kiwoom).
- **Brokerage connection (optional):** connect a Korea Investment & Securities or Kiwoom Securities Open API account for 1-minute bars and more detailed investor flows. Quotes only; no orders, no account access.
- **Excel export:** save any table as an Excel file.

About 70 tools in all. See the [tool guide](https://github.com/Johnhyeon/stocklens-mcp/blob/main/guides/en/TOOLS.md) and [usage examples](https://github.com/Johnhyeon/stocklens-mcp/blob/main/guides/en/USAGE.md).

## Labels come first

AI usually gets markets wrong not by bad math but by mislabeled numbers: yesterday's value called today's, an estimate stated as fact. StockLens fixes the labels first.

- **A timestamp on every figure.** "As of Sep 23 close", so you know when each number is from.
- **No mixing.** After-market prices and regular-session closes, consolidated and separate statements, are never put on one line.
- **Partial means partial.** If only part of the data was read, it says so. Missing values are never filled in as zero.
- **It says what it got.** Ask for 60 days and receive 20, and the answer says 20.

## Where it runs

| | |
|---|---|
| AI apps | Claude Desktop, Codex (ChatGPT account), Claude Code |
| OS | Windows, macOS |
| Not supported | Claude.ai on the web (it cannot connect to your PC) |

The AI app's own subscription is separate from LeetKit.

## Setup and pricing

- **Setup:** install with a button in LeetKit Manager and paste your key. No commands. Updates are the [Update] button on each Lens card.
- **Trial:** 14 days free with all three Lenses, email only, no card.
- **Pricing:** one-time payment, no subscription. StockLens alone (LeetKit STOCK) or all three Lenses (LeetKit FULL Package). Checkout is Korean; overseas cards may not work, so email us first.

Trial and prices: **[leetkey.kr/en](https://leetkey.kr/en/)**

## Not investment advice

StockLens is a data tool that lets your AI look up public market data. It is not an investment advisory, discretionary management or stock recommendation service. It does not recommend buying or selling any security and has no order execution. Data can be delayed or wrong depending on the source, and AI answers are for reference only. Investment decisions and their outcomes are your own responsibility.

## License

Proprietary software. The source code is not public, and a valid license key is required. See [LICENSE](https://github.com/Johnhyeon/stocklens-mcp/blob/main/LICENSE). This repository holds the overview, usage docs and patch notes only.

Contact: support@leetkey.kr · Made by Leetkey Lab (리트키랩)
