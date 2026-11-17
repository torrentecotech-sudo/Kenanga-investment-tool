"""Kenanga + Halogen NAV backend. Render start command: gunicorn app:app"""
from datetime import datetime, timezone
import json
import re

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template

app = Flask(__name__)
KENANGA_SOURCE = "https://www.kenangainvestors.com.my/fund-list"
HALOGEN_SOURCE = "https://www.halogen.my/fund/price/"

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
    # A broad sanity range; still allows small NAV values while rejecting zero.
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
    """Read Kenanga Fund Name + NAV Date + NAV Price columns by header labels.

    Kenanga's page also has NAV Change. This parser deliberately never reads
    that column as a fallback price.
    """
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

            # First locate an exact selected fund name in any cell.
            matched = None
            matched_idx = None
            for idx, text in enumerate(texts):
                key = norm(text)
                if key in aliases:
                    matched, matched_idx = aliases[key], idx
                    break
            if not matched:
                continue

            date = None
            nav = None
            if fund_idx is not None and date_idx is not None and price_idx is not None:
                # Header indexes are authoritative; account for cells that align with the table.
                if max(fund_idx, date_idx, price_idx) < len(texts):
                    date = parse_date(texts[date_idx])
                    nav = parse_price(texts[price_idx])

            # Strict structural fallback for markup that omits usable headers:
            # immediately after the exact fund-name cell expect NAV Date then NAV Price.
            if date is None or nav is None:
                if matched_idx + 2 < len(texts):
                    candidate_date = parse_date(texts[matched_idx + 1])
                    candidate_nav = parse_price(texts[matched_idx + 2])
                    if candidate_date and candidate_nav:
                        date, nav = candidate_date, candidate_nav

            if date and nav:
                results[matched] = {
                    "name": matched,
                    "nav": nav,
                    "date": date,
                    "source": "Kenanga Investors",
                }
    return results


def _record_from_mapping(display, date_value, nav_value, source):
    date = parse_date(date_value)
    nav = parse_price(nav_value)
    if date and nav:
        return {"name": display, "nav": nav, "date": date, "source": source}
    return None


def extract_halogen(html):
    """Parse dated NAVs exposed in Halogen's official price page HTML.

    The official page can render its price selector/data dynamically. We parse
    visible tables and embedded JSON only; if no dated price is exposed, return
    no match rather than guessing from a nearby number or a fact sheet.
    """
    soup = BeautifulSoup(html, "html.parser")
    aliases = {norm(alias): name for name, values in HALOGEN_MAPPING.items() for alias in values}
    results = {}

    # Standard HTML table rows, using explicit column names where available.
    for table in soup.find_all("table"):
        headers = _table_headers(table)
        date_idx = next((i for i, h in enumerate(headers) if "date" in h), None)
        nav_idx = next((i for i, h in enumerate(headers) if "nav" in h or "price" in h), None)
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            texts = [c.get_text(" ", strip=True) for c in cells]
            if not texts:
                continue
            display = next((aliases[norm(t)] for t in texts if norm(t) in aliases), None)
            if not display:
                continue
            if date_idx is not None and nav_idx is not None and max(date_idx, nav_idx) < len(texts):
                record = _record_from_mapping(display, texts[date_idx], texts[nav_idx], "Halogen Capital")
                if record:
                    results[display] = record

    # Embedded JSON / application state. Require fund name, explicit NAV key,
    # and explicit date key in the same object.
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        if not raw or not any(alias in raw.casefold() for alias in aliases):
            continue
        candidates = []
        for match in re.finditer(r"\{[^{}]{0,3000}\}", raw, re.S):
            snippet = match.group(0)
            try:
                obj = json.loads(snippet)
            except Exception:
                continue
            if isinstance(obj, dict):
                candidates.append(obj)
        for obj in candidates:
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

    try:
        halogen = extract_halogen(request_html(HALOGEN_SOURCE))
        prices.update(halogen)
        statuses["Halogen Capital"] = {"matched": len(halogen), "expected": len(HALOGEN_MAPPING)}
        if len(halogen) < len(HALOGEN_MAPPING):
            errors.append("Halogen official page did not expose all dated NAV rows in its server-rendered HTML. The page may load prices dynamically; no guessed price was used.")
    except Exception as exc:
        statuses["Halogen Capital"] = {"matched": 0, "expected": len(HALOGEN_MAPPING)}
        errors.append("Halogen source: " + str(exc)[:250])

    missing = [name for name in ALL_FUNDS if name not in prices]
    return {
        "source": {"Kenanga Investors": KENANGA_SOURCE, "Halogen Capital": HALOGEN_SOURCE},
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


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "kenanga-halogen-nav"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
