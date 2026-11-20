"""Kenanga + Halogen NAV backend. Render start command: gunicorn app:app"""
from datetime import datetime, timezone
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import local, Thread, Lock
import time
import secrets

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

KENANGA_SOURCE = "https://www.kenangainvestors.com.my/fund-list"
HALOGEN_SOURCE = "https://www.halogen.my/fund/price/"
# KenWealth's public unit-trust pricing table includes Halogen Capital funds.
# This is a clearly labelled fallback, not represented as Halogen's own website.
HALOGEN_FALLBACK_SOURCE = "https://www.kenanga.com.my/ms/platform-dalam-talian/kenwealth/pricing-rates/unit-trusts/page/19/"

KENANGA_MAPPING = {
    "Kenanga Growth Fund": ["Kenanga Growth Fund"],
    "Kenanga Syariah Growth Fund": ["Kenanga Syariah Growth Fund"],
    "Kenanga Shariah Growth Opportunities Fund": ["Kenanga Shariah Growth Opportunities Fund"],
    "Kenanga Blue Chip Fund": ["Kenanga Blue Chip Fund"],
    "Kenanga Premier Fund": ["Kenanga Premier Fund"],
    "Kenanga Islamic Balanced Fund": ["Kenanga Islamic Balanced Fund"],
    "Kenanga Managed Growth Fund": ["Kenanga Managed Growth Fund"],
    "Kenanga Growth Fund Series 2 (MYR)": ["Kenanga Growth Fund Series 2 (MYR)"],
    "Kenanga SyariahEXTRA Fund": ["Kenanga SyariahEXTRA Fund"],
    "Kenanga Malaysian Inc Fund": ["Kenanga Malaysian Inc Fund"],
    "Kenanga Islamic Asia Pacific (ex Japan) Total Return Fund": ["Kenanga Islamic Asia Pacific (ex Japan) Total Return Fund"],
    "Kenanga Global Islamic Fund": ["Kenanga Global Islamic Fund"],
    "Kenanga ASnitaBOND Fund": ["Kenanga ASnitaBOND Fund"],
    "Kenanga Alternative Series: Global Real Assets Fund": ["Kenanga Alternative Series: Global Real Assets Fund", "Kenanga Alternative Series Global Real Assets Fund"],
    "Kenanga Shariah US Titans 50 Fund": ["Kenanga Shariah US Titans 50 Fund", "Kenanga Shariah US Titan 50 Fund"],
}

HALOGEN_MAPPING = {
    "Halogen Shariah Bitcoin Fund": ["Halogen Shariah Bitcoin Fund", "Halogen Shariah Bitcoin Fund (MYR)"],
    "Halogen Shariah Crypto Titans Fund": ["Halogen Shariah Crypto Titans Fund", "HALOGEN SHARIAH CRYPTO TITANS FUND"],
    "Halogen Shariah Ethereum Fund": ["Halogen Shariah Ethereum Fund", "HALOGEN SHARIAH ETHERIUM FUND"],
}
HALOGEN_API_URL = "https://api2.halogen.my/query"
HALOGEN_API_FUNDS = {
    "Halogen Shariah Bitcoin Fund": "1d3ede3d8023fb76a6b73ab2972bdc28b73cc549",
    "Halogen Shariah Crypto Titans Fund": "36b15668cad57d279c63f4529af0c527603f9983",
    "Halogen Shariah Ethereum Fund": "bab842d7c4e6187ea42f6fa0d7a0d302aecbf6ad",
}
ALL_FUNDS = list(KENANGA_MAPPING) + list(HALOGEN_MAPPING)


# Supabase-backed ATH/ATL store. Configure SUPABASE_URL and
# SUPABASE_SERVICE_ROLE_KEY in Render Environment. No Render Persistent Disk needed.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_READY = bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _sb_headers(prefer=None):
    if not SUPABASE_READY:
        raise RuntimeError("Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Render.")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _sb_request(method, table, params=None, payload=None, prefer=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    response = requests.request(method, url, params=params, json=payload,
                                headers=_sb_headers(prefer), timeout=(5, 20))
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"Supabase {table} request failed ({response.status_code}): {detail}")
    if not response.text.strip():
        return []
    try:
        return response.json()
    except ValueError:
        return []


def _sb_get(table, params=None):
    return _sb_request("GET", table, params=params or {"select": "*"})


def _sb_upsert(table, payload, conflict):
    return _sb_request("POST", table, params={"on_conflict": conflict}, payload=payload,
                       prefer="resolution=merge-duplicates,return=representation")


_bulk_lock = Lock()
_bulk_thread = None
_bulk_state = {"status": "idle", "current_fund": None, "current_year": None,
               "completed_funds": 0, "total_funds": 15, "completed_years": 0,
               "message": "No bulk scan has been started."}


def _extreme_row(row):
    if not row:
        return None
    return {
        "fund": row.get("fund_name"), "fund_id": row.get("fund_id"),
        "high": ({"nav": float(row["ath_nav"]), "date": row["ath_date"]}
                 if row.get("ath_nav") is not None else None),
        "low": ({"nav": float(row["atl_nav"]), "date": row["atl_date"]}
                if row.get("atl_nav") is not None else None),
        "nextYear": 1980,
        "complete": bool(row.get("scan_complete")),
        "lastChecked": row.get("last_checked_date"),
        "recordsScanned": 0,
    }


def _get_extreme(fund_name):
    rows = _sb_get("fund_extremes", {"select": "*", "fund_name": f"eq.{fund_name}", "limit": "1"})
    return rows[0] if rows else None


def _get_all_extremes():
    return _sb_get("fund_extremes", {"select": "*", "order": "fund_name.asc", "limit": "100"})


def _save_extreme(fund_name, fund_id, high, low, next_year, complete, records_count):
    payload = {
        "fund_name": fund_name, "fund_id": int(fund_id),
        "ath_nav": high["nav"] if high else None,
        "ath_date": high["date"] if high else None,
        "atl_nav": low["nav"] if low else None,
        "atl_date": low["date"] if low else None,
        "scan_complete": bool(complete),
        "last_checked_date": datetime.now(timezone.utc).date().isoformat(),
        "coverage_start": "1980-01-01",
        "coverage_end": datetime.now(timezone.utc).date().isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    rows = _sb_upsert("fund_extremes", payload, "fund_name")
    return rows[0] if rows else _get_extreme(fund_name)


def _get_progress(fund_name):
    rows = _sb_get("scan_progress", {"select": "*", "fund_name": f"eq.{fund_name}", "limit": "1"})
    return rows[0] if rows else None


def _save_progress(fund_name, fund_id, next_year, last_completed_year, status, records_count):
    payload = {"fund_name": fund_name, "fund_id": int(fund_id), "next_year": int(next_year),
               "last_completed_year": last_completed_year, "status": status,
               "records_count": int(records_count), "updated_at": datetime.now(timezone.utc).isoformat()}
    return _sb_upsert("scan_progress", payload, "fund_name")


def _bulk_job_state():
    state = dict(_bulk_state)
    if SUPABASE_READY:
        try:
            rows = _sb_get("scan_progress", {"select": "fund_name,status,last_completed_year,records_count", "limit": "100"})
            state["completed_funds"] = sum(1 for r in rows if r.get("status") == "completed")
            state["total_funds"] = len(KENANGA_FUND_IDS)
            state["completed_years"] = sum(max(0, int(r.get("last_completed_year") or 1979) - 1979) for r in rows)
        except Exception as exc:
            state["message"] = f"Could not read Supabase scan progress: {str(exc)[:180]}"
    return state


def _set_bulk_job(**values):
    _bulk_state.update(values)
    _bulk_state["updated_at"] = datetime.now(timezone.utc).isoformat()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-MY,en;q=0.9",
}


