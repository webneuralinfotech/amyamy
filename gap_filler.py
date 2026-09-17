"""
Company Gap-Filler
--------------------
Goes through the Google Sheet and finds rows where Website or Email is
missing (says "N/A" or "Not found"). For each of those, searches Google
(via Serper.dev, a legal Google-results API) for the company name, grabs
their real website from the top result, then crawls that site's own
navigation links looking for a public email address anywhere on the
site - header, footer, body, contact page, about page, wherever it is.

Uses a two-stage approach for reading each page:
  1. Fast plain-HTML fetch (requests) - works for most normal sites.
  2. If that finds nothing, falls back to a real headless browser
     (Playwright) that actually runs the site's JavaScript - needed for
     modern JS-built sites (React/Next.js/Vue) where the real content,
     including emails, doesn't exist in the raw HTML at all.

This runs as a SEPARATE step from the main scraper, after it, so the
main scraper's job stays focused on Internshala/HelloIntern only.

Sheet column order must match: Company Name | Website | Email |
Internship URL | Date/Time Scraped | Internshala Company Page

--------------------
BULLETPROOF UPDATE:
The script used to crash and stop completely if ANY single step failed
- for example a temporary Google Sheets connection drop, a browser
crash, or an unexpected error on one company's website. That meant one
bad row could stop hundreds of good rows behind it from ever being
processed.

This version wraps every risky operation in retries and safety nets:
  - Saving to Google Sheets retries 3 times before giving up on that
    one row (fixes the SSLEOFError connection-drop crash).
  - Each company is processed inside its own try/except - if anything
    goes wrong on one company, it's logged and the script moves on to
    the next company instead of stopping.
  - If the browser itself crashes, it's automatically restarted so the
    run can continue instead of dying.
  - Website search and page fetches also get one automatic retry before
    being treated as "not found" for that company.

--------------------
AMI Dev - Aug 2026 - DATE CUTOFF UPDATE:
Only ~50-100 real companies exist right now, but this script was
re-checking an old historical backlog of unresolved gaps every single
day, which burned through the entire monthly GitHub Actions budget in
one run (3+ hours).

The sheet is processed newest-first (bottom to top). This version reads
each row's "Date/Time Scraped" column and stops the ENTIRE run the
moment it reaches a company older than MAX_DAYS_BACK days (default 2)
- it does not continue upward through the older backlog at all. This
keeps each run fast and cheap, only covering genuinely new companies.
"""

import os
import re
import sys
import time
import json
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SHEET_TAB_NAME = os.environ.get("GOOGLE_SHEET_TAB_NAME", "Sheet1")
SERPER_API_KEY = os.environ["SERPER_API_KEY"]
MAX_PAGES_PER_SITE = int(os.environ.get("GAP_FILLER_MAX_PAGES_PER_SITE", "8"))
REQUEST_DELAY_SECONDS = 2
PLAYWRIGHT_TIMEOUT_MS = 15000

# How many times to retry a risky network operation (Sheets save, Serper
# search, page fetch) before giving up on it for that one company.
MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 5

# AMI Dev - Aug 2026 - Only companies added within the last N days are
# processed. Since the sheet is processed newest-first (bottom to top),
# the run stops entirely the moment it reaches a row older than this
# cutoff, instead of grinding through the whole historical sheet daily.
MAX_DAYS_BACK = int(os.environ.get("GAP_FILLER_MAX_DAYS_BACK", "2"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
EMAIL_BLOCKLIST_DOMAINS = (
    "sentry.io", "wixpress.com", "example.com", "godaddy.com",
    "yourdomain.com", "domain.com", "internshala.com", "hellointern.in",
    "sentry-next.wixpress.com", "w3.org", "schema.org",
)

# Words that suggest a link leads to a page likely to contain contact info.
# Used to PRIORITIZE which internal links to visit first - not a strict
# whitelist, since we fall back to crawling any internal link if needed.
PRIORITY_LINK_KEYWORDS = (
    "contact", "about", "reach", "connect", "touch", "support",
    "help", "team", "office", "location",
)

# Domains that are never a real company's own website - job boards,
# directories, data aggregators, download sites, social platforms, etc.
NON_COMPANY_DOMAINS = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "internshala.com", "hellointern.in", "unstop.com", "naukri.com",
    "glassdoor.com", "indeed.com", "youtube.com", "wikipedia.org",
    "justdial.com", "crunchbase.com", "google.com",
    "ziprecruiter.com", "tracxn.com", "boardgamegeek.com", "softonic.com",
    "reddit.com", "quora.com", "medium.com", "pinterest.com",
    "apple.com", "music.apple.com", "spotify.com", "app.spotify.com",
    "play.google.com", "apps.apple.com", "amazon.com", "amazon.in",
    "flipkart.com", "pharmeasy.in", "1mg.com", "netmeds.com",
    "linktr.ee", "bit.ly", "tinyurl.com",
    # Trade registries, business data aggregators, and directory sites -
    # these list company info but are never the company's own website
    "zaubacorp.com", "f6s.com", "falconebiz.com", "zoominfo.com",
    "planetexim.net", "find-and-update.company-information.service.gov.uk",
    "company-information.service.gov.uk", "indiamart.com",
    "lawctopus.com", "konfhub.com", "usnews.com", "ghar.tv",
    "preqin.com", "bebee.com", "tohundguide.com", "penguinrandomhouse.com",
    "m.indiamart.com",
)

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheets_service():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_info = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(creds_info, scopes=SHEETS_SCOPES)
    return build("sheets", "v4", credentials=credentials)


def get_all_rows(sheets_service):
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB_NAME}!A1:F",
    ).execute()
    return result.get("values", [])


