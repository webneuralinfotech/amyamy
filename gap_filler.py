"""
Company Gap Filler
AMI Dev - Sep 2026 - Full rewrite with hard run limits.
AMI Dev - Oct 2026 - Covers 10 days back, safe date reading, no early stop, checked marker in column G.

Goes through the Google Sheet and finds rows where Website or Email is
missing (says "N/A" or "Not found"). For each of those, searches Google
(via Serper.dev, a legal Google results API) for the company name, grabs
their real website from the top result, then crawls that site's own
navigation links looking for a public email address anywhere on the
site: header, footer, body, contact page, about page, wherever it is.

Two stage reading of each page:
  1. Fast plain HTML fetch (requests), works for most normal sites.
  2. If that finds nothing, a real headless browser (Playwright) that
     runs the site's JavaScript, needed for modern JS built sites where
     the real content does not exist in the raw HTML at all.

Sheet column order must match: Company Name | Website | Email |
Internship URL | Date/Time Scraped | Internshala Company Page |
Gap Filler Checked

BULLETPROOF BEHAVIOUR (kept from the earlier version):
  Saving to Google Sheets retries 3 times before giving up on one row.
  Each company is processed inside its own try/except, so one bad
  company never stops the run.
  If the browser crashes, it is restarted automatically.
  Website search and page fetches get one automatic retry.

AMI Dev - Sep 2026 - WHAT CHANGED IN THE SEP VERSION:
  1. The date cutoff is anchored to the date of the NEWEST company row
     in the sheet, not to today's date.
  2. A hard limit on how many companies a run may search.
  3. A hard limit on total run minutes.
  4. A time limit per company so one slow site cannot eat the run.
  5. More date formats are understood when reading Date/Time Scraped.

AMI Dev - Oct 2026 - WHAT CHANGED IN THIS VERSION:
  1. Default days back raised from 2 to 10.
  2. Dates that could be read two ways (day/month or month/day) now use
     the reading that is not in the future, preferring the more recent
     one. A wrongly read future date can no longer make every real row
     look old and stop the run after a few rows.
  3. Reaching one old row no longer stops the run. Old rows are skipped,
     and the run only stops after STOP_AFTER_OLD_ROWS old rows in a row.
  4. The row limit now counts only companies actually searched, not
     every row looked at, so complete rows do not use up the limit.
  5. After a company is searched, today's date is written in column G
     (Gap Filler Checked). Rows with a value in column G are never
     searched again, which protects Serper credits now that the run
     covers 10 days. Rows that hit an error are not marked, so they are
     retried on the next run.
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

# AMI Dev - Oct 2026 - Default raised from 2 to 10 days. Counts the day of
# the newest company row as day one. The yml value overrides this default.
MAX_DAYS_BACK = int(os.environ.get("GAP_FILLER_MAX_DAYS_BACK", "10"))

# AMI Dev - Sep 2026 - Hard safety limits so a run can never go on for hours.
# AMI Dev - Oct 2026 - MAX_ROWS_PER_RUN now counts only companies actually
# searched (rows with gaps), not every row looked at.
MAX_ROWS_PER_RUN = int(os.environ.get("GAP_FILLER_MAX_ROWS_PER_RUN", "250"))
MAX_RUN_MINUTES = int(os.environ.get("GAP_FILLER_MAX_RUN_MINUTES", "50"))

# AMI Dev - Sep 2026 - Maximum seconds spent crawling one company website
# before giving up on it and moving to the next company.
MAX_SECONDS_PER_COMPANY = int(os.environ.get("GAP_FILLER_MAX_SECONDS_PER_COMPANY", "120"))

# AMI Dev - Oct 2026 - The run only stops when this many rows older than
# the cutoff are found one after another. A single odd or old row in the
# middle of new rows no longer ends the run.
STOP_AFTER_OLD_ROWS = int(os.environ.get("GAP_FILLER_STOP_AFTER_OLD_ROWS", "30"))

# AMI Dev - Oct 2026 - Column G is used to record that a row was already
# searched, so it is never searched again.
CHECKED_COLUMN_LETTER = "G"
CHECKED_COLUMN_INDEX = 6
CHECKED_COLUMN_HEADER = "Gap Filler Checked"

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
# Used to PRIORITIZE which internal links to visit first. This is not a
# strict whitelist, since we fall back to crawling any internal link.
PRIORITY_LINK_KEYWORDS = (
    "contact", "about", "reach", "connect", "touch", "support",
    "help", "team", "office", "location",
)

# Domains that are never a real company's own website: job boards,
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
    # Trade registries, business data aggregators, and directory sites.
    # These list company info but are never the company's own website.
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
    # AMI Dev - Oct 2026 - Range widened from A to F up to A to G so the
    # Gap Filler Checked column is read too.
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB_NAME}!A1:G",
    ).execute()
    return result.get("values", [])


def write_cell_range_with_retry(sheets_service, cell_range, values, label):
    """
    AMI Dev - Oct 2026 - Shared helper that writes values to a sheet range
    with retries. Used for the website/email save and the checked marker.
    """
    body = {"values": values}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"{SHEET_TAB_NAME}!{cell_range}",
                valueInputOption="USER_ENTERED",
                body=body,
            ).execute()
            return True
        except Exception as e:
            print(f"  [warn] Sheets save failed for {label} (attempt {attempt}/{MAX_RETRIES}): {e}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SECONDS)

    print(f"  [error] Could not save {label} after {MAX_RETRIES} attempts. Continuing the run.", file=sys.stderr)
    return False


def update_row(sheets_service, row_number, website, email):
    """
    Saves the website and email for one row into the Google Sheet.

    BULLETPROOF: retries up to MAX_RETRIES times if the connection to
    Google drops mid request. If it still fails after all retries, it logs
    the failure and lets the script continue to the next company instead
    of crashing the entire run.
    """
    return write_cell_range_with_retry(
        sheets_service,
        f"B{row_number}:C{row_number}",
        [[website, email]],
        f"row {row_number} website and email",
    )


def mark_row_checked(sheets_service, row_number):
    """
    AMI Dev - Oct 2026 - Writes today's date into column G of this row so
    the gap filler never searches this company again on later runs.
    """
    today_text = datetime.now().strftime("%Y-%m-%d")
    return write_cell_range_with_retry(
        sheets_service,
        f"{CHECKED_COLUMN_LETTER}{row_number}",
        [[today_text]],
        f"row {row_number} checked marker",
    )


def ensure_checked_header(sheets_service, header_row):
    """
    AMI Dev - Oct 2026 - Adds the "Gap Filler Checked" title to cell G1
    if that cell is empty. Never changes any other header cell.
    """
    current_value = header_row[CHECKED_COLUMN_INDEX] if len(header_row) > CHECKED_COLUMN_INDEX else ""
    if str(current_value).strip():
        return
    write_cell_range_with_retry(
        sheets_service,
        f"{CHECKED_COLUMN_LETTER}1",
        [[CHECKED_COLUMN_HEADER]],
        "header cell G1",
    )


def search_company_website(company_name):
    """
    Uses Serper.dev (real Google search results via a legal API) to find
    the company's real website.

    BULLETPROOF: retries once on any network error before giving up.
    """
    result = {}
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

        # Strip an accidentally attached URL prefix (for example a broken
        # "http://name@domain.com" scraped from a malformed link).
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

    # If we know the company's own domain, prefer emails that match it.
    # This stops picking up a stray email from an ad, widget or embed that
    # belongs to a completely different company.
    if allowed_domain and cleaned:
        matching = [e for e in cleaned if e.endswith("@" + allowed_domain) or allowed_domain in e.split("@")[-1]]
        if matching:
            return matching
        # No exact domain match found. Still return candidates, since a
        # company sometimes legitimately uses a Gmail or Outlook address.
        generic_providers = ("gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "rediffmail.com")
        generic = [e for e in cleaned if e.split("@")[-1] in generic_providers]
        return generic if generic else cleaned

    return cleaned


def extract_emails_from_html(html, allowed_domain=None):
    """Scans the FULL page (header, footer, body, everywhere) for emails."""
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
    Pulls every internal link from a page's actual navigation and footer,
    not a guessed list of page names. Returns them ranked so contact and
    about style pages are tried first, but ANY internal link is included
    as a fallback.
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
    """Fast plain HTML fetch. Returns (html, final_url) or (None, None)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return None, None
        return resp.text, resp.url
    except Exception:
        return None, None