def norm(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = re.sub(r"\s*[*^]+\s*$", "", value).strip()
    return value.casefold()


def parse_date(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_price(value):
    """Accept a single NAV price, not a percentage/change string."""
    raw = str(value or "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d{3,8})", raw):
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    return number if 0.001 <= number <= 100 else None


def request_html(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=25)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        if response is not None:
            detail = re.sub(r"\s+", " ", response.text or "").strip()[:160]
            raise RuntimeError(f"HTTP {response.status_code}" + (f" - {detail}" if detail else "")) from exc
        raise RuntimeError(str(exc)[:200]) from exc


def _table_headers(table):
    rows = table.find_all("tr")
    for row in rows[:3]:
        cells = row.find_all(["th", "td"], recursive=False)
        labels = [norm(cell.get_text(" ", strip=True)) for cell in cells]
        if any("fund name" in label or label == "fund" for label in labels):
            return labels
    return []


def extract_kenanga(html):
    """Read Kenanga Investors Fund Name + NAV Date + NAV Price columns."""
    soup = BeautifulSoup(html, "html.parser")
    aliases = {norm(alias): name for name, values in KENANGA_MAPPING.items() for alias in values}
    results = {}
    for table in soup.find_all("table"):
        headers = _table_headers(table)
        fund_idx = next((i for i, h in enumerate(headers) if "fund name" in h or h == "fund"), None)
        date_idx = next((i for i, h in enumerate(headers) if "nav date" in h or h == "date"), None)
        price_idx = next((i for i, h in enumerate(headers) if "nav price" in h or h == "nav"), None)
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if not cells:
                continue
            texts = [cell.get_text(" ", strip=True) for cell in cells]
            matched = next((aliases[norm(t)] for t in texts if norm(t) in aliases), None)
            if not matched:
                continue
            if fund_idx is not None and date_idx is not None and price_idx is not None and max(fund_idx, date_idx, price_idx) < len(texts):
                date, nav = parse_date(texts[date_idx]), parse_price(texts[price_idx])
            else:
                date = nav = None
            if date and nav:
                results[matched] = {"name": matched, "nav": nav, "date": date, "source": "Kenanga Investors"}
    return results


def _record_from_mapping(display, date_value, nav_value, source):
    date, nav = parse_date(date_value), parse_price(nav_value)
    if date and nav:
        return {"name": display, "nav": nav, "date": date, "source": source}
    return None


def extract_halogen(html):
    """Parse dated NAVs exposed in Halogen Capital's own price page HTML/JSON."""
    soup = BeautifulSoup(html, "html.parser")
    aliases = {norm(alias): name for name, values in HALOGEN_MAPPING.items() for alias in values}
    results = {}
    for table in soup.find_all("table"):
        headers = _table_headers(table)
        date_idx = next((i for i, h in enumerate(headers) if "date" in h), None)
        nav_idx = next((i for i, h in enumerate(headers) if "nav" in h or "price" in h), None)
        for row in table.find_all("tr"):
            texts = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"], recursive=False)]
            display = next((aliases[norm(t)] for t in texts if norm(t) in aliases), None)
            if display and date_idx is not None and nav_idx is not None and max(date_idx, nav_idx) < len(texts):
                record = _record_from_mapping(display, texts[date_idx], texts[nav_idx], "Halogen Capital")
                if record:
                    results[display] = record
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        if not raw or not any(alias in raw.casefold() for alias in aliases):
            continue
        for match in re.finditer(r"\{[^{}]{0,3000}\}", raw, re.S):
            try:
                obj = json.loads(match.group(0))
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            flat = {norm(k): v for k, v in obj.items()}
            fund_value = next((v for k, v in flat.items() if k in {"fund", "fund name", "name", "productname"}), None)
            display = aliases.get(norm(fund_value)) if fund_value is not None else None
            if not display:
                continue
            date_value = next((v for k, v in flat.items() if "date" in k), None)
            nav_value = next((v for k, v in flat.items() if k in {"nav", "price", "nav price", "nav per unit", "unit price"}), None)
            record = _record_from_mapping(display, date_value, nav_value, "Halogen Capital")
            if record:
                results[display] = record
    return results


def extract_halogen_kenwealth_fallback(html):
    """Read exact Halogen fund rows from KenWealth's published unit-trust price table."""
    soup = BeautifulSoup(html, "html.parser")
    aliases = {norm(alias): name for name, values in HALOGEN_MAPPING.items() for alias in values}
    results = {}
    for table in soup.find_all("table"):
        headers = []
        for row in table.find_all("tr")[:3]:
            cells = row.find_all(["th", "td"], recursive=False)
            labels = [norm(cell.get_text(" ", strip=True)) for cell in cells]
            if "fund" in labels and "nav" in labels and "date" in labels:
                headers = labels
                break
        if not headers:
            continue
        fund_idx = headers.index("fund")
        nav_idx = headers.index("nav")
        date_idx = headers.index("date")
        provider_idx = headers.index("provider") if "provider" in headers else None
        for row in table.find_all("tr"):
            texts = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)]
            if max(fund_idx, nav_idx, date_idx) >= len(texts):
                continue
            display = aliases.get(norm(texts[fund_idx]))
            if not display:
                continue
            if provider_idx is not None and (provider_idx >= len(texts) or "halogen" not in norm(texts[provider_idx])):
                continue
            record = _record_from_mapping(display, texts[date_idx], texts[nav_idx], "Kenanga KenWealth (Halogen fund listing)")
            if record:
                results[display] = record
    return results



def _halogen_post(name, payload=None):
    headers = {"Content-Type": "application/json", "Origin": "https://halogen.my", "Referer": "https://halogen.my/", "User-Agent": HEADERS["User-Agent"]}
    body = {"name": name, "payloadJSON": json.dumps(payload or {}, separators=(",", ":"))}
    response = requests.post(HALOGEN_API_URL, json=body, headers=headers, timeout=(8, 25))
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"])[:200])
    return data


def _walk_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_objects(child)


