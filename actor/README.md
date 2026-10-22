# FTC Scam Alerts

Turns the FTC's consumer alerts into structured scam intelligence a fraud team can query:
what kind of scam it is, which payment methods scammers demand, and the FTC's own red-flag sentences.

Built for [Second Signature](../README.md), an overnight fraud analyst for small credit unions. When an older member
buys $500 of gift cards at 9:47pm, triage can cite the current FTC alert that matches.

## Output (one item per alert)

| Field | Example |
|---|---|
| `date`, `title`, `url` | `2026-10-22`, "The FTC is not calling to deactivate your phone number" |
| `scam_types` | `["government_impersonation", "business_impersonation"]` |
| `payment_methods` | `["gift_card", "wire_transfer"]` (gift_card, wire_transfer, crypto, cash, payment_app) |
| `red_flags` | `["A real FTC employee will never ever reach out to ask you to send money, ..."]` |
| `summary`, `content` | First 600 / 6000 characters of the alert |

## Input

- `maxAlerts` (default 20): the newest N alerts.
- `crawlDelaySecs` (default and minimum 10): consumer.ftc.gov's robots.txt sets `Crawl-delay: 10`, and the actor never goes faster.
- `onlyPaymentMethods`: keep only alerts that mention these, e.g. `["gift_card"]`.

## How it works

Plain HTTP and BeautifulSoup; the listing is server-rendered, so no browser is needed. Tagging is keyword-based, so it's
deterministic, cheap and needs no API key. Only public FTC pages are fetched, and nothing personal is collected.

Run locally: `pip install -r requirements.txt && python -m src` (reads `storage/key_value_stores/default/INPUT.json`).
