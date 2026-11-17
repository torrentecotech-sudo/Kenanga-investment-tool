"""Kenanga + Halogen NAV refresh backend for Render.

Start command: gunicorn app:app
The API returns only dated NAVs found on the official source pages.
If a publisher blocks automated requests or changes its page structure, the
API reports the HTTP status/error instead of inventing or reusing a price.
"""
from datetime import datetime, timezone
import re

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template

app = Flask(__name__)

KENANGA_SOURCE = "https://www.kenangainvestors.com.my/fund-list"
KENWEALTH_BASE = (
    "https://www.kenanga.com.my/ms/platform-dalam-talian/"
    "kenwealth/pricing-rates/unit-trusts/"
)
KENWEALTH_PAGE_HINT = 19

KENANGA_MAPPING = {
    "Kenanga Growth Fund": "Kenanga Growth Fund",
    "Kenanga Syariah Growth Fund": "Kenanga Syariah Growth Fund",
    "Kenanga Shariah Growth Opportunities Fund": "Kenanga Shariah Growth Opportunities Fund",
    "Kenanga Blue Chip Fund": "Kenanga Blue Chip Fund",
    "Kenanga Premier Fund": "Kenanga Premier Fund",
    "Kenanga Islamic Balanced Fund": "Kenanga Islamic Balanced Fund",
    "Kenanga Managed Growth Fund": "Kenanga Managed Growth Fund",
    "Kenanga Growth Fund Series 2 (MYR)": "Kenanga Growth Fund Series 2 (MYR)",
    "Kenanga SyariahEXTRA Fund": "Kenanga SyariahEXTRA Fund",
    "Kenanga Malaysian Inc Fund": "Kenanga Malaysian Inc Fund",
    "Kenanga Islamic Asia Pacific (ex Japan) Total Return Fund": "Kenanga Islamic Asia Pacific (ex Japan) Total Return Fund",
    "Kenanga Global Islamic Fund": "Kenanga Global Islamic Fund",
    "Kenanga ASnitaBOND Fund": "Kenanga ASnitaBOND Fund",
}

HALOGEN_MAPPING = {
    "Halogen Shariah Crypto Titans Fund": [
        "Halogen Shariah Crypto Titans Fund",
        "HALOGEN SHARIAH CRYPTO TITANS FUND",
    ],
    "Halogen Shariah Ethereum Fund": [
        "Halogen Shariah Ethereum Fund",
        "HALOGEN SHARIAH ETHERIUM FUND",
        "Halogen Shariah Etherium Fund",
    ],
}

ALL_FUNDS = list(KENANGA_MAPPING) + list(HALOGEN_MAPPING)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ms;q=0.8",
}


def normalize(value):
    value = re.sub(r"\s+", " ", str(value)).strip()
    value = re.sub(r"\s*([*^]+)\s*$", "", value).strip()
    return value.casefold()


def parse_date(value):
    value = re.sub(r"\s+", " ", str(value)).strip()
    for fmt in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%d",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_nav(value):
    cleaned = str(value).replace(",", "").strip()
    match = re.search(r"(?<!\d)(\d+\.\d{3,8})(?!\d)", cleaned)
    if not match:
        return None
    try:
        nav = float(match.group(1))
    except ValueError:
        return None
    return nav if 0 < nav < 1000 else None


def request_page(url):
    """Fetch a page and return its HTML; raise a useful HTTP diagnostic."""
    try:
        response = requests.get(url, timeout=25, headers=HEADERS)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        if response is not None:
            status = response.status_code
            detail = re.sub(r"\s+", " ", response.text or "").strip()[:220]
            raise RuntimeError(
                f"HTTP {status}" + (f" - {detail}" if detail else "")
            ) from exc
        raise RuntimeError(str(exc)[:220]) from exc