def _halogen_date(value):
    if value is None: return None
    if isinstance(value, (int, float)):
        # API timestamps may be seconds or milliseconds.
        stamp = value / 1000 if value > 10**11 else value
        try: return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        except Exception: return None
    return parse_date(str(value)[:10]) or parse_date(str(value))


def _halogen_nav(value):
    try:
        n = float(str(value).replace(",", ""))
        return n if 0.000001 <= n <= 100000 else None
    except (TypeError, ValueError): return None


def fetch_halogen_history(fund_name):
    fund_id = HALOGEN_API_FUNDS[fund_name]
    raw = _halogen_post("list_fund_net_asset_value", {"fundId": fund_id, "fundClassSequence": 1, "timeframe": "all"})
    points, seen = [], set()
    date_keys = ("date", "createdAt", "navDate", "priceDate", "effectiveDate", "timestamp", "asOf")
    nav_keys = ("netAssetValuePerUnit", "nav", "value", "price", "unitPrice", "netAssetValue", "navPerUnit")
    for obj in _walk_objects(raw):
        low = {str(k).lower(): v for k, v in obj.items()}
        date_value = next((low[k.lower()] for k in date_keys if k.lower() in low), None)
        nav_value = next((low[k.lower()] for k in nav_keys if k.lower() in low), None)
        date, nav = _halogen_date(date_value), _halogen_nav(nav_value)
        if date and nav is not None and (date, nav) not in seen:
            seen.add((date, nav)); points.append({"date": date, "nav": nav, "source": "Halogen Capital API"})
    points.sort(key=lambda x: x["date"])
    if not points:
        raise RuntimeError("Halogen history API response returned no recognised dated NAV records")
    return points


def fetch_halogen_daily():
    raw = _halogen_post("list_daily_fund_prices", {})
    rows = raw.get("dailyFundPrices", []) if isinstance(raw, dict) else []
    by_id = {v: k for k, v in HALOGEN_API_FUNDS.items()}
    result = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict): continue
        name = by_id.get(str(row.get("id", "")))
        if not name: continue
        nav = _halogen_nav(row.get("netAssetValuePerUnit"))
        date = _halogen_date(row.get("createdAt") or row.get("date"))
        if nav is not None and date:
            result[name] = {"name": name, "nav": nav, "date": date, "source": "Halogen Capital API"}
    return result


# Historical NAV endpoint discovered from Kenanga Investors browser Network capture.
KENANGA_NAV_HISTORY_URL = "https://www.kenangainvestors.com.my/fund/fund/ajax-get-fund-nav-list"
# All 15 IDs were confirmed by the bulk discovery result using a unique
# latest NAV date + price match against Kenanga's official fund list.
KENANGA_FUND_IDS = {
    "Kenanga Growth Fund": 49,
    "Kenanga Syariah Growth Fund": 30,
    "Kenanga Shariah Growth Opportunities Fund": 29,
    "Kenanga Blue Chip Fund": 1,
    "Kenanga Premier Fund": 5,
    "Kenanga Islamic Balanced Fund": 24,
    "Kenanga Managed Growth Fund": 14,
    "Kenanga Growth Fund Series 2 (MYR)": 50,
    "Kenanga SyariahEXTRA Fund": 28,
    "Kenanga Malaysian Inc Fund": 53,
    "Kenanga Islamic Asia Pacific (ex Japan) Total Return Fund": 70,
    "Kenanga Global Islamic Fund": 41,
    "Kenanga ASnitaBOND Fund": 22,
    "Kenanga Alternative Series: Global Real Assets Fund": 75,
    "Kenanga Shariah US Titans 50 Fund": 77,
}


def _history_form_payload(fund_id, from_date, end_date, draw, start, length):
    """Build the DataTables form payload used by Kenanga's historical NAV table."""
    payload = {
        "draw": str(draw),
        "start": str(start),
        "length": str(length),
        "search[value]": "",
        "search[regex]": "false",
        "id": str(fund_id),
        "from_date": from_date,
        "end_date": end_date,
    }
    for i in range(4):
        payload.update({
            f"columns[{i}][data]": str(i),
            f"columns[{i}][name]": "",
            f"columns[{i}][searchable]": "true",
            f"columns[{i}][orderable]": "false",
            f"columns[{i}][search][value]": "",
            f"columns[{i}][search][regex]": "false",
        })
    return payload


