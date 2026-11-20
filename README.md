# Kenanga Adviser Tool

## Deploy on Render
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app`
- `app.py` must remain at repository root and `templates/index.html` in `templates/`.

## Selected funds
- 15 Kenanga funds (IDs 1, 5, 14, 22, 24, 28, 29, 30, 41, 49, 50, 53, 70, 75, 77)
- Halogen Shariah Bitcoin Fund
- Halogen Shariah Crypto Titans Fund
- Halogen Shariah Ethereum Fund

## Data sources
- Kenanga current NAV: https://www.kenangainvestors.com.my/fund-list
- Kenanga history: official AJAX NAV history endpoint
- Halogen API: `https://api2.halogen.my/query`
  - `list_daily_fund_prices`
  - `list_fund_net_asset_value`

The Halogen API integration uses the fund IDs and class sequence 1 supplied by the user. The parser validates dated NAV records and does not silently use old hard-coded sample prices.

## Main API routes
- `/api/nav` refreshes current NAVs.
- `/api/nav/history?fund=...&from=YYYY-MM-DD&to=YYYY-MM-DD` retrieves dated history.
- `/api/nav/by-date?fund=...&date=YYYY-MM-DD` returns the latest published NAV on or before the selected date plus its previous published NAV and change.
- `/api/nav/earliest?fund=...` returns the earliest dated NAV found in the official history. For Kenanga, this searches year-by-year and caches the result in process memory.
- `/api/nav/extremes` returns saved Kenanga ATH/ATL from Supabase and derives Halogen ATH/ATL from the Halogen history API.

## Important notes
- Kenanga ATH/ATL persistence still requires `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in Render and the existing `fund_extremes` table.
- The UI intentionally labels Top 5 Holdings as not verified until a dated official factsheet for each fund is connected. It does not fabricate holdings or weights.
- Earliest available NAV is a data-coverage date, not necessarily the legal fund launch date. Confirm launch date from the current official prospectus/factsheet before presenting it as launch date.
- Cost averaging: gross payment is the amount paid; service charge is applied at 3%; SST is 8% of the service charge; net investment is used to calculate units. The UI reports gross and net average cost and return.
- This is an adviser reference tool, not a transaction or dealing platform. Verify official dealing documents, NAV cut-off dates, charges and share classes before client use.


## Factsheet holdings
The dashboard embeds only holdings transcribed from identified Kenanga Investors factsheets and displays the report date plus a link to the original PDF. Coverage currently includes Kenanga Blue Chip Fund, Kenanga Global Islamic Fund, Kenanga Islamic Balanced Fund (equity and fixed income), Kenanga Growth Fund Series 2 (MYR), Kenanga Syariah Growth Fund, Kenanga Managed Growth Fund, Kenanga Malaysian Inc Fund, and Kenanga Premier Fund (two underlying collective investment schemes as listed in its factsheet). Other selected funds, including Halogen funds, display an explicit not-verified state until their own official portfolio disclosures are sourced. Holdings are periodic snapshots, not live portfolio data.

## Monthly automated holdings snapshots
- `GET /api/holdings` serves the latest Supabase snapshot for each fund.
- `POST /api/holdings/sync` discovers Kenanga factsheet PDF links, extracts labelled Top Equity / Top Fixed Income Holdings, reads the report month from the PDF, and appends a dated snapshot. It does not replace prior snapshots if parsing fails.
- `pypdf` is installed through requirements.
- Set `HOLDINGS_SYNC_TOKEN` in Render Environment. Use the same token in `supabase/holdings_monthly_setup.sql`; the Render URL is already pre-filled as `https://kenanga-investment-tool.onrender.com`. Keep the real token out of public GitHub repositories. Existing `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are used server-side.
- Supabase Cron calls the Render sync endpoint monthly on day 5 at 03:15 UTC. Monitor Supabase Cron job history and Render logs.
- Kenanga's official Fund Documents page is checked first. Eight known direct factsheet PDF URLs are also used as a fallback if the listing is blocked or JavaScript-rendered. The sync reads the report date from the PDF (full date where printed, otherwise the first day of the stated month), and safely skips unparsed files while retaining the last valid snapshot. Halogen holdings require a separate official Halogen report source and are not inferred.


## Render environment for monthly holdings
Set these in Render → your Web Service → Environment:
- `SUPABASE_URL` = your Supabase project URL
- `SUPABASE_SERVICE_ROLE_KEY` = your Supabase service-role key (keep private)
- `HOLDINGS_SYNC_TOKEN` = a long random secret; use exactly the same value in Supabase Vault as `kenanga_holdings_sync_token`

The monthly job runs on the 5th of each month at 03:15 UTC (11:15 Malaysia time). The SQL script schedules the job and calls:
`https://kenanga-investment-tool.onrender.com/api/holdings/sync`

After deployment, test `GET /health`, then test the sync with a POST request carrying the `X-Holdings-Token` header. Do not share the token publicly.
