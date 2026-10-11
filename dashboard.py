"""
Raymond James peer comparison dashboard.

Pulls each company's reported financials from the SEC's XBRL "company facts"
API, computes trailing twelve month (TTM) profitability and balance sheet
ratios, adds a current share price, and writes a self-contained HTML
dashboard to docs/index.html.

Usage:
    export SEC_USER_AGENT="Your Name your.email@example.com"
    python dashboard.py                 # fetch fresh data and build
    python dashboard.py --offline       # rebuild from cached SEC files
    python dashboard.py --no-prices     # skip the share price lookup

The SEC asks every automated request to identify itself with a name and
email address in the User-Agent header. See https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn_plain}/{accn}-index.htm"
REPORT_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}
TREND_QUARTERS = 8

# XBRL tags that can represent each concept. Companies do not all use the same
# tag, and some switch tags over time. For each concept the script tries the
# candidates in order and uses the first one that has data for the period.
TAGS = {
    "net_revenue": ["RevenuesNetOfInterestExpense", "Revenues"],
    "net_income_common": ["NetIncomeLossAvailableToCommonStockholdersBasic", "NetIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "income_tax": ["IncomeTaxExpenseBenefit"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "compensation": ["LaborAndRelatedExpense", "EmployeeBenefitsAndShareBasedCompensation"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "dps_declared": ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"],
    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "preferred": ["PreferredStockValue", "PreferredStockValueOutstanding"],
    "goodwill_and_intangibles": ["IntangibleAssetsNetIncludingGoodwill", "GoodwillAndIntangibleAssetsNet"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill"],
    "intangibles_finite": ["FiniteLivedIntangibleAssetsNet"],
    "intangibles_indefinite": ["IndefiniteLivedIntangibleAssetsExcludingGoodwill"],
    "assets": ["Assets"],
    "shares_outstanding": ["dei:EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"],
    "shares_basic_weighted": ["WeightedAverageNumberOfSharesOutstandingBasic"],
}

# Metrics shown on the dashboard. "fmt" tells the page how to display them.
METRICS = [
    {"key": "roe", "label": "Return on equity", "fmt": "pct", "group": "Profitability"},
    {"key": "rotce", "label": "Return on tangible equity", "fmt": "pct", "group": "Profitability"},
    {"key": "pretax_margin", "label": "Pre-tax margin", "fmt": "pct", "group": "Profitability"},
    {"key": "comp_ratio", "label": "Compensation ratio", "fmt": "pct", "group": "Profitability"},
    {"key": "revenue_growth", "label": "Net revenue growth", "fmt": "pct", "group": "Growth"},
    {"key": "net_revenue", "label": "Net revenue ($bn)", "fmt": "bn", "group": "Growth"},
    {"key": "net_income_common", "label": "Net income to common ($bn)", "fmt": "bn", "group": "Growth"},
    {"key": "eps_diluted", "label": "Diluted EPS ($)", "fmt": "usd", "group": "Per share"},
    {"key": "bvps", "label": "Book value per share ($)", "fmt": "usd", "group": "Per share"},
    {"key": "tbvps", "label": "Tangible book per share ($)", "fmt": "usd", "group": "Per share"},
    {"key": "equity_to_assets", "label": "Equity to assets", "fmt": "pct", "group": "Balance sheet"},
    {"key": "pe", "label": "P/E (TTM)", "fmt": "x", "group": "Valuation"},
    {"key": "pb", "label": "Price to book", "fmt": "x", "group": "Valuation"},
    {"key": "ptbv", "label": "Price to tangible book", "fmt": "x", "group": "Valuation"},
    {"key": "div_yield", "label": "Dividend yield", "fmt": "pct", "group": "Valuation"},
    {"key": "market_cap", "label": "Market cap ($bn)", "fmt": "bn", "group": "Valuation"},
]
TREND_METRICS = ["roe", "rotce", "pretax_margin"]


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def sec_headers() -> dict:
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in agent:
        sys.exit(
            "Set the SEC_USER_AGENT environment variable to your name and email, "
            'for example: export SEC_USER_AGENT="Jane Doe jane@example.com"'
        )
    return {"User-Agent": agent, "Accept-Encoding": "gzip, deflate"}


def load_company_facts(cik: int, cache_dir: Path, offline: bool) -> dict:
    cache_file = cache_dir / f"CIK{cik:010d}.json"
    if offline:
        if not cache_file.exists():
            sys.exit(f"--offline was set but {cache_file} does not exist. Run once without --offline.")
        return json.loads(cache_file.read_text())
    resp = requests.get(SEC_FACTS_URL.format(cik=cik), headers=sec_headers(), timeout=60)
    resp.raise_for_status()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(resp.text)
    time.sleep(0.2)  # the SEC allows 10 requests per second; stay well under it
    return resp.json()


def fetch_price(ticker: str) -> dict | None:
    """Latest share price from Yahoo's public chart endpoint, with Stooq as a backup."""
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"range": "5d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        resp.raise_for_status()
        meta = resp.json()["chart"]["result"][0]["meta"]
        when = dt.datetime.fromtimestamp(meta["regularMarketTime"], dt.timezone.utc)
        return {"price": float(meta["regularMarketPrice"]), "as_of": when.date().isoformat(), "source": "Yahoo Finance"}
    except Exception as exc:  # noqa: BLE001 - any failure falls through to the backup
        print(f"  Yahoo price failed for {ticker}: {exc}")
    try:
        resp = requests.get(
            "https://stooq.com/q/l/",
            params={"s": f"{ticker.lower()}.us", "f": "sd2c", "h": "", "e": "csv"},
            timeout=20,
        )
        resp.raise_for_status()
        symbol, date_str, close = resp.text.strip().splitlines()[1].split(",")
        return {"price": float(close), "as_of": date_str, "source": "Stooq"}
    except Exception as exc:  # noqa: BLE001
        print(f"  Stooq price failed for {ticker}: {exc}")
    return None


