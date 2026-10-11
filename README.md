# RJF Peer Comparison Dashboard

A dashboard that compares Raymond James Financial (RJF) with five wealth management and brokerage peers: Stifel, LPL Financial, Ameriprise, Morgan Stanley, and Charles Schwab. It refreshes itself every weekday.

**Live dashboard:** https://colepartain.github.io/rjf-peer-dashboard/

A Python script pulls each company's reported financials straight from the SEC, computes trailing twelve month ratios, adds current share prices, and writes a single HTML page. A scheduled GitHub Action runs the script after the market closes each weekday and publishes the result with GitHub Pages.

This project grew out of my Raymond James valuation model in Excel, where peer multiples were typed in by hand and went stale within weeks. This dashboard keeps them current automatically.

## What it shows

- Headline tiles for RJF's return on equity, return on tangible equity, pre-tax margin, and P/E, each ranked against the peers
- A bar chart that compares all six companies on any metric, with the peer median marked
- An eight-quarter trend of ROE, ROTCE, and pre-tax margin
- A sortable table of every metric, where hovering a value shows the period and the XBRL tag it came from
- Links to the SEC filing behind each company's numbers

## How it works

1. **Pull filings.** The script calls the SEC's free XBRL company facts API (`data.sec.gov/api/xbrl/companyfacts/`), which returns every value a company has tagged in its 10-K and 10-Q filings.
2. **Pick the right tag.** Companies do not all use the same XBRL tags, and some switch tags over time. For example, Raymond James no longer tags pre-tax income, so the script rebuilds it as net income plus income tax expense. Each concept has a list of candidate tags in `dashboard.py`, and the script records which one it used.
3. **Build trailing twelve month figures.** 10-Qs report year-to-date numbers, so TTM = last full fiscal year + this year to date - last year to date. This also handles Raymond James's September fiscal year without special cases.
4. **Compute ratios** and the peer median (which excludes RJF).
5. **Add prices** from Yahoo Finance, with Stooq as a backup. If both fail, the price-based metrics show n/a and everything else still builds.
6. **Write `docs/index.html`**, a self-contained page with the data embedded, plus `docs/data.json` with the raw results.

## Metric definitions

| Metric | Definition |
|---|---|
| Return on equity | TTM net income to common / average common equity (now and one year ago) |
| Return on tangible equity | TTM net income to common / average (common equity - goodwill - intangibles) |
| Pre-tax margin | TTM pre-tax income / TTM net revenue |
| Compensation ratio | TTM compensation and benefits / TTM net revenue |
| Net revenue growth | TTM net revenue vs. TTM net revenue one year earlier |
| Book value per share | Common equity / shares outstanding |
| Tangible book per share | (Common equity - goodwill - intangibles) / shares outstanding |
| Equity to assets | Total equity / total assets |
| P/E | Share price / TTM GAAP diluted EPS |
| Price to tangible book | Share price / tangible book per share |
| Dividend yield | TTM dividends declared per share / share price |

Common equity is total stockholders' equity less preferred stock.

## Setup

1. Create a new GitHub repository and upload these files.
2. Add a repository secret named `SEC_USER_AGENT` with your name and email, for example `Jane Doe jane@example.com`. The SEC requires automated requests to identify themselves this way. (Settings, Secrets and variables, Actions, New repository secret.)
3. Turn on GitHub Pages: Settings, Pages, Source "Deploy from a branch", branch `main`, folder `/docs`.
4. Open the Actions tab and run "Update dashboard" once by hand. After that it runs on its own every weekday at 6:30pm Eastern.

## Run it locally

```bash
pip install -r requirements.txt
export SEC_USER_AGENT="Your Name your.email@example.com"   # Windows PowerShell: $env:SEC_USER_AGENT="..."
python dashboard.py
```

Then open `docs/index.html` in a browser. Useful options:

- `--offline` rebuilds from the SEC files cached in `data/raw/` without downloading again
- `--no-prices` skips the share price lookup
- `python -m pytest tests` runs the unit tests, which use small made-up filings and need no internet

## Adding or changing peers

Edit `peers.json`. Each company needs a ticker, name, and SEC CIK number (search the company on [EDGAR](https://www.sec.gov/edgar/search/) to find it). Optional fields:

- `skip`: metrics to leave blank because they are not comparable for that company
- `overrides`: hand-entered values for items the company does not tag, with a note explaining the source
- `notes`: caveats shown at the bottom of the dashboard

## Known limitations

- **Morgan Stanley preferred stock** is not tagged separately in its recent filings, so `peers.json` holds a manual value ($9.75bn). It needs updating if Morgan Stanley issues or redeems preferred stock.
- **LPL** reports revenue before advisor payouts, so its compensation ratio and pre-tax margin are left blank. Its goodwill exceeds its equity, so tangible equity is negative and ROTCE is blank.
- **Ameriprise** has a very high ROE because unrealized losses on its insurance investments reduce its equity. It is hidden from the trend chart by default so the other lines are readable, and can be shown with one click.
- **Tangible equity** here subtracts goodwill and intangibles without adding back the related deferred tax liabilities, so it runs slightly below company-reported figures.
- **P/E uses GAAP EPS**, which can differ from the adjusted EPS companies highlight in earnings releases.

## Project structure

```
dashboard.py                        fetch, compute, and build
template.html                       dashboard page (data is injected at build time)
peers.json                          company list, overrides, and notes
tests/test_dashboard.py             unit tests for the TTM and equity logic
.github/workflows/update-dashboard.yml   weekday schedule and publishing
docs/                               generated dashboard served by GitHub Pages
```

Data: SEC EDGAR XBRL API and Yahoo Finance. For research and education, not investment advice.