def fetch_kenanga_nav_history(fund_name, from_date, end_date, page_size=100):
    """Fetch all historical NAV rows for a supported Kenanga fund. Dates: DD-MM-YYYY."""
    fund_id = KENANGA_FUND_IDS.get(fund_name)
    if fund_id is None:
        raise ValueError(f"Historical fund ID not yet verified for {fund_name}")
    for value, label in ((from_date, "from_date"), (end_date, "end_date")):
        if not re.fullmatch(r"\d{2}-\d{2}-\d{4}", value or ""):
            raise ValueError(f"{label} must use DD-MM-YYYY")
    session = requests.Session()
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://www.kenangainvestors.com.my",
        "Referer": "https://www.kenangainvestors.com.my/",
        "X-Requested-With": "XMLHttpRequest",
    }
    # Establish first-party cookies without copying a user's browser cookie into the app.
    session.get("https://www.kenangainvestors.com.my/", headers=HEADERS, timeout=(4, 8))
    all_rows, start, draw, total = [], 0, 1, None
    while total is None or start < total:
        form = _history_form_payload(fund_id, from_date, end_date, draw, start, page_size)
        response = session.post(KENANGA_NAV_HISTORY_URL, data=form, headers=headers, timeout=(4, 10))
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("Kenanga historical NAV endpoint did not return JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise RuntimeError("Unexpected response structure from Kenanga historical NAV endpoint")
        total = int(body.get("recordsFiltered", body.get("recordsTotal", len(body["data"]))))
        rows = body["data"]
        if not rows:
            break
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 4:
                continue
            date_text, currency, nav_text, change_text = row[:4]
            date_iso = parse_date(date_text)
            nav = parse_price(nav_text)
            if date_iso and nav is not None:
                all_rows.append({
                    "date": date_iso,
                    "date_display": date_text,
                    "currency": currency,
                    "nav": nav,
                    "change": change_text,
                    "source": "Kenanga Investors",
                    "fund": fund_name,
                })
        start += len(rows)
        draw += 1
        if len(rows) < page_size:
            break
    return {"fund": fund_name, "fund_id": fund_id, "from_date": from_date,
            "end_date": end_date, "records_total": total or 0, "records_returned": len(all_rows),
            "source_url": KENANGA_NAV_HISTORY_URL, "data": all_rows}


def _extract_fund_id_candidates(html):
    """Extract possible numeric IDs from official detail-page markup."""
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for tag in soup.find_all(True):
        for attr in ("data-fund-id", "data-id", "data-fundid"):
            value = str(tag.get(attr) or "").strip()
            if value.isdigit():
                found.add(int(value))
        if tag.name == "input":
            name = norm(tag.get("name") or tag.get("id"))
            value = str(tag.get("value") or "").strip()
            if value.isdigit() and name in {"id", "fund id", "fund_id", "fundid"}:
                found.add(int(value))
    patterns = [
        r"(?:fund[_-]?id|fundId|fundid|data-fund-id)\D{0,12}(\d+)",
        r"data-id\D{0,12}(\d+)",
        r"(?:^|[\"'\s,{])id[\"']?\s*[:=]\s*[\"']?(\d+)",
        r"(?:fund|product)[_-]?id[\"']?\s*[:=]\s*[\"']?(\d+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.I):
            found.add(int(match.group(1)))
    return sorted(found)


_KENANGA_WORKER = local()


def _probe_kenanga_id(fund_id):
    """Return the newest dated NAV row for a numeric ID, if the ID is valid."""
    session = getattr(_KENANGA_WORKER, "session", None)
    try:
        if session is None:
            session = requests.Session()
            session.headers.update(HEADERS)
            session.get("https://www.kenangainvestors.com.my/", timeout=15)
            _KENANGA_WORKER.session = session
        ajax_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.kenangainvestors.com.my",
            "Referer": "https://www.kenangainvestors.com.my/",
            "X-Requested-With": "XMLHttpRequest",
        }
        form = _history_form_payload(
            fund_id, "01-01-2000", datetime.now().strftime("%d-%m-%Y"), 1, 0, 1
        )
        response = session.post(KENANGA_NAV_HISTORY_URL, data=form, headers=ajax_headers, timeout=20)
        response.raise_for_status()
        body = response.json()
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            return None
        date_iso, nav = parse_date(row[0]), parse_price(row[2])
        if date_iso and nav is not None:
            return {"fund_id": fund_id, "date": date_iso, "nav": round(nav, 8)}
    except Exception:
        return None
    return None


def discover_kenanga_fund_ids(max_id=200):
    """Bulk-discover IDs by matching historical endpoint NAV rows to the official fund list.

    No profile-page scraping is used because Cloudflare may return HTTP 403 to Render.
    A fund is accepted only when one scanned ID uniquely matches its latest list NAV
    on both date and price.
    """
    html = request_html(KENANGA_SOURCE)
    current_prices = extract_kenanga(html)
    targets = {
        name: {"date": record["date"], "nav": round(record["nav"], 8)}
        for name, record in current_prices.items() if name in KENANGA_MAPPING
    }

    # Keep IDs that have already been confirmed from the user's browser/network capture.
    matches = {name: fund_id for name, fund_id in KENANGA_FUND_IDS.items()}
    reverse = {fund_id: name for name, fund_id in matches.items()}
    ids_to_scan = [i for i in range(1, max_id + 1) if i not in reverse]
    probed = []
    # Moderate concurrency avoids a long serial scan while limiting load on the source.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_probe_kenanga_id, fund_id): fund_id for fund_id in ids_to_scan}
        for future in as_completed(futures):
            row = future.result()
            if row:
                probed.append(row)

    # Exact date + NAV match only. Multiple matches are intentionally left unresolved.
    target_to_ids = {}
    for row in probed:
        key = (row["date"], row["nav"])
        target_to_ids.setdefault(key, []).append(row["fund_id"])

    for fund_name, target in targets.items():
        if fund_name in matches:
            continue
        key = (target["date"], target["nav"])
        candidate_ids = target_to_ids.get(key, [])
        if len(candidate_ids) == 1:
            matches[fund_name] = candidate_ids[0]

    # Only persist uniquely matched IDs in this running app process.
    for fund_name, fund_id in matches.items():
        KENANGA_FUND_IDS[fund_name] = fund_id

    results = []
    for fund_name in KENANGA_MAPPING:
        fund_id = matches.get(fund_name)
        if fund_id is not None:
            results.append({"fund": fund_name, "fund_id": fund_id, "status": "verified"})
        elif fund_name not in targets:
            results.append({"fund": fund_name, "fund_id": None, "status": "not_in_current_fund_list"})
        else:
            target = targets[fund_name]
            candidate_ids = target_to_ids.get((target["date"], target["nav"]), [])
            results.append({
                "fund": fund_name, "fund_id": None,
                "status": "ambiguous" if len(candidate_ids) > 1 else "not_matched",
                "candidate_ids": candidate_ids,
                "latest_nav_date": target["date"], "latest_nav": target["nav"],
            })

    return {
        "source": KENANGA_SOURCE,
        "verified": sum(1 for item in results if item["status"] == "verified"),
        "total": len(KENANGA_MAPPING),
        "scanned_id_range": [1, max_id],
        "valid_ids_found": len(probed),
        "funds": results,
        "note": "IDs are verified only when a unique latest NAV date and price match is found. Ambiguous or unmatched funds are not guessed. Increase max_id query parameter to scan a wider range.",
    }


def _kenanga_profile_url(href, fund_name):
    """Resolve the fund-list disclaimer redirect to the actual public fund profile."""
    from urllib.parse import urlparse, parse_qs

    absolute = requests.compat.urljoin(KENANGA_SOURCE, href)
    parsed = urlparse(absolute)
    if parsed.path.rstrip("/").endswith("fund-disclaimer"):
        slug = parse_qs(parsed.query).get("link", [""])[0].strip()
        # Most disclaimer links contain the profile slug. A few use internal
        # short codes, so use the known public profile slug for those funds.
        code_slugs = {
            "KIAPTRF": "kenanga-islamic-asia-pacific-ex-japan-total-return-fund",
            "KASGRAF": "kenanga-alternative-series-global-real-assets-fund",
        }
        if slug in code_slugs:
            slug = code_slugs[slug]
        if slug:
            return f"https://www.kenangainvestors.com.my/{slug}"
    return absolute


def refresh_payload():
    prices, errors, statuses = {}, [], {}
    try:
        kenanga = extract_kenanga(request_html(KENANGA_SOURCE))
        prices.update(kenanga)
        statuses["Kenanga Investors"] = {"matched": len(kenanga), "expected": len(KENANGA_MAPPING)}
    except Exception as exc:
        statuses["Kenanga Investors"] = {"matched": 0, "expected": len(KENANGA_MAPPING)}
        errors.append("Kenanga source: " + str(exc)[:250])
    try:
        halogen = fetch_halogen_daily()
        prices.update(halogen)
        statuses["Halogen Capital API"] = {"matched": len(halogen), "expected": len(HALOGEN_MAPPING)}
    except Exception as exc:
        statuses["Halogen Capital API"] = {"matched": 0, "expected": len(HALOGEN_MAPPING)}
        errors.append("Halogen API: " + str(exc)[:250])
    missing = [name for name in ALL_FUNDS if name not in prices]
    if missing: errors.append("No current dated NAV found for: " + ", ".join(missing))
    return {"source": {"Kenanga Investors": KENANGA_SOURCE, "Halogen Capital API": HALOGEN_API_URL},
            "sources": statuses, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "funds": list(prices.values()), "prices": list(prices.values()), "missing": missing,
            "error": "; ".join(errors) if errors else None, "matched": len(prices), "expected": len(ALL_FUNDS)}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/nav")
@app.get("/api/refresh")
def api_nav():
    payload = refresh_payload()
    status = 200 if payload["matched"] else 502
    return jsonify(payload), status



@app.get("/api/nav/fund-ids/discover")
def api_discover_fund_ids():
    try:
        try:
            max_id = int(request.args.get("max_id", "200"))
        except ValueError:
            max_id = 200
        max_id = max(50, min(max_id, 500))
        return jsonify(discover_kenanga_fund_ids(max_id=max_id))
    except Exception as exc:
        return jsonify({"error": str(exc)[:250]}), 502


@app.get("/api/nav/fund-ids")
def api_list_fund_ids():
    return jsonify({
        "verified": [{"fund": name, "fund_id": fund_id} for name, fund_id in KENANGA_FUND_IDS.items()],
        "pending": [name for name in KENANGA_MAPPING if name not in KENANGA_FUND_IDS],
        "note": "Only verified IDs are listed. Discovery is best-effort and never guesses IDs."
    })

@app.get("/api/nav/history")
def api_nav_history():
    fund_name = request.args.get("fund", "Kenanga Growth Fund")
    # HTML date inputs send YYYY-MM-DD; also accept DD-MM-YYYY.
    from_value = request.args.get("from") or request.args.get("from_date", "")
    to_value = request.args.get("to") or request.args.get("end_date", "")
    def to_ddmmyyyy(value):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%m-%Y")
        return value
    from_date, end_date = to_ddmmyyyy(from_value), to_ddmmyyyy(to_value)
    try:
        if fund_name in HALOGEN_API_FUNDS:
            all_points = fetch_halogen_history(fund_name)
            lo = datetime.strptime(from_value, "%Y-%m-%d").date() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_value or "") else datetime.strptime(from_value, "%d-%m-%Y").date()
            hi = datetime.strptime(to_value, "%Y-%m-%d").date() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", to_value or "") else datetime.strptime(to_value, "%d-%m-%Y").date()
            selected = [x for x in all_points if lo.isoformat() <= x["date"] <= hi.isoformat()]
            return jsonify({"fund": fund_name, "points": selected, "records_returned": len(selected), "source_note": "Halogen Capital API · historical NAV"})
        result = fetch_kenanga_nav_history(fund_name, from_date, end_date)
        # Chronological points for the performance chart.
        result["points"] = [
            {"date": row["date"], "nav": row["nav"], "source": row["source"]}
            for row in sorted(result["data"], key=lambda item: item["date"])
        ]
        result["source_note"] = "Kenanga Investors · official historical NAV"
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": "Kenanga request failed: " + str(exc)[:200]}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)[:250]}), 502




