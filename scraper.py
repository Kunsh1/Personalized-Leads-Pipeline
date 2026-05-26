"""
CAMX 2026 Exhibitor Scraper
============================
Uses Playwright to scrape all exhibitors from:
https://camx2026.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false

Output: exhibitors_raw.json  (all exhibitors, with flags for missing data)
"""

import json
import re
import asyncio
from playwright.async_api import async_playwright

BASE_URL = "https://camx2026.mapyourshow.com"
GALLERY_URL = f"{BASE_URL}/8_0/explore/exhibitor-gallery.cfm?featured=false"

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def clean(text: str) -> str:
    """Strip and normalise whitespace."""
    return re.sub(r"\s+", " ", (text or "").strip())


# ──────────────────────────────────────────────
# Step 1 – collect all exhibitor links from gallery
# ──────────────────────────────────────────────

async def collect_exhibitor_links(page) -> list[dict]:
    """
    Load the gallery, click 'Load More Results' until all exhibitors are visible,
    then return a list of {name, exhid, detail_url}.
    """
    print(f"[1/3] Opening gallery: {GALLERY_URL}")
    await page.goto(GALLERY_URL, wait_until="networkidle", timeout=60_000)

    # wait for the exhibitor cards to appear
    await page.wait_for_selector("a[href*='exhibitor-details']", timeout=30_000)

    loaded = 0
    while True:
        # Count currently visible cards
        cards = await page.query_selector_all("a[href*='exhibitor-details']")
        current_count = len(cards)

        # Try to find and click "Load More Results"
        load_more = await page.query_selector("button:has-text('Load More'), a:has-text('Load More')")
        if not load_more:
            print(f"   No more 'Load More' button. Total cards visible: {current_count}")
            break

        is_visible = await load_more.is_visible()
        if not is_visible:
            break

        print(f"   Loaded {current_count} so far — clicking Load More…")
        await load_more.click()
        # Wait for new cards to appear
        await page.wait_for_timeout(2500)

        new_count = len(await page.query_selector_all("a[href*='exhibitor-details']"))
        if new_count == current_count:
            # nothing new loaded — we're done
            break
        loaded = new_count

    # Scrape all links
    all_links = await page.eval_on_selector_all(
        "a[href*='exhibitor-details']",
        """els => els.map(el => ({
            name: el.innerText.trim(),
            href: el.getAttribute('href')
        }))"""
    )

    # Deduplicate and normalise
    seen = set()
    exhibitors = []
    for item in all_links:
        href = item["href"]
        if not href:
            continue
        # Make absolute URL
        if href.startswith("/"):
            href = BASE_URL + href
        # Extract exhid
        match = re.search(r"exhid=(\d+)", href, re.I)
        exhid = match.group(1) if match else None
        if exhid and exhid not in seen:
            seen.add(exhid)
            exhibitors.append({
                "name": clean(item["name"]),
                "exhid": exhid,
                "detail_url": href,
            })

    print(f"   → Found {len(exhibitors)} unique exhibitors")
    return exhibitors


# ──────────────────────────────────────────────
# Step 2 – scrape each exhibitor's detail page
# ──────────────────────────────────────────────

