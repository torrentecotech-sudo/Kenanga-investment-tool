"""Kenanga + Halogen NAV refresh backend for Render (gunicorn app:app)."""
from datetime import datetime, timezone
import re
from flask import Flask, jsonify, render_template
from bs4 import BeautifulSoup
import requests

app = Flask(__name__)
KENANGA_SOURCE = 'https://www.kenangainvestors.com.my/fund-list'
HALOGEN_SOURCE = 'https://www.halogen.my/fund/price'

# Exact names prevent mixing similarly named funds or share classes.
KENANGA_MAPPING = {
 'Kenanga Growth Fund': 'Kenanga Growth Fund',
 'Kenanga Syariah Growth Fund': 'Kenanga Syariah Growth Fund',
 'Kenanga Shariah Growth Opportunities Fund': 'Kenanga Shariah Growth Opportunities Fund',
 'Kenanga Blue Chip Fund': 'Kenanga Blue Chip Fund',
 'Kenanga Premier Fund': 'Kenanga Premier Fund',
 'Kenanga Islamic Balanced Fund': 'Kenanga Islamic Balanced Fund',
 'Kenanga Managed Growth Fund': 'Kenanga Managed Growth Fund',
 'Kenanga Growth Fund Series 2 (MYR)': 'Kenanga Growth Fund Series 2 (MYR)',
 'Kenanga SyariahEXTRA Fund': 'Kenanga SyariahEXTRA Fund',
 'Kenanga Malaysian Inc Fund': 'Kenanga Malaysian Inc Fund',
 'Kenanga Islamic Asia Pacific (ex Japan) Total Return Fund': 'Kenanga Islamic Asia Pacific (ex Japan) Total Return Fund',
 'Kenanga Global Islamic Fund': 'Kenanga Global Islamic Fund',
 'Kenanga ASnitaBOND Fund': 'Kenanga ASnitaBOND Fund',
}
HALOGEN_MAPPING = {
 'Halogen Shariah Crypto Titans Fund': ['Halogen Shariah Crypto Titans Fund', 'HALOGEN SHARIAH CRYPTO TITANS FUND'],
 'Halogen Shariah Ethereum Fund': ['Halogen Shariah Ethereum Fund', 'HALOGEN SHARIAH ETHERIUM FUND'],
}
ALL_FUNDS = list(KENANGA_MAPPING) + list(HALOGEN_MAPPING)
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; KenangaNAVDashboard/1.1)'}


def normalize(value):
    value = re.sub(r'\s+', ' ', str(value)).strip()
    return re.sub(r'\s*([*^]+)\s*$', '', value).strip().casefold()


def parse_date(value):
    value = re.sub(r'\s+', ' ', str(value)).strip()
    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%d %b %Y', '%d %B %Y', '%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def extract_kenanga(html):
    soup = BeautifulSoup(html, 'html.parser')
    wanted = {normalize(v): k for k, v in KENANGA_MAPPING.items()}
    found = {}
    for row in soup.select('tr'):
        cells = row.find_all(['td', 'th'], recursive=False)
        if len(cells) < 3:
            continue
        # Kenanga Fund List currently exposes fund name, date and NAV in this order.
        name = normalize(cells[0].get_text(' ', strip=True))
        if name not in wanted:
            continue
        date = parse_date(cells[1].get_text(' ', strip=True))
        price_text = cells[2].get_text(' ', strip=True).replace(',', '')
        match = re.search(r'(?<!\d)(\d+\.\d{3,6})(?!\d)', price_text)
        if date and match:
            nav = float(match.group(1))
            if nav > 0:
                fund = wanted[name]
                found[fund] = {'name': fund, 'nav': nav, 'date': date, 'source': 'Kenanga Investors'}
    return found


def extract_halogen(html):
    """Parse only exact fund rows from Halogen's own Daily Fund Price page.

    The publisher currently renders parts of the page dynamically. This parser
    supports tabular/embedded text representations and fails closed if a dated
    NAV cannot be identified. It never substitutes an old or guessed price.
    """
    soup = BeautifulSoup(html, 'html.parser')
    aliases = {normalize(alias): display for display, names in HALOGEN_MAPPING.items() for alias in names}
    found = {}

    # First pass: ordinary HTML tables.
    for row in soup.select('tr'):
        cells = row.find_all(['td', 'th'], recursive=False)
        if not cells:
            continue
        cell_texts = [c.get_text(' ', strip=True) for c in cells]
        joined = ' '.join(cell_texts)
        matched = next((display for alias, display in aliases.items() if alias in normalize(joined)), None)
        if not matched:
            continue
        date = next((d for d in (parse_date(x) for x in cell_texts) if d), None)
        candidates = []
        for text in cell_texts:
            clean = text.replace(',', '').strip()
            if parse_date(clean):
                continue
            if re.fullmatch(r'\d+(?:\.\d{3,8})', clean):
                value = float(clean)
                if 0 < value < 1000:
                    candidates.append(value)
        if date and candidates:
            found[matched] = {'name': matched, 'nav': candidates[-1], 'date': date, 'source': 'Halogen Capital'}

    # Second pass: embedded text/JSON-like page content. Require both an exact
    # fund name and a nearby dated decimal NAV to avoid false matches.
    if len(found) < len(HALOGEN_MAPPING):
        text = soup.get_text(' ', strip=True)
        for display, names in HALOGEN_MAPPING.items():
            if display in found:
                continue
            for alias in names:
                pos = normalize(text).find(normalize(alias))
                if pos < 0:
                    continue
                fragment = text[max(0, pos - 120):pos + 500]
                dates = re.findall(r'\b(?:\d{1,2}/\d{1,2}/\d{4}|\d{1,2}[-.]\d{1,2}[-.]\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2})\b', fragment)
                numbers = re.findall(r'(?<![\d.])(\d+\.\d{3,8})(?![\d.])', fragment)
                parsed_dates = [parse_date(d) for d in dates]
                parsed_dates = [d for d in parsed_dates if d]
                values = [float(n) for n in numbers if 0 < float(n) < 1000]
                if parsed_dates and values:
                    found[display] = {'name': display, 'nav': values[-1], 'date': parsed_dates[0], 'source': 'Halogen Capital'}
                    break
    return found


def fetch_source(url):
    response = requests.get(url, timeout=25, headers=HEADERS)
    response.raise_for_status()
    return response.text


@app.get('/')
def index():
    return render_template('index.html')


@app.get('/api/nav')
@app.get('/api/refresh')
def refresh_nav():
    prices = {}
    errors = []
    source_status = {}

    try:
        kenanga_prices = extract_kenanga(fetch_source(KENANGA_SOURCE))
        prices.update(kenanga_prices)
        source_status['Kenanga Investors'] = {'matched': len(kenanga_prices), 'expected': len(KENANGA_MAPPING)}
        if not kenanga_prices:
            errors.append('Kenanga source returned no matching dated NAV rows.')
    except Exception as exc:
        source_status['Kenanga Investors'] = {'matched': 0, 'expected': len(KENANGA_MAPPING)}
        errors.append('Kenanga source: ' + str(exc)[:180])

    try:
        halogen_prices = extract_halogen(fetch_source(HALOGEN_SOURCE))
        prices.update(halogen_prices)
        source_status['Halogen Capital'] = {'matched': len(halogen_prices), 'expected': len(HALOGEN_MAPPING)}
        if len(halogen_prices) < len(HALOGEN_MAPPING):
            missing_halogen = [name for name in HALOGEN_MAPPING if name not in halogen_prices]
            errors.append('Halogen source did not expose a dated NAV for: ' + ', '.join(missing_halogen))
    except Exception as exc:
        source_status['Halogen Capital'] = {'matched': 0, 'expected': len(HALOGEN_MAPPING)}
        errors.append('Halogen source: ' + str(exc)[:180])

    missing = [name for name in ALL_FUNDS if name not in prices]
    payload = {
        'source': {'Kenanga Investors': KENANGA_SOURCE, 'Halogen Capital': HALOGEN_SOURCE},
        'sources': source_status,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'funds': list(prices.values()),
        'prices': list(prices.values()),
        'missing': missing,
        'error': '; '.join(errors) if errors else None,
        'matched': len(prices),
        'expected': len(ALL_FUNDS),
    }
    # Partial results are useful: keep existing values for funds not returned.
    if not prices:
        return jsonify(payload), 502
    return jsonify(payload)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