class BrowserDeadError(Exception):
    """Raised when the Playwright browser process itself has crashed."""
    pass


def fetch_with_playwright(url, browser):
    """
    Real headless browser fetch. Runs the page's JavaScript like a normal
    visitor's browser would, then returns the fully built HTML. Needed
    for modern JS built sites (React, Next.js, Vue, Wix) where the raw
    HTML is just an empty shell before JavaScript runs.

    Uses "domcontentloaded" instead of "networkidle". It waits for the
    real page content to be ready, not for every tracking script, ad or
    chat widget to finish loading in the background.

    BULLETPROOF: if the browser itself has crashed, this raises
    BrowserDeadError so the caller can restart it instead of the whole
    script dying.
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


# Sites known to be permanently broken, blocking, or extremely slow to
# load. Skip these entirely rather than wasting a Playwright timeout on
# them every single run.
SKIP_SITES_ENTIRELY = (
    "justdial.com",
)


def find_email_on_site(website_url, browser):
    """
    Universal email finder with two stages per page:
      1. Fast plain HTML fetch (requests), works for most sites.
      2. If nothing found, real headless browser (Playwright), which
         catches modern JS built sites where content only appears after
         JavaScript runs.

    Visits the homepage first, then follows the site's own real internal
    links, prioritizing contact and about style pages, falling back to
    any internal link, until an email is found, MAX_PAGES_PER_SITE is
    hit, or MAX_SECONDS_PER_COMPANY is used up.

    BULLETPROOF: lets BrowserDeadError bubble up to main() so the browser
    can be restarted. Any other error just means no email found on this
    site instead of crashing.
    """
    if not website_url:
        return None

    if any(bad_site in website_url.lower() for bad_site in SKIP_SITES_ENTIRELY):
        print(f"  [skip] Site is in SKIP_SITES_ENTIRELY list, not attempting: {website_url}")
        return None

    # AMI Dev - Sep 2026 - Start of the time budget for this one company.
    site_started_at = time.time()

    allowed_domain = urlparse(website_url).netloc.replace("www.", "")

    # Homepage
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
        # AMI Dev - Sep 2026 - Give up on this company if its time budget is used up.
        if (time.time() - site_started_at) > MAX_SECONDS_PER_COMPANY:
            print(f"  [skip] Time limit of {MAX_SECONDS_PER_COMPANY} seconds reached for this company site. Moving on.")
            return None

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
    Parses the "Date/Time Scraped" column value. Returns a datetime, or
    None if it cannot be read. A None result means the row is treated as
    recent, to be safe, rather than accidentally skipping a new company.

    AMI Dev - Oct 2026 - Every known format is tried, not just the first
    one that works. A value like 10/12/2026 can mean 10 December or
    12 October. Readings that land in the future are thrown away, and of
    the readings left, the most recent one is used. This stops a wrongly
    read future date from making every real row look old, and it never
    makes a new company look older than it really is.
    """
    if not raw_value:
        return None
    raw_value = str(raw_value).strip()

    formats_to_try = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y %I:%M:%S %p",
        "%d/%m/%Y",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y",
        "%d %b %Y",
        "%d %B %Y",
    )

    readings = []
    for fmt in formats_to_try:
        try:
            readings.append(datetime.strptime(raw_value, fmt))
        except ValueError:
            continue

    # Last attempt: ISO style strings with extra parts.
    try:
        iso_value = datetime.fromisoformat(raw_value.replace("Z", ""))
        readings.append(iso_value.replace(tzinfo=None))
    except ValueError:
        pass

    if not readings:
        return None

    # AMI Dev - Oct 2026 - Allow one day of leeway for time zone differences
    # between the scrapers and the GitHub server, then drop future readings.
    latest_allowed = datetime.now() + timedelta(days=1)
    valid_readings = [d for d in readings if d <= latest_allowed]

    if not valid_readings:
        return None

    return max(valid_readings)


