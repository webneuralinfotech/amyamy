"""
Shiksha College Email Gap-Filler
----------------------------------
Simplified, email-only version of gap_filler.py, pointed at Sheet2
(university data) instead of Sheet1 (company data). Never touches
Sheet1 or any company scraper file.

Sheet2 columns (5 total): College Name | Website | Email | Page URL |
Timestamp Scraped

FIX (this version): earlier versions tried to guess the college's
real website by looking for an outbound link on its Shiksha profile
page - this repeatedly grabbed the WRONG link (App Store links, ad
banners, unrelated partner sites like InfoEdge). This version drops
that approach entirely and instead uses the exact same proven method
as gap_filler.py (the company version): search Google via Serper API
for the college's name, take the real top result's website (skipping
Shiksha, social media, job sites, and other known non-college
domains), then crawl that site for a public email.

Flow per college row:
  1. Read Sheet2 rows where Email is still "Not found"
  2. Search Google (Serper API) for the college name
  3. Take the first real result that isn't shiksha.com or another
     known non-college domain
  4. Crawl that website (two-stage: fast requests fetch first,
     Playwright fallback for JS-heavy sites) looking for a public
     email - same proven logic as gap_filler.py
  5. If no website or no email found, leave Email as "Not found"

Processes rows BOTTOM to TOP, capped at MAX_ROWS_PER_RUN (600) per
run. Since new rows are added daily to the bottom of the sheet by
scraper_shiksha.py, this naturally means each day's run starts at
whatever the current last row is and works upward from there - no
progress file needed, the sheet itself is the source of truth for
where "the bottom" is on any given day.
"""

import os
import re
import sys
import time
import json
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SHEET_TAB_NAME = os.environ.get("SHIKSHA_SHEET_TAB_NAME", "Sheet2")

SERPER_API_KEY = os.environ["SERPER_API_KEY"]

MAX_PAGES_PER_SITE = int(os.environ.get("SHIKSHA_GAP_FILLER_MAX_PAGES_PER_SITE", "6"))
REQUEST_DELAY_SECONDS = 2
PLAYWRIGHT_TIMEOUT_MS = 15000

MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 5

MAX_ROWS_PER_RUN = int(os.environ.get("SHIKSHA_GAP_FILLER_MAX_ROWS", "600"))

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
    "yourdomain.com", "domain.com", "shiksha.com", "w3.org", "schema.org",
)

PRIORITY_LINK_KEYWORDS = (
    "contact", "about", "reach", "connect", "touch", "support",
    "help", "admission", "enquiry",
)

NON_COLLEGE_DOMAINS = (
    "shiksha.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "youtube.com", "wikipedia.org", "google.com",
    "justdial.com", "indiamart.com", "quora.com", "reddit.com",
    "apps.apple.com", "play.google.com", "itunes.apple.com",
    "onelink.me", "app.link", "bit.ly", "tinyurl.com",
    "wa.me", "api.whatsapp.com", "t.me", "pinterest.com",
    "naukri.com", "infoedge.in", "collegedunia.com", "careers360.com",
    "getmyuni.com", "collegedekho.com", "indiatoday.in", "timesofindia.com",
    "ndtv.com", "hindustantimes.com", "glassdoor.com", "indeed.com",
    "amazon.com", "amazon.in", "flipkart.com", "zaubacorp.com",
    "tofler.in", "crunchbase.com",
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
        range=f"{SHEET_TAB_NAME}!A1:E",
    ).execute()
    return result.get("values", [])


def update_row(sheets_service, row_number, website, email):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"{SHEET_TAB_NAME}!B{row_number}:C{row_number}",
                valueInputOption="USER_ENTERED",
                body={"values": [[website, email]]},
            ).execute()
            return True
        except Exception as e:
            print(f"  [warn] Sheets save failed for row {row_number} (attempt {attempt}/{MAX_RETRIES}): {e}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SECONDS)
    print(f"  [error] Could not save row {row_number} after {MAX_RETRIES} attempts - skipping.", file=sys.stderr)
    return False


def search_college_website(college_name):
    for attempt in range(1, 3):
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": SERPER_API_KEY,
                    "Content-Type": "application/json",
                },
                json={"q": college_name, "num": 5},
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
            break
        except Exception as e:
            print(f"  [warn] Serper API error for '{college_name}' (attempt {attempt}/2): {e}", file=sys.stderr)
            if attempt == 2:
                return None
            time.sleep(3)

    items = result.get("organic", [])
    for item in items:
        link = item.get("link", "")
        if not any(bad_domain in link for bad_domain in NON_COLLEGE_DOMAINS):
            return link

    return None


