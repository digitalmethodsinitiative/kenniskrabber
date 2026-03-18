import json
import csv
import os
import hashlib
import time
from collections import deque
from typing import Callable, Optional
import re
import sys
import pickle
import pandas as pd
import tempfile
import shutil
from datetime import datetime, timedelta
from urllib.parse import urlparse
from markdownify import markdownify as md

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, ElementNotInteractableException, StaleElementReferenceException
from selenium.webdriver.support.wait import WebDriverWait

from nicegui import ui, run


def get_default_output_dir(input_file=False):
    """Gets the folder where the app is running and creates a default 'scrapes' path."""
    # Check if we are running as a PyInstaller compiled executable
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        # Running as a normal Python script
        base_dir = os.path.abspath(os.path.dirname(__file__))

    if input_file:
        return os.path.join(base_dir, "query_file.csv")

    # Create a default folder name (e.g., /scrapes/2026-03-04_17-30)
    # Adding a timestamp ensures they don't overwrite old scrapes by accident!
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    default_path = os.path.join(base_dir, "scrapes", f"scrape_{timestamp}")

    # Make sure the base 'scrapes' directory actually exists
    os.makedirs(default_path, exist_ok=True)

    return default_path

class GoogleAIScraper:
    # Default CSS selectors for AI Overview scraping
    DEFAULT_AO_SELECTORS = {
        "ao_container": "#eKIzJc",
        "ao_show_more": "div.Jzkafd[role='button']",
        "ao_failed_elements": ".YWpX0d[style='']",
        "ao_inner_text": "div[jsname][data-rl] > div:not([id]) div[data-container-id='main-col']",
        "ao_show_more_urls": "#rw0ISc",
        "ao_url_divs": "li.CyMdWb > div",
        "ao_url_divs_no_show_more_button": "li.jydCyd > div[data-src-id]",
        "ao_url_attribution": "div[data-attrid='SGEAttributionFeedback']",
        "ao_url_description": ".vhJ6Pe > span[data-crb-snippet-text]",
        "ao_url_description_fallback": ".dMCttd",
    }

    # Default CSS selectors for AI Mode scraping
    DEFAULT_AM_SELECTORS = {
        "am_answers_container": "div[data-xid='aim-mars-turn-root']",
        "am_answers": "#aim-chrome-initial-inline-async-container div[data-container-id=main-col]",
        "am_processed_answer": "section #aim-chrome-initial-inline-async-container > div[data-processed=true] div.pWvJNd",
        "am_show_more_urls": "div[data-processed=true] > div.BjvG9b",
        "am_url_box": "li.CyMdWb > div[data-complete=true]",
        "am_url_description": ".vhJ6Pe",
    }

    def __init__(self, scrape_mode="both", base_url=None, profile="", iterate_queries=set(),
                 inserted_queries=None, query_file=None, results_file="", output_dir="", offset=0,
                 shuffle_queries=False, generating_strings=None, throttled_strings=None,
                 ao_selectors=None, am_selectors=None,
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
        self.offset = offset
        self.generating_strings = [g_s.strip() for g_s in generating_strings.split(",")] if generating_strings else [
            "Searching", "Generating", "Thinking..."]
        self.throttled_strings = [t_s.strip() for t_s in throttled_strings.split(",")] if throttled_strings else [
            "Try again later", "Something went wrong"]
        self.ao_selectors = {**self.DEFAULT_AO_SELECTORS, **(ao_selectors or {})}
        self.am_selectors = {**self.DEFAULT_AM_SELECTORS, **(am_selectors or {})}
        self.progress_callback = progress_callback
        self.log_callback: Optional[Callable] = log_callback
        self.profile = profile
        self.is_running = False

        self.finished_ai_overview_queries = set()
        self.finished_ai_mode_queries = set()
        self.scrape_durations = deque(maxlen=20)

    def log(self, msg, classes=None):
        """Send messages to the NiceGUI log UI"""
        if self.log_callback and self.log_callback is not print:
            self.log_callback(f"> {str(msg)}", classes=classes)
        else:
            print(msg)

    def get_driver(self, custom_profile=""):
        options = webdriver.FirefoxOptions()
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"
        options.add_argument(f'--user-agent={user_agent}')

        if custom_profile:
            self.log(f"Copying custom profile {custom_profile}...")
            temp_profile = tempfile.mkdtemp()
            shutil.copytree(custom_profile, temp_profile, dirs_exist_ok=True)
            options.add_argument(f'--profile={temp_profile}')

        driver = webdriver.Firefox(options=options)
        time.sleep(1)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(120)
        driver.implicitly_wait(.1)

        if os.path.exists("stealthify.js"):
            driver.execute_script(open("stealthify.js").read())
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        return driver

    def prepare(self):

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

                    # Get query from csv
                    # Fallback to first column if 'query' header is missing
                    query = row.get("query", list(row.values())[0])
                    query_id = row.get("id", hashlib.md5(query.encode()).hexdigest())

                    self.iterate_queries.add((query_id, query))

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
            return
        else:
            self.log(f"Found {len(self.iterate_queries)} unique queries to scrape.", classes="text-blue")

        # Start browser
        self.log("Launching Firefox browser. Please wait...", classes="text-orange")
        try:
            self.driver = self.get_driver(custom_profile=self.profile)
            self.wait = WebDriverWait(self.driver, timeout=2)

            if not os.path.isdir(self.output_dir):
                os.makedirs(self.output_dir)

            self.results_file = os.path.join(self.output_dir, "google_ai_results.json")

            self.driver.get(f"{self.base_url}search?q=is scraping legal")
            self.check_for_captcha()

            if not os.path.isfile("search_cookies.pkl"):
                with open("search_cookies.pkl", "wb") as out_cookies:
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
                        except Exception as parse_err:
                            self.log(f"Warning: could not parse results line: {parse_err}", classes="text-orange")
                self.log(
                    f"Already collected {len(self.finished_ai_overview_queries)} AI Overviews and {len(self.finished_ai_mode_queries)} AI Modes", classes="text-orange")

            self.log(
                "Browser ready.", classes="text-blue")
            self.log("Please complete any CAPTCHAs or logins in the Firefox window, then press '2. Start Scraping'.", classes="text-orange")
            return True

        except Exception as e:
            self.log(f"ERROR: {str(e)}", classes="text-red")
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
                            self.log(f"Parsed AI Overview for: {query}", classes="text-blue")
                            with open(self.results_file, "a", encoding="utf-8") as out_file:
                                out_file.write(json.dumps(result) + "\n")

                        self.save_html(query, page_id, mode)
                        self.save_screenshot(query, page_id, mode)
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

                        # Replace this with your actual parse_ai_mode call
                        result = self.parse_ai_mode(page_id, query)

                        if result:
                            self.log(f"Parsed AI Mode for: {query}", classes="text-blue")
                            with open(self.results_file, "a", encoding="utf-8") as out_file:
                                out_file.write(json.dumps(result) + "\n")

                        self.save_html(query, page_id, mode)
                        self.save_screenshot(query, page_id, mode)
                        self.finished_ai_mode_queries.add(page_id)
                        self.print_elapsed_time(start, len(self.iterate_queries), count)
                    else:
                        self.log("Already scraped AI Mode data, skipping", classes="text-orange")

            if self.is_running and self.results_file:
                self.log("Converting results JSON to csv")
                with open(self.results_file, "r", encoding="utf-8") as in_json:
                    with open(self.results_file[:-5] + ".csv", "w", encoding="utf-8", newline="") as out_csv:
                        first_line = True
                        for line in in_json.readlines():
                            csv_line = json.loads(line)
                            if first_line:
                                writer = csv.DictWriter(out_csv, fieldnames=csv_line.keys())
                                writer.writeheader()
                                first_line = False
                            writer.writerow(csv_line)

                self.log("Done! Scraping completed successfully.", classes="text-blue")
            else:
                self.log("No AI output found...", classes="text-red")
        except Exception as e:
            self.log(f"ERROR: {str(e)}", classes="text-red")
            print(str(e))
            import traceback
            self.log(traceback.format_exc())
        finally:
            self.stop()


    def parse_ai_overview(self, page_id, query) -> dict:
        time.sleep(1)
        sel = self.ao_selectors
        while True:
            ai_overview = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_container"])
            if ai_overview and ai_overview[0].is_displayed():
                ai_overview = ai_overview[0]
                ai_overview_inner = ai_overview.text

                if any(generating_string in ai_overview_inner for generating_string in self.generating_strings):
                    retries = 0
                    while retries <= 5:
                        retries += 1
                        self.log("AI Overview may not be done generating, waiting 2 secs...", classes="text-orange")
                        time.sleep(2)
                        ai_overview = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_container"])
                        if ai_overview:
                            ai_overview = ai_overview[0]
                            break

                # Wait for as long as the 'generating' animations are there.
                generating_waits = 0
                while generating_waits < 10:
                    generating_div = ai_overview.find_elements(by=By.CSS_SELECTOR, value="#folsrch-ghost")
                    if generating_div and generating_div[0].is_displayed():
                        time.sleep(0.5)
                        generating_waits += 1
                        continue
                    break
                else:
                    self.log("Warning: #folsrch-ghost never appeared, continuing anyway", classes="text-orange")


                ai_overview_data = {
                    "mode": "ai_overview",
                    "id": page_id,
                    "from_url": self.driver.current_url,
                    "query": query,
                    "timestamp_scraped": datetime.now().isoformat(),
                    "timestamp_scraped_unix": int(datetime.timestamp(datetime.now())),
                    "not_available": False,
                    "text": "",
                    "sources": []
                }

                show_more_button = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_show_more"])
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
                else:
                    if show_more_button:
                        try:
                            show_more_button[0].click()
                        except (StaleElementReferenceException, ElementNotInteractableException):
                            self.log("Could not find the 'Show more' button anymore, continuing", classes="text-orange")

                    ai_overview_inner = None
                    retry_ai_overview_count = 0
                    while not ai_overview_inner:
                        ai_overview_inner = ai_overview.find_elements(by=By.CSS_SELECTOR,
                                                                     value=sel["ao_inner_text"])

                        retry_ai_overview_count += 1
                        if retry_ai_overview_count > 5:
                            self.log("AI Overview hidden or not found, skipping...", classes="text-orange")
                            return {}

                    ai_overview_inner = ai_overview_inner[0].get_attribute("outerHTML")

                    ai_overview_contents_md = md(ai_overview_inner, strip=["a", "img"])
                    ai_overview_data["text"] = ai_overview_contents_md

                    # Get sources
                    urls = []
                    show_more_urls_button = ai_overview.find_elements(by=By.CSS_SELECTOR,
                                                                      value=sel["ao_show_more_urls"])
                    # If there's 3 sources or a horizontal scroller, you can't expand
                    if show_more_urls_button:
                        for show_more_url_button in show_more_urls_button:
                            try:
                                self.wait.until(lambda _: show_more_url_button.is_displayed())
                                if show_more_url_button.is_displayed():
                                    try:
                                        show_more_url_button.click()
                                    except ElementNotInteractableException:
                                        self.log("ERROR: Could not expand URL box, continuing anyway", classes="text-orange")
                            except TimeoutException:
                                self.log("ERROR: Could not expand URL box, continuing anyway", classes="text-orange")


                        url_divs = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["ao_url_divs"])
                    else:
                        url_divs = self.driver.find_elements(by=By.CSS_SELECTOR,
                                                             value=sel["ao_url_divs_no_show_more_button"])
                    url_divs += self.driver.find_elements(by=By.CSS_SELECTOR,
                                                          value=sel["ao_url_attribution"])
                    for url_div in url_divs:
                        if url_div.is_displayed():
                            url_div_a = url_div.find_element(by=By.CSS_SELECTOR, value="a")
                            url_div_url = url_div_a.get_attribute("href")
                            url_div_description = url_div.find_elements(by=By.CSS_SELECTOR, value=sel["ao_url_description"])
                            url_div_description += url_div.find_elements(by=By.CSS_SELECTOR, value=sel["ao_url_description_fallback"])
                            description = url_div_description[0].text if url_div_description else ""

                            url = {
                                "title": url_div_a.get_attribute("aria-label"),
                                "description": description,
                                "domain": urlparse(url_div_url).netloc,
                                "url": url_div_url,
                            }
                            urls.append(url)

                    ai_overview_data["sources"] = urls

                return ai_overview_data
            else:
                return {}

    def parse_ai_mode(self, page_id, query) -> dict:
        time.sleep(4)

        sel = self.am_selectors

        while True:
            ai_mode = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["am_answers_container"])
            if ai_mode and ai_mode[0].is_displayed():
                ai_mode = ai_mode[0]
                ai_mode_inner = ai_mode.text

                if (any(generating_string in ai_mode_inner for generating_string in self.generating_strings)
                        or not ai_mode.find_elements(by=By.CSS_SELECTOR,
                                                     value=sel["am_processed_answer"])):
                    retries = 0
                    while retries <= 5:
                        retries += 1
                        self.log("AI Overview may not be done generating, waiting 2 secs...", classes="text-orange")
                        time.sleep(2)
                        generated = True if ai_mode.find_elements(by=By.CSS_SELECTOR,
                                                                  value=sel["am_processed_answer"]) else False
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
                    "sources": []
                }

                throttled = None
                for throttled_string in self.throttled_strings:
                    if throttled_string in ai_mode_inner:
                        throttled = throttled_string
                        break
                if throttled:
                    self.log(f"Throttled (found: '{throttled}'), trying again in 10 minutes. Keep the browser window open.", classes="text-orange")
                    time.sleep(600)
                    self.driver.refresh()
                    self.check_for_captcha()
                    continue

                show_more_urls_button = ai_mode.find_elements(by=By.CSS_SELECTOR,
                                                              value=sel["am_show_more_urls"])
                if show_more_urls_button:
                    for show_more_url_button in show_more_urls_button:
                        try:
                            self.wait.until(lambda _: show_more_url_button.is_displayed())
                            if show_more_url_button.is_displayed():
                                try:
                                    show_more_url_button.click()
                                except ElementNotInteractableException:
                                    self.log("ERROR: Could not expand URL box, continuing anyway", classes="text-orange")
                        except TimeoutException:
                            self.log("ERROR: Could not expand URL box, continuing anyway", classes="text-orange")

                ai_mode_html = ai_mode.find_elements(by=By.CSS_SELECTOR, value=sel["am_answers"])
                if not ai_mode_html:
                    time.sleep(6)
                    ai_mode.find_elements(by=By.CSS_SELECTOR, value=sel["am_answers"])
                ai_mode_html = ai_mode_html[0]
                ai_mode_html = ai_mode_html.get_attribute("outerHTML")

                ai_mode_contents_md = md(ai_mode_html, strip=["a", "img"])
                ai_mode_data["text"] = ai_mode_contents_md.split("AI-reactions can")[0]

                urls = []
                url_divs = self.driver.find_elements(by=By.CSS_SELECTOR, value=sel["am_url_box"])

                for url_div in url_divs:
                    url_div_a = url_div.find_element(by=By.CSS_SELECTOR, value="a")
                    url_div_url = url_div_a.get_attribute("href")
                    url_div_description = url_div.find_elements(by=By.CSS_SELECTOR, value=sel["am_url_description"])
                    description = url_div_description[0].text if url_div_description else ""

                    url = {
                        "title": url_div_a.get_attribute("aria-label"),
                        "description": description,
                        "domain": urlparse(url_div_url).netloc,
                        "url": url_div_url,
                    }
                    urls.append(url)

                ai_mode_data["sources"] = urls
                ai_mode_data["not_available"] = False
                break

        return ai_mode_data if ai_mode_data and ai_mode_data.get("text") else {}

    def save_html(self, query, page_id, mode):
        html_dir = self.output_dir + "/html"
        if not os.path.isdir(html_dir):
            os.mkdir(html_dir)

        filename_query = re.sub(r'[\\/*?:"<>|]', "", query)
        filename_base = f"{page_id}_{mode}_{filename_query}"
        filename = filename_base + ".html"
        html_location = f"{html_dir}/{filename}"

        with open(html_location, "w", encoding="utf-8") as out_html:
            out_html.write(self.driver.page_source)

    def save_screenshot(self, query, page_id, mode):
        screenshots_dir = self.output_dir + "/screenshots"
        if not os.path.isdir(screenshots_dir):
            os.mkdir(screenshots_dir)

        filename_query = query
        filename_query = re.sub(r'[\\/*?:"<>|]', "", filename_query)
        filename_base = f"{page_id}_{mode}_{filename_query}"
        filename = filename_base + ".png"
        screenshot_location = f"{screenshots_dir}/{filename}"

        if mode == "ai_overviews":
            window_height = self.driver.execute_script("return document.documentElement.scrollHeight")
            self.driver.set_window_size(1920, window_height)
        else:
            chat_box_height = self.driver.execute_script(f"return document.querySelector('div.WzWwpc').offsetHeight;")
            height_maximalized = 300 + int(chat_box_height)
            window_height = 1080 if height_maximalized < 1080 else height_maximalized
            self.driver.set_window_size(1920, window_height)

        self.driver.execute_script("window.scrollTo(0, 0);")
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
                self.driver.refresh()
        if retries > max_retries:
            raise Exception("Couldn't load page after 10 retries")

    def check_for_captcha(self):
        captcha = self.driver.find_elements(By.CSS_SELECTOR, "#recaptcha")
        if captcha and captcha[0].is_displayed():
            self.log("CAPTCHA detected, please solve it in the Firefox window...", classes="text-orange")
            while captcha and captcha[0].is_displayed():
                time.sleep(2)
                captcha = self.driver.find_elements(By.CSS_SELECTOR, "#recaptcha")
        return

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
        self.log(f"Processed in {duration:.2f}s (avg {avg_duration:.2f}s over last {len(self.scrape_durations)}), {hours}h {minutes}m left")


