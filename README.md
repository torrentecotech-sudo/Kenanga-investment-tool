# Kenanga Adviser Tool

Flask dashboard for selected Kenanga and Halogen funds.

## Deploy to Render
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Keep `app.py` in the repository root and `index.html` in `templates/`.
- `server.py` is a lightweight local static/server helper; Render should run the Flask app through the Procfile.

## Selected funds (18)
- 15 Kenanga funds, including Kenanga Alternative Series: Global Real Assets Fund and Kenanga Shariah US Titans 50 Fund.
- Halogen Shariah Bitcoin Fund
- Halogen Shariah Crypto Titans Fund
- Halogen Shariah Ethereum Fund

## Official price and history sources
- Kenanga current NAV: https://www.kenangainvestors.com.my/fund-list
- Kenanga historical NAV endpoint: https://www.kenangainvestors.com.my/fund/fund/ajax-get-fund-nav-list
- Halogen API: `https://api2.halogen.my/query`
  - Current daily NAV action: `list_daily_fund_prices`
  - Historical NAV action: `list_fund_net_asset_value`
- Halogen fund identifiers are maintained in `HALOGEN_FUNDS` in `app.py`.

The app validates dated NAV records and does not substitute NAV-change percentages for NAV prices. Halogen API connectivity and response shape should be confirmed in the deployed Render environment; the app reports an error rather than fabricating data if the upstream schema changes.

## Historical performance
- Period shortcuts: 1M, 3M, 6M, YTD, 1Y, 3Y, 5Y and Since Inception.
- Custom from/to dates are available.
- Kenanga requests are split into calendar-year chunks to avoid one large historical request timing out.
- Halogen history is requested from the official API using `timeframe=all`, then filtered to the selected dates.
- Return is NAV price return and excludes distributions and fees.

## ATH / ATL
- Kenanga ATH/ATL scan is saved in Supabase using the existing `fund_extremes` and `scan_progress` tables.
- Halogen ATH/ATL is calculated from the official history API response when the extremes endpoint is requested; it is not written to the existing numeric `fund_id` Supabase table.
- Configure `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in Render for persistent Kenanga scan progress.

## Cost averaging
- Gross client payment is the total amount paid.
- Service charge: 3%; SST: 8% of service charge.
- Net investment when charges apply: `gross / (1 + service charge rate * (1 + SST rate))`.
- Charge toggle is per transaction and defaults to on.
- Units = net investment / purchase NAV.
- Average cost = total net investment / total units.
- Profit/loss = current value - total net investment.
- Return = profit/loss / total net investment.

## Data limitations
- Top 5 holdings are intentionally not displayed: no verified official holdings feed or factsheet archive is connected in this project. Do not populate holdings with inferred or invented securities.
- NAV change is shown as unavailable unless the upstream current-price source supplies a separately validated change field. The current integration does not invent a daily change.
- Current reference NAVs in the browser are fallback display values only. A successful official refresh replaces them with dated source records.

This is an adviser reference/simulation tool, not a transaction or dealing platform. Verify NAV dates, charges, dealing cut-offs, share classes and investor eligibility against official fund documents before client use.
