"""Kenanga + Halogen NAV backend. Render start command: gunicorn app:app"""
from datetime import datetime, timezone
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import local

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
    "Halogen Shariah Crypto Titans Fund": ["Halogen Shariah Crypto Titans Fund", "HALOGEN SHARIAH CRYPTO TITANS FUND"],
    "Halogen Shariah Ethereum Fund": ["Halogen Shariah Ethereum Fund", "HALOGEN SHARIAH ETHERIUM FUND"],
}
ALL_FUNDS = list(KENANGA_MAPPING) + list(HALOGEN_MAPPING)

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
    session.get("https://www.kenangainvestors.com.my/", headers=HEADERS, timeout=25)
    all_rows, start, draw, total = [], 0, 1, None
    while total is None or start < total:
        form = _history_form_payload(fund_id, from_date, end_date, draw, start, page_size)
        response = session.post(KENANGA_NAV_HISTORY_URL, data=form, headers=headers, timeout=30)
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
        if not kenanga:
            errors.append("Kenanga page loaded but no selected dated NAV Price rows were recognised.")
    except Exception as exc:
        statuses["Kenanga Investors"] = {"matched": 0, "expected": len(KENANGA_MAPPING)}
        errors.append("Kenanga source: " + str(exc)[:250])

    halogen_primary = {}
    try:
        halogen_primary = extract_halogen(request_html(HALOGEN_SOURCE))
        prices.update(halogen_primary)
        statuses["Halogen Capital"] = {"matched": len(halogen_primary), "expected": len(HALOGEN_MAPPING)}
    except Exception as exc:
        statuses["Halogen Capital"] = {"matched": 0, "expected": len(HALOGEN_MAPPING)}
        errors.append("Halogen source: " + str(exc)[:250])

    halogen_missing = [name for name in HALOGEN_MAPPING if name not in prices]
    if halogen_missing:
        try:
            fallback = extract_halogen_kenwealth_fallback(request_html(HALOGEN_FALLBACK_SOURCE))
            for name, record in fallback.items():
                if name not in prices:
                    prices[name] = record
            statuses["Kenanga KenWealth Halogen fallback"] = {
                "matched": len(fallback), "expected": len(HALOGEN_MAPPING),
                "used_for": [name for name in fallback if name not in halogen_primary],
            }
        except Exception as exc:
            statuses["Kenanga KenWealth Halogen fallback"] = {"matched": 0, "expected": len(HALOGEN_MAPPING)}
            errors.append("KenWealth Halogen fallback: " + str(exc)[:250])

    missing = [name for name in ALL_FUNDS if name not in prices]
    if missing:
        errors.append("No dated official/fallback NAV found for: " + ", ".join(missing))
    return {
        "source": {
            "Kenanga Investors": KENANGA_SOURCE,
            "Halogen Capital": HALOGEN_SOURCE,
            "Kenanga KenWealth Halogen fallback": HALOGEN_FALLBACK_SOURCE,
        },
        "sources": statuses,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "funds": list(prices.values()),
        "prices": list(prices.values()),
        "missing": missing,
        "error": "; ".join(errors) if errors else None,
        "matched": len(prices),
        "expected": len(ALL_FUNDS),
    }


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


@app.get("/api/nav/history/check-all")
def api_check_all_kenanga_history():
    """Check exactly one fund per request to keep Render workers responsive.

    Call with offset=0, then use the returned next_offset (1, 2, ...).
    This intentionally avoids concurrent calls and large multi-fund requests.
    """
    from_date = request.args.get("from_date", "01-01-2000")
    end_date = request.args.get("end_date", datetime.now().strftime("%d-%m-%Y"))
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        return jsonify({"error": "offset must be an integer"}), 400

    fund_names = list(KENANGA_MAPPING)
    if offset >= len(fund_names):
        return jsonify({
            "verified_fund_ids": len(KENANGA_FUND_IDS),
            "total_funds": len(fund_names),
            "offset": offset,
            "funds": [],
            "next_offset": None,
            "complete": True,
            "note": "All funds have been checked."
        })

    fund_name = fund_names[offset]
    try:
        # Keep each request bounded; fetch_kenanga_nav_history paginates in
        # 100-row pages and returns the full date range for this one fund.
        result = fetch_kenanga_nav_history(
            fund_name, from_date, end_date, page_size=100
        )
        rows = result.get("data", [])
        dates = [row["date"] for row in rows if row.get("date")]
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
    except Exception as exc:
        fund_result = {
            "fund": fund_name,
            "fund_id": KENANGA_FUND_IDS.get(fund_name),
            "status": "error",
            "error": str(exc)[:220],
        }

    next_offset = offset + 1
    if next_offset >= len(fund_names):
        next_offset = None
    return jsonify({
        "source": KENANGA_NAV_HISTORY_URL,
        "from_date": from_date,
        "end_date": end_date,
        "verified_fund_ids": len(KENANGA_FUND_IDS),
        "offset": offset,
        "total_funds": len(fund_names),
        "funds": [fund_result],
        "next_offset": next_offset,
        "complete": next_offset is None,
        "note": "One fund per request; continue with next_offset. NAV high/low are limited to the requested date range."
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "kenanga-halogen-nav"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