# --------------------------------------------------------------------------
# Parsing XBRL facts
# --------------------------------------------------------------------------

def parse_date(s: str | None) -> dt.date | None:
    return dt.date.fromisoformat(s) if s else None


def get_facts(company_facts: dict, tag: str) -> list[dict]:
    """All 10-K and 10-Q values for one tag, one value per period (latest filing wins)."""
    namespace, _, name = tag.rpartition(":")
    node = company_facts.get("facts", {}).get(namespace or "us-gaap", {}).get(name)
    if not node:
        return []
    units = node["units"]
    unit = next((u for u in ("USD", "USD/shares", "shares") if u in units), next(iter(units)))
    by_period: dict[tuple, dict] = {}
    for f in units[unit]:
        if f.get("form") not in REPORT_FORMS:
            continue
        fact = {
            "start": parse_date(f.get("start")),
            "end": parse_date(f["end"]),
            "val": float(f["val"]),
            "filed": f.get("filed", ""),
            "accn": f.get("accn", ""),
            "form": f.get("form"),
            "tag": tag,
        }
        key = (fact["start"], fact["end"])
        if key not in by_period or fact["filed"] > by_period[key]["filed"]:
            by_period[key] = fact
    return list(by_period.values())


def duration_days(f: dict) -> int:
    return (f["end"] - f["start"]).days


def near(a: dt.date, b: dt.date, tol: int = 7) -> bool:
    return abs((a - b).days) <= tol


def ttm_value(facts: list[dict], end: dt.date) -> float | None:
    """
    Trailing twelve months ending on `end`.

    Companies report year-to-date figures in 10-Qs and full-year figures in
    10-Ks, so a TTM figure is: last full year + this year to date - last year
    to date. If a full-year value already ends on `end`, use it directly.
    """
    durations = [f for f in facts if f["start"] is not None]
    at_end = [f for f in durations if f["end"] == end]
    full_year = [f for f in at_end if 350 <= duration_days(f) <= 380]
    if full_year:
        return full_year[0]["val"]
    ytd_options = [f for f in at_end if duration_days(f) < 350]
    if not ytd_options:
        return None
    ytd = max(ytd_options, key=duration_days)
    prior_year_end = ytd["start"] - dt.timedelta(days=1)
    prior_full = next(
        (f for f in durations if near(f["end"], prior_year_end) and 350 <= duration_days(f) <= 380), None
    )
    prior_ytd = next(
        (
            f for f in durations
            if near(f["end"], end - dt.timedelta(days=365))
            and abs(duration_days(f) - duration_days(ytd)) <= 10
        ),
        None,
    )
    if prior_full is None or prior_ytd is None:
        return None
    return prior_full["val"] + ytd["val"] - prior_ytd["val"]


def instant_value(facts: list[dict], on: dt.date, max_age_days: int = 200) -> tuple[float, dt.date] | None:
    """Balance sheet value on `on`, or the most recent one before it if it is not too old."""
    instants = [f for f in facts if f["start"] is None and f["end"] <= on]
    if not instants:
        return None
    latest = max(instants, key=lambda f: f["end"])
    if (on - latest["end"]).days > max_age_days:
        return None
    return latest["val"], latest["end"]


def latest_quarter_value(facts: list[dict], end: dt.date) -> float | None:
    """Value for the single quarter ending on `end` (used for weighted share counts)."""
    options = [f for f in facts if f["start"] is not None and f["end"] == end]
    return min(options, key=duration_days)["val"] if options else None


# --------------------------------------------------------------------------
# Company model
# --------------------------------------------------------------------------

