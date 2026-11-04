import json
import base64
import csv
import os
import hashlib
import time
from collections import deque
from typing import Callable, Optional
import re
import sys
import webbrowser
import pickle
import pandas as pd
import tempfile
import shutil
import requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urljoin
from markdownify import markdownify as md

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import WebDriverException, TimeoutException, ElementNotInteractableException, StaleElementReferenceException, NoSuchElementException, ElementClickInterceptedException
from selenium.webdriver.support.wait import WebDriverWait

from nicegui import ui, run

if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.abspath(os.path.dirname(__file__))

def get_default_output_dir(input_file=False):
    """Gets a safe user directory to save scrapes cross-platform."""
    user_home = os.path.expanduser("~")
    base_dir = os.path.join(user_home, "Documents", "Kenniskrabber")
    os.makedirs(base_dir, exist_ok=True)

    if input_file:
        return os.path.join(base_dir, "query_file.csv")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    default_path = os.path.join(base_dir, "scrapes", f"scrape_{timestamp}")
    os.makedirs(default_path, exist_ok=True)

    return default_path

SELECTORS_GITHUB_URL = "https://raw.githubusercontent.com/digitalmethodsinitiative/kenniskrabber/master/css-selectors.json"
LOCAL_SELECTORS_PATH = os.path.join(base_path, "css-selectors.json")

APP_VERSION = "0.82"
# Run with --debug to skip the update check and always use the local css-selectors.json.
DEBUG = "--debug" in sys.argv
RELEASES_API_URL = "https://api.github.com/repos/digitalmethodsinitiative/kenniskrabber/releases/latest"
RELEASES_PAGE_URL = "https://github.com/digitalmethodsinitiative/kenniskrabber/releases/latest"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"

def _version_tuple(tag):
    """Turn a version tag like 'v0.6' or '1.2.0' into a comparable int tuple."""
    return tuple(int(n) for n in re.findall(r"\d+", tag or "")) or (0,)

def check_for_update():
    """Ask GitHub for the latest release and compare it to APP_VERSION.

    Returns (latest_version, release_url) when a newer release is available,
    otherwise None. Fails silently (returns None) on any network/parse error
    so a failed check never disrupts the app.
    """
    try:
        response = requests.get(RELEASES_API_URL, timeout=5)
        response.raise_for_status()
        latest_tag = response.json().get("tag_name", "")
    except Exception:
        return None

    if not latest_tag:
        return None

    current, latest = _version_tuple(APP_VERSION), _version_tuple(latest_tag)
    # Pad to equal length so e.g. (1, 0) and (1, 0, 0) compare as equal.
    length = max(len(current), len(latest))
    current += (0,) * (length - len(current))
    latest += (0,) * (length - len(latest))

    if latest > current:
        return latest_tag.lstrip("vV"), RELEASES_PAGE_URL
    return None