@app.get("/api/nav/by-date")
def api_nav_by_date():
    fund = request.args.get("fund", "").strip()
    day = request.args.get("date", "").strip()
    if not fund or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return jsonify({"error": "Choose a fund and valid date (YYYY-MM-DD)."}), 400
    try:
        if fund in HALOGEN_API_FUNDS:
            points = fetch_halogen_history(fund)
        elif fund in KENANGA_FUND_IDS:
            d = datetime.strptime(day, "%Y-%m-%d").date()
            prev = (d.replace(day=1) - __import__("datetime").timedelta(days=1))
            start = prev.replace(day=1).strftime("%d-%m-%Y")
            end = d.strftime("%d-%m-%Y")
            points = [{"date": x["date"], "nav": x["nav"], "source": x["source"]} for x in fetch_kenanga_nav_history(fund, start, end).get("data", [])]
        else:
            return jsonify({"error": "Fund is not supported."}), 400
        points = sorted([x for x in points if x["date"] <= day], key=lambda x: x["date"])
        if not points: return jsonify({"error": "No published NAV found on or before this date."}), 404
        current = points[-1]; previous = points[-2] if len(points) > 1 else None
        change = (current["nav"] - previous["nav"]) if previous else None
        pct = (change / previous["nav"] * 100) if previous and previous["nav"] else None
        return jsonify({"fund": fund, "requested_date": day, "nav": current["nav"], "date": current["date"], "previous_nav": previous["nav"] if previous else None, "previous_date": previous["date"] if previous else None, "change": change, "change_pct": pct, "source": current.get("source", "Official NAV")})
    except Exception as exc:
        return jsonify({"error": str(exc)[:250]}), 502


_EARLIEST_NAV_CACHE = {}
@app.get("/api/nav/earliest")
def api_nav_earliest():
    fund = request.args.get("fund", "").strip()
    if fund not in ALL_FUNDS: return jsonify({"error": "Unsupported fund."}), 400
    if fund in _EARLIEST_NAV_CACHE: return jsonify(_EARLIEST_NAV_CACHE[fund])
    try:
        if fund in HALOGEN_API_FUNDS:
            pts = fetch_halogen_history(fund)
            result = {"fund": fund, "earliest_date": pts[0]["date"], "earliest_nav": pts[0]["nav"], "source": "Halogen Capital API history"}
        else:
            found = None
            # Query bounded calendar-year chunks, stopping at the first year with records.
            for year in range(1980, datetime.now().year + 1):
                rows = fetch_kenanga_nav_history(fund, f"01-01-{year}", f"31-12-{year}", page_size=100).get("data", [])
                if rows:
                    first = min(rows, key=lambda x: x["date"])
                    found = {"fund": fund, "earliest_date": first["date"], "earliest_nav": first["nav"], "source": "Kenanga Investors historical NAV"}
                    break
            if not found: return jsonify({"error": "No historical NAV records found."}), 404
            result = found
        _EARLIEST_NAV_CACHE[fund] = result
        return jsonify(result)
    except Exception as exc: return jsonify({"error": str(exc)[:240]}), 502

@app.get("/api/nav/history/year")
def api_nav_history_year():
    """Return one calendar year's official NAV history for a fund.

    The browser calls this endpoint year-by-year, allowing all-time highs/lows
    to be calculated from the earliest available records without one huge
    request that can time out on the upstream publisher.
    """
    fund_name = request.args.get("fund", "").strip()
    try:
        year = int(request.args.get("year", ""))
    except ValueError:
        return jsonify({"error": "year must be an integer"}), 400
    if fund_name not in KENANGA_FUND_IDS:
        return jsonify({"error": "Historical NAV is currently supported only for verified Kenanga fund IDs"}), 400
    if year < 1980 or year > datetime.now().year:
        return jsonify({"error": f"year must be between 1980 and {datetime.now().year}"}), 400
    from_date = f"01-01-{year}"
    end_date = f"31-12-{year}" if year < datetime.now().year else datetime.now().strftime("%d-%m-%Y")
    try:
        result = fetch_kenanga_nav_history(fund_name, from_date, end_date, page_size=100)
        points = sorted(
            [{"date": row["date"], "nav": row["nav"]} for row in result.get("data", [])],
            key=lambda item: item["date"]
        )
        return jsonify({
            "fund": fund_name, "fund_id": result["fund_id"], "year": year,
            "from_date": from_date, "end_date": end_date, "points": points,
            "records_returned": len(points),
            "source": "Kenanga Investors · official historical NAV"
        })
    except requests.Timeout:
        return jsonify({"error": f"Kenanga history request timed out for {fund_name} ({year}). Retry this year."}), 504
    except requests.RequestException as exc:
        return jsonify({"error": "Kenanga request failed: " + str(exc)[:180]}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)[:220]}), 502