async def scrape_detail(page, exhibitor: dict) -> dict:
    """Visit an exhibitor detail page and extract all available fields."""
    url = exhibitor["detail_url"]
    result = {
        "exhid":             exhibitor["exhid"],
        "name":              exhibitor["name"],
        "detail_url":        url,
        "address":           None,
        "website":           None,
        "phone":             None,
        "booth":             None,
        "building":          None,
        "description":       None,
        "product_categories": [],
        "has_description":   False,
        "has_website":       False,
        "missing_fields":    [],
    }

    # Domains that belong to the event/platform, not the exhibiting company
    NON_COMPANY_DOMAINS = [
        "mapyourshow.com", "thecamx.org", "camx.org",
        "facebook.com", "twitter.com", "linkedin.com",
        "youtube.com", "instagram.com", "google.com",
    ]

    # Regex for phone numbers: handles (NXX) NXX-XXXX, NXX.NXX.XXXX,
    # NXX-NXX-XXXX, +1XXXXXXXXXX, and plain 10-digit strings
    PHONE_RE = re.compile(
        r'(?<!\d)'                            # not preceded by digit
        r'(\+?1[\s.\-]?)?'                    # optional country code
        r'\(?(\d{3})\)?[\s.\-]?'             # area code
        r'(\d{3})[\s.\-]?'                   # exchange
        r'(\d{4})'                            # subscriber
        r'(?!\d)'                             # not followed by digit
    )

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2000)     # let JS populate the DOM

        # ── Name ──────────────────────────────────────────────────────────
        h1 = await page.query_selector("h1")
        if h1:
            result["name"] = clean(await h1.inner_text()) or result["name"]

        # ── Description ───────────────────────────────────────────────────
        # FIX: Use <meta name="description"> — always contains the About
        # text, never the Notes section. No selector ambiguity possible.
        desc = await page.get_attribute('meta[name="description"]', "content") \
            or await page.get_attribute('meta[property="og:description"]', "content")
        if desc:
            desc = clean(desc)
            # Guard: reject the generic "Notes" placeholder text
            if desc and "log in to my show planner" not in desc.lower():
                result["description"]    = desc
                result["has_description"] = True

        # ── Booth ─────────────────────────────────────────────────────────
        # FIX: Extract the booth CODE from the URL parameter (booth=N35),
        # not the link text which can be "Floor Plan" or the full label.
        booth_links = await page.query_selector_all("a[href*='floorplan_link']")
        for link in booth_links:
            href = await link.get_attribute("href") or ""
            m = re.search(r"[?&]booth=([A-Z0-9]+)", href, re.I)
            if m:
                result["booth"]    = m.group(1).upper()
                # Full display label e.g. "Building C, Level 1 — N35"
                result["building"] = clean(await link.inner_text()) or None
                break

        # ── Address ───────────────────────────────────────────────────────
        # MapYourShow renders the address block in JS. We grab the whole
        # "Company Information" section text, then parse it line-by-line.
        addr_raw: str = await page.evaluate("""
            () => {
                // Walk all headings to find "Company Information"
                const headings = [...document.querySelectorAll('h2, h3, h4')];
                const compInfoH = headings.find(
                    h => /company\\s+information/i.test(h.textContent)
                );
                if (!compInfoH) return '';
                // Grab the nearest container that holds the address lines
                const section = compInfoH.closest('section')
                    || compInfoH.parentElement;
                return section ? section.innerText : '';
            }
        """)
        if addr_raw:
            lines = []
            for l in addr_raw.splitlines():
                l = l.strip()
                if not l:
                    continue
                # Skip section headings, booth/building labels, nav text
                if re.search(
                    r'company\s+information|floor\s+plan|planner|notes|'
                    r'^booths?$|building\s+[a-z]|level\s+\d|'
                    r'^\s*[A-Z]{1,3}\d+\s*$',   # bare booth codes e.g. FF35, N35
                    l, re.I
                ):
                    continue
                # Skip pure-number lines (phone numbers)
                if re.fullmatch(r'[\d\s\-.()+]+', l):
                    continue
                lines.append(l)
            if lines:
                result["address"] = ", ".join(lines)

        # ── Website ───────────────────────────────────────────────────────
        # FIX: Scan ALL external links but skip anything from event/nav
        # domains. The first remaining link is the company's own site.
        all_links = await page.query_selector_all("a[href^='http'], a[href^='https']")
        for link in all_links:
            href = (await link.get_attribute("href") or "").strip()
            if href and not any(d in href for d in NON_COMPANY_DOMAINS):
                result["website"]    = href
                result["has_website"] = True
                break

        # ── Phone ─────────────────────────────────────────────────────────
        # FIX: Phone is plain text on MapYourShow, never a tel: link.
        # Strategy 1 — tel: link (catches sites that do use it)
        tel_el = await page.query_selector("a[href^='tel:']")
        if tel_el:
            result["phone"] = clean(
                (await tel_el.get_attribute("href") or "").replace("tel:", "")
                or await tel_el.inner_text()
            )

        # Strategy 2 — regex scan of the Company Information section text
        if not result["phone"] and addr_raw:
            m = PHONE_RE.search(addr_raw)
            if m:
                result["phone"] = clean(m.group(0))

        # Strategy 3 — regex scan of the full visible page text (last resort)
        if not result["phone"]:
            body_text = await page.inner_text("body")
            # Limit search to the first 3000 chars to avoid matching
            # unrelated numbers deep in the page footer
            m = PHONE_RE.search(body_text[:3000])
            if m:
                result["phone"] = clean(m.group(0))

        # ── Product Categories ────────────────────────────────────────────
        cat_els = await page.query_selector_all(
            "a[href*='/searchtype/category/']"
        )
        # Must use a regular loop — await inside a generator expression
        # creates an async generator that list()/dict.fromkeys() cannot
        # consume, causing 'async_generator object is not iterable'.
        cat_texts = []
        for el in cat_els:
            text = clean(await el.inner_text())
            if text:
                cat_texts.append(text)
        result["product_categories"] = list(dict.fromkeys(cat_texts))

    except Exception as e:
        result["scrape_error"] = str(e)
        print(f"   ⚠  Error on {url}: {e}")

    # ── Flag missing fields ──
    missing = []
    for field in ["address", "website", "phone", "booth", "description"]:
        if not result.get(field):
            missing.append(field)
    result["missing_fields"] = missing

    return result


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        # ── Step 1: collect all links ──
        exhibitors = await collect_exhibitor_links(page)

        # ── Step 2: scrape detail pages concurrently ──
        CONCURRENCY = 10   # open 10 tabs at once — polite but fast
        semaphore = asyncio.Semaphore(CONCURRENCY)
        total = len(exhibitors)
        completed = 0

        async def scrape_with_sem(exh: dict) -> dict:
            nonlocal completed
            async with semaphore:
                tab = await context.new_page()
                try:
                    result = await scrape_detail(tab, exh)
                finally:
                    await tab.close()
                completed += 1
                print(f"   [{completed}/{total}] {exh['name']}")
                return result

        print(f"\n[2/3] Scraping {total} detail pages ({CONCURRENCY} concurrent tabs)…")
        results = await asyncio.gather(*[scrape_with_sem(exh) for exh in exhibitors])

        await browser.close()

    # ── Step 3: save output ──
    print(f"\n[3/3] Saving results…")
    with open("exhibitors_raw.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Summary
    total = len(results)
    with_desc = sum(1 for r in results if r["has_description"])
    without_desc = total - with_desc

    print(f"\n✅ Done!")
    print(f"   Total exhibitors   : {total}")
    print(f"   With description   : {with_desc}  → will go to Sheet 2 (AI enrichment)")
    print(f"   Without description: {without_desc} → flagged in Sheet 1 only")
    print(f"   Output: exhibitors_raw.json")


if __name__ == "__main__":
    asyncio.run(main())