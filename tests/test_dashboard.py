"""
Tests for the TTM and balance sheet logic, using small made-up filings so they
run without internet access:  python -m pytest tests
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard import Company, instant_value, ttm_value  # noqa: E402

D = dt.date


def fact(start, end, val, filed="2026-08-01"):
    return {"start": start, "end": end, "val": val, "filed": filed, "accn": "x", "form": "10-Q", "tag": "t"}


# A December fiscal year company with a full 2025 year and first-half 2025/2026 figures.
DEC_FACTS = [
    fact(D(2025, 1, 1), D(2025, 12, 31), 400.0),   # FY2025
    fact(D(2025, 1, 1), D(2025, 6, 30), 180.0),    # 1H 2025
    fact(D(2026, 1, 1), D(2026, 6, 30), 220.0),    # 1H 2026
]


def test_ttm_uses_full_year_plus_ytd_minus_prior_ytd():
    assert ttm_value(DEC_FACTS, D(2026, 6, 30)) == 400 + 220 - 180


def test_ttm_returns_full_year_when_period_is_year_end():
    assert ttm_value(DEC_FACTS, D(2025, 12, 31)) == 400


def test_ttm_handles_september_fiscal_year():
    # Raymond James style: fiscal year ends September 30.
    facts = [
        fact(D(2024, 10, 1), D(2025, 9, 30), 14065.0),
        fact(D(2024, 10, 1), D(2025, 6, 30), 10338.0),
        fact(D(2025, 10, 1), D(2026, 6, 30), 11522.0),
    ]
    assert ttm_value(facts, D(2026, 6, 30)) == 14065 + 11522 - 10338


def test_ttm_is_none_when_prior_period_missing():
    assert ttm_value([fact(D(2026, 1, 1), D(2026, 6, 30), 220.0)], D(2026, 6, 30)) is None


def test_instant_value_respects_max_age():
    facts = [fact(None, D(2025, 12, 31), 50.0)]
    assert instant_value(facts, D(2026, 6, 30), max_age_days=200) == (50.0, D(2025, 12, 31))
    assert instant_value(facts, D(2026, 6, 30), max_age_days=100) is None


def companyfacts(tags: dict) -> dict:
    """Build a minimal SEC company facts payload from {tag: [raw facts]}."""
    gaap = {}
    for tag, rows in tags.items():
        unit = "USD/shares" if "PerShare" in tag else "USD"
        gaap[tag] = {"units": {unit: [{"form": "10-Q", "filed": "2026-08-01", "accn": "a", **r} for r in rows]}}
    return {"facts": {"us-gaap": gaap}}


def test_falls_back_to_net_income_plus_tax_when_pretax_tag_missing():
    raw = companyfacts({
        "NetIncomeLoss": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 80},
            {"start": "2025-01-01", "end": "2025-06-30", "val": 40},
            {"start": "2026-01-01", "end": "2026-06-30", "val": 50},
        ],
        "IncomeTaxExpenseBenefit": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 20},
            {"start": "2025-01-01", "end": "2025-06-30", "val": 10},
            {"start": "2026-01-01", "end": "2026-06-30", "val": 12},
        ],
    })
    company = Company({"ticker": "TEST", "cik": 1}, raw)
    assert company.pretax(D(2026, 6, 30)) == (80 + 50 - 40) + (20 + 12 - 10)


def test_preferred_override_reduces_common_equity():
    raw = companyfacts({"StockholdersEquity": [{"end": "2026-06-30", "val": 1000}]})
    company = Company({"ticker": "TEST", "cik": 1, "overrides": {"preferred": {"value": 100}}}, raw)
    assert company.common_equity(D(2026, 6, 30)) == 900