class Company:
    def __init__(self, cfg: dict, company_facts: dict):
        self.cfg = cfg
        self.ticker = cfg["ticker"]
        self.raw = company_facts
        self.tags_used: dict[str, str] = {}
        self.recording = False  # only record source tags for the latest period
        self.overrides = cfg.get("overrides", {})

    def note_tag(self, concept: str, tag: str) -> None:
        if self.recording:
            self.tags_used.setdefault(concept, tag)

    def facts(self, tag: str) -> list[dict]:
        return get_facts(self.raw, tag)

    def ttm(self, concept: str, end: dt.date) -> float | None:
        for tag in TAGS[concept]:
            value = ttm_value(self.facts(tag), end)
            if value is not None:
                self.note_tag(concept, tag)
                return value
        return None

    def instant(self, concept: str, on: dt.date, max_age_days: int = 200) -> float | None:
        for tag in TAGS[concept]:
            found = instant_value(self.facts(tag), on, max_age_days)
            if found is not None:
                self.note_tag(concept, tag)
                return found[0]
        return None

    def period_ends(self) -> list[dt.date]:
        """Quarter-end dates the company has reported revenue for, oldest first."""
        for tag in TAGS["net_revenue"]:
            ends = {f["end"] for f in self.facts(tag) if f["start"] is not None}
            if ends:
                return sorted(ends)
        return []

    def latest_filing(self, end: dt.date) -> dict:
        candidates = [f for tag in TAGS["net_revenue"] for f in self.facts(tag) if f["end"] == end]
        f = max(candidates, key=lambda x: x["filed"])
        accn = f["accn"]
        return {
            "form": f["form"],
            "filed": f["filed"],
            "url": FILING_INDEX_URL.format(cik=self.cfg["cik"], accn_plain=accn.replace("-", ""), accn=accn),
        }

    # Building blocks ------------------------------------------------------

    def pretax(self, end: dt.date) -> float | None:
        value = self.ttm("pretax_income", end)
        if value is not None:
            return value
        ni, tax = self.ttm("net_income", end), self.ttm("income_tax", end)
        if ni is None or tax is None:
            return None
        self.note_tag("pretax_income", "NetIncomeLoss + IncomeTaxExpenseBenefit")
        return ni + tax

    def preferred(self, on: dt.date) -> float:
        if "preferred" in self.overrides:
            self.note_tag("preferred", "manual override (see notes)")
            return float(self.overrides["preferred"]["value"])
        value = self.instant("preferred", on, max_age_days=120)
        return value or 0.0

    def goodwill_and_intangibles(self, on: dt.date) -> float | None:
        combined = self.instant("goodwill_and_intangibles", on, max_age_days=120)
        if combined is not None:
            return combined
        goodwill = self.instant("goodwill", on, max_age_days=370)
        if goodwill is None:
            return None
        intangibles = self.instant("intangibles", on, max_age_days=370)
        if intangibles is None:
            finite = self.instant("intangibles_finite", on, max_age_days=370) or 0.0
            indefinite = self.instant("intangibles_indefinite", on, max_age_days=370) or 0.0
            intangibles = finite + indefinite
        return goodwill + intangibles

    def common_equity(self, on: dt.date) -> float | None:
        total = self.instant("total_equity", on, max_age_days=120)
        return None if total is None else total - self.preferred(on)

    def tangible_equity(self, on: dt.date) -> float | None:
        common, gwi = self.common_equity(on), self.goodwill_and_intangibles(on)
        return None if common is None or gwi is None else common - gwi

    def shares(self, end: dt.date) -> float | None:
        # Cover-page share counts are dated after the quarter ends, so look a few months ahead.
        value = self.instant("shares_outstanding", end + dt.timedelta(days=100), max_age_days=130)
        if value is not None:
            return value
        for tag in TAGS["shares_basic_weighted"]:
            value = latest_quarter_value(self.facts(tag), end)
            if value is not None:
                self.note_tag("shares_outstanding", tag + " (latest quarter average)")
                return value
        return None

    # Metrics ----------------------------------------------------------------

    def metrics(self, end: dt.date, price: float | None = None) -> dict:
        year_ago = end - dt.timedelta(days=365)
        m: dict[str, float | None] = {}

        revenue = self.ttm("net_revenue", end)
        revenue_prior = self.ttm("net_revenue", self._closest_end(year_ago))
        ni_common = self.ttm("net_income_common", end)
        pretax = self.pretax(end)
        comp = self.ttm("compensation", end)

        ce_now, ce_prior = self.common_equity(end), self.common_equity(year_ago)
        tce_now, tce_prior = self.tangible_equity(end), self.tangible_equity(year_ago)
        total_equity = self.instant("total_equity", end, max_age_days=120)
        assets = self.instant("assets", end, max_age_days=120)
        shares = self.shares(end)

        m["net_revenue"] = revenue
        m["net_income_common"] = ni_common
        m["revenue_growth"] = ratio(revenue, revenue_prior, minus_one=True)
        m["pretax_margin"] = ratio(pretax, revenue)
        m["comp_ratio"] = ratio(comp, revenue)
        m["roe"] = ratio(ni_common, average(ce_now, ce_prior))
        avg_tce = average(tce_now, tce_prior)
        # Negative tangible equity (goodwill larger than equity) makes the ratio meaningless.
        m["rotce"] = ratio(ni_common, avg_tce) if avg_tce is not None and avg_tce > 0 else None
        m["equity_to_assets"] = ratio(total_equity, assets)
        m["eps_diluted"] = self.ttm("eps_diluted", end)
        m["bvps"] = ratio(ce_now, shares)
        m["tbvps"] = ratio(tce_now, shares)
        dps = self.ttm("dps_declared", end)

        if price is not None:
            m["pe"] = ratio(price, m["eps_diluted"]) if (m["eps_diluted"] or 0) > 0 else None
            m["pb"] = ratio(price, m["bvps"])
            m["ptbv"] = ratio(price, m["tbvps"]) if (m["tbvps"] or 0) > 0 else None
            m["div_yield"] = ratio(dps, price)
            m["market_cap"] = price * shares if shares else None

        for key in self.cfg.get("skip", []):
            m[key] = None
        for key in ("net_revenue", "net_income_common", "market_cap"):
            if m.get(key) is not None:
                m[key] = m[key] / 1e9
        return m

    def _closest_end(self, target: dt.date) -> dt.date:
        ends = self.period_ends()
        return min(ends, key=lambda d: abs((d - target).days)) if ends else target


