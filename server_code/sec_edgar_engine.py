"""SEC EDGAR fundamentals engine for the Anvil server runtime.

This keeps SEC requests and normalization on the server. The client receives
transient JSON data for labels, RepeatingPanel rows, Plot figures, and export.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import anvil.server
from anvil.tables import app_tables
import requests


logger = logging.getLogger("sec_edgar_engine")
SEC_HEADERS = {
  "User-Agent": "Bhavesh Sanagapalli, Bsanagapalli@gmail.com",
  "Accept-Encoding": "gzip, deflate",
}
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_REQUEST_DELAY = 0.12
TICKER_CACHE_TTL = 24 * 60 * 60
FACTS_CACHE_TTL = 6 * 60 * 60
CURRENT_USER_WINDOW_SECONDS = 15 * 60
ADMIN_KEY = "Bhavesh#13"
_lock = threading.RLock()
_last_request_at = 0.0
_ticker_cache = (0.0, None)
_facts_cache = {}
_submissions_cache = {}

CONCEPT_TAG_MAP = {
  "Revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
  "CostOfRevenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
  "OperatingIncome": ["OperatingIncomeLoss"],
  "NetIncome": ["NetIncomeLoss", "ProfitLoss"],
  "TotalAssets": ["Assets"],
  "TotalLiabilities": ["Liabilities"],
  "StockholdersEquity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
  "CurrentAssets": ["AssetsCurrent"],
  "CurrentLiabilities": ["LiabilitiesCurrent"],
  "OperatingCashFlow": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
  "CapitalExpenditures": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForCapitalImprovements"],
}


def sec_get(url, max_retries=4, timeout=20):
  """Perform a rate-limited SEC request with retry handling."""
  global _last_request_at
  backoff = 1.0
  for attempt in range(1, max_retries + 1):
    wait = SEC_REQUEST_DELAY - (time.monotonic() - _last_request_at)
    if wait > 0:
      time.sleep(wait)
    try:
      response = requests.get(url, headers=SEC_HEADERS, timeout=timeout)
      _last_request_at = time.monotonic()
      if response.status_code == 200:
        return response.json()
      if response.status_code == 404:
        return None
      if response.status_code in (429, 500, 502, 503, 504):
        logger.warning("SEC status %s, retry %d/%d", response.status_code, attempt, max_retries)
        time.sleep(backoff)
        backoff *= 2
        continue
      logger.error("Unexpected SEC status %s for %s", response.status_code, url)
      return None
    except requests.exceptions.RequestException as exc:
      logger.warning("SEC request failed on attempt %d/%d: %s", attempt, max_retries, exc)
      time.sleep(backoff)
      backoff *= 2
  return None


def ticker_to_cik(ticker, force_refresh=False):
  global _ticker_cache
  now = time.time()
  with _lock:
    cached_at, ticker_map = _ticker_cache
    if force_refresh or ticker_map is None or now - cached_at >= TICKER_CACHE_TTL:
      ticker_map = sec_get(TICKER_MAP_URL)
      _ticker_cache = (now, ticker_map)
  if not ticker_map:
    return None
  ticker = ticker.strip().upper()
  for entry in ticker_map.values():
    if entry.get("ticker", "").upper() == ticker:
      return str(entry["cik_str"]).zfill(10)
  return None


def _cached_json(cache, key, url, force_refresh, ttl):
  now = time.time()
  with _lock:
    cached_at, value = cache.get(key, (0, None))
    if not force_refresh and value is not None and now - cached_at < ttl:
      return value
  value = sec_get(url)
  if value is not None:
    with _lock:
      cache[key] = (now, value)
  return value


def fetch_company_facts(cik, force_refresh=False):
  return _cached_json(_facts_cache, cik, FACTS_URL.format(cik=cik), force_refresh, FACTS_CACHE_TTL)


def fetch_submissions(cik, force_refresh=False):
  return _cached_json(_submissions_cache, cik, SUBMISSIONS_URL.format(cik=cik), force_refresh, FACTS_CACHE_TTL)


def _extract_rows(facts_json, candidates):
  us_gaap = facts_json.get("facts", {}).get("us-gaap", {})
  rows = []
  for priority, tag in enumerate(candidates):
    for record in us_gaap.get(tag, {}).get("units", {}).get("USD", []):
      row = dict(record)
      row["tag_priority"] = priority
      rows.append(row)
  return rows


def _dedupe(rows):
  best = {}
  for row in rows:
    key = tuple(row.get(column) for column in ("fy", "fp", "form", "start", "end"))
    previous = best.get(key)
    if previous is None or row.get("tag_priority", 0) < previous.get("tag_priority", 0) or (
      row.get("tag_priority", 0) == previous.get("tag_priority", 0)
      and row.get("filed", "") > previous.get("filed", "")
    ):
      best[key] = row
  return best.values()


def build_annual_series(rows):
  series = {}
  for row in _dedupe(rows):
    if row.get("form") != "10-K" or row.get("fy") is None:
      continue
    if row.get("start"):
      try:
        days = (datetime.fromisoformat(row["end"]) - datetime.fromisoformat(row["start"])).days
      except (TypeError, ValueError):
        continue
      if not 300 <= days <= 400:
        continue
    series[int(row["fy"])] = row.get("val")
  return dict(sorted(series.items()))


def _divide(numerator, denominator):
  if numerator is None or denominator in (None, 0):
    return None
  return numerator / denominator


def compute_ratios(normalized):
  years = sorted({year for series in normalized.values() for year in series})
  rows = []
  previous_revenue = None
  for year in years:
    revenue = normalized.get("Revenue", {}).get(year)
    cogs = normalized.get("CostOfRevenue", {}).get(year)
    op_income = normalized.get("OperatingIncome", {}).get(year)
    net_income = normalized.get("NetIncome", {}).get(year)
    assets = normalized.get("TotalAssets", {}).get(year)
    liabilities = normalized.get("TotalLiabilities", {}).get(year)
    equity = normalized.get("StockholdersEquity", {}).get(year)
    ocf = normalized.get("OperatingCashFlow", {}).get(year)
    capex = normalized.get("CapitalExpenditures", {}).get(year)
    current_assets = normalized.get("CurrentAssets", {}).get(year)
    current_liabilities = normalized.get("CurrentLiabilities", {}).get(year)
    gross_profit = revenue - cogs if revenue is not None and cogs is not None else None
    fcf = ocf - abs(capex) if ocf is not None and capex is not None else None
    row = {
      "year": year, "revenue": revenue, "net_income": net_income,
      "free_cash_flow": fcf, "gross_margin": _divide(gross_profit, revenue),
      "operating_margin": _divide(op_income, revenue), "net_margin": _divide(net_income, revenue),
      "roe": _divide(net_income, equity), "debt_equity": _divide(liabilities, equity),
      "debt_assets": _divide(liabilities, assets), "current_ratio": _divide(current_assets, current_liabilities),
      "revenue_growth": _divide(revenue - previous_revenue, previous_revenue) if revenue is not None and previous_revenue not in (None, 0) else None,
    }
    rows.append(row)
    if revenue is not None:
      previous_revenue = revenue
  return rows


def _money(value):
  if value is None:
    return "—"
  if abs(value) >= 1e9:
    return "${:,.1f}B".format(value / 1e9)
  if abs(value) >= 1e6:
    return "${:,.1f}M".format(value / 1e6)
  return "${:,.0f}".format(value)


def _percent(value):
  return "—" if value is None else "{:.1%}".format(value)


def _ratio(value):
  return "—" if value is None else "{:.2f}x".format(value)


def _growth(value):
  return "—" if value is None else "{:+.1%} YoY".format(value)


def _history_row(row):
  display = {
    "year": "FY {}".format(row["year"]), "revenue": _money(row["revenue"]),
    "net_income": _money(row["net_income"]), "fcf": _money(row["free_cash_flow"]),
    "gross_margin": _percent(row["gross_margin"]), "operating_margin": _percent(row["operating_margin"]),
    "roe": _percent(row["roe"]), "debt_equity": _ratio(row["debt_equity"]),
  }
  display["search_text"] = " ".join(str(value) for value in display.values()).lower()
  return display


def _layout(y_title, percent=False):
  yaxis = {"title": y_title, "gridcolor": "#edf1f5", "zerolinecolor": "#dbe4ed", "tickfont": {"size": 10, "color": "#667085"}}
  if percent:
    yaxis["tickformat"] = ".0%"
  return {
    "margin": {"l": 48, "r": 16, "t": 8, "b": 38}, "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"family": "Inter, sans-serif", "size": 10, "color": "#667085"}, "hovermode": "x unified",
    "legend": {"orientation": "h", "y": 1.12, "x": 0, "font": {"size": 10}},
    "xaxis": {"showgrid": False, "tickfont": {"size": 10, "color": "#667085"}}, "yaxis": yaxis,
  }


def _charts(rows):
  rows = rows[-8:]
  years = [str(row["year"]) for row in rows]
  return {
    "revenue_growth": {"data": [{"type": "bar", "x": years, "y": [row["revenue_growth"] for row in rows], "marker": {"color": ["#d99445" if row["revenue_growth"] is not None and row["revenue_growth"] < 0 else "#3b82c4" for row in rows]}, "name": "Revenue growth", "hovertemplate": "%{y:.1%}<extra></extra>"}], "layout": _layout("Growth", True)},
    "profitability": {"data": [{"type": "scatter", "mode": "lines+markers", "x": years, "y": [row["gross_margin"] for row in rows], "name": "Gross margin", "line": {"color": "#d99445", "width": 2.5}}, {"type": "scatter", "mode": "lines+markers", "x": years, "y": [row["operating_margin"] for row in rows], "name": "Operating margin", "line": {"color": "#3b82c4", "width": 2.5}}, {"type": "scatter", "mode": "lines+markers", "x": years, "y": [row["net_margin"] for row in rows], "name": "Net margin", "line": {"color": "#8b72d8", "width": 2.5}}], "layout": _layout("Margin", True)},
    "free_cash_flow": {"data": [{"type": "bar", "x": years, "y": [None if row["free_cash_flow"] is None else row["free_cash_flow"] / 1e9 for row in rows], "marker": {"color": "#6eb99a"}, "name": "FCF ($B)", "hovertemplate": "$%{y:.1f}B<extra></extra>"}], "layout": _layout("$B")},
    "leverage": {"data": [{"type": "scatter", "mode": "lines+markers", "x": years, "y": [row["debt_equity"] for row in rows], "name": "Debt / equity", "line": {"color": "#3b82c4", "width": 2.5}}, {"type": "scatter", "mode": "lines+markers", "x": years, "y": [row["debt_assets"] for row in rows], "name": "Debt / assets", "line": {"color": "#6eb99a", "width": 2.5}}], "layout": _layout("Ratio")},
  }


def _delta(rows, field):
  if len(rows) < 2 or rows[-1].get(field) is None or rows[-2].get(field) in (None, 0):
    return None
  return _divide(rows[-1][field] - rows[-2][field], rows[-2][field])


def _build_result(ticker, cik, facts, submissions):
  normalized = {concept: build_annual_series(_extract_rows(facts, tags)) for concept, tags in CONCEPT_TAG_MAP.items()}
  rows = [row for row in compute_ratios(normalized) if row["revenue"] is not None]
  if not rows:
    return None
  latest = rows[-1]
  metrics = {
    "revenue": _money(latest["revenue"]), "revenue_delta": _growth(latest["revenue_growth"]),
    "net_income": _money(latest["net_income"]), "net_income_delta": _growth(_delta(rows, "net_income")),
    "free_cash_flow": _money(latest["free_cash_flow"]), "free_cash_flow_delta": _growth(_delta(rows, "free_cash_flow")),
    "roe": _percent(latest["roe"]), "roe_delta": _percent(latest["roe"] - rows[-2]["roe"] if len(rows) > 1 and latest["roe"] is not None and rows[-2]["roe"] is not None else None),
    "operating_margin": _percent(latest["operating_margin"]), "net_margin": _percent(latest["net_margin"]),
    "debt_equity": _ratio(latest["debt_equity"]), "current_ratio": _ratio(latest["current_ratio"]),
  }
  return {
    "ok": True, "ticker": ticker, "cik": cik,
    "company_name": (submissions or {}).get("name") or "{} filing profile".format(ticker),
    "latest_year": latest["year"], "metrics": metrics,
    "history": [_history_row(row) for row in reversed(rows)], "charts": _charts(rows),
    "updated_at": datetime.now(timezone.utc).isoformat(),
  }


def _record_usage(ticker):
  """Persist one successful analysis request for the protected admin view."""
  client = anvil.server.context.client
  app_tables.usage_events.add_row(
    occurred_at=datetime.now(timezone.utc),
    ticker=ticker,
    visitor_ip=client.ip or "unknown",
    client_type=client.type or "unknown",
  )
  return _visitor_counts()


def _visitor_counts():
  """Return all-time and recently active distinct anonymous visitor counts."""
  now = datetime.now(timezone.utc)
  all_time_ips = set()
  current_ips = set()
  for row in app_tables.usage_events.search():
    visitor_ip = row["visitor_ip"] or "unknown"
    all_time_ips.add(visitor_ip)
    occurred_at = row["occurred_at"]
    if occurred_at is not None:
      if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
      if (now - occurred_at).total_seconds() <= CURRENT_USER_WINDOW_SECONDS:
        current_ips.add(visitor_ip)
  return len(all_time_ips), len(current_ips)


def _usage_row(row):
  occurred_at = row["occurred_at"]
  return {
    "timestamp": occurred_at.strftime("%Y-%m-%d %H:%M UTC") if occurred_at else "unknown",
    "ticker": row["ticker"] or "—",
    "visitor_ip": row["visitor_ip"] or "unknown",
    "client_type": row["client_type"] or "unknown",
  }


@anvil.server.callable
def analyze_ticker(ticker, force_refresh=False):
  ticker = (ticker or "").strip().upper()
  if not ticker:
    return {"ok": False, "message": "Enter a stock ticker."}
  cik = ticker_to_cik(ticker, force_refresh=force_refresh)
  if cik is None:
    return {"ok": False, "message": "Ticker '{}' was not found in SEC EDGAR.".format(ticker)}
  facts = fetch_company_facts(cik, force_refresh=force_refresh)
  if facts is None:
    return {"ok": False, "message": "SEC company facts could not be loaded for {}.".format(ticker)}
  result = _build_result(ticker, cik, facts, fetch_submissions(cik, force_refresh=force_refresh))
  if result is None:
    return {"ok": False, "message": "No usable annual revenue data was found for {}.".format(ticker)}
  all_time_users, current_users = _record_usage(ticker)
  result["visitor_count"] = all_time_users
  result["current_user_count"] = current_users
  return result


@anvil.server.callable
def get_public_user_counts():
  """Return public aggregate counts without exposing usage details."""
  all_time_users, current_users = _visitor_counts()
  return {"ok": True, "all_time_users": all_time_users, "current_users": current_users}


@anvil.server.callable
def get_admin_usage(admin_key):
  """Return usage events only when the server-side admin key matches."""
  if admin_key != ADMIN_KEY:
    return {"ok": False, "message": "Invalid admin key."}
  rows = list(app_tables.usage_events.search())
  rows.sort(key=lambda row: row["occurred_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
  return {"ok": True, "events": [_usage_row(row) for row in rows]}
