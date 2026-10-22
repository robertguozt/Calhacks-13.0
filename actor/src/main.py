"""FTC Scam Alerts actor: scrape consumer.ftc.gov/consumer-alerts and tag each alert with
the scam type, the payment methods scammers demand, and the FTC's own red-flag sentences.

Plain HTTP + BeautifulSoup (the listing is server-rendered). robots.txt asks for
Crawl-delay: 10, so requests are spaced at least 10s apart.
"""

import asyncio
import re

import httpx
from apify import Actor
from bs4 import BeautifulSoup

BASE = "https://consumer.ftc.gov"
UA = "SecondSignature/1.0 (scam-intel actor; honors robots.txt crawl-delay)"
MIN_DELAY = 10  # robots.txt Crawl-delay

SCAM_TYPES = {
    "tech_support": ["tech support", "computer", "microsoft", "virus", "pop-up", "remote access"],
    "government_impersonation": ["irs", "social security", "government", "jury duty", "arrest", "police", "medicare", "ftc is not"],
    "family_emergency": ["grandchild", "grandson", "granddaughter", "family emergency", "bail"],
    "romance": ["romance", "dating", "online relationship"],
    "business_impersonation": ["your bank", "fraud department", "amazon", "deactivate", "account was compromised"],
    "investment_crypto": ["investment", "crypto", "bitcoin"],
    "prize_lottery": ["prize", "lottery", "sweepstakes"],
    "job_business": ["job offer", "franchise", "business opportunity", "work from home"],
    "health_insurance": ["health insurance", "open enrollment", "marketplace plan"],
}
PAYMENTS = {
    "gift_card": ["gift card"],
    "wire_transfer": ["wire money", "wire transfer", "western union", "moneygram"],
    "crypto": ["cryptocurrency", "bitcoin", "crypto atm"],
    "cash": ["cash", "courier"],
    "payment_app": ["zelle", "venmo", "cash app", "paypal"],
}


def tags(text: str, table: dict[str, list[str]]) -> list[str]:
    t = text.lower()
    return [k for k, words in table.items() if any(w in t for w in words)]


def red_flags(text: str) -> list[str]:
    """The FTC's own warning sentences, e.g. "Only scammers will tell you to pay with a gift card." """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    keep = [s.strip() for s in sentences
            if re.search(r"\b(never|only a scammer|only scammers|is a scam|scammers? (will|may|might))\b", s, re.I)]
    return keep[:4]


def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    seen, out = set(), []
    for art in soup.select('article[about*="/consumer-alerts/20"]'):
        url = art["about"]
        title = art.select_one("h3")
        when = art.select_one("time[datetime]")
        if url in seen or not title:
            continue
        seen.add(url)
        out.append({"url": url, "title": title.get_text(" ", strip=True), "date": when["datetime"][:10] if when else None})
    return out


def parse_article(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    art = soup.select_one(f'article.node--view-mode-cfg-default[about="{url}"]') or soup
    body = art.select_one(".field--name-body")
    text = (body or art).get_text(" ", strip=True)
    return re.sub(r"\s+([,.;:!?)])", r"\1", text)  # get_text(" ") spaces out punctuation after links


async def main():
    async with Actor:
        inp = await Actor.get_input() or {}
        max_alerts = int(inp.get("maxAlerts", 20))
        delay = max(MIN_DELAY, float(inp.get("crawlDelaySecs", MIN_DELAY)))
        only_payments = set(inp.get("onlyPaymentMethods") or [])

        async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as http:
            alerts, page = [], 0
            while len(alerts) < max_alerts and page < 20:
                r = await http.get(f"{BASE}/consumer-alerts", params={"page": page} if page else None)
                r.raise_for_status()
                found = [a for a in parse_listing(r.text) if a["url"] not in {x["url"] for x in alerts}]
                if not found:
                    break
                alerts += found
                page += 1
                await asyncio.sleep(delay)
            # newest first; the mega-menu teasers on each page are deduped above
            alerts = sorted(alerts, key=lambda a: a["date"] or "", reverse=True)[:max_alerts]

            pushed = 0
            for a in alerts:
                try:
                    r = await http.get(a["url"])
                    r.raise_for_status()
                    text = parse_article(r.text, a["url"])
                except Exception as e:
                    Actor.log.warning(f"skip {a['url']}: {e}")
                    continue
                finally:
                    await asyncio.sleep(delay)
                item = {
                    **a,
                    "scam_types": tags(f"{a['title']} {text}", SCAM_TYPES) or ["other"],
                    "payment_methods": tags(text, PAYMENTS),
                    "red_flags": red_flags(text),
                    "summary": text[:600],
                    "content": text[:6000],
                    "source": "FTC Consumer Alerts",
                }
                if only_payments and not only_payments & set(item["payment_methods"]):
                    continue
                await Actor.push_data(item)
                pushed += 1
                Actor.log.info(f"{a['date']} {item['scam_types']} {item['payment_methods']} {a['title'][:70]}")
            Actor.log.info(f"pushed {pushed} alerts")