class GoogleAIScraper:

    def __init__(self, scrape_mode="both", base_url=None, profile="", iterate_queries=set(),
                 inserted_queries=None, query_file=None, results_file="", output_dir="", offset=0,
                 shuffle_queries=False, scrape_claims=False, scrape_sources=False, follow_goto_links=True,
                 throttled_strings=None, ao_selectors=None, am_selectors=None,
                 bg_selectors=None, dk_selectors=None, scrape_bing=False, scrape_duckduckgo=False,
                 progress_callback=None, log_callback=None):
        self.scrape_mode = scrape_mode
        self.base_url = base_url if base_url else "https://www.google.com/"
        self.driver = None
        self.wait = None
        self.iterate_queries = iterate_queries
        self.inserted_queries = inserted_queries
        self.query_file = query_file
        self.results_file = results_file
        self.output_dir = output_dir
        self.shuffle_queries = shuffle_queries
        self.scrape_claims = scrape_claims
        self.scrape_sources = scrape_sources
        self.follow_goto_links = follow_goto_links
        self.offset = offset
        self.throttled_strings = [t_s.strip() for t_s in throttled_strings.split(",")] if throttled_strings else [
            "Try again later", "Something went wrong"]
        self.ao_selectors = {**(ao_selectors or {})}
        self.am_selectors = {**(am_selectors or {})}
        self.bg_selectors = {**(bg_selectors or {})}
        self.dk_selectors = {**(dk_selectors or {})}
        self.scrape_bing = scrape_bing
        self.scrape_duckduckgo = scrape_duckduckgo
        self.bing_base_url = "https://www.bing.com/"
        self.duckduckgo_base_url = "https://duckduckgo.com/"
        self.progress_callback = progress_callback
        self.log_callback: Optional[Callable] = log_callback
        self.profile = profile
        self.is_running = False

        self.finished_ai_overview_queries = set()
        self.finished_ai_mode_queries = set()
        self.finished_bing_queries = set()
        self.finished_duckduckgo_queries = set()
        self.source_urls_to_scrape = set()
        self.resolved_goto_links = {}  # Google /goto link -> actual URL (or None)
        self.goto_notice_logged = False
        self.http_session = requests.Session()
        self.http_session.headers["User-Agent"] = USER_AGENT
        self.scrape_durations = deque(maxlen=20)
        self.query_extra_data = {}  # query_id -> dict of extra columns from query file

    def log(self, msg, classes=None):
        """Send messages to the NiceGUI log UI"""
        if self.log_callback and self.log_callback is not print:
            self.log_callback(f"> {str(msg)}", classes=classes)
        else:
            print(msg)

    @staticmethod
    def load_selectors(log=print):
        """Return (ao_selectors, am_selectors, bg_selectors, dk_selectors) from the merged css-selectors.json.

        Prefers the latest version fetched from GitHub so selectors can be
        updated without shipping a new build; falls back to the bundled local
        file if the fetch fails. Status is reported through the given log
        callback so it shows up in the user output log.
        """
        merged = None
        if DEBUG:
            log("Debug mode: using local CSS selectors.")
        else:
            try:
                response = requests.get(SELECTORS_GITHUB_URL, timeout=5)
                response.raise_for_status()
                merged = response.json()
                log("Loaded CSS selectors from GitHub.")
            except Exception as e:
                log(f"Could not fetch CSS selectors from GitHub ({e}); using local file.")
        # Local selectors fill in keys the GitHub version doesn't have (yet)
        with open(LOCAL_SELECTORS_PATH, "r", encoding="utf-8") as file:
            merged = {**json.load(file), **(merged or {})}

        ao_selectors = {k: v for k, v in merged.items() if k.startswith("ao_")}
        am_selectors = {k: v for k, v in merged.items() if k.startswith("am_")}
        bg_selectors = {k: v for k, v in merged.items() if k.startswith("bg_")}
        dk_selectors = {k: v for k, v in merged.items() if k.startswith("dk_")}
        return ao_selectors, am_selectors, bg_selectors, dk_selectors

    def get_driver(self, custom_profile=""):
        options = webdriver.FirefoxOptions()
        options.add_argument(f'--user-agent={USER_AGENT}')

        if custom_profile:
            self.log(f"Copying custom profile {custom_profile}...")
            temp_profile = tempfile.mkdtemp()
            shutil.copytree(custom_profile, temp_profile, dirs_exist_ok=True)
            options.add_argument(f'--profile={temp_profile}')

        driver = None
        max_retries = 3

        for attempt in range(max_retries):
            try:
                driver = webdriver.Firefox(options=options)
                break  # Success, exit the retry loop

            except WebDriverException as e:
                error_msg = str(e).lower()

                # Catch the specific background update exit
                if "process unexpectedly closed with status 0" in error_msg:
                    if attempt < max_retries - 1:
                        wait_time = 10 * (attempt + 1) # Progressive backoff: 10s, 20s...
                        self.log(f"Firefox appears to be installing an update. Waiting {wait_time}s to retry (Attempt {attempt + 1}/{max_retries})...", classes="text-orange")
                        time.sleep(wait_time)
                        continue
                    else:
                        raise Exception("Firefox update took too long. Please open Firefox manually to let it update, then try again.")

                # Catch standard missing binary errors
                elif "binary location" in error_msg or "not found" in error_msg or "firefox" in error_msg:
                    raise Exception("Firefox could not be found. Please install Mozilla Firefox to run this scraper.")

                # Catch anything else
                else:
                    raise Exception(f"Failed to start browser: {str(e)}")

        time.sleep(1)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(120)
        driver.implicitly_wait(.1)

        if os.path.exists("stealthify.js"):
            driver.execute_script(open("stealthify.js").read())

        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        return driver

    def prepare(self):
        google_on = self.scrape_mode in ["ai_overviews_&_ai_modes", "ai_overviews", "ai_modes"]
        if not google_on and not self.scrape_bing and not self.scrape_duckduckgo:
            self.log("Nothing selected to collect. Tick Google, Bing and/or DuckDuckGo.", classes="text-red")
            return False

        # Get queries
        n_urls = 0
        self.log("Getting queries")
        self.iterate_queries = set()
        if self.query_file and os.path.isfile(self.query_file):
            with open(self.query_file, "r", encoding="utf-8") as in_csv:
                reader = csv.DictReader(in_csv)
                for row in reader:
                    n_urls += 1
                    if self.offset and n_urls < self.offset:
                        continue

                    query = row.get("query", list(row.values())[0])
                    query_id = row.get("id", hashlib.md5(query.encode()).hexdigest())
                    self.iterate_queries.add((query_id, query))

                    # Store extra columns (all columns except 'query' and 'id')
                    extra = {k: v for k, v in row.items() if k not in ("query", "id")}
                    if extra:
                        self.query_extra_data[query_id] = extra

        elif self.inserted_queries:
            for line in self.inserted_queries.split("\n"):
                n_urls += 1
                if self.offset and n_urls < self.offset:
                    continue
                query = line.strip()
                if not query:
                    continue
                query_id = hashlib.md5(query.encode()).hexdigest()
                self.iterate_queries.add((query_id, query))

        if not self.iterate_queries:
            self.log("No queries found. Please add search queries in the text box or as a csv and try again.", classes="text-red")
            return False
        else:
            self.log(f"Found {len(self.iterate_queries)} unique queries to scrape.", classes="text-blue")

        # Start browser
        self.log("Launching Firefox browser. Please wait...", classes="text-orange")
        try:
            self.driver = self.get_driver(custom_profile=self.profile)
            self.wait = WebDriverWait(self.driver, timeout=2)

            if not os.path.isdir(self.output_dir):
                os.makedirs(self.output_dir)

            self.results_file = os.path.join(self.output_dir, f"google_ai_results.json")

            self.install_cookie_blocker()

            self.driver.get(f"{self.base_url}search?q=is scraping legal")
            self.check_for_captcha()

            cookie_path = os.path.join(os.path.expanduser("~"), "Documents", "Kenniskrabber", "search_cookies.pkl")
            if not os.path.isfile(cookie_path):
                with open(cookie_path, "wb") as out_cookies:
                    pickle.dump(self.driver.get_cookies(), out_cookies)
                    self.log("Saved cookies")

            if os.path.isfile(self.results_file):
                self.log("Found existing results file. Assuming you want to skip already scraped queries.", classes="text-orange")
                with open(self.results_file, "r", encoding="utf-8") as f:
                    for line in f.readlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            finished_query = json.loads(line)
                            if finished_query.get("mode") == "ai_overview":
                                self.finished_ai_overview_queries.add(finished_query["id"])
                            if finished_query.get("mode") == "ai_mode":
                                self.finished_ai_mode_queries.add(finished_query["id"])
                            if finished_query.get("mode") == "bing":
                                self.finished_bing_queries.add(finished_query["id"])
                            if finished_query.get("mode") == "duckduckgo":
                                self.finished_duckduckgo_queries.add(finished_query["id"])
                        except Exception as parse_err:
                            self.log(f"Warning: could not parse results line: {parse_err}", classes="text-orange")
                self.log(f"Already collected {len(self.finished_ai_overview_queries)} AI Overviews and {len(self.finished_ai_mode_queries)} AI Modes", classes="text-orange")


            self.log("Browser ready.", classes="text-blue")
            self.log("Please complete any CAPTCHAs or logins in the Firefox window, then press '2. Start Scraping'.", classes="text-orange")
            return True

        except Exception as e:
            self.log(f"ERROR: {str(e)}", classes="text-red")
            if "Please install Mozilla Firefox" not in str(e):
                import traceback
                self.log(traceback.format_exc())
            return False

    def process(self):
        self.is_running = True

        try:
            count = 0
            for query_and_id in self.iterate_queries:
                count += 1
                if not self.is_running:
                    self.log("Scraping stopped by user.", classes="text-red")
                    break

                page_id = query_and_id[0]
                query = query_and_id[1]
                self.log(f"Scraping query {count}/{len(self.iterate_queries)}: {query}")

                if self.progress_callback:
                    self.progress_callback(count, len(self.iterate_queries))

                # AI Overviews
                if self.scrape_mode in ["ai_overviews_&_ai_modes", "ai_overviews"]:
                    if page_id not in self.finished_ai_overview_queries:
                        start = time.time()
                        mode = "ai_overviews"
                        query_url = f"{self.base_url}search?q={self.get_url_string(query)}"
                        self.open_page(query_url)

                        result = self.parse_ai_overview(page_id, query)

                        if result:
                            # Merge extra query file columns (skip clashing keys)
                            for k, v in self.query_extra_data.get(page_id, {}).items():
                                if k not in result:
                                    result[k] = v
                            self.log(f"Parsed AI Overview for: {query}", classes="text-blue")
                            with open(self.results_file, "a", encoding="utf-8") as out_file:
                                out_file.write(json.dumps(result) + "\n")

                        self.save_html(query, page_id, mode)
                        self.save_screenshot(query=query, page_id=page_id, mode=mode)
                        self.finished_ai_overview_queries.add(page_id)
                        self.print_elapsed_time(start, len(self.iterate_queries), count)
                    else:
                        self.log("Already scraped AI Overview data, skipping", classes="text-orange")

                # AI Modes
                if self.scrape_mode in ["ai_overviews_&_ai_modes", "ai_modes"]:
                    mode = "ai_modes"
                    if page_id not in self.finished_ai_mode_queries:
                        start = time.time()
                        query_url = f"{self.base_url}search?udm=50&q={self.get_url_string(query)}"
                        self.open_page(query_url)

                        result = self.parse_ai_mode(page_id, query)

                        if result:
                            # Merge extra query file columns (skip clashing keys)
                            for k, v in self.query_extra_data.get(page_id, {}).items():
                                if k not in result:
                                    result[k] = v
                            self.log(f"Parsed AI Mode for: {query}", classes="text-blue")
                            with open(self.results_file, "a", encoding="utf-8") as out_file:
                                out_file.write(json.dumps(result) + "\n")

                        self.save_html(query, page_id, mode)
                        self.save_screenshot(query=query, page_id=page_id, mode=mode)
                        self.finished_ai_mode_queries.add(page_id)
                        self.print_elapsed_time(start, len(self.iterate_queries), count)
                    else:
                        self.log("Already scraped AI Mode data, skipping", classes="text-orange")

                # Bing (Copilot Search) and DuckDuckGo (DuckAssist)
                if self.scrape_bing:
                    self.scrape_extra_engine("bing", page_id, query, count)
                if self.scrape_duckduckgo:
                    self.scrape_extra_engine("duckduckgo", page_id, query, count)

            # JSON to CSV Conversion
            if self.is_running and self.results_file:
                self.log("Converting results JSON to csv")
                csv_filename = self.results_file[:-5] + ".csv"
                write_mode = "w" if not os.path.isfile(csv_filename) else "a"
                if write_mode == "a":
                    self.log("Results CSV already exists, appending")
                with open(self.results_file, "r", encoding="utf-8") as in_json:
                    with open(csv_filename, write_mode, encoding="utf-8-sig", newline="") as out_csv:
                        write_header = True if write_mode == "w" else False
                        writer = None # Pre-define writer
                        all_lines = [json.loads(line) for line in in_json.readlines() if line.strip()]

                        if all_lines:
                            # Collect all fieldnames across all rows to handle variable extra columns
                            all_keys = list(all_lines[0].keys())
                            for csv_line in all_lines[1:]:
                                for k in csv_line.keys():
                                    if k not in all_keys:
                                        all_keys.append(k)

                            writer = csv.DictWriter(out_csv, fieldnames=all_keys, extrasaction='ignore')
                            if write_header:
                                writer.writeheader()

                            for csv_line in all_lines:
                                csv_line["sources"] = self.source_to_string(csv_line["sources"]) if csv_line.get("sources") else ""
                                if "claims" in csv_line:
                                    csv_line["claims"] = self.claims_to_string(csv_line["claims"])
                                writer.writerow(csv_line)

                self.log("Google scraping completed.", classes="text-blue")

                # Scrape source links if selected
                if self.scrape_sources and self.source_urls_to_scrape:
                    self.process_sources()

            else:
                self.log("No AI output found...", classes="text-red")
        except Exception as e:
            self.log(f"Error: {str(e)}", classes="text-red")
            print(str(e))
            import traceback
            self.log(traceback.format_exc())
        finally:
            self.stop()


    def process_sources(self):
        """ Worker to iterate through extracted source URLs."""
        self.log(f"Scraping {len(self.source_urls_to_scrape)} linked source pages.", classes="text-blue")

        for i, url in enumerate(list(self.source_urls_to_scrape)):
            if not self.is_running:
                break

            self.log(f"Scraping source {i+1}/{len(self.source_urls_to_scrape)}: {url}")
            try:
                self.driver.get(url)

                # Check for generic Cloudflare/hCaptcha blocks
                self.check_for_generic_captcha()

                # Give I Don't Care About Cookies 3 seconds
                time.sleep(3)

                self.save_screenshot(output_dir="/screenshots_sources")
                self.save_html(output_dir="/html_sources")

            except TimeoutException:
                self.log(f"Timeout loading {url}, skipping.", classes="text-orange")
            except Exception as e:
                self.log(f"Error scraping {url}: {e}", classes="text-red")

    def parse_ai_overview(self, page_id, query) -> dict:
        time.sleep(1)
        sel = self.ao_selectors

        while True:
            ai_overview = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_container"])
            if ai_overview and ai_overview[0].is_displayed():
                ai_overview = ai_overview[0]

                try:
                    # Wait up to 10 seconds for the ghost to either appear and disappear,
                    # OR to never appear at all (meaning content was already fully loaded)
                    WebDriverWait(self.driver, 10).until(
                        lambda d: len(d.find_elements(By.CSS_SELECTOR, "#folsrch-ghost")) == 0
                    )
                except TimeoutException:
                    self.log("Warning: #folsrch-ghost still present after timeout, continuing anyway",
                             classes="text-orange")

                # Answer data object
                ai_overview_data = {
                    "mode": "ai_overview",
                    "id": page_id,
                    "from_url": self.driver.current_url,
                    "query": query,
                    "timestamp_scraped": datetime.now().isoformat(),
                    "timestamp_scraped_unix": int(datetime.timestamp(datetime.now())),
                    "not_available": False,
                    "text": "",
                    "main_answer": "",
                    "claims": "",
                    "sources": [],
                    "disclaimer": ""
                }

                failed_elements = ai_overview.find_elements(by=By.CSS_SELECTOR, value=sel["ao_failed_elements"])
                throttled = None
                ai_overview_inner_text = ai_overview.find_elements(by=By.CSS_SELECTOR, value=sel["ao_inner_text"])
                if ai_overview_inner_text:
                    ai_overview_inner_text = ai_overview_inner_text[0].get_attribute("innerHTML")
                if ai_overview_inner_text:
                    for throttled_string in self.throttled_strings:
                        if throttled_string in ai_overview_inner_text:
                            throttled = throttled_string
                            break

                show_more_button = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_show_more"])
                if not show_more_button and (failed_elements or throttled):
                    if failed_elements:
                        ai_overview_data["text"] = ai_overview.text
                    else:
                        ai_overview_data["text"] = ai_overview.get_attribute("innerHTML")
                    ai_overview_data["not_available"] = True

                    if throttled:
                        self.log(f"Throttled (found: '{throttled}'), trying again in 10 minutes", classes="text-orange")
                        time.sleep(600)
                        self.driver.refresh()
                        self.check_for_captcha()
                        continue

                # Click the show more button
                for attempt in range(3):  # try to click three times
                    try:
                        show_more_button[0].click()
                        break
                    except (StaleElementReferenceException, ElementNotInteractableException, NoSuchElementException, ElementClickInterceptedException, IndexError):
                        self.driver.execute_script("window.scrollTo(0, 0);")
                        self.driver.execute_script("document.elementFromPoint(0, 0).click();")
                        show_more_button = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_show_more"])
                        continue_notice = "trying again" if attempt < 2 else "skipping"
                        time.sleep(1)

                # Get contents of AI Overview
                ai_overview_inner = None
                retry_ai_overview_count = 0
                while not ai_overview_inner:
                    ai_overview_inner = ai_overview.find_elements(by=By.CSS_SELECTOR, value=sel["ao_inner_text"])
                    retry_ai_overview_count += 1
                    if retry_ai_overview_count > 5:
                        self.log("AI Overview hidden or not found, skipping...", classes="text-orange")
                        return {}
                    if not ai_overview_inner:
                        time.sleep(0.5)

                # Remove prompt box
                prompt_input = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_prompt_input"])
                if prompt_input:
                    self.driver.execute_script("""
                                var element = arguments[0];
                                element.parentNode.removeChild(element);
                                """, prompt_input[0])

                # Get text (with the inline source badge buttons removed)
                ai_overview_outer = self.html_without_source_buttons(ai_overview_inner[0])
                ai_overview_contents_md = md(ai_overview_outer, strip=["a", "img", "button", "span[id]"])
                ai_overview_data["text"] = ai_overview_contents_md

                # Get highlighted answer
                main_answer = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_main_claim"])
                ai_overview_data["main_answer"] = main_answer[0].text if main_answer else ""

                # Get sources
                urls = []
                url_divs = []
                carousel_divs = None
                show_more_urls_button = ai_overview.find_elements(by=By.CSS_SELECTOR, value=sel["ao_show_more_urls"])

                if show_more_urls_button:
                    for show_more_url_button in show_more_urls_button:
                        try:
                            self.wait.until(lambda d: any(
                                el.is_displayed() for el in
                                d.find_elements(By.CSS_SELECTOR, sel["ao_show_more_urls"])
                            ))
                            show_more_url_button.click()
                            url_divs = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_url_divs"])
                        except (TimeoutException, ElementNotInteractableException):
                            url_divs = self.driver.find_elements(by=By.CSS_SELECTOR,
                                                                 value=sel["ao_url_divs_not_expandable"])
                            self.log("Warning: Could not expand URL box, continuing anyway", classes="text-orange")

                else:
                    carousel_divs = ai_overview.find_elements(by=By.CSS_SELECTOR, value=sel["ao_url_divs_carousel"])
                    if carousel_divs:
                        url_divs = carousel_divs
                    else:
                        url_divs = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_url_divs_not_expandable"])

                for url_div in url_divs:
                    if url_div.is_displayed():
                        url_div_title = url_div.find_elements(by=By.CSS_SELECTOR, value=sel["ao_url_title"] + " > span" if not carousel_divs else sel["ao_url_title"])[0].text
                        url_div_a = url_div.find_element(by=By.CSS_SELECTOR, value="a")
                        url_div_url = url_div_a.get_attribute("href")
                        url_div_description = url_div.find_elements(by=By.CSS_SELECTOR, value=sel["ao_url_description"])
                        url_div_description += url_div.find_elements(by=By.CSS_SELECTOR, value=sel["ao_url_description_fallback"])
                        description = url_div_description[0].text if url_div_description else ""

                        url = {
                            "title": url_div_title,
                            "description": description,
                            **self.source_link_fields(url_div_url),
                        }
                        urls.append(url)
                        self.source_urls_to_scrape.add(url.get("translated_url") or url["url"]) # Add to queue

                ai_overview_data["sources"] = urls

                # Get claims
                if self.scrape_claims:
                    ai_overview_data["claims"] = self.get_claims(ai_overview_inner[0])

                # Get disclaimer at the bottom; at the moment one is visible, one is hidden
                disclaimers = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_disclaimer"])
                for disclaimer in disclaimers:
                    if disclaimer and disclaimer.is_displayed():
                        ai_overview_data["disclaimer"] = disclaimer.text
                        break

                # Reset stuff; scroll to top and click top left
                self.driver.execute_script("window.scrollTo(0, 0);")
                self.driver.execute_script("document.elementFromPoint(0, 0).click();")
                return ai_overview_data
            else:
                return {}

    def parse_ai_mode(self, page_id, query) -> dict:
        time.sleep(4)
        sel = self.am_selectors

        while True:
            ai_mode = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["am_container"])
            if ai_mode and ai_mode[0].is_displayed():
                ai_mode = ai_mode[0]
                ai_mode_inner = ai_mode.find_elements(by=By.CSS_SELECTOR, value=sel["am_inner_text"])[0]
                ai_mode_inner_text = ai_mode_inner.text

                if not ai_mode.find_elements(by=By.CSS_SELECTOR, value=sel["am_processed_answer"]):
                    retries = 0
                    while retries <= 5:
                        retries += 1
                        self.log("AI Mode may not be done generating, waiting 2 secs...", classes="text-orange")
                        time.sleep(2)
                        generated = True if ai_mode.find_elements(by=By.CSS_SELECTOR, value=sel["am_processed_answer"]) else False
                        if generated:
                            break

                time.sleep(2)

                ai_mode_data = {
                    "mode": "ai_mode",
                    "id": page_id,
                    "from_url": self.driver.current_url,
                    "query": query,
                    "timestamp_scraped": datetime.now().isoformat(),
                    "timestamp_scraped_unix": int(datetime.timestamp(datetime.now())),
                    "not_available": True,
                    "text": "",
                    "sources": [],
                    "disclaimer": ""
                }

                throttled = None
                for throttled_string in self.throttled_strings:
                    if throttled_string in ai_mode_inner_text:
                        throttled = throttled_string
                        break

                something_went_wrong = ai_mode.find_elements(by=By.CSS_SELECTOR, value=sel["am_something_went_wrong"])
                if throttled or something_went_wrong:
                    self.log(f"Throttled, trying again in 10 minutes. Keep the browser window open.", classes="text-orange")
                    time.sleep(600)
                    self.driver.refresh()
                    self.check_for_captcha()
                    continue

                url_divs = []
                show_more_urls_button = ai_mode.find_elements(by=By.CSS_SELECTOR, value=sel["am_show_more_urls"])
                if show_more_urls_button:
                    for show_more_url_button in show_more_urls_button:
                        try:
                            self.wait.until(lambda d: any(
                                el.is_displayed() for el in
                                d.find_elements(By.CSS_SELECTOR, sel["am_show_more_urls"])
                            ))
                            show_more_url_button.click()
                            url_divs = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["am_url_divs"])
                        except (TimeoutException, ElementNotInteractableException):
                            url_divs = self.driver.find_elements(by=By.CSS_SELECTOR,
                                                                 value=sel["am_url_divs_not_expandable"])
                            self.log("Warning: Could not expand URL box, continuing anyway", classes="text-orange")

                ai_mode_html = ai_mode.find_elements(by=By.CSS_SELECTOR, value=sel["am_answers"])

                if not ai_mode_html:
                    time.sleep(6)
                    ai_mode_html = ai_mode.find_elements(by=By.CSS_SELECTOR, value=sel["am_answers"])

                # Remove prompt box
                prompt_input = self.driver.find_elements(by=By.CSS_SELECTOR, value=self.ao_selectors["ao_prompt_input"])
                if prompt_input:
                    self.driver.execute_script("""
                                var element = arguments[0];
                                element.parentNode.removeChild(element);
                                """, prompt_input[0])

                # Get text (with the inline source badge buttons removed)
                ai_mode_html = self.html_without_source_buttons(ai_mode_html[0])
                ai_mode_contents_md = md(ai_mode_html, strip=["a", "img"])
                ai_mode_data["text"] = ai_mode_contents_md.split("\nAI-reac")[0]

                # Get main claim
                main_answer = self.driver.find_elements(by=By.CSS_SELECTOR, value=self.am_selectors["am_main_claim"])
                ai_mode_data["main_answer"] = main_answer[0].text if main_answer else ""

                urls = []
                for url_div in url_divs:
                    url_div_a = url_div.find_element(by=By.CSS_SELECTOR, value="a")
                    url_div_url = url_div_a.get_attribute("href")
                    url_div_description = url_div.find_elements(by=By.CSS_SELECTOR, value=sel["am_url_description"])
                    description = url_div_description[0].text if url_div_description else ""

                    url = {
                        "title": url_div_a.get_attribute("aria-label"),
                        "description": description,
                        **self.source_link_fields(url_div_url),
                    }
                    urls.append(url)
                    self.source_urls_to_scrape.add(url.get("translated_url") or url["url"]) # Add to queue

                ai_mode_data["sources"] = urls

                # Get claims
                if self.scrape_claims:
                    ai_mode_data["claims"] = self.get_claims(ai_mode_inner, mode="ai_mode")

                # Get disclaimer
                disclaimers = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["am_disclaimer"])
                for disclaimer in disclaimers:
                    if disclaimer and disclaimer.is_displayed():
                        ai_mode_data["disclaimer"] = disclaimer.text
                        break

                ai_mode_data["not_available"] = False
                break

        return ai_mode_data if ai_mode_data and ai_mode_data.get("text") else {}


    def scrape_extra_engine(self, engine, page_id, query, count):
        """Scrape one query on Bing or DuckDuckGo; same flow as the Google blocks in process()."""
        finished = self.finished_bing_queries if engine == "bing" else self.finished_duckduckgo_queries
        if page_id in finished:
            self.log(f"Already scraped {engine} data, skipping", classes="text-orange")
            return

        start = time.time()
        try:
            if engine == "bing":
                self.search_bing(query)
                result = self.parse_bing_answer(page_id, query)
            else:
                self.open_page(f"{self.duckduckgo_base_url}?q={self.get_url_string(query)}")
                result = self.parse_duckduckgo_answer(page_id, query)
        except Exception as e:
            self.log(f"Error scraping {engine} for '{query}': {e}", classes="text-red")
            result = {}

        if result:
            # Merge extra query file columns (skip clashing keys)
            for k, v in self.query_extra_data.get(page_id, {}).items():
                if k not in result:
                    result[k] = v
            self.log(f"Parsed {engine} answer for: {query}", classes="text-blue")
            with open(self.results_file, "a", encoding="utf-8") as out_file:
                out_file.write(json.dumps(result) + "\n")
        else:
            self.log(f"No {engine} answer found for: {query}", classes="text-orange")

        self.save_html(query=query, page_id=page_id, mode=engine)
        self.save_screenshot(query=query, page_id=page_id, mode=engine)
        finished.add(page_id)
        self.print_elapsed_time(start, len(self.iterate_queries), count)

    def wait_for_page_ready(self, timeout=10):
        """Poll document.readyState instead of a flat sleep."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.driver.execute_script("return document.readyState") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.2)

    def search_bing(self, query):
        """Type the query into bing.com's homepage search box, like a person would.
        Falls back to a direct search URL if the box can't be found or used."""
        sel = self.bg_selectors
        self.open_page(self.bing_base_url)
        self.check_for_generic_captcha()

        try:
            search_box = self.driver.find_element(By.CSS_SELECTOR, sel["bg_search_box"])
            search_box.click()
            search_box.clear()
            search_box.send_keys(query)
            time.sleep(0.3)
            search_box.send_keys(Keys.ENTER)
            self.wait_for_page_ready()
            self.check_for_generic_captcha()
        except (NoSuchElementException, ElementNotInteractableException, ElementClickInterceptedException) as e:
            self.log(f"Couldn't use Bing's homepage search box ({e}); falling back to a direct search URL.", classes="text-orange")
            self.open_page(f"{self.bing_base_url}search?q={self.get_url_string(query)}")

    def parse_bing_answer(self, page_id, query) -> dict:
        """Bing's AI answer (Copilot Search). Each claim's citations are plain inline
        <a class="md_citlink"> links inside the claim's own <li>/<p>, so no hovering is needed.
        The real URL is base64-encoded in the redirect link's `u=` parameter."""
        sel = self.bg_selectors
        time.sleep(1)

        # Copilot Search streams in after the page loads, so poll for a few seconds
        container = mdroot = None
        for _ in range(6):
            container_els = self.driver.find_elements(By.CSS_SELECTOR, sel["bg_container"])
            if container_els and container_els[0].is_displayed():
                mdroot_els = container_els[0].find_elements(By.CSS_SELECTOR, sel["bg_mdroot"])
                if mdroot_els:
                    container, mdroot = container_els[0], mdroot_els[0]
                    break
            time.sleep(0.5)
        if container is None or mdroot is None:
            return {}

        bing_data = {
            "mode": "bing",
            "id": page_id,
            "from_url": self.driver.current_url,
            "query": query,
            "timestamp_scraped": datetime.now().isoformat(),
            "timestamp_scraped_unix": int(datetime.timestamp(datetime.now())),
            "not_available": False,
            "text": "",
            "main_answer": "",
            "claims": "",
            "sources": [],
            "disclaimer": ""
        }

        # References list at the bottom: full title and display URL per numbered citation
        citation_map = {}
        cit_root = container.find_elements(By.CSS_SELECTOR, sel["bg_cit_root"])
        if cit_root:
            for cit in cit_root[0].find_elements(By.CSS_SELECTOR, sel["bg_cit_item"]):
                num = cit.get_attribute("data-citation-num")
                if not num:
                    continue
                try:
                    href = cit.find_element(By.CSS_SELECTOR, "a").get_attribute("href")
                except Exception:
                    href = None
                title_el = cit.find_elements(By.CSS_SELECTOR, sel["bg_cit_title"])
                site_el = cit.find_elements(By.CSS_SELECTOR, sel["bg_cit_site"])
                url_el = cit.find_elements(By.CSS_SELECTOR, sel["bg_cit_url"])
                citation_map[num] = {
                    "title": title_el[0].text if title_el else (site_el[0].text if site_el else ""),
                    "href": href,
                    "display_url": url_el[0].text if url_el else "",  # fallback if decoding fails
                }

        answer_html = self.html_without_citation_links(mdroot)
        bing_data["text"] = md(answer_html, strip=["a", "img"]).strip()

        headings = mdroot.find_elements(By.CSS_SELECTOR, sel["bg_main_heading"])
        bing_data["main_answer"] = headings[0].text if headings else ""

        claims = self.get_bing_claims(mdroot, citation_map)

        # Top-level sources: every unique reference the answer cites
        seen_hrefs = set()
        for claim in claims:
            for src in claim["sources"]:
                href = src.pop("_href", None)
                if href and href not in seen_hrefs:
                    seen_hrefs.add(href)
                    bing_data["sources"].append(dict(src))
                    self.source_urls_to_scrape.add(src.get("translated_url") or src["url"])  # Add to queue

        if self.scrape_claims:
            bing_data["claims"] = claims

        # The disclaimer tooltip is hidden in the DOM, so read textContent instead of .text
        disclaimer_els = container.find_elements(By.CSS_SELECTOR, sel["bg_disclaimer"])
        if disclaimer_els:
            bing_data["disclaimer"] = (self.driver.execute_script(
                "return arguments[0].textContent;", disclaimer_els[0]
            ) or "").strip()

        # Bing clips the answer box visually; expand it so the screenshot shows the full answer
        show_more = self.driver.find_elements(By.CSS_SELECTOR, sel["bg_show_more"])
        if show_more and show_more[0].is_displayed():
            try:
                show_more[0].click()
                time.sleep(0.3)
            except Exception:
                pass

        self.driver.execute_script("window.scrollTo(0, 0);")
        return bing_data

    def get_bing_claims(self, mdroot, citation_map) -> list:
        """One claim per <li>/<p> in the answer: its text (citation links stripped) plus the
        reference(s) its inline <a data-sups="N"> links point at. Uncited claims get no sources."""
        sel = self.bg_selectors
        claims = []
        for item in mdroot.find_elements(By.CSS_SELECTOR, sel["bg_claim_items"]):
            if not item.is_displayed():
                continue

            claim_sources = []
            seen_nums = set()
            for link in item.find_elements(By.CSS_SELECTOR, sel["bg_citlink"]):
                num = link.get_attribute("data-sups")
                if not num or num in seen_nums:
                    continue
                seen_nums.add(num)
                ref = citation_map.get(num, {})
                href = ref.get("href")
                decoded_url = self.decode_bing_redirect_url(href) if href else ""
                final_url = decoded_url or ref.get("display_url") or href or ""
                claim_sources.append({
                    "title": ref.get("title") or (link.get_attribute("aria-label") or "").removeprefix("Sources: "),
                    "description": "",
                    "url_id": hashlib.md5(final_url.encode()).hexdigest(),
                    **self.source_link_fields(final_url),
                    "_href": final_url,
                })

            claim_text = self.html_without_citation_links(item, return_text=True)
            if not claim_text:
                continue
            claims.append({"claim": claim_text, "sources": claim_sources})

        return claims

    def html_without_citation_links(self, element, return_text=False):
        """Return an element's outerHTML (or plain textContent) from a clone with Bing's inline
        citation links (<a class="md_citlink">) removed, so site names don't leak into the text."""
        return self.driver.execute_script(
            """
            const clone = arguments[0].cloneNode(true);
            clone.querySelectorAll(arguments[1]).forEach(a => a.remove());
            return arguments[2] ? clone.textContent.replace(/\\s+/g, ' ').trim() : clone.outerHTML;
            """,
            element,
            self.bg_selectors["bg_citlink"],
            return_text,
        )

    @staticmethod
    def decode_bing_redirect_url(href) -> str:
        """Decode the real URL from a bing.com/ck/a redirect (base64 in the `u=` parameter,
        after a 2-char prefix). Returns the href unchanged if it can't be decoded."""
        if not href:
            return ""
        try:
            payload = parse_qs(urlparse(href).query).get("u", [None])[0]
            if not payload:
                return href
            stripped = payload[2:]  # drop the fixed 2-char prefix (e.g. "a1")
            padded = stripped + "=" * (-len(stripped) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8")
        except Exception:
            return href

    def parse_duckduckgo_answer(self, page_id, query) -> dict:
        """DuckDuckGo's DuckAssist: one answer (one or more <p>) and ONE shared list of source
        links, with no markup tying a source to a sentence. So 'claims' is a single entry
        covering the whole answer, paired with the full source list."""
        sel = self.dk_selectors
        time.sleep(1)

        container = None
        for _ in range(6):
            container_els = self.driver.find_elements(By.CSS_SELECTOR, sel["dk_container"])
            if container_els and container_els[0].is_displayed():
                container = container_els[0]
                break
            time.sleep(0.5)
        if container is None:
            return {}

        ddg_data = {
            "mode": "duckduckgo",
            "id": page_id,
            "from_url": self.driver.current_url,
            "query": query,
            "timestamp_scraped": datetime.now().isoformat(),
            "timestamp_scraped_unix": int(datetime.timestamp(datetime.now())),
            "not_available": False,
            "text": "",
            "main_answer": "",
            "claims": "",
            "sources": [],
            "disclaimer": ""
        }

        paragraphs = [p for p in container.find_elements(By.CSS_SELECTOR, sel["dk_paragraphs"]) if p.is_displayed()]
        answer_text = "\n\n".join(p.text for p in paragraphs if p.text).strip()
        ddg_data["text"] = answer_text

        main_strong = container.find_elements(By.CSS_SELECTOR, sel["dk_main_strong"])
        if main_strong:
            ddg_data["main_answer"] = main_strong[0].text
        elif paragraphs:
            ddg_data["main_answer"] = paragraphs[0].text  # no bold lead-in: use the first paragraph

        sources = []
        for a_el in container.find_elements(By.CSS_SELECTOR, sel["dk_source_links"]):
            if not a_el.is_displayed():
                continue
            href = a_el.get_attribute("href")
            if not href:
                continue
            sources.append({
                "title": a_el.text.strip(),
                "description": "",
                "url_id": hashlib.md5(href.encode()).hexdigest(),
                **self.source_link_fields(href),
            })
            self.source_urls_to_scrape.add(sources[-1].get("translated_url") or sources[-1]["url"])  # Add to queue
        ddg_data["sources"] = sources

        if self.scrape_claims and answer_text:
            ddg_data["claims"] = [{"claim": answer_text, "sources": sources}]

        disclaimers = self.driver.find_elements(By.CSS_SELECTOR, sel["dk_disclaimer"])
        for disclaimer in disclaimers:
            if disclaimer.is_displayed():
                ddg_data["disclaimer"] = disclaimer.text
                break

        self.driver.execute_script("window.scrollTo(0, 0);")
        return ddg_data

    def get_claims(self, ai_text_inner, mode="ai_overview") -> list:
        """
        Get the claims from an AI Overview or AI Mode.
        This uses AI Overview selectors, which as of June 2026 are the same for
        claims as AI Mode claims.
        Iterates badges in *reverse* order so each badge is scrolled to the top
        of the viewport, forcing the popup to open downward (never obscuring the
        next badge). Scrolling to the next badge naturally dismisses the current
        popup. The collected claims are reversed before returning so the final
        list matches the original top-to-bottom order.
        """

        claims = []
        actions = ActionChains(self.driver)

        # AI Mode answers are often shorter than the viewport, so a badge near
        # the bottom cannot be scrolled to the top (there is nothing below it to
        # scroll up into). Add a viewport's worth of padding at the bottom so
        # every badge can be brought to the top and its popup opens downward.
        # Removed again in the finally-block so it doesn't affect screenshots.
        if mode == "ai_mode":
            self.driver.execute_script("document.body.style.paddingBottom = window.innerHeight + 'px';")

        try:
            all_badges = [
                b for b in ai_text_inner.find_elements(by=By.CSS_SELECTOR, value=self.ao_selectors["ao_source_badge"])
                if b.is_displayed()
            ]
            if not all_badges:
                self.log("No source badges found, so no claims can be extracted (check the 'Source badge' selector).", classes="text-orange")

            for link_badge in reversed(all_badges):
                try:
                    # Scroll badge to the TOP of the viewport so the popup opens downward,
                    # and so the scroll itself dismisses any previously open popup.
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'start'});", link_badge)
                    self.driver.execute_script("window.scrollBy(0, -200);")
                    time.sleep(0.2)

                    # Hover over the badge to trigger the popup
                    actions.move_to_element(link_badge).perform()
                    time.sleep(0.4)

                    # Read this badge's own claim by walking up the DOM from the exact badge
                    # we just hovered (no reliance on a "currently highlighted" selector).
                    claim_text = self.driver.execute_script(
                        """
                        const badge = arguments[0];
                        const scopeRoot = arguments[1];

                        // Prefer the enclosing <li> (the boundary of a bulleted claim),
                        // bounded to the AI answer so we never climb into unrelated page <li>s.
                        let root = badge.closest('li');
                        if (root && !scopeRoot.contains(root)) {
                            root = null;
                        }

                        if (!root) {
                            // No enclosing <li>: this is the lead-in sentence. Fall back to the
                            // nearest ancestor carrying Google's per-segment data-hveid,
                            // starting from the badge's PARENT (the badge itself also has one).
                            root = badge.parentElement;
                            while (root && !root.hasAttribute('data-hveid')) {
                                root = root.parentElement;
                            }
                        }
                        if (!root) return '';

                        // textContent (not leaf elements) so bare text nodes are included;
                        // strip the citation buttons from a clone so their labels don't leak in.
                        const clone = root.cloneNode(true);
                        clone.querySelectorAll('button').forEach(b => b.remove());
                        return clone.textContent.replace(/\\s+/g, ' ').trim();
                        """,
                        link_badge,
                        ai_text_inner,
                    )

                    claim_sources = []
                    for source_box in self.driver.find_elements(by=By.CSS_SELECTOR, value=self.ao_selectors["ao_claim_url"]):
                        if not source_box.is_displayed():
                            continue
                        try:
                            claim_source = {}
                            title_el = source_box.find_elements(by=By.CSS_SELECTOR, value=self.ao_selectors["ao_url_title"])
                            claim_source["title"] = title_el[0].text if title_el else ""
                            claim_url = source_box.find_element(by=By.CSS_SELECTOR, value="a").get_attribute("href")
                            claim_source["url_id"] = hashlib.md5((claim_url or "").encode()).hexdigest()
                            claim_source.update(self.source_link_fields(claim_url))

                            claim_source_descriptions = None
                            if mode == "ai_overview":
                                claim_source_descriptions = source_box.find_elements(by=By.CSS_SELECTOR, value=self.ao_selectors["ao_url_description"])
                            elif mode == "ai_mode":
                                claim_source_descriptions = source_box.find_elements(by=By.CSS_SELECTOR, value=self.am_selectors["am_url_description"])

                            claim_source["description"] = claim_source_descriptions[0].text if claim_source_descriptions else ""

                            claim_sources.append(claim_source)
                        except Exception:
                            continue

                    claims.append({
                        "claim": claim_text,
                        "sources": claim_sources
                    })
                except Exception as e:
                    self.log(f"(skipped one badge: {e})", classes="text-orange")
                    continue
        finally:
            # Remove the temporary bottom padding added for AI Mode.
            if mode == "ai_mode":
                self.driver.execute_script("document.body.style.paddingBottom = '';")

            # Move the pointer to the top-left corner of the viewport (a neutral,
            # non-badge area) so the last badge's hover popup closes and does not
            # linger in the saved HTML/screenshot taken afterwards.
            dismiss = ActionChains(self.driver)
            dismiss.w3c_actions.pointer_action.move_to_location(0, 0)
            dismiss.perform()
            time.sleep(0.3)

        # Reverse back to chronological (top-to-bottom) order
        claims.reverse()
        return claims

    def html_without_source_buttons(self, element) -> str:
        """Return the element's outerHTML with source-badge buttons removed.

        AI Overview and AI Mode answers contain inline source badges rendered as
        <button> elements. We strip them (on a clone, so the live page is
        untouched) so their citation text does not end up in the saved answer
        text -- the same buttons we skip when collecting claims.
        """
        return self.driver.execute_script(
            """
            const clone = arguments[0].cloneNode(true);
            clone.querySelectorAll('button').forEach(b => b.remove());
            return clone.outerHTML;
            """,
            element,
        )

    def save_html(self, query=None, page_id=None, mode=None, output_dir="/html"):
        html_dir = self.output_dir + output_dir
        if not os.path.isdir(html_dir):
            os.mkdir(html_dir)

        filename = self.get_page_str(query=query, page_id=page_id, mode=mode, suffix=".html")

        with open(f"{html_dir}/{filename}", "w", encoding="utf-8") as out_html:
            out_html.write(self.driver.page_source)

    def save_screenshot(self, query=None, page_id=None, mode=None, output_dir="/screenshots"):
        screenshots_dir = self.output_dir + output_dir
        if not os.path.isdir(screenshots_dir):
            os.mkdir(screenshots_dir)

        filename = self.get_page_str(query=query, page_id=page_id, mode=mode, suffix=".png")

        screenshot_location = f"{screenshots_dir}/{filename}"

        # Always start from the very top before capturing. get_claims may leave
        # the page scrolled down, which makes the sticky AI Mode header render
        # mid-page and overlay the answer in the screenshot.
        self.driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.3)

        if mode == "ai_overview":
            window_height = self.driver.execute_script("return document.documentElement.scrollHeight")
            self.driver.set_window_size(1920, window_height)
        elif mode == "ai_mode":
            chat_box_height = self.driver.execute_script(f"return document.querySelector('div.WzWwpc').offsetHeight;")
            height_maximalized = 300 + int(chat_box_height)
            window_height = 1080 if height_maximalized < 1080 else height_maximalized
            self.driver.set_window_size(1920, window_height)
        else:
            self.driver.get_full_page_screenshot_as_file(screenshot_location)  # don't try to scroll to the end
            self.driver.set_window_size(1920, 1080)
            return

        self.driver.execute_script("window.scrollTo(0, 0);")
        self.driver.execute_script("document.elementFromPoint(0, 0).click();")

        time.sleep(0.5)
        self.driver.save_screenshot(screenshot_location)
        self.driver.set_window_size(1920, 1080)

    def stop(self):
        self.is_running = False
        if self.driver:
            try:
                self.driver.quit()
                self.log("Firefox browser closed.", classes="text-blue")
                self.driver = None
            except:
                pass

    def restart_driver(self):
        """Quit the current browser and start a fresh one, preserving the wait object."""
        self.log("Restarting browser...", classes="text-orange")
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = self.get_driver(custom_profile=self.profile)
        self.wait = WebDriverWait(self.driver, timeout=2)
        self.log("Browser restarted.", classes="text-orange")

    def open_page(self, url):
        retries = 0
        max_retries = 3
        while retries <= max_retries:
            try:
                self.driver.get(url)
                self.check_for_captcha()
                break
            except TimeoutException:
                retries += 1
                self.log(f"Page timed out, trying again in 10 seconds (retry {retries}/{max_retries})...", classes="text-orange")
                time.sleep(10)
                try:
                    self.driver.refresh()
                except WebDriverException:
                    self.log("Browser unresponsive after timeout, restarting...", classes="text-orange")
                    self.restart_driver()
            except WebDriverException as e:
                if "marionette" in str(e).lower() or "decode" in str(e).lower():
                    retries += 1
                    self.log(f"Marionette connection lost, restarting browser (retry {retries}/{max_retries})...", classes="text-orange")
                    self.restart_driver()
                else:
                    raise
        if retries > max_retries:
            raise Exception("Couldn't load page after max retries")

    def check_for_captcha(self):
        """Checks for Google's specific reCAPTCHA container"""
        captcha = self.driver.find_elements(By.CSS_SELECTOR, "#recaptcha")
        if captcha and captcha[0].is_displayed():
            self.log("Google CAPTCHA detected, please solve it in the Firefox window...", classes="text-orange")

            while captcha and captcha[0].is_displayed():
                time.sleep(2)
                captcha = self.driver.find_elements(By.CSS_SELECTOR, "#recaptcha")
        return

    def check_for_generic_captcha(self):
        """Generic CAPTCHA checker for third party sources"""
        # Look for Cloudflare, Datadome, or standard hCaptcha/reCAPTCHA iframes
        captcha_selectors = [
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='hcaptcha.com']",
            "iframe[src*='datadome.co']",
            "#recaptcha"
        ]

        for selector in captcha_selectors:
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if elements and elements[0].is_displayed():
                self.log(f"CAPTCHA detected on source page, please solve it in the Firefox window or refresh...", classes="text-orange")

                # Wait until the iframe vanishes or user navigates away
                while elements and elements[0].is_displayed():
                    time.sleep(2)
                    try:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    except (StaleElementReferenceException, WebDriverException):
                        break # Elements resolved or page changed
                self.log("Captcha resolved. Continuing...", classes="text-blue")
                return

    def get_current_url(self, safe=True) -> str:
        """ Returns the current URL as a string"""
        current_url = self.driver.current_url
        if safe:
            current_url = re.sub(r'[\\/*?:"<>|]', "_", current_url.replace("https://", "").replace("http://", ""))[:150]
        return current_url

    def install_cookie_blocker(self):
        """ Install cookie blocker extension """
        try:
            extension_url = "https://addons.mozilla.org/firefox/downloads/file/4637154/istilldontcareaboutcookies-latest.xpi"
            temp_xpi_path = os.path.join(tempfile.gettempdir(), "istilldontcare.xpi")

            # Download the extension and explicitly write it to a file
            response = requests.get(extension_url)
            response.raise_for_status() # Make sure the download actually succeeded

            with open(temp_xpi_path, 'wb') as f:
                f.write(response.content)

            # Install from the local file path
            self.driver.install_addon(temp_xpi_path, temporary=True)
            self.log(f"Installed 'I Still Don't Care About Cookies' extension", classes="text-blue")

        except Exception as e:
            self.log(f"Failed to install extension cookie blocker: {e}", classes="text-orange")

    def get_url_string(self, query: str) -> str:
        return query.lower().strip().replace(" ", "+")

    def print_elapsed_time(self, start_time, n_urls, count):
        duration = time.time() - start_time
        self.scrape_durations.append(duration)
        avg_duration = sum(self.scrape_durations) / len(self.scrape_durations)
        remaining = n_urls - count
        td = timedelta(seconds=remaining * avg_duration)
        total_minutes = td.seconds // 60
        hours, minutes = divmod(total_minutes, 60)
        self.log(f"Processed in {duration:.2f}s (avg {avg_duration:.2f}s, {hours}h {minutes}m left)")

    def get_page_str(self, query=None, page_id=None, mode=None, suffix=None):
        if query and page_id and mode:
            filename_query = re.sub(r'[\\/*?:"<>|]', "", query)
            filename = f"{page_id}_{mode}_{filename_query}"
        elif page_id and mode:
            filename = f"{page_id}_{mode}_{self.get_current_url(safe=True)}"
        elif page_id:
            filename = f"{page_id}_{self.get_current_url(safe=True)}"
        else:
            current_url = self.get_current_url(safe=True)
            page_id = hashlib.md5(current_url.encode()).hexdigest()
            filename = f"{page_id}_{current_url}"

        if suffix:
            filename += suffix
        return filename


    @staticmethod
    def is_goto_link(url) -> bool:
        """Whether a source link is an obfuscated Google redirect (https://www.google.xx/goto?url=...)."""
        parsed = urlparse(url or "")
        return parsed.path == "/goto" and re.fullmatch(r"(www\.)?google\.[a-z.]+", parsed.netloc) is not None

    def resolve_goto_link(self, goto_url) -> Optional[str]:
        """Return the URL a Google /goto link redirects to, or None if it can't be resolved.

        Only reads the redirect's Location header; the target page itself isn't loaded.
        """
        if goto_url in self.resolved_goto_links:
            return self.resolved_goto_links[goto_url]

        resolved = None
        try:
            response = self.http_session.get(goto_url, allow_redirects=False, timeout=10)
            location = response.headers.get("Location")
            if response.is_redirect and location:
                resolved = urljoin(goto_url, location)
            else:
                self.log(f"Could not follow source link (HTTP {response.status_code}): {goto_url}", classes="text-orange")
        except requests.RequestException as e:
            self.log(f"Could not follow source link ({e}): {goto_url}", classes="text-orange")

        self.resolved_goto_links[goto_url] = resolved
        return resolved

    def source_link_fields(self, href) -> dict:
        """Return the domain/url fields of a source, following Google /goto redirects if enabled,
        and resolving translate links.."""
        fields = {}
        link = href or ""

        if self.is_goto_link(link):
            if self.follow_goto_links:
                resolved = self.resolve_goto_link(link)
                if resolved:
                    fields["goto_url"] = link
                    link = resolved
            elif not self.goto_notice_logged:
                self.log("Source links are obfuscated Google /goto redirects. Enable 'Follow obfuscated source links' to get the actual links.",
                         classes="text-orange")
                self.goto_notice_logged = True

        is_translated = "translate.google.com" in link
        if is_translated:
            translated = self.parse_translate_url(link)
            display_url = translated["original_url"]
            display_domain = translated["original_domain"]
        else:
            display_url = link
            display_domain = urlparse(link).netloc

        link_fields = {"domain": display_domain, "url": display_url, "is_translated": is_translated}
        if is_translated:
            link_fields["translated_url"] = link
        return {**link_fields, **fields}

    @staticmethod
    def parse_translate_url(url: str) -> dict:
        """If a URL is a Google Translate proxy, return the original URL and its domain."""
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        original_url = params.get("u", [None])[0]
        if original_url:
            return {
                "original_url": original_url,
                "original_domain": urlparse(original_url).netloc,
            }
        return {"original_url": url, "original_domain": urlparse(url).netloc}

    @staticmethod
    def format_claim(c) -> str:
        src_str = "; ".join(
            f"{s.get('title', '')} ({s.get('url', '')})" for s in c.get("sources", [])
        )
        return f"{c.get('claim', '')} [{src_str}]"

    @classmethod
    def claims_to_string(cls, claims) -> str:
        if not claims:
            return ""
        return "\n".join(cls.format_claim(c) for c in claims)

    @staticmethod
    def source_to_string(sources) -> str:
        source_string = []
        for source in sources:
            source_string.append(f"{source.get('title', '')}: {source.get('description', '')} - {source.get('url', '')}")
        return "\n".join(source_string) if source_string else ""