def ratio(a: float | None, b: float | None, minus_one: bool = False) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b - 1 if minus_one else a / b


def average(a: float | None, b: float | None) -> float | None:
    if a is None:
        return None
    return a if b is None else (a + b) / 2


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build(args: argparse.Namespace) -> dict:
    config = json.loads((ROOT / "peers.json").read_text())
    cache_dir = ROOT / args.cache_dir
    companies_out, trend_out = [], {}

    for cfg in config["companies"]:
        print(f"{cfg['ticker']}: loading SEC data")
        company = Company(cfg, load_company_facts(cfg["cik"], cache_dir, args.offline))
        ends = company.period_ends()
        if not ends:
            print(f"  no revenue data found, skipping")
            continue
        latest = ends[-1]
        quote = None if args.no_prices else fetch_price(cfg["ticker"])
        company.recording = True
        metrics = company.metrics(latest, quote["price"] if quote else None)
        company.recording = False

        trend_out[cfg["ticker"]] = []
        for end in ends[-TREND_QUARTERS:]:
            point = company.metrics(end)
            trend_out[cfg["ticker"]].append({"end": end.isoformat(), **{k: point[k] for k in TREND_METRICS}})

        notes = list(cfg.get("notes", []))
        notes += [o["note"] for o in cfg.get("overrides", {}).values() if "note" in o]
        companies_out.append({
            "ticker": cfg["ticker"],
            "name": cfg["name"],
            "cik": cfg["cik"],
            "period_end": latest.isoformat(),
            "filing": company.latest_filing(latest),
            "price": quote,
            "metrics": metrics,
            "tags": company.tags_used,
            "notes": notes,
        })
        print(f"  latest period {latest}, ROE {fmt_pct(metrics['roe'])}, pre-tax margin {fmt_pct(metrics['pretax_margin'])}")

    focus = config.get("focus")
    medians = {}
    for metric in METRICS:
        values = [c["metrics"].get(metric["key"]) for c in companies_out if c["ticker"] != focus]
        values = [v for v in values if v is not None]
        medians[metric["key"]] = statistics.median(values) if values else None

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "focus": focus,
        "metrics": METRICS,
        "trend_metrics": TREND_METRICS,
        "companies": companies_out,
        "peer_median": medians,
        "trend": trend_out,
        "trend_hidden": config.get("trend_hidden_by_default", []),
    }


def fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1%}"


def write_outputs(data: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data.json").write_text(json.dumps(data, indent=2))
    template = (ROOT / "template.html").read_text()
    html = template.replace("/*__DATA__*/null", json.dumps(data))
    (out_dir / "index.html").write_text(html)
    print(f"Wrote {out_dir / 'index.html'} and {out_dir / 'data.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="docs", help="output folder (default: docs)")
    parser.add_argument("--cache-dir", default="data/raw", help="where raw SEC files are cached")
    parser.add_argument("--offline", action="store_true", help="use cached SEC files instead of downloading")
    parser.add_argument("--no-prices", action="store_true", help="skip the share price lookup")
    args = parser.parse_args()
    write_outputs(build(args), ROOT / args.out)


if __name__ == "__main__":
    main()
