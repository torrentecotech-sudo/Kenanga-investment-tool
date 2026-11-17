# Kenanga + Halogen Investment Dashboard (Base Project)

## Deploy to Render
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Keep `app.py` in the repository root.
- Keep `index.html` inside `templates/`.

## Included funds
- 13 selected Kenanga funds
- Halogen Shariah Crypto Titans Fund
- Halogen Shariah Ethereum Fund

## Official sources
- Kenanga Investors: https://www.kenangainvestors.com.my/fund-list
- Halogen Capital: https://www.halogen.my/fund/price/

## NAV safety rules
- Kenanga parser reads the table headers `Fund Name`, `NAV Date`, and `NAV Price`.
- It does not use `NAV Change` as the fund price.
- A record is accepted only when an exact selected fund name, a valid NAV date, and a positive NAV Price are present.
- The Halogen parser checks the official price page's server-rendered tables and embedded JSON. If Halogen loads prices only after client-side JavaScript and does not include the data in the HTML response, the API reports those funds as unavailable. It will not guess a price or silently substitute a third-party value.
- The UI keeps the last saved/reference NAV when a refresh fails or returns only partial results.

## API endpoints
- `/api/nav` — refresh selected official sources and return matched prices, missing funds, and source diagnostics.
- `/health` — service health check.

## Initial reference NAVs
Initial values in the browser are reference values dated 16 Nov 2026, including the Halogen prices shown in the user's screenshot. They are not represented as a live refresh. A successful API refresh replaces them only when a dated official NAV is parsed and validated.

## Cost averaging
- Client payment is gross amount.
- Service charge and SST toggle is per transaction (enabled by default).
- Service charge 3%; SST 8% on service charge.
- Net investment = gross / (1 + service charge rate * (1 + SST rate)) when charges are enabled.
- Units = net investment / purchase NAV.
- Average cost = total net investment / total units.
- Profit/Loss = current value - total net investment.
- Return = Profit/Loss / total net investment.

This is an adviser reference/simulation tool, not a transaction or dealing platform. Verify NAV dates, charges, dealing cut-offs, share classes and investor eligibility against the relevant official documents before client use.