# NICEGUI INTERFACE
def load_readme() -> str:
    with open('README.md', 'r', encoding='utf-8') as file:
        return "\n".join(file.readlines())

def load_css() -> str:
    with open('stylesheet.css', 'r', encoding='utf-8') as file:
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
            ui.image('logo_cutout.png').style('width: 75px; height: 75px; object-fit: cover; flex-shrink: 0;')
            with ui.row().classes('p-4 self-center items-baseline gap-3'):
                ui.label('Kenniskrabber').classes('main-title');
                ui.label('v0.5').classes('main-title version')

        with ui.row().classes('w-full p-4 gap-4 items-stretch'):
            # LEFT COLUMN - SETTINGS
            with ui.column().classes('w-1/2 min-w-[400px]'):
                with ui.card().classes('w-full'):
                    ui.label('What to collect').classes('card-title text-xl mb-2')

                    self.mode = ui.select(
                        ['AI Overviews & AI Modes', 'AI Overviews', 'AI Modes'],
                        value='AI Overviews & AI Modes'
                    ).classes('w-full')

                    self.input_method = ui.radio([self.label_insert, self.label_queryfile], value=self.label_insert).props(
                        'inline')

                    # Visibility is bound to the radio button state
                    default_query_file = get_default_output_dir(input_file=True)
                    self.file_input = ui.input('Query file csv location', value=default_query_file).classes('w-full') \
                        .bind_visibility_from(self.input_method, 'value', value=self.label_queryfile)

                    self.text_input = ui.textarea('Enter queries (one per line)',
                                                  placeholder="is scraping legal?\nmacy conference\nDSA audits") \
                        .classes('w-full').bind_visibility_from(self.input_method, 'value', value=self.label_insert)
                    self.shuffle = ui.checkbox('Shuffle queries')


                    default_output_folder = get_default_output_dir()
                    self.output_dir = ui.input('Output directory', value=default_output_folder).classes('full-width')

            with ui.column().classes('w-1/2 min-w-[400px]'):
                with ui.card().classes('w-full'):
                    ui.label('Scrape settings').classes('card-title text-xl mb-2')
                    self.profile = ui.input('Firefox Profile Path (Optional)',
                                            placeholder='C:/Users/User/AppData/Roaming/Mozilla/Firefox/Profiles/xxx.default').classes(
                        'w-full')

                    with ui.row().classes('w-full gap-2'):
                        self.tld = ui.input('Top-level domain', value='.com').classes('flex-grow')
                        self.offset = ui.number('Offset', value=0, format='%.0f').classes('flex-grow')

                    with ui.row().classes('w-full items-center gap-1'):
                        self.gen_strings = ui.input('Generating text', value='Searching, Generating, Thinking').classes('flex-grow')
                        with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                            ui.tooltip('Comma-separated, case-sensitive texts that appear while an AI Overview/Mode answer is still generating, in the language of your Google (e.g. "Searching, Generating, Thinking")')
                    with ui.row().classes('w-full items-center gap-1'):
                        self.throttle_strings = ui.input('Throttled text', value='Something went wrong, Try again later').classes('flex-grow')
                        with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                            ui.tooltip('Comma-separated, case-sensitive texts that appear when Google throttles your AI Overview/Mode requests, in the language of your Google (e.g. "Something went wrong, Try again later")')

                    # CSS Selectors accordion
                    AO_SELECTOR_META = {
                        "ao_container": ("Container", "Main AI Overview container element"),
                        "ao_show_more": ("Show more btn", "Button to expand the full AI Overview"),
                        "ao_failed_elements": ("Failed elements", "Elements indicating AI Overview failed to load"),
                        "ao_inner_text": ("Inner text", "Element containing the AI Overview inner text"),
                        "ao_show_more_urls": ("Show more URLs btn", "Button to expand the source URL list"),
                        "ao_url_divs": ("URL items", "List items for sources after clicking the 'Show more' button"),
                        "ao_url_divs_no_show_more_button": ("URL items without 'show more'", "List items for sources if there's no 'Show more"
                                                                                             "  button."),
                        "ao_url_attribution": ("URL attribution", "Attribution feedback div for sources"),
                        "ao_url_description": ("URL description", "Element with the source description text"),
                        "ao_url_description_fallback": ("URL desc. fallback", "Fallback element for source description"),
                    }
                    AM_SELECTOR_META = {
                        "am_answers_container": ("Container", "Main AI Mode answers container element"),
                        "am_answers": ("Answers", "Element containing the AI Mode answer content"),
                        "am_processed_answer": ("Processed answer", "Element indicating the answer is fully generated"),
                        "am_show_more_urls": ("Show more URLs btn", "Button to expand the source URL list"),
                        "am_url_box": ("URL items", "List items for source URLs"),
                        "am_url_description": ("URL description", "Element with the source description text"),
                    }

                    with ui.expansion('CSS Selectors').classes('w-full'):
                        self.selector_toggle = ui.toggle(
                            ['AI Overview', 'AI Mode'], value='AI Overview'
                        ).classes('w-full mb-2').props('spread no-wrap')

                        # AI Overview selectors panel
                        self.ao_selector_inputs = {}
                        ao_defaults = GoogleAIScraper.DEFAULT_AO_SELECTORS
                        with ui.column().classes('w-full gap-1').bind_visibility_from(
                                self.selector_toggle, 'value', value='AI Overview') as self.ao_panel:
                            for key, (label, tooltip) in AO_SELECTOR_META.items():
                                with ui.row().classes('w-full items-center gap-1'):
                                    inp = ui.input(label, value=ao_defaults[key]).classes('flex-grow').props('dense')
                                    with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                                        ui.tooltip(tooltip)
                                    self.ao_selector_inputs[key] = inp

                        # AI Mode selectors panel
                        self.am_selector_inputs = {}
                        am_defaults = GoogleAIScraper.DEFAULT_AM_SELECTORS
                        with ui.column().classes('w-full gap-1').bind_visibility_from(
                                self.selector_toggle, 'value', value='AI Mode') as self.am_panel:
                            for key, (label, tooltip) in AM_SELECTOR_META.items():
                                with ui.row().classes('w-full items-center gap-1'):
                                    inp = ui.input(label, value=am_defaults[key]).classes('flex-grow').props('dense')
                                    with ui.icon('help_outline').classes('text-grey cursor-pointer'):
                                        ui.tooltip(tooltip)
                                    self.am_selector_inputs[key] = inp
            # LOGS AND ACTIONS
            with ui.column().classes('w-1/2 min-w-[400px] flex-grow'):
                with ui.card().classes('w-full'):
                    ui.label('Run').classes('card-title text-xl mb-2')
                    with ui.row().classes('w-full gap-2'):
                        self.btn_prepare = ui.button('1. Prepare Browser', on_click=self.on_prepare).classes(
                            'flex-grow')
                        self.btn_start = ui.button('2. Start Scraping', on_click=self.on_start).classes(
                            'flex-grow')
                        self.btn_start.disable()
                        self.btn_stop = ui.button('Stop', color='red', on_click=self.on_stop).classes(
                            'flex-grow')
                        self.btn_stop.disable()

                with ui.card().classes('w-full h-96 flex-grow'):
                    ui.label('Status').classes('card-title text-xl')

                    self.progress_label = ui.label('Progress: 0/0')
                    self.progress_bar = ui.linear_progress(value=0, show_value=False).classes('w-full mb-4')

                    self.btn_open_folder = ui.button('Open output folder', color='red', on_click=self.open_output_folder) \
                        .classes('w-full flex-grow')
                    self.btn_open_folder.set_visibility(False)

                    self.log_scroll = ui.scroll_area().classes('w-full h-full rounded')
                    with self.log_scroll:
                        self.log_column = ui.column().classes('w-full p-2 gap-0')

        with ui.row().classes('footer-text w-full p-2 justify-center'):
            ui.html('<a href="https://deep-culture.org" target="_blank">Deep Culture</a> · <a href="https://digitalmethods.net" target="_blank">Digital Methods Initiative</a>').classes('text-xs footer-text')

    def update_progress(self, current, total):
        self.progress_label.set_text(f'Progress: {current}/{total}')
        self.progress_bar.set_value(current / total if total > 0 else 0)

    def log_push(self, msg, classes=None):
        """Push a styled line into the log scroll area."""
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

        # Handle the temporary query file if user used text area
        query_file = self.file_input.value
        if self.input_method.value == 'Insert queries':
            temp_file = os.path.join(self.output_dir.value or './', 'temp_manual_queries.csv')
            os.makedirs(os.path.dirname(temp_file), exist_ok=True)
            queries = [q.strip() for q in self.text_input.value.split('\n') if q.strip()]

            with open(temp_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['query'])
                for q in queries:
                    writer.writerow([q])
            query_file = temp_file

        # Handle shuffling logic
        if self.shuffle.value and os.path.isfile(query_file):
            shuffled_filename = query_file[:-4] + "_shuffled.csv"
            self.log_push(f"Shuffling rows to {shuffled_filename}")
            df = pd.read_csv(query_file)
            df = df.sample(frac=1)
            df.to_csv(shuffled_filename, index=False)
            query_file = shuffled_filename

        scrape_mode = self.mode.value.lower().replace(" ", "_")
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
            generating_strings=self.gen_strings.value,
            throttled_strings=self.throttle_strings.value,
            ao_selectors={k: inp.value for k, inp in self.ao_selector_inputs.items()},
            am_selectors={k: inp.value for k, inp in self.am_selector_inputs.items()},
            progress_callback=self.update_progress,
            log_callback=self.log_push  # Send all scraper prints straight to the GUI
        )

        # Run prepare in a background thread so the UI doesn't freeze!
        success = await run.io_bound(self.scraper.prepare)

        if success:
            self.btn_start.enable()
        else:
            self.btn_prepare.enable()

    async def on_start(self):
        self.btn_start.disable()
        self.btn_stop.enable()

        # Run process in a background thread
        await run.io_bound(self.scraper.process)

        self.btn_prepare.enable()
        self.btn_stop.disable()
        self.btn_open_folder.set_visibility(True)

    def on_stop(self):
        self.btn_stop.disable()
        if self.scraper:
            self.scraper.stop()

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

def root():
    ui.label('Hello from PyInstaller')

if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

nicegui_app.add_static_files('/static', os.path.dirname(os.path.abspath(__file__)))
gui = GUI()
ui.run(root, title='Kenniskrabber', native=True, reload=False, window_size=(500, 800))