@app.get("/api/nav/history/check-all")
def api_check_all_kenanga_history():
    """Check one fund for one calendar year per request.

    This keeps historical lookups bounded: the old 2000-to-current request
    could make Kenanga's upstream endpoint stall long enough for Gunicorn to
    kill the worker. Continue with next_offset / next_year from the response.
    """
    try:
        offset = max(0, int(request.args.get("offset", "0")))
        year = int(request.args.get("year", str(datetime.now().year)))
    except ValueError:
        return jsonify({"error": "offset and year must be integers"}), 400

    fund_names = list(KENANGA_MAPPING)
    if offset >= len(fund_names):
        return jsonify({
            "verified_fund_ids": len(KENANGA_FUND_IDS),
            "total_funds": len(fund_names),
            "offset": offset,
            "year": year,
            "funds": [],
            "next_offset": None,
            "next_year": year - 1 if year > 2000 else None,
            "complete": year <= 2000,
            "note": "No more funds in this year. Set offset=0 and use next_year to continue with the previous year.",
        })
    if year < 2000 or year > datetime.now().year:
        return jsonify({"error": f"year must be between 2000 and {datetime.now().year}"}), 400

    fund_name = fund_names[offset]
    from_date = f"01-01-{year}"
    end_date = f"31-12-{year}" if year < datetime.now().year else datetime.now().strftime("%d-%m-%Y")
    try:
        result = fetch_kenanga_nav_history(fund_name, from_date, end_date, page_size=50)
        rows = result.get("data", [])
        latest = max(rows, key=lambda row: row["date"]) if rows else None
        earliest = min(rows, key=lambda row: row["date"]) if rows else None
        fund_result = {
            "fund": fund_name,
            "fund_id": result["fund_id"],
            "status": "history_available" if rows else "no_records_in_range",
            "records_total": result.get("records_total", 0),
            "records_returned": len(rows),
            "first_nav_date": earliest["date"] if earliest else None,
            "first_nav": earliest["nav"] if earliest else None,
            "latest_nav_date": latest["date"] if latest else None,
            "latest_nav": latest["nav"] if latest else None,
            "highest_nav": max((row["nav"] for row in rows), default=None),
            "lowest_nav": min((row["nav"] for row in rows), default=None),
        }
    except requests.Timeout as exc:
        fund_result = {
            "fund": fund_name, "fund_id": KENANGA_FUND_IDS.get(fund_name),
            "status": "upstream_timeout",
            "error": "Kenanga's historical NAV server did not respond within the short timeout. Retry this fund/year later.",
        }
    except Exception as exc:
        fund_result = {
            "fund": fund_name,
            "fund_id": KENANGA_FUND_IDS.get(fund_name),
            "status": "error",
            "error": str(exc)[:220],
        }

    next_offset = offset + 1 if offset + 1 < len(fund_names) else 0
    next_year = year if next_offset else (year - 1 if year > 2000 else None)
    return jsonify({
        "source": KENANGA_NAV_HISTORY_URL,
        "from_date": from_date,
        "end_date": end_date,
        "year": year,
        "verified_fund_ids": len(KENANGA_FUND_IDS),
        "offset": offset,
        "total_funds": len(fund_names),
        "funds": [fund_result],
        "next_offset": next_offset if next_year is not None else None,
        "next_year": next_year,
        "complete": next_year is None,
        "note": "One fund and one calendar year per request. Continue using next_offset and next_year. High/low values cover only the returned year, not all-time history.",
    })



@app.get("/api/nav/extremes")
def api_get_extremes():
    """Return pre-saved ATH/ATL values from Supabase; no scan is performed."""
    try:
        rows = _get_all_extremes()
        result = {row["fund_name"]: _extreme_row(row) for row in rows}
        for fund_name in HALOGEN_API_FUNDS:
            try:
                pts = fetch_halogen_history(fund_name)
                hi = max(pts, key=lambda x: x["nav"])
                lo = min(pts, key=lambda x: x["nav"])
                result[fund_name] = {"fund": fund_name, "high": {"nav": hi["nav"], "date": hi["date"]},
                                     "low": {"nav": lo["nav"], "date": lo["date"]}, "complete": True,
                                     "source": "Halogen Capital API history"}
            except Exception as halogen_exc:
                result[fund_name] = {"fund": fund_name, "error": str(halogen_exc)[:180]}
        return jsonify({"funds": result, "storage": "supabase + live Halogen API"})
    except Exception as exc:
        return jsonify({"error": str(exc)[:300], "funds": {}, "storage": "supabase"}), 503


@app.get("/api/nav/extremes/scan-year")
def api_scan_extremes_year():
    fund_name = request.args.get("fund", "").strip()
    try:
        year = int(request.args.get("year", ""))
    except ValueError:
        return jsonify({"error": "year must be an integer"}), 400
    if fund_name not in KENANGA_FUND_IDS:
        return jsonify({"error": "Fund ID is not verified for this fund"}), 400
    if year < 1980 or year > datetime.now().year:
        return jsonify({"error": f"year must be between 1980 and {datetime.now().year}"}), 400
    try:
        result = fetch_kenanga_nav_history(fund_name, f"01-01-{year}",
            f"31-12-{year}" if year < datetime.now().year else datetime.now().strftime("%d-%m-%Y"), page_size=100)
        points = sorted([{"nav": row["nav"], "date": row["date"]} for row in result.get("data", [])], key=lambda x: x["date"])
        old = _get_extreme(fund_name)
        state = _extreme_row(old) or {"high": None, "low": None, "recordsScanned": 0}
        high, low = state["high"], state["low"]
        for point in points:
            if high is None or point["nav"] > high["nav"]: high = point
            if low is None or point["nav"] < low["nav"]: low = point
        next_year = max(year + 1, int(state.get("nextYear") or 1980))
        complete = next_year > datetime.now().year
        saved = _save_extreme(fund_name, result["fund_id"], high, low, next_year, complete,
                              state["recordsScanned"] + len(points))
        _save_progress(fund_name, result["fund_id"], next_year, year,
                       "completed" if complete else "running", state["recordsScanned"] + len(points))
        return jsonify({"ok": True, "year": year, "records_returned": len(points), "state": _extreme_row(saved)})
    except requests.Timeout:
        return jsonify({"error": f"Kenanga timed out for {fund_name} ({year}); saved progress is unchanged. Retry this year."}), 504
    except Exception as exc:
        return jsonify({"error": str(exc)[:300]}), 502