def clean_email_list(raw_emails, allowed_domain=None):
    seen = set()
    cleaned = []
    for email in raw_emails:
        email_clean = email.lower().strip().strip(".").strip(",")
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

    if allowed_domain and cleaned:
        matching = [e for e in cleaned if e.endswith("@" + allowed_domain) or allowed_domain in e.split("@")[-1]]
        if matching:
            return matching
        generic_providers = ("gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "rediffmail.com")
        generic = [e for e in cleaned if e.split("@")[-1] in generic_providers]
        return generic if generic else cleaned

    return cleaned


def extract_emails_from_html(html, allowed_domain=None):
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
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return None, None
        return resp.text, resp.url
    except Exception:
        return None, None


def fetch_with_playwright(url, browser):
    page = None
    try:
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
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
        if "has been closed" in str(e) or "Target page, context or browser has been closed" in str(e):
            raise BrowserDeadError(str(e))
        return None, None


class BrowserDeadError(Exception):
    pass


def find_email_on_site(website_url, browser):
    if not website_url:
        return None

    allowed_domain = urlparse(website_url).netloc.replace("www.", "")

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


def process_one_college(sheets_service, browser, i, row):
    college_name = row[0] if len(row) > 0 else ""
    current_email = row[2] if len(row) > 2 else ""

    email_missing = current_email in ("", "Not found")
    if not college_name or not email_missing:
        return False

    print(f"[Row {i}] Looking up email for '{college_name}'")

    website = search_college_website(college_name)
    if not website:
        print(f"  [not found] No website found via search for '{college_name}'")
        return False

    print(f"  [found] Website: {website}")
    time.sleep(REQUEST_DELAY_SECONDS)

    email = find_email_on_site(website, browser)
    if not email:
        print(f"  [not found] No email found on {website}")
        update_row(sheets_service, i, website, "Not found")
        return True

    print(f"  [found] Email: {email}")
    saved = update_row(sheets_service, i, website, email)
    return saved


def main():
    sheets_service = get_sheets_service()
    rows = get_all_rows(sheets_service)

    if len(rows) <= 1:
        print("No data rows found in Sheet2.")
        return

    data_rows = rows[1:]
    indexed_rows = list(enumerate(data_rows, start=2))
    indexed_rows.reverse()
    indexed_rows = indexed_rows[:MAX_ROWS_PER_RUN]

    print(f"Processing up to {MAX_ROWS_PER_RUN} rows, bottom-to-top. Total rows in sheet: {len(data_rows)}.")

    processed_count = 0
    failed_count = 0

    playwright_ctx = sync_playwright().start()
    browser = playwright_ctx.chromium.launch(headless=True)

    try:
        for i, row in indexed_rows:
            try:
                if process_one_college(sheets_service, browser, i, row):
                    processed_count += 1
            except BrowserDeadError:
                print(f"  [warn] Browser crashed on row {i} - restarting and retrying.", file=sys.stderr)
                try:
                    browser.close()
                except Exception:
                    pass
                browser = playwright_ctx.chromium.launch(headless=True)
                try:
                    if process_one_college(sheets_service, browser, i, row):
                        processed_count += 1
                except Exception as e:
                    print(f"  [error] Row {i} failed again after restart, skipping: {e}", file=sys.stderr)
                    failed_count += 1
            except Exception as e:
                print(f"  [error] Unexpected error on row {i}, skipping: {e}", file=sys.stderr)
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

    print(f"\nDone. Found emails/websites for {processed_count} colleges this run. {failed_count} skipped due to errors.")


if __name__ == "__main__":
    main()