def update_row(sheets_service, row_number, website, email):
    """
    Saves the website/email for one row into the Google Sheet.

    BULLETPROOF: retries up to MAX_RETRIES times if the connection to
    Google drops mid-request (the exact issue that crashed the last
    run - ssl.SSLEOFError). Waits a few seconds between attempts to let
    any temporary network issue clear up. If it still fails after all
    retries, it logs the failure and lets the script continue to the
    next company instead of crashing the entire run.
    """
    body = {"values": [[website, email]]}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"{SHEET_TAB_NAME}!B{row_number}:C{row_number}",
                valueInputOption="USER_ENTERED",
                body=body,
            ).execute()
            return True
        except Exception as e:
            print(f"  [warn] Sheets save failed for row {row_number} (attempt {attempt}/{MAX_RETRIES}): {e}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SECONDS)

    print(f"  [error] Could not save row {row_number} after {MAX_RETRIES} attempts - skipping this row, continuing run.", file=sys.stderr)
    return False


def search_company_website(company_name):
    """
    Uses Serper.dev (real Google search results via a legal API) to find
    the company's real website.

    BULLETPROOF: retries once on any network error before giving up.
    """
    for attempt in range(1, 3):
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": SERPER_API_KEY,
                    "Content-Type": "application/json",
                },
                json={"q": company_name, "num": 5},
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
            break
        except Exception as e:
            print(f"  [warn] Serper API error for '{company_name}' (attempt {attempt}/2): {e}", file=sys.stderr)
            if attempt == 2:
                return None
            time.sleep(3)

    items = result.get("organic", [])
    for item in items:
        link = item.get("link", "")
        if not any(bad_domain in link for bad_domain in NON_COMPANY_DOMAINS):
            return link

    return None