def _run_bulk_extremes_scan():
    try:
        names = list(KENANGA_FUND_IDS.keys())
        _set_bulk_job(status="running", current_fund=None, current_year=None,
                      completed_funds=0, total_funds=len(names), completed_years=0,
                      message="Historical scan started on Render; results are saved in Supabase.")
        current_year = datetime.now().year
        for fund_name in names:
            fund_id = KENANGA_FUND_IDS[fund_name]
            progress = _get_progress(fund_name) or {"next_year": 1980, "last_completed_year": 1979,
                                                     "records_count": 0, "status": "pending"}
            saved = _get_extreme(fund_name)
            state = _extreme_row(saved) or {"high": None, "low": None, "recordsScanned": 0, "complete": False}
            start_year = int(progress.get("next_year") or 1980)
            if progress.get("status") == "completed" or state.get("complete"):
                start_year = current_year
            high, low = state.get("high"), state.get("low")
            records_count = int(progress.get("records_count") or state.get("recordsScanned") or 0)
            last_completed = int(progress.get("last_completed_year") or (start_year - 1))
            for year in range(max(1980, start_year), current_year + 1):
                _set_bulk_job(status="running", current_fund=fund_name, current_year=year,
                              message=f"Scanning {fund_name} · {year}")
                start = f"01-01-{year}"
                end = f"31-12-{year}" if year < current_year else datetime.now().strftime("%d-%m-%Y")
                result = fetch_kenanga_nav_history(fund_name, start, end, page_size=100)
                points = sorted([{"nav": row["nav"], "date": row["date"]}
                                 for row in result.get("data", [])], key=lambda x: x["date"])
                for point in points:
                    if high is None or point["nav"] > high["nav"]: high = point
                    if low is None or point["nav"] < low["nav"]: low = point
                records_count += len(points)
                next_year = year + 1
                complete = next_year > current_year
                _save_extreme(fund_name, result["fund_id"], high, low, next_year, complete, records_count)
                _save_progress(fund_name, fund_id, next_year, year,
                               "completed" if complete else "running", records_count)
                last_completed = year
                _set_bulk_job(completed_years=int(_bulk_state.get("completed_years", 0)) + 1)
                time.sleep(0.2)
            _save_progress(fund_name, fund_id, current_year, last_completed, "completed", records_count)
            _set_bulk_job(completed_funds=int(_bulk_state.get("completed_funds", 0)) + 1)
        _set_bulk_job(status="completed", current_fund=None, current_year=None,
                      completed_funds=len(names), total_funds=len(names),
                      message="Historical ATH/ATL scan completed for all verified Kenanga funds.")
    except Exception as exc:
        _set_bulk_job(status="paused", message=f"Scan paused: {str(exc)[:240]}. Completed years remain saved in Supabase; press Scan / Update to resume.")
    finally:
        global _bulk_thread
        with _bulk_lock:
            _bulk_thread = None


@app.post("/api/nav/extremes/bulk-start")
def api_start_bulk_extremes():
    global _bulk_thread
    if not SUPABASE_READY:
        return jsonify({"error": "Supabase is not configured. Check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Render."}), 503
    with _bulk_lock:
        if _bulk_thread is not None and _bulk_thread.is_alive():
            return jsonify({"ok": True, "already_running": True, "job": _bulk_job_state()})
        try:
            # Test credentials/table access before starting the background worker.
            _sb_get("fund_extremes", {"select": "fund_name", "limit": "1"})
            _sb_get("scan_progress", {"select": "fund_name", "limit": "1"})
        except Exception as exc:
            return jsonify({"error": f"Supabase connection check failed: {str(exc)[:240]}"}), 503
        _bulk_thread = Thread(target=_run_bulk_extremes_scan, daemon=True, name="kenanga-ath-atl-scan")
        _bulk_thread.start()
    return jsonify({"ok": True, "started": True, "job": _bulk_job_state()}), 202


@app.get("/api/nav/extremes/bulk-status")
def api_bulk_extremes_status():
    try:
        funds = {row["fund_name"]: _extreme_row(row) for row in _get_all_extremes()}
        return jsonify({"job": _bulk_job_state(), "funds": funds})
    except Exception as exc:
        return jsonify({"error": str(exc)[:300]}), 503


# Monthly factsheet holdings automation. Supabase Cron calls /api/holdings/sync.
HOLDINGS_SYNC_TOKEN = os.environ.get("HOLDINGS_SYNC_TOKEN", "")
KENANGA_DOCUMENTS_URL = "https://www.kenangainvestors.com.my/fund-document-download"
KNOWN_FACTSHEET_URLS = {'Kenanga Blue Chip Fund': 'https://www.kenanga.com.my/wp-content/uploads/kenwealth/IBC%20FFS.pdf', 'Kenanga Global Islamic Fund': 'https://www.kenanga.com.my/wp-content/uploads/kenwealth/KIF%20FFS.pdf', 'Kenanga Islamic Balanced Fund': 'https://www.kenanga.com.my/wp-content/uploads/kenwealth/KIBF%20FFS.pdf', 'Kenanga Growth Fund Series 2 (MYR)': 'https://www.kenanga.com.my/wp-content/uploads/kenwealth/KGFS2MYR%20FFS.pdf', 'Kenanga Syariah Growth Fund': 'https://www.kenanga.com.my/wp-content/uploads/kenwealth/KSGF%20FFS.pdf', 'Kenanga Managed Growth Fund': 'https://www.kenanga.com.my/wp-content/uploads/kenwealth/IMG%20FFS.pdf', 'Kenanga Malaysian Inc Fund': 'https://www.kenanga.com.my/wp-content/uploads/kenwealth/KMIF%20FFS.pdf', 'Kenanga Premier Fund': 'https://www.kenanga.com.my/wp-content/uploads/kenwealth/PKOCF%20FFS.pdf'}

@app.get("/api/holdings")
def api_holdings():
    """Return latest verified factsheet snapshot per fund."""
    if not SUPABASE_READY:
        return jsonify({"funds": {}, "configured": False, "note": "Supabase is not configured."})
    try:
        rows = _sb_get("fund_holdings_snapshots", {
            "select": "fund_name,report_date,source_url,holdings,updated_at",
            "order": "report_date.desc", "limit": "500"
        })
        latest = {}
        for row in rows:
            latest.setdefault(row["fund_name"], row)
        return jsonify({"funds": latest, "configured": True})
    except Exception as exc:
        return jsonify({"funds": {}, "configured": True, "error": str(exc)[:300]}), 502


def _extract_pdf_holdings(pdf_bytes):
    """Extract labelled Top Equity/Fixed Income Holdings tables; reject unlabelled guesses."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:
        raise RuntimeError(f"PDF text extraction failed: {exc}")
    text = re.sub(r"[ \t]+", " ", pages)
    result = {"equity": [], "fixed": []}
    # Parse rows after recognised table headings; only accept explicit percentage rows.
    headings = [
        ("equity", r"TOP\s+(?:5\s+)?EQUITY\s+HOLDINGS"),
        ("fixed", r"TOP\s+(?:5\s+)?(?:FIXED\s+INCOME|BOND|SUKUK)\s+HOLDINGS"),
    ]
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    for kind, heading in headings:
        active = False
        for line in lines:
            if re.search(heading, line, re.I):
                active = True
                continue
            if active:
                if re.search(r"TOP\s+.*HOLDINGS|SECTOR\s+ALLOCATION|ASSET\s+ALLOCATION", line, re.I):
                    break
                m = re.match(r"(.{3,100}?)\s+(\d{1,2}(?:\.\d{1,2})?)\s*%", line)
                if m:
                    name = m.group(1).strip(" -:|")
                    pct = float(m.group(2))
                    if name and pct <= 100 and not re.search(r"TOTAL|CASH|NAV", name, re.I):
                        result[kind].append([name, pct])
                        if len(result[kind]) >= 5:
                            break
    result["equity"] = result["equity"][:5]
    result["fixed"] = result["fixed"][:5]
    if not result["equity"] and not result["fixed"]:
        raise RuntimeError("Recognised Top Holdings table not found; snapshot not replaced.")
    return result


def _discover_factsheet_links():
    """Discover current official PDFs and include known direct factsheet URLs as fallback."""
    links_by_url = {}
    try:
        response = requests.get(KENANGA_DOCUMENTS_URL, headers=HEADERS, timeout=(8, 25))
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            label = " ".join([a.get_text(" ", strip=True), a.parent.get_text(" ", strip=True)])
            full = requests.compat.urljoin(KENANGA_DOCUMENTS_URL, href)
            if ".pdf" in full.lower() and re.search(r"fact.?sheet|ffs|monthly", label + " " + full, re.I):
                links_by_url[full] = label or "Official factsheet listing"
    except Exception:
        # The official listing may be blocked or JavaScript-rendered on hosted servers.
        # Known direct PDF links below provide a conservative fallback.
        pass

    for fund, url in KNOWN_FACTSHEET_URLS.items():
        links_by_url.setdefault(url, fund)

    return [(url, label) for url, label in links_by_url.items()]


def _match_factsheet_fund(label, url):
    # Direct URL-to-fund mapping is unambiguous for the known factsheet URLs.
    for fund, known_url in KNOWN_FACTSHEET_URLS.items():
        if url.rstrip("/") == known_url.rstrip("/"):
            return fund

    hay = re.sub(r"[^a-z0-9]+", " ", (label + " " + url).lower()).strip()
    matches = []
    for fund in KENANGA_MAPPING:
        key = re.sub(r"[^a-z0-9]+", " ", fund.lower()).strip()
        if key and re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", hay):
            matches.append(fund)
    return matches[0] if len(matches) == 1 else None


def _factsheet_report_date(pdf_bytes):
    """Read an explicit report date/month from the first two PDF pages."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        body = "\n".join((page.extract_text() or "") for page in reader.pages[:2])
    except Exception as exc:
        raise RuntimeError(f"PDF report-date extraction failed: {exc}")

    months = ("January|February|March|April|May|June|July|August|September|"
              "October|November|December")
    full_date = re.search(r"\b(\d{1,2})\s+(" + months + r")\s+(20\d{2})\b", body, re.I)
    if full_date:
        return datetime.strptime(
            f"{full_date.group(1)} {full_date.group(2)} {full_date.group(3)}",
            "%d %B %Y"
        ).date().isoformat()

    month_year = re.search(
        r"\b(20\d{2})\s+(" + months + r")\b|\b(" + months + r")\s+(20\d{2})\b",
        body, re.I
    )
    if not month_year:
        raise RuntimeError("Factsheet report month not found in first two PDF pages.")
    month_text = " ".join(part for part in month_year.groups() if part)
    for fmt in ("%Y %B", "%B %Y"):
        try:
            return datetime.strptime(month_text.title(), fmt).date().isoformat()
        except ValueError:
            pass
    raise RuntimeError("Factsheet report month could not be interpreted.")


@app.post("/api/holdings/sync")
def api_holdings_sync():
    """Monthly job: discover official factsheets, parse and append new report snapshots."""
    if not HOLDINGS_SYNC_TOKEN:
        return jsonify({"error": "HOLDINGS_SYNC_TOKEN is not configured."}), 503
    supplied = request.headers.get("X-Holdings-Token", "")
    if not secrets.compare_digest(supplied, HOLDINGS_SYNC_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    if not SUPABASE_READY:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        _sb_get("fund_holdings_snapshots", {"select": "fund_name", "limit": "1"})
    except Exception as exc:
        return jsonify({"error": "Create fund_holdings_snapshots table using supabase/holdings_monthly_setup.sql", "detail": str(exc)[:250]}), 503
    try:
        links = _discover_factsheet_links()
    except Exception as exc:
        return jsonify({"error": "Factsheet discovery failed", "detail": str(exc)[:250]}), 502

    outcomes = []
    saved_reports = set()
    for url, label in links:
        fund = _match_factsheet_fund(label, url)
        if not fund:
            continue
        try:
            pdf = requests.get(url, headers=HEADERS, timeout=(8, 30))
            pdf.raise_for_status()
            holdings = _extract_pdf_holdings(pdf.content)
            # Report date is read from the PDF itself; never substitute today's date.
            report_date = _factsheet_report_date(pdf.content)
            snapshot_key = (fund, report_date)
            if snapshot_key in saved_reports:
                continue
            payload = {"fund_name": fund, "report_date": report_date, "source_url": url,
                       "holdings": holdings, "updated_at": datetime.now(timezone.utc).isoformat()}
            _sb_upsert("fund_holdings_snapshots", payload, "fund_name,report_date")
            saved_reports.add(snapshot_key)
            outcomes.append({"fund": fund, "report_date": report_date, "status": "updated",
                             "source_url": url})
        except Exception as exc:
            outcomes.append({"fund": fund, "status": "skipped", "source_url": url,
                             "reason": str(exc)[:220]})
    return jsonify({"checked_links": len(links), "results": outcomes,
                    "updated": sum(1 for x in outcomes if x["status"] == "updated"),
                    "skipped": sum(1 for x in outcomes if x["status"] == "skipped"),
                    "note": "Only dated, explicitly labelled holdings are saved. Existing snapshots remain if parsing fails."})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "kenanga-halogen-nav", "supabase_configured": SUPABASE_READY})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