def process_one_company(sheets_service, browser, i, row):
    """
    Handles everything for ONE company row: finding the website and email
    and saving the result.

    AMI Dev - Oct 2026 - Now returns a status word instead of True/False:
      "no_gaps"   row is complete or has no company name, nothing searched
      "updated"   a website or email was found and saved
      "no_change" the company was searched but nothing new was found

    BULLETPROOF: this entire function is called inside a try/except in
    main(), so no matter what unexpected error happens for this one
    company, it is logged and the script moves on to the next company
    instead of stopping.
    """
    company_name = row[0] if len(row) > 0 else ""
    current_website = row[1] if len(row) > 1 else ""
    current_email = row[2] if len(row) > 2 else ""

    website_missing = current_website in ("", "N/A")
    email_missing = current_email in ("", "Not found")

    if not company_name or (not website_missing and not email_missing):
        return "no_gaps"

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

    status = "no_change"
    if new_website != current_website or new_email != current_email:
        saved = update_row(sheets_service, i, new_website or "N/A", new_email or "Not found")
        if saved:
            print(f"  [updated] Row {i} saved.")
            status = "updated"

    time.sleep(REQUEST_DELAY_SECONDS)
    return status


def main():
    sheets_service = get_sheets_service()
    rows = get_all_rows(sheets_service)

    if len(rows) <= 1:
        print("No data rows found in sheet.")
        return

    # AMI Dev - Oct 2026 - Make sure column G has its title in row 1.
    ensure_checked_header(sheets_service, rows[0])

    data_rows = rows[1:]
    processed_count = 0
    failed_count = 0
    rows_examined = 0
    companies_searched = 0
    already_checked_count = 0
    old_rows_skipped = 0
    consecutive_old_rows = 0

    # Process from the bottom of the sheet to the top. The newest entries
    # are at the bottom and are the ones that still have gaps, so working
    # from the bottom targets where gap filling is actually needed.
    indexed_rows = list(enumerate(data_rows, start=2))
    indexed_rows.reverse()

    # AMI Dev - Sep 2026 - The cutoff is anchored to the date of the NEWEST
    # company row in the sheet, not to today's date.
    # AMI Dev - Oct 2026 - Future dates are now ignored by parse_scraped_date,
    # so one wrongly read date can no longer push the cutoff into the future.
    parsed_dates = []
    for r in data_rows:
        d = parse_scraped_date(r[4] if len(r) > 4 else "")
        if d is not None:
            parsed_dates.append(d)

    if parsed_dates:
        newest_date = max(parsed_dates)
        newest_day_start = datetime(newest_date.year, newest_date.month, newest_date.day)
        cutoff_date = newest_day_start - timedelta(days=max(MAX_DAYS_BACK, 1) - 1)
        print(f"Newest company row is dated {newest_date.date()}. Only processing rows on or after {cutoff_date.date()} ({MAX_DAYS_BACK} day(s) counting the newest day).")
    else:
        # AMI Dev - Sep 2026 - Fallback if no date could be read from any row.
        cutoff_date = datetime.now() - timedelta(days=MAX_DAYS_BACK)
        print(f"Could not read any dates from the sheet. Falling back to cutoff {cutoff_date.date()}.")

    print(f"Safety limits for this run: {MAX_ROWS_PER_RUN} companies searched, {MAX_RUN_MINUTES} minutes, {MAX_SECONDS_PER_COMPANY} seconds per company, stop after {STOP_AFTER_OLD_ROWS} old rows in a row.")

    run_started_at = time.time()

    playwright_ctx = sync_playwright().start()
    browser = playwright_ctx.chromium.launch(headless=True)

    try:
        for i, row in indexed_rows:
            # AMI Dev - Oct 2026 - The limit now counts companies actually searched.
            if companies_searched >= MAX_ROWS_PER_RUN:
                print(f"Reached the limit of {MAX_ROWS_PER_RUN} companies searched for this run. Remaining rows will be picked up on the next run.")
                break
            if (time.time() - run_started_at) > MAX_RUN_MINUTES * 60:
                print(f"Reached the time limit of {MAX_RUN_MINUTES} minutes for this run. Remaining rows will be picked up on the next run.")
                break
            rows_examined += 1

            scraped_date_raw = row[4] if len(row) > 4 else ""
            scraped_date = parse_scraped_date(scraped_date_raw)

            # AMI Dev - Oct 2026 - An old row is skipped instead of ending the run.
            # The run only stops after STOP_AFTER_OLD_ROWS old rows in a row.
            if scraped_date is not None and scraped_date < cutoff_date:
                old_rows_skipped += 1
                consecutive_old_rows += 1
                if consecutive_old_rows >= STOP_AFTER_OLD_ROWS:
                    print(f"[Row {i}] Found {STOP_AFTER_OLD_ROWS} rows in a row older than the cutoff {cutoff_date.date()}. Stopping run here.")
                    break
                continue
            consecutive_old_rows = 0

            # AMI Dev - Oct 2026 - Skip rows already searched on an earlier run.
            checked_value = row[CHECKED_COLUMN_INDEX] if len(row) > CHECKED_COLUMN_INDEX else ""
            if str(checked_value).strip():
                already_checked_count += 1
                continue

            result = None
            try:
                result = process_one_company(sheets_service, browser, i, row)
            except BrowserDeadError:
                # The browser process crashed. Restart it and retry this
                # SAME row once before moving on, so we do not lose a
                # company just because of a browser hiccup.
                print(f"  [warn] Browser crashed on row {i}. Restarting browser and retrying this row.", file=sys.stderr)
                try:
                    browser.close()
                except Exception:
                    pass
                browser = playwright_ctx.chromium.launch(headless=True)
                try:
                    result = process_one_company(sheets_service, browser, i, row)
                except Exception as e:
                    print(f"  [error] Row {i} failed again after browser restart, skipping: {e}", file=sys.stderr)
                    failed_count += 1
            except Exception as e:
                # Catch all: anything unexpected on this one company gets
                # logged, and the run continues to the next company.
                print(f"  [error] Unexpected error on row {i} ('{row[0] if row else ''}'), skipping: {e}", file=sys.stderr)
                failed_count += 1

            # AMI Dev - Oct 2026 - Count the search and mark the row as checked
            # in column G. Rows that errored are not marked, so they are
            # retried on the next run.
            if result in ("updated", "no_change"):
                companies_searched += 1
                if result == "updated":
                    processed_count += 1
                mark_row_checked(sheets_service, i)
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            playwright_ctx.stop()
        except Exception:
            pass

    elapsed_minutes = round((time.time() - run_started_at) / 60, 1)
    print(f"\nDone in {elapsed_minutes} minutes. Examined {rows_examined} rows. Searched {companies_searched} companies. Filled gaps for {processed_count} companies. Skipped {already_checked_count} rows already checked earlier and {old_rows_skipped} rows older than the cutoff. {failed_count} companies skipped due to errors.")


if __name__ == "__main__":
    main()