def clean_email_list(raw_emails, allowed_domain=None):
    seen = set()
    cleaned = []
    for email in raw_emails:
        email_clean = email.lower().strip().strip(".").strip(",")

        # Strip an accidentally-attached URL prefix (e.g. a broken
        # "http://name@domain.com" scraped from a malformed link)
        for prefix in ("http://", "https://"):
            if email_clean.startswith(prefix):
                email_clean = email_clean[len(prefix):]

        if " " in email_clean:
            continue
        if email_clean in seen:
            continue
        if any(bad in email_clean for bad in EMAIL_BLOCKLIST_DOMAINS):
            continue
        if email_clean.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
            continue
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email_clean):
            continue
        seen.add(email_clean)
        cleaned.append(email_clean)

    # If we know the company's own domain, prefer emails that match it -
    # this stops picking up a stray email from an ad/widget/embed that
    # belongs to a completely different company.
    if allowed_domain and cleaned:
        matching = [e for e in cleaned if e.endswith("@" + allowed_domain) or allowed_domain in e.split("@")[-1]]
        if matching:
            return matching
        # No exact domain match found - still return candidates, since a
        # company sometimes legitimately uses a Gmail/Outlook address.
        generic_providers = ("gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "rediffmail.com")
        generic = [e for e in cleaned if e.split("@")[-1] in generic_providers]
        return generic if generic else cleaned

    return cleaned


def extract_emails_from_html(html, allowed_domain=None):
    """Scans the FULL page - header, footer, body, everywhere - for emails."""
    soup = BeautifulSoup(html, "html.parser")

    mailto_emails = [
        a["href"].replace("mailto:", "").split("?")[0]
        for a in soup.find_all("a", href=True)
        if a["href"].lower().startswith("mailto:")
    ]

    page_text = soup.get_text(" ")
    text_emails = EMAIL_REGEX.findall(page_text)

    return clean_email_list(mailto_emails + text_emails, allowed_domain)


def get_internal_links(html, base_url):
    """
    Pulls every internal link from a page's actual navigation/footer -
    not a guessed list of page names. Returns them ranked so
    contact/about-style pages are tried first, but ANY internal link
    is included as a fallback.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc

    priority_links = []
    other_links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        if parsed.netloc != base_domain:
            continue
        if full_url in seen:
            continue
        seen.add(full_url)

        link_text = (a.get_text(" ") or "").lower()
        href_lower = href.lower()

        if any(kw in link_text or kw in href_lower for kw in PRIORITY_LINK_KEYWORDS):
            priority_links.append(full_url)
        else:
            other_links.append(full_url)

    return priority_links + other_links


def fetch_with_requests(url):
    """Fast plain-HTML fetch. Returns (html, final_url) or (None, None)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return None, None
        return resp.text, resp.url
    except Exception:
        return None, None


def fetch_with_playwright(url, browser):
    """
    Real headless browser fetch - runs the page's JavaScript like a
    normal visitor's browser would, then returns the fully-built HTML.
    Needed for modern JS-built sites (React/Next.js/Vue/Wix) where the
    raw HTML is just an empty shell before JavaScript runs.

    Uses "domcontentloaded" instead of "networkidle" - waits for the
    real page content to be ready, not for every tracking script, ad,
    or chat widget to finish loading in the background. Much faster and
    avoids timing out on sites that never go fully idle.

    BULLETPROOF: if the browser itself has crashed/died, this raises a
    special signal (BrowserDeadError) so the caller can restart it,
    instead of the whole script dying.
    """
    page = None
    try:
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)  # brief pause for JS to render content
        html = page.content()
        final_url = page.url
        page.close()
        return html, final_url
    except Exception as e:
        print(f"  [warn] Playwright error loading '{url}': {e}", file=sys.stderr)
        if page:
            try:
                page.close()
            except Exception:
                pass
        # If the browser process itself has died, signal that so main()
        # can restart it and keep the run going.
        if "has been closed" in str(e) or "Target page, context or browser has been closed" in str(e):
            raise BrowserDeadError(str(e))
        return None, None


class BrowserDeadError(Exception):
    """Raised when the Playwright browser process itself has crashed."""
    pass


# Sites known to be permanently broken, blocking, or extremely slow to
# load - skip these entirely rather than wasting a Playwright timeout
# on them every single run.
SKIP_SITES_ENTIRELY = (
    "justdial.com",
)


def find_email_on_site(website_url, browser):
    """
    Universal email finder with two stages per page:
      1. Fast plain-HTML fetch (requests) - works for most sites.
      2. If nothing found, real headless browser (Playwright) - catches
         modern JS-built sites where content only appears after
         JavaScript runs.

    Visits the homepage first, then follows the site's own real internal
    links - prioritizing contact/about-style pages, falling back to any
    internal link - until an email is found or MAX_PAGES_PER_SITE is hit.

    BULLETPROOF: lets BrowserDeadError bubble up to main() so the
    browser can be restarted; any other error just means "no email
    found on this site" instead of crashing.
    """
    if not website_url:
        return None

    if any(bad_site in website_url.lower() for bad_site in SKIP_SITES_ENTIRELY):
        print(f"  [skip] Site is in SKIP_SITES_ENTIRELY list, not attempting: {website_url}")
        return None

    allowed_domain = urlparse(website_url).netloc.replace("www.", "")

    # --- Homepage ---
    html, final_url = fetch_with_requests(website_url)

    candidates = extract_emails_from_html(html, allowed_domain) if html else []
    if not candidates:
        html, pw_url = fetch_with_playwright(website_url, browser)
        if html:
            final_url = pw_url or website_url
            candidates = extract_emails_from_html(html, allowed_domain)

    if candidates:
        return candidates[0]

    if not html:
        return None

    links_to_visit = get_internal_links(html, final_url)[:MAX_PAGES_PER_SITE]

    for link in links_to_visit:
        time.sleep(REQUEST_DELAY_SECONDS)

        page_html, _ = fetch_with_requests(link)
        candidates = extract_emails_from_html(page_html, allowed_domain) if page_html else []

        if not candidates:
            page_html, _ = fetch_with_playwright(link, browser)
            if page_html:
                candidates = extract_emails_from_html(page_html, allowed_domain)

        if candidates:
            return candidates[0]

    return None


def parse_scraped_date(raw_value):
    """
    Parses the "Date/Time Scraped" column value. Tries a few common
    formats since the exact format used by the main scraper isn't
    guaranteed here. Returns a datetime, or None if it can't be parsed
    (in which case the caller treats the row as recent, to be safe,
    rather than accidentally skipping a genuinely new company).
    """
    if not raw_value:
        return None
    raw_value = raw_value.strip()
    formats_to_try = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    )
    for fmt in formats_to_try:
        try:
            return datetime.strptime(raw_value, fmt)
        except ValueError:
            continue
    return None


def process_one_company(sheets_service, browser, i, row):
    """
    Handles everything for ONE company row: finding website/email and
    saving the result.

    BULLETPROOF: this entire function is called inside a try/except in
    main(), so no matter what unexpected error happens for this one
    company, it will be logged and the script will move on to the next
    company instead of stopping.
    """
    company_name = row[0] if len(row) > 0 else ""
    current_website = row[1] if len(row) > 1 else ""
    current_email = row[2] if len(row) > 2 else ""

    website_missing = current_website in ("", "N/A")
    email_missing = current_email in ("", "Not found")

    if not company_name or (not website_missing and not email_missing):
        return False

    print(f"[Row {i}] Filling gaps for '{company_name}' (website_missing={website_missing}, email_missing={email_missing})")

    new_website = current_website
    new_email = current_email

    if website_missing:
        found_website = search_company_website(company_name)
        if found_website:
            new_website = found_website
            print(f"  [found] Website: {found_website}")
        time.sleep(REQUEST_DELAY_SECONDS)

    website_to_check = new_website if new_website not in ("", "N/A") else None
    if email_missing and website_to_check:
        found_email = find_email_on_site(website_to_check, browser)
        if found_email:
            new_email = found_email
            print(f"  [found] Email: {found_email}")
        else:
            print(f"  [not found] No email found across site for '{company_name}'")

    updated = False
    if new_website != current_website or new_email != current_email:
        saved = update_row(sheets_service, i, new_website or "N/A", new_email or "Not found")
        if saved:
            print(f"  [updated] Row {i} saved.")
            updated = True

    time.sleep(REQUEST_DELAY_SECONDS)
    return updated


def main():
    sheets_service = get_sheets_service()
    rows = get_all_rows(sheets_service)

    if len(rows) <= 1:
        print("No data rows found in sheet.")
        return

    data_rows = rows[1:]
    processed_count = 0
    failed_count = 0

    # Process bottom-to-top: newest sheet entries are far more likely to
    # still have gaps (older top rows have already been filled in past
    # runs), so working from the bottom first targets where gap-filling
    # is actually needed instead of re-checking already-complete rows.
    indexed_rows = list(enumerate(data_rows, start=2))
    indexed_rows.reverse()

    cutoff_date = datetime.now() - timedelta(days=MAX_DAYS_BACK)
    print(f"Only processing companies added on or after {cutoff_date.date()} (last {MAX_DAYS_BACK} day(s)).")

    playwright_ctx = sync_playwright().start()
    browser = playwright_ctx.chromium.launch(headless=True)

    try:
        for i, row in indexed_rows:
            scraped_date_raw = row[4] if len(row) > 4 else ""
            scraped_date = parse_scraped_date(scraped_date_raw)

            if scraped_date is not None and scraped_date < cutoff_date:
                print(f"[Row {i}] Reached a company older than {MAX_DAYS_BACK} day(s) (scraped {scraped_date.date()}). Stopping run here.")
                break

            try:
                if process_one_company(sheets_service, browser, i, row):
                    processed_count += 1
            except BrowserDeadError:
                # The browser process crashed - restart it and retry
                # this SAME row once before moving on, so we don't lose
                # a company just because of a browser hiccup.
                print(f"  [warn] Browser crashed on row {i} - restarting browser and retrying this row.", file=sys.stderr)
                try:
                    browser.close()
                except Exception:
                    pass
                browser = playwright_ctx.chromium.launch(headless=True)
                try:
                    if process_one_company(sheets_service, browser, i, row):
                        processed_count += 1
                except Exception as e:
                    print(f"  [error] Row {i} failed again after browser restart, skipping: {e}", file=sys.stderr)
                    failed_count += 1
            except Exception as e:
                # Catch-all: absolutely anything unexpected on this one
                # company gets logged, and the run continues to the
                # next company no matter what.
                print(f"  [error] Unexpected error on row {i} ('{row[0] if row else ''}'), skipping: {e}", file=sys.stderr)
                failed_count += 1
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            playwright_ctx.stop()
        except Exception:
            pass

    print(f"\nDone. Filled gaps for {processed_count} companies this run. {failed_count} companies skipped due to errors.")


if __name__ == "__main__":
    main()