def extract_kenanga(html):
    """Extract exact fund-name matches with a parseable date and NAV."""
    soup = BeautifulSoup(html, "html.parser")
    wanted = {normalize(source_name): display for display, source_name in KENANGA_MAPPING.items()}
    found = {}

    for row in soup.select("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 3:
            continue

        texts = [cell.get_text(" ", strip=True) for cell in cells]
        matched_name = next(
            (wanted[normalize(text)] for text in texts if normalize(text) in wanted),
            None,
        )
        if not matched_name:
            continue

        date = next((parsed for parsed in (parse_date(text) for text in texts) if parsed), None)
        nav_values = [value for value in (parse_nav(text) for text in texts) if value is not None]

        if date and nav_values:
            found[matched_name] = {
                "name": matched_name,
                "nav": nav_values[-1],
                "date": date,
                "source": "Kenanga Investors",
            }

    return found


def extract_kenwealth(html):
    """Extract exact Halogen fund rows from Kenwealth dated pricing tables."""
    soup = BeautifulSoup(html, "html.parser")
    aliases = {
        normalize(alias): display
        for display, names in HALOGEN_MAPPING.items()
        for alias in names
    }
    found = {}

    for row in soup.select("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if not cells:
            continue

        texts = [cell.get_text(" ", strip=True) for cell in cells]
        joined = normalize(" ".join(texts))
        display = next(
            (name for alias, name in aliases.items() if alias in joined),
            None,
        )
        if not display:
            continue

        date = next((parsed for parsed in (parse_date(text) for text in texts) if parsed), None)
        nav_values = [value for value in (parse_nav(text) for text in texts) if value is not None]

        if date and nav_values:
            found[display] = {
                "name": display,
                "nav": nav_values[-1],
                "date": date,
                "source": "Kenwealth",
            }

    # Some versions of the pricing page expose the table as text/embedded data.
    # Only accept a nearby exact fund alias together with a dated decimal NAV.
    if len(found) < len(HALOGEN_MAPPING):
        page_text = soup.get_text(" ", strip=True)
        normalized_text = normalize(page_text)

        for display, names in HALOGEN_MAPPING.items():
            if display in found:
                continue
            for alias in names:
                alias_norm = normalize(alias)
                position = normalized_text.find(alias_norm)
                if position < 0:
                    continue

                fragment = page_text[max(0, position - 160): position + 700]
                date_matches = re.findall(
                    r"\b(?:\d{1,2}/\d{1,2}/\d{4}|"
                    r"\d{1,2}[-.]\d{1,2}[-.]\d{4}|"
                    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|"
                    r"\d{4}-\d{2}-\d{2})\b",
                    fragment,
                )
                dates = [parsed for parsed in (parse_date(item) for item in date_matches) if parsed]
                nav_matches = re.findall(r"(?<![\d.])(\d+\.\d{3,8})(?![\d.])", fragment)
                nav_values = [
                    float(item)
                    for item in nav_matches
                    if 0 < float(item) < 1000
                ]

                if dates and nav_values:
                    found[display] = {
                        "name": display,
                        "nav": nav_values[-1],
                        "date": dates[0],
                        "source": "Kenwealth",
                    }
                    break

    return found


def fetch_kenanga_prices():
    html = request_page(KENANGA_SOURCE)
    return extract_kenanga(html)


def fetch_kenwealth_prices():
    """Try the known Kenwealth page and a small set of nearby page numbers."""
    page_numbers = [KENWEALTH_PAGE_HINT, 1, 2, 3]
    errors = []

    for page_number in page_numbers:
        url = f"{KENWEALTH_BASE}?page={page_number}"
        try:
            html = request_page(url)
            results = extract_kenwealth(html)
            if results:
                return results
            errors.append(f"page {page_number}: HTTP 200 but no matching dated NAV rows")
        except Exception as exc:
            errors.append(f"page {page_number}: {str(exc)[:240]}")

    raise RuntimeError("Kenwealth pages could not be read (" + "; ".join(errors) + ")")


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/nav")
@app.get("/api/refresh")
def refresh_nav():
    prices = {}
    errors = []
    source_status = {}

    try:
        kenanga_prices = fetch_kenanga_prices()
        prices.update(kenanga_prices)
        source_status["Kenanga Investors"] = {
            "matched": len(kenanga_prices),
            "expected": len(KENANGA_MAPPING),
        }
        if not kenanga_prices:
            errors.append("Kenanga source returned no matching dated NAV rows.")
    except Exception as exc:
        source_status["Kenanga Investors"] = {
            "matched": 0,
            "expected": len(KENANGA_MAPPING),
        }
        errors.append("Kenanga source: " + str(exc)[:500])

    try:
        kenwealth_prices = fetch_kenwealth_prices()
        prices.update(kenwealth_prices)
        source_status["Kenwealth"] = {
            "matched": len(kenwealth_prices),
            "expected": len(HALOGEN_MAPPING),
        }
    except Exception as exc:
        source_status["Kenwealth"] = {
            "matched": 0,
            "expected": len(HALOGEN_MAPPING),
        }
        errors.append("Kenwealth source: " + str(exc)[:700])

    missing = [name for name in ALL_FUNDS if name not in prices]
    payload = {
        "source": {
            "Kenanga Investors": KENANGA_SOURCE,
            "Kenwealth": KENWEALTH_BASE,
        },
        "sources": source_status,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "funds": list(prices.values()),
        "prices": list(prices.values()),
        "missing": missing,
        "error": "; ".join(errors) if errors else None,
        "matched": len(prices),
        "expected": len(ALL_FUNDS),
    }

    if not prices:
        return jsonify(payload), 502
    return jsonify(payload)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