# NICEGUI INTERFACE
def load_readme() -> str:
    with open(os.path.join(base_path, 'README.md'), 'r', encoding='utf-8') as file:
        return "\n".join(file.readlines())

def load_css() -> str:
    with open(os.path.join(base_path, 'assets', 'stylesheet.css'), 'r', encoding='utf-8') as file:
        return file.read()

class GUI:
    def __init__(self):
        self.scraper = None
        self.label_queryfile = 'Use query file'
        self.label_insert = 'Insert queries'
        self.setup_ui()

    def setup_ui(self):
        ui.colors(primary='#c1350c', secondary='#fbf1d0', accent='#1c5276', positive='#f5a30f')
        ui.add_head_html('''
            <link rel="preconnect" href="https://fonts.googleapis.com">
            <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
            <link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600&family=Nerko+One&display=swap" rel="stylesheet">
        ''')

        ui.add_css(load_css())

        with ui.header().classes('bg-primary text-blue p-0 gap-0'):
            ui.image(os.path.join(base_path, 'assets', 'logo_cutout.png')).style('width: 75px; height: 75px; object-fit: cover; flex-shrink: 0;')
            with ui.row().classes('p-4 self-center items-baseline gap-3'):
                ui.label('Kenniskrabber').classes('main-title')
                ui.label(f'v{APP_VERSION}').classes('main-title version')

        with ui.row().classes('w-full p-4 gap-4 items-stretch'):
            # LEFT COLUMN - SETTINGS
            with ui.column().classes('w-1/2 min-w-[400px]'):
                with ui.card().classes('w-full'):
                    ui.label('What to collect').classes('card-title text-xl mb-2')

                    with ui.row().classes('items-center gap-4'):
                        self.google_cb = ui.checkbox('Google', value=True)
                        self.scrape_bing_cb = ui.checkbox('Bing (Copilot Search)')
                        self.scrape_duckduckgo_cb = ui.checkbox('DuckDuckGo (DuckAssist)')

                    self.mode = ui.select(
                        ['AI Overviews & AI Modes', 'AI Overviews', 'AI Modes'],
                        value='AI Overviews & AI Modes',
                        label='Google'
                    ).classes('w-full').bind_enabled_from(self.google_cb, 'value')

                    # Bing and DuckDuckGo only have an AI Overview-style answer, so they are
                    # only available when Google is set to 'AI Overviews' (or Google is off).
                    self.google_cb.on_value_change(self.update_engine_options)
                    self.mode.on_value_change(self.update_engine_options)
                    self.update_engine_options()

                    self.input_method = ui.radio([self.label_insert, self.label_queryfile], value=self.label_insert).props('inline')

                    default_query_file = get_default_output_dir(input_file=True)
                    self.file_input = ui.input('Query file csv location', value=default_query_file).classes('w-full') \
                        .bind_visibility_from(self.input_method, 'value', value=self.label_queryfile)

                    self.text_input = ui.textarea('Enter queries (one per line)',
                                                  placeholder="is scraping legal?\nmacy conference\nDSA audits") \
                        .classes('w-full').bind_visibility_from(self.input_method, 'value', value=self.label_insert)

                    with ui.row().classes('items-center gap-1'):
                        self.shuffle = ui.checkbox('Shuffle queries')
                        with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                            ui.tooltip('Can be useful for longitudinal scrapes.')

                    with ui.row().classes('items-center gap-1'):
                        self.scrape_claims = ui.checkbox('Extract claims', value=True)
                        with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                            ui.tooltip('Extracts claims and their associated sources from AI answers.')

                    with ui.row().classes('items-center gap-1'):
                        self.scrape_sources_cb = ui.checkbox('Scrape sources')
                        with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                            ui.tooltip('Extracts HTML and screenshots of all linked sources after collecting Google data.')

                    with ui.row().classes('items-center gap-1'):
                        self.follow_goto_cb = ui.checkbox('Follow obfuscated sources', value=True)
                        with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                            ui.tooltip('Google may hide sources behind a redirect '
                                       '(google.com/goto?url=...), so the actual URL and domain are not on the page. '
                                       'When checked, these redirects are followed with a web request to get the '
                                       'actual source link.')

                    default_output_folder = get_default_output_dir()
                    self.output_dir = ui.input('Output directory', value=default_output_folder).classes('full-width')

            with ui.column().classes('w-1/2 min-w-[400px]'):
                with ui.card().classes('w-full'):
                    ui.label('Scrape settings').classes('card-title text-xl mb-2')
                    self.profile = ui.input('Firefox Profile Path (Optional)',
                                            placeholder='C:/Users/User/AppData/Roaming/Mozilla/Firefox/Profiles/xxx.default').classes('w-full')

                    with ui.row().classes('w-full gap-2'):
                        self.tld = ui.input('Top-level domain', value='.com').classes('flex-grow')
                        self.offset = ui.number('Offset', value=0, format='%.0f').classes('flex-grow')

                    with ui.row().classes('w-full items-center gap-1'):
                        self.throttle_strings = ui.input('Throttled text', value='Something went wrong, Try again later').classes('flex-grow')
                        with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                            ui.tooltip('Comma-separated texts that appear when Google throttles requests.')

                    # CSS Selectors accordion
                    AO_SELECTOR_META = {
                        "ao_container": ("Container", "Main AI Overview container element"),
                        "ao_show_more": ("Show more button", "Button to expand the full AI Overview"),
                        "ao_failed_elements": ("Failed elements", "Elements indicating AI Overview failed to load"),
                        "ao_inner_text": ("Inner text", "Element containing the AI Overview inner text"),
                        "ao_source_badge": ("Source badge", "Source badge within the AI Overview"),
                        "ao_main_claim": ("Main answer", "Highlighted claim in the AI Overview"),
                        "ao_claim_url": ("Claim source box", "Source box in pop-up that opens after hovering over a source badge."),
                        "ao_show_more_urls": ("Show more URLs button", "Button to expand the source URL list"),
                        "ao_url_divs": ("Sources (expandable box)", "List items for sources after clicking 'Show more'"),
                        "ao_url_divs_not_expandable": ("Sources (not expandable)", "List items for sources without 'Show more' button"),
                        "ao_url_divs_carousel": ("Sources (carousel)", "List items for sources at bottom, horizontally ordered"),
                        "ao_url_title": ("Source title", "Element containing the source title"),
                        "ao_url_description": ("Source description", "Element with the source description text"),
                        "ao_url_description_fallback": ("Source description (fallback)", "Fallback element for description"),
                        "ao_disclaimer": ("Disclaimer", "Element containing the disclaimer text (e.g. 'AI can make mistakes')"),
                    }
                    AM_SELECTOR_META = {
                        "am_container": ("Container", "Main AI Mode answers container element"),
                        "am_answers": ("Answers", "Element containing the AI Mode answer content"),
                        "am_processed_answer": ("Processed answer", "Element indicating the answer is fully generated"),
                        "am_main_claim": ("Main answer", "Highlighted claim in the AI Overview"),
                        "am_show_more_urls": ("Show more URLs btn", "Button to expand the source URL list"),
                        "am_url_divs": ("Sources (expandable box)", "List items for sources after clicking 'Show more'"),
                        "am_url_divs_not_expandable": ("Sources (not expandable)", "List items for sources without 'Show more' button"),
                        "am_url_description": ("URL description", "Element with the source description text"),
                        "am_something_went_wrong": ("Failed generation box", "Element indicating AI Mode failed to generate an answer, often due to throttling"),
                        "am_disclaimer": ("Disclaimer", "Element containing the disclaimer text (e.g. 'AI can make mistakes')"),
                    }

                    BG_SELECTOR_META = {
                        "bg_container": ("Container", "Main Bing Copilot Search answer container"),
                        "bg_mdroot": ("Answer root", "Element holding the rendered answer markdown (headings/lists/paragraphs)"),
                        "bg_claim_items": ("Claim items", "The list items / paragraphs that count as one claim each"),
                        "bg_citlink": ("Citation link", "Inline citation link inside a claim, pointing at one reference"),
                        "bg_cit_root": ("References root", "Container for the References list at the bottom of the answer"),
                        "bg_cit_item": ("Reference item", "One entry in the References list"),
                        "bg_cit_title": ("Reference title", "Element with a reference's page title"),
                        "bg_cit_site": ("Reference site", "Element with a reference's site name"),
                        "bg_cit_url": ("Reference URL", "Element with a reference's plain display URL"),
                        "bg_main_heading": ("Main answer heading", "The heading level that wraps the top-line summary answer"),
                        "bg_search_box": ("Homepage search box", "The query input on bing.com's homepage, used to type the search in"),
                        "bg_show_more": ("Show more button", "Button to expand the visually clipped answer box before the screenshot"),
                        "bg_disclaimer": ("Disclaimer", "The 'This summary was generated by AI...' tooltip text"),
                    }
                    DK_SELECTOR_META = {
                        "dk_container": ("Container", "Main DuckAssist answer container"),
                        "dk_paragraphs": ("Paragraphs", "The <p> elements making up the answer text"),
                        "dk_main_strong": ("Main answer (bold lead-in)", "The bolded lead-in phrase used as the main answer"),
                        "dk_source_links": ("Source links", "Links in the shared source list below the answer"),
                        "dk_disclaimer": ("Disclaimer", "Element with the 'Auto-generated...' disclaimer text"),
                    }

                    with ui.expansion('CSS Selectors').classes('w-full'):
                        self.selector_toggle = ui.toggle(['AI Overview', 'AI Mode', 'Bing', 'DuckDuckGo'], value='AI Overview').classes('w-full mb-2').props('spread no-wrap')

                        # Inputs are created empty here and populated once the log
                        # area exists, so load_selectors() can report its source.
                        self.ao_selector_inputs = {}
                        with ui.column().classes('w-full gap-1').bind_visibility_from(self.selector_toggle, 'value', value='AI Overview'):
                            for key, (label, tooltip) in AO_SELECTOR_META.items():
                                with ui.row().classes('w-full items-center gap-1'):
                                    inp = ui.input(label).classes('flex-grow').props('dense')
                                    with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                                        ui.tooltip(tooltip)
                                    self.ao_selector_inputs[key] = inp

                        self.am_selector_inputs = {}
                        with ui.column().classes('w-full gap-1').bind_visibility_from(self.selector_toggle, 'value', value='AI Mode'):
                            for key, (label, tooltip) in AM_SELECTOR_META.items():
                                with ui.row().classes('w-full items-center gap-1'):
                                    inp = ui.input(label).classes('flex-grow').props('dense')
                                    with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                                        ui.tooltip(tooltip)
                                    self.am_selector_inputs[key] = inp

                        self.bg_selector_inputs = {}
                        self.dk_selector_inputs = {}
                        for toggle_label, meta, inputs in [('Bing', BG_SELECTOR_META, self.bg_selector_inputs),
                                                           ('DuckDuckGo', DK_SELECTOR_META, self.dk_selector_inputs)]:
                            with ui.column().classes('w-full gap-1').bind_visibility_from(self.selector_toggle, 'value', value=toggle_label):
                                for key, (label, tooltip) in meta.items():
                                    with ui.row().classes('w-full items-center gap-1'):
                                        inp = ui.input(label).classes('flex-grow').props('dense')
                                        with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                                            ui.tooltip(tooltip)
                                        inputs[key] = inp

            # LOGS AND ACTIONS
            with ui.column().classes('w-1/2 min-w-[400px] flex-grow'):
                with ui.card().classes('w-full'):
                    ui.label('Run').classes('card-title text-xl mb-2')
                    with ui.row().classes('w-full gap-2'):
                        self.btn_prepare = ui.button('1. Prepare Browser', on_click=self.on_prepare).classes('flex-grow')
                        self.btn_start = ui.button('2. Start Scraping', on_click=self.on_start).classes('flex-grow')
                        self.btn_start.disable()
                        self.btn_stop = ui.button('Stop', color='red', on_click=self.on_stop).classes('flex-grow')
                        self.btn_stop.disable()

                with ui.card().classes('w-full h-96 flex-grow'):
                    ui.label('Status').classes('card-title text-xl')
                    self.progress_label = ui.label('Progress: 0/0')
                    self.progress_bar = ui.linear_progress(value=0, show_value=False).classes('w-full mb-4')

                    self.btn_open_folder = ui.button('Open output folder', color='red', on_click=self.open_output_folder).classes('w-full flex-grow')
                    self.btn_open_folder.set_visibility(False)

                    self.log_scroll = ui.scroll_area().classes('w-full h-full rounded')
                    with self.log_scroll:
                        self.log_column = ui.column().classes('w-full p-2 gap-0')

        with ui.row().classes('footer-text w-full p-2 justify-center'):
            ui.html('<a href="https://deep-culture.org" target="_blank">Deep Culture</a> · <a href="https://digitalmethods.net" target="_blank">Digital Methods Initiative</a>').classes('text-xs footer-text')

        self.ao_defaults, self.am_defaults, self.bg_defaults, self.dk_defaults = GoogleAIScraper.load_selectors(log=self.log_push)
        for key, inp in self.ao_selector_inputs.items():
            if key in self.ao_defaults:
                inp.value = self.ao_defaults[key]
        for key, inp in self.am_selector_inputs.items():
            if key in self.am_defaults:
                inp.value = self.am_defaults[key]

        for key, inp in self.bg_selector_inputs.items():
            if key in self.bg_defaults:
                inp.value = self.bg_defaults[key]
        for key, inp in self.dk_selector_inputs.items():
            if key in self.dk_defaults:
                inp.value = self.dk_defaults[key]

        # Check for a newer release in the background and notify if one exists.
        if not DEBUG:
            ui.timer(0.5, self.check_for_update, once=True)

    async def check_for_update(self):
        result = await run.io_bound(check_for_update)
        if not result:
            return
        latest_version, url = result

        with ui.dialog() as dialog, ui.card().classes('items-center gap-3'):
            ui.label('Update available').classes('text-lg font-bold')
            ui.label(f'A new version (v{latest_version}) is available. '
                     f'You are running v{APP_VERSION}.').classes('text-center')
            with ui.row().classes('gap-2'):
                ui.button('Not now', on_click=dialog.close).props('flat')
                ui.button('Download', on_click=lambda: webbrowser.open(url))
        dialog.open()

    def update_engine_options(self, _=None):
        """Grey out (and untick) Bing and DuckDuckGo when Google is set to AI Modes or both."""
        available = (not self.google_cb.value) or self.mode.value == 'AI Overviews'
        for checkbox in (self.scrape_bing_cb, self.scrape_duckduckgo_cb):
            if available:
                checkbox.enable()
            else:
                checkbox.set_value(False)
                checkbox.disable()

    def update_progress(self, current, total):
        self.progress_label.set_text(f'Progress: {current}/{total}')
        self.progress_bar.set_value(current / total if total > 0 else 0)

    def log_push(self, msg, classes=None):
        base = 'text-sm font-mono w-full break-words'
        extra = f' {classes}' if classes else ''
        with self.log_column:
            ui.label(msg).classes(base + extra).style('white-space: pre-wrap; word-break: break-word;')
        self.log_scroll.scroll_to(percent=1.0)

    def log_clear(self):
        self.log_column.clear()

    async def on_prepare(self):
        self.log_clear()
        self.btn_open_folder.set_visibility(False)
        self.btn_prepare.disable()

        if not (self.output_dir.value or "").strip():
            self.output_dir.value = get_default_output_dir()
            self.log_push(f"No output directory given; using default: {self.output_dir.value}")

        query_file = self.file_input.value
        if self.input_method.value == 'Insert queries':
            temp_file = os.path.join(self.output_dir.value or './', 'queries.csv')
            os.makedirs(os.path.dirname(temp_file), exist_ok=True)
            queries = [q.strip() for q in self.text_input.value.split('\n') if q.strip()]

            with open(temp_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['query'])
                for q in queries:
                    writer.writerow([q])
            query_file = temp_file

        if self.shuffle.value and os.path.isfile(query_file):
            shuffled_filename = query_file[:-4] + "_shuffled.csv"
            self.log_push(f"Shuffling rows to {shuffled_filename}")
            df = pd.read_csv(query_file)
            df = df.sample(frac=1)
            df.to_csv(shuffled_filename, index=False)
            query_file = shuffled_filename

        scrape_mode = self.mode.value.lower().replace(" ", "_") if self.google_cb.value else "no_google"
        tld = self.tld.value.strip().lower()
        if not tld.startswith('.'):
            tld = '.' + tld

        self.scraper = GoogleAIScraper(
            scrape_mode=scrape_mode,
            base_url=f"https://google{tld}/",
            profile=self.profile.value,
            output_dir=self.output_dir.value,
            inserted_queries=self.text_input.value if self.input_method.value == self.label_insert else None,
            query_file=query_file if self.input_method.value == self.label_queryfile else None,
            offset=int(self.offset.value),
            scrape_claims=self.scrape_claims.value,
            scrape_sources=self.scrape_sources_cb.value,
            follow_goto_links=self.follow_goto_cb.value,
            throttled_strings=self.throttle_strings.value,
            ao_selectors={**self.ao_defaults, **{k: inp.value for k, inp in self.ao_selector_inputs.items()}},
            am_selectors={**self.am_defaults, **{k: inp.value for k, inp in self.am_selector_inputs.items()}},
            bg_selectors={**self.bg_defaults, **{k: inp.value for k, inp in self.bg_selector_inputs.items()}},
            dk_selectors={**self.dk_defaults, **{k: inp.value for k, inp in self.dk_selector_inputs.items()}},
            scrape_bing=self.scrape_bing_cb.value,
            scrape_duckduckgo=self.scrape_duckduckgo_cb.value,
            progress_callback=self.update_progress,
            log_callback=self.log_push
        )

        success = await run.io_bound(self.scraper.prepare)

        if success:
            self.btn_start.enable()
        else:
            self.btn_prepare.enable()

    async def on_start(self):
        self.btn_start.disable()
        self.btn_stop.enable()

        await run.io_bound(self.scraper.process)

        self.btn_prepare.enable()
        self.btn_stop.disable()
        self.btn_open_folder.set_visibility(True)

    def on_stop(self):
        self.btn_stop.disable()
        if self.scraper:
            self.scraper.is_running = False

    def open_output_folder(self):
        output_dir = self.output_dir.value
        if output_dir and os.path.isdir(output_dir):
            if sys.platform == 'win32':
                os.startfile(output_dir)
            elif sys.platform == 'darwin':
                import subprocess
                subprocess.Popen(['open', output_dir])
            else:
                import subprocess
                subprocess.Popen(['xdg-open', output_dir])

from nicegui import app as nicegui_app

nicegui_app.add_static_files('/static', base_path)
nicegui_app.native.window_args['text_select'] = True  # So we can copy/paste log outputs

@ui.page('/')
def index():
    GUI()

ui.run(title='Kenniskrabber', native=True, reload=False, window_size=(500, 800))