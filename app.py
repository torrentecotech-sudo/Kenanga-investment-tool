"""Kenanga + Halogen NAV refresh backend for Render (gunicorn app:app)."""
from datetime import datetime, timezone
import re
from flask import Flask, jsonify, render_template
from bs4 import BeautifulSoup
import requests

app = Flask(__name__)
KENANGA_SOURCE = 'https://www.kenangainvestors.com.my/fund-list'
KENWEALTH_BASE = 'https://www.kenanga.com.my/online-platforms/kenwealth/pricing-rates/unit-trusts/'
KENWEALTH_PAGE_HINT = 19

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


def extract_kenwealth_page(html):
    """Extract the two Halogen NAV rows from a Kenwealth Unit Trusts page."""
    soup = BeautifulSoup(html, 'html.parser')
    aliases = {
        normalize('HALOGEN SHARIAH CRYPTO TITANS FUND'): 'Halogen Shariah Crypto Titans Fund',
        normalize('HALOGEN SHARIAH ETHERIUM FUND'): 'Halogen Shariah Ethereum Fund',
        normalize('HALOGEN SHARIAH ETHEREUM FUND'): 'Halogen Shariah Ethereum Fund',
    }
    found = {}
    for row in soup.select('tr'):
        cells = row.find_all(['td', 'th'], recursive=False)
        if len(cells) < 4:
            continue
        values = [c.get_text(' ', strip=True) for c in cells]
        fund_text = normalize(values[1])
        display = aliases.get(fund_text)
        if not display:
            continue
        nav_text = values[2].replace(',', '').strip()
        date = parse_date(values[3])
        if not date or not re.fullmatch(r'\d+(?:\.\d{3,8})', nav_text):
            continue
        nav = float(nav_text)
        if nav <= 0:
            continue
        found[display] = {
            'name': display, 'nav': nav, 'date': date,
            'source': 'Kenwealth (Kenanga)'
        }
    return found


def fetch_kenwealth_prices():
    """Try the known Halogen page first, then discover the current page if it moved.

    Kenwealth paginates the Unit Trust table. The Halogen rows were found on
    page 19 in the verified page layout. If pagination changes, scan the
    remaining pages and stop as soon as both exact funds are found.
    """
    expected = set(HALOGEN_MAPPING)
    pages_to_try = [KENWEALTH_PAGE_HINT] + [p for p in range(1, 54) if p != KENWEALTH_PAGE_HINT]
    all_found = {}
    for page in pages_to_try:
        url = KENWEALTH_BASE if page == 1 else f'{KENWEALTH_BASE}page/{page}/'
        response = requests.get(url, timeout=15, headers=HEADERS)
        response.raise_for_status()
        page_found = extract_kenwealth_page(response.text)
        all_found.update(page_found)
        if expected.issubset(all_found):
            break
    return all_found

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
        halogen_prices = fetch_kenwealth_prices()
        prices.update(halogen_prices)
        source_status['Kenwealth'] = {'matched': len(halogen_prices), 'expected': len(HALOGEN_MAPPING)}
        if len(halogen_prices) < len(HALOGEN_MAPPING):
            missing_halogen = [name for name in HALOGEN_MAPPING if name not in halogen_prices]
            errors.append('Kenwealth did not return dated NAV rows for: ' + ', '.join(missing_halogen))
    except Exception as exc:
        source_status['Kenwealth'] = {'matched': 0, 'expected': len(HALOGEN_MAPPING)}
        errors.append('Kenwealth source: ' + str(exc)[:180])

    missing = [name for name in ALL_FUNDS if name not in prices]
    payload = {
        'source': {'Kenanga Investors': KENANGA_SOURCE, 'Kenwealth': KENWEALTH_BASE},
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
