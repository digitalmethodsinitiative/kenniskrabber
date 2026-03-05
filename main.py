import json
import csv
import os
import hashlib
import time
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
	def __init__(self, scrape_mode="both", base_url=None, profile="",
				 queries=None, query_file=None, results_file="", output_dir="", offset=0,
				 shuffle_queries=False, generating_strings=None, throttled_strings=None,
				 progress_callback=None, log_callback=None):
		self.scrape_mode = scrape_mode
		self.base_url = base_url if base_url else "https://www.google.com/"
		self.driver = None
		self.wait = None
		self.queries = queries
		self.query_file = query_file
		self.results_file = results_file
		self.output_dir = output_dir
		self.shuffle_queries = shuffle_queries
		self.offset = offset
		self.generating_strings = [g_s.strip() for g_s in generating_strings.split(",")] if generating_strings else [
			"Searching", "Generating", "Thinking..."]
		self.throttled_strings = [t_s.strip() for t_s in throttled_strings.split(",")] if throttled_strings else [
			"Try again later", "Something went wrong"]
		self.progress_callback = progress_callback
		self.log_callback = log_callback or print
		self.profile = profile
		self.is_running = False

		self.finished_ai_overview_queries = set()
		self.finished_ai_mode_queries = set()

	def log(self, msg):
		"""Send messages to the NiceGUI log UI"""
		if self.log_callback:
			self.log_callback(str(msg))
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
				self.log("Found existing results file. Assuming you want to skip already scraped queries.")
				with open(self.results_file, "rb") as f:
					for line in f.readlines():
						try:
							finished_query = json.loads(line)
							if finished_query.get("mode") == "ai_overviews":
								self.finished_ai_overview_queries.add(finished_query["id"])
							if finished_query.get("mode") == "ai_modes":
								self.finished_ai_mode_queries.add(finished_query["id"])
						except:
							pass
				self.log(
					f"Already collected {len(self.finished_ai_overview_queries)} AI Overviews and {len(self.finished_ai_mode_queries)} AI Modes")

			self.log(
				"Browser ready. Please complete any CAPTCHAs or logins in the Firefox window, then press '2. Start Scraping'.")
			return True

		except Exception as e:
			self.log(f"ERROR: {str(e)}")
			import traceback
			self.log(traceback.format_exc())
			return False

	def process(self):
		self.is_running = True
		iterate_queries = set()

		n_urls = 0
		try:
			self.log("Getting queries")
			print(self.query_file)
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

						iterate_queries.add((query_id, query))

			elif self.queries:
				for line in self.queries.split("\n"):
					n_urls += 1
					if self.offset and n_urls < self.offset:
						continue
					query = line.strip()
					query_id = hashlib.md5(query.encode()).hexdigest()

					iterate_queries.add((query_id, query))

			count = 0
			print(iterate_queries)
			print(self.scrape_mode)
			for query_and_id in iterate_queries:

				if not self.is_running:
					self.log("Scraping stopped by user.")
					break

				page_id = query_and_id[0]
				query = query_and_id[1]
				self.log(f"Getting data for url {count}/{n_urls}: {query}")

				if self.progress_callback:
					self.progress_callback(count, n_urls)

				# --- AI Overviews ---
				if self.scrape_mode in ["ai_overviews_&_ai_modes", "ai_overviews"]:
					if page_id not in self.finished_ai_overview_queries:
						start = time.time()
						mode = "ai_overviews"
						query_url = f"{self.base_url}search?q={self.get_url_string(query)}"
						self.open_page(query_url)

						# Replace this with your actual parse_ai_overview call
						self.log(f"Parsing AI Overview for: {query}")

						result = self.parse_ai_overview(page_id, query)

						if result:
							with open(self.results_file, "a", encoding="utf-8") as out_file:
								out_file.write(json.dumps(result) + "\n")

						self.save_html(query, page_id, mode)
						self.save_screenshot(query, page_id, mode)
						self.finished_ai_overview_queries.add(page_id)
						self.print_elapsed_time(start, n_urls, count)
					else:
						self.log("Already scraped AI Overview data, skipping")

				# --- AI Modes ---
				if self.scrape_mode in ["ai_overviews_&_ai_modes", "ai_modes"]:
					mode = "ai_modes"
					if page_id not in self.finished_ai_mode_queries:
						start = time.time()
						query_url = f"{self.base_url}search?udm=50&q={self.get_url_string(query)}"
						self.open_page(query_url)

						# Replace this with your actual parse_ai_mode call
						self.log(f"Parsing AI Mode for: {query}")
						result = self.parse_ai_mode(page_id, query)

						if result:
							with open(self.results_file, "a", encoding="utf-8") as out_file:
								out_file.write(json.dumps(result) + "\n")

						self.save_html(query, page_id, mode)
						self.save_screenshot(query, page_id, mode)
						self.finished_ai_mode_queries.add(page_id)
						self.print_elapsed_time(start, n_urls, count)
					else:
						self.log("Already scraped AI Mode data, skipping")

				count += 1

			if self.is_running:
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

				self.log("Done! Scraping completed successfully.")

		except Exception as e:
			self.log(f"ERROR: {str(e)}")
			import traceback
			self.log(traceback.format_exc())
		finally:
			self.stop()


	def parse_ai_overview(self, page_id, query) -> dict:
		time.sleep(1)
		while True:
			ai_overview = self.driver.find_elements(by=By.CSS_SELECTOR, value="#eKIzJc")
			if ai_overview and ai_overview[0].is_displayed():
				ai_overview = ai_overview[0]
				ai_overview_inner = ai_overview.text

				if any(generating_string in ai_overview_inner for generating_string in self.generating_strings):
					while True:
						print("AI overview answer may not be done generating, waiting 2 secs...")
						time.sleep(2)
						ai_overview = self.driver.find_elements(by=By.CSS_SELECTOR, value="#eKIzJc")
						if ai_overview:
							ai_overview = ai_overview[0]
							break

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

				show_more_button = self.driver.find_elements(by=By.CSS_SELECTOR, value="div.zNsLfb.Jzkafd")
				failed_elements = ai_overview.find_elements(by=By.CSS_SELECTOR, value=".YWpX0d[style='']")
				throttled = False
				ai_overview_inner_text = ai_overview.find_elements(by=By.CSS_SELECTOR, value=".YWpX0d")
				if ai_overview_inner_text:
					ai_overview_inner_text = ai_overview_inner_text[0].get_attribute("innerHTML")
				if ai_overview_inner_text and any(
						throttled_string in ai_overview_inner_text for throttled_string in self.throttled_strings):
					throttled = True

				if not show_more_button and (failed_elements or throttled):
					if failed_elements:
						ai_overview_data["text"] = ai_overview.text
					else:
						ai_overview_data["text"] = ai_overview.get_attribute("innerHTML")
					ai_overview_data["not_available"] = True

					if throttled:
						print("Throttled, trying again in 10 minutes")
						time.sleep(600)
						self.driver.refresh()
						self.check_for_captcha()
						continue
				else:
					if show_more_button:
						try:
							show_more_button[0].click()
						except (StaleElementReferenceException, ElementNotInteractableException):
							print("Could not find the 'Show more' button anymore, continuing")

					show_more_urls_button = ai_overview.find_elements(by=By.CSS_SELECTOR,
																	  value="li > div > div.niO4u.VDgVie.SlP8xc")
					if show_more_urls_button:
						for show_more_button in show_more_urls_button:
							self.wait.until(lambda _: show_more_button.is_displayed())
							if show_more_button.is_displayed():
								try:
									show_more_button.click()
								except ElementNotInteractableException:
									print("ERROR: Could not expand URL box, continuing anyway")

					ai_overview_html = None
					retry_ai_overview_count = 0
					while not ai_overview_html:
						ai_overview_html = ai_overview.find_elements(by=By.CSS_SELECTOR,
																	 value="div[jsname][data-rl] > div:not([id])")

						retry_ai_overview_count += 1
						if retry_ai_overview_count > 5:
							print("AI Overview hidden or not found, skipping...")
							return {}

					ai_overview_html = ai_overview_html[0]
					ai_overview_html = ai_overview_html.get_attribute("outerHTML")

					ai_overview_contents_md = md(ai_overview_html, strip=["a", "img"])
					ai_overview_data["text"] = ai_overview_contents_md

					urls = []
					url_divs = self.driver.find_elements(by=By.CSS_SELECTOR, value="ul.zVKf0d.w2xCsc > li.LLtSOc")
					if not url_divs:
						url_divs = self.driver.find_elements(by=By.CSS_SELECTOR,
															 value="ul.zVKf0d.Cgh8Qc > li.LLtSOc")
					url_divs += self.driver.find_elements(by=By.CSS_SELECTOR,
														  value="div[data-attrid='SGEAttributionFeedback']")
					for url_div in url_divs:
						if url_div.is_displayed():
							url_div_a = url_div.find_element(by=By.CSS_SELECTOR, value="a")
							url_div_url = url_div_a.get_attribute("href")
							url_div_description = url_div.find_elements(by=By.CSS_SELECTOR, value=".gxZfx")
							url_div_description += url_div.find_elements(by=By.CSS_SELECTOR, value=".dMCttd")
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
		while True:
			ai_mode = self.driver.find_elements(by=By.CSS_SELECTOR, value="section")
			if ai_mode and ai_mode[0].is_displayed():
				ai_mode = ai_mode[0]
				ai_mode_inner = ai_mode.text

				if (any(generating_string in ai_mode_inner for generating_string in self.generating_strings)
						or not ai_mode.find_elements(by=By.CSS_SELECTOR,
													 value="section #aim-chrome-initial-inline-async-container > div[data-processed=true]")):
					while True:
						print("AI overview may not be done generating, waiting 2 secs...")
						time.sleep(2)
						generated = True if ai_mode.find_elements(by=By.CSS_SELECTOR,
																  value="section #aim-chrome-initial-inline-async-container > div[data-processed=true]") else False
						if generated:
							break
				time.sleep(1)

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

				throttled = False
				if any(throttled_string in ai_mode_inner for throttled_string in self.throttled_strings):
					throttled = True
				if throttled:
					print("Throttled, trying again in 10 minutes. Keep the browser window open.")
					time.sleep(600)
					self.driver.refresh()
					self.check_for_captcha()
					continue

				show_more_urls_button = ai_mode.find_elements(by=By.CSS_SELECTOR,
															  value="div[data-processed=true] > div.BjvG9b")
				if show_more_urls_button:
					for show_more_button in show_more_urls_button:
						try:
							self.wait.until(lambda _: show_more_button.is_displayed())
							if show_more_button.is_displayed():
								try:
									show_more_button.click()
								except ElementNotInteractableException:
									print("ERROR: Could not expand URL box, continuing anyway")
						except TimeoutException:
							print("ERROR: Could not expand URL box, continuing anyway")

				main_col_id = "#aim-chrome-initial-inline-async-container div[data-container-id=main-col]"
				ai_mode_html = ai_mode.find_elements(by=By.CSS_SELECTOR, value=main_col_id)
				if not ai_mode_html:
					time.sleep(6)
					ai_mode.find_elements(by=By.CSS_SELECTOR, value=main_col_id)
				ai_mode_html = ai_mode_html[0]
				ai_mode_html = ai_mode_html.get_attribute("outerHTML")

				ai_mode_contents_md = md(ai_mode_html, strip=["a", "img"])
				ai_mode_data["text"] = ai_mode_contents_md.split("AI-reacties kunnen")[0]

				urls = []
				url_divs = self.driver.find_elements(by=By.CSS_SELECTOR, value="li.CyMdWb > div > div[data-ved]")

				for url_div in url_divs:
					url_div_a = url_div.find_element(by=By.CSS_SELECTOR, value="a")
					url_div_url = url_div_a.get_attribute("href")
					url_div_description = url_div.find_elements(by=By.CSS_SELECTOR, value=".vhJ6Pe")
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
				self.log("Firefox browser closed.")
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
				self.log(f"Page timed out, trying again in 10 seconds (retry {retries}/{max_retries})...")
				time.sleep(10)
				self.driver.refresh()
		if retries > max_retries:
			raise Exception("Couldn't load page after 10 retries")

	def check_for_captcha(self):
		captcha = self.driver.find_elements(By.CSS_SELECTOR, "#recaptcha")
		if captcha and captcha[0].is_displayed():
			self.log("CAPTCHA detected, please solve it in the Firefox window...")
			while captcha and captcha[0].is_displayed():
				time.sleep(2)
				captcha = self.driver.find_elements(By.CSS_SELECTOR, "#recaptcha")
		return

	def get_url_string(self, query: str) -> str:
		return query.lower().strip().replace(" ", "+")

	def print_elapsed_time(self, start_time, n_urls, count):
		end = time.time()
		time_elapsed = end - start_time
		td = timedelta(seconds=(n_urls - count) * time_elapsed)
		total_minutes = td.seconds // 60
		hours, minutes = divmod(total_minutes, 60)
		self.log(f"Processed in {time_elapsed:.2f} seconds, {hours} hours and {minutes} minutes left")


# --- NICEGUI INTERFACE ---
def load_readme() -> str:
	with open('README.md', 'r', encoding='utf-8') as file:
		return file.read()
class GUI:
	def __init__(self):
		self.scraper = None
		self.label_queryfile = 'Use query file'
		self.label_insert = 'Insert queries'
		self.setup_ui()


	def setup_ui(self):
		ui.colors(primary='#1976D2', secondary='#26A69A', accent='#9C27B0', positive='#21BA45')

		with ui.header().classes('bg-primary text-white p-4'):
			ui.label('Kenniskrabber').classes('text-2xl font-bold')

		with ui.row().classes('w-full p4'):
			with ui.column().classes('w-1/2 min-w-[400px]'):
				ui.markdown(load_readme(), sanitize=False)

		with ui.row().classes('w-full p-4 gap-4 items-stretch'):
			# LEFT COLUMN - SETTINGS
			with ui.column().classes('w-1/2 min-w-[400px]'):
				with ui.card().classes('w-full'):
					ui.label('What to collect').classes('text-xl font-bold mb-2')

					self.mode = ui.select(
						['AI Overviews & AI Modes', 'AI Overviews', 'AI Modes'],
						value='AI Overviews & AI Modes', label='What to scrape'
					).classes('w-full')

					ui.label('Queries').classes('text-xl font-bold mb-2')

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

					ui.label('Browser and scrape settings').classes('text-xl font-bold mb-2')
					self.profile = ui.input('Firefox Profile Path (Optional)',
											placeholder='C:/Users/User/AppData/Roaming/Mozilla/Firefox/Profiles/xxx.default').classes(
						'w-full')

					with ui.row().classes('w-full gap-2'):
						self.tld = ui.input('Top-level domain', value='.com').classes('flex-grow')
						self.offset = ui.number('Offset', value=0, format='%.0f').classes('flex-grow')

					self.gen_strings = ui.input('Generating text', value='Searching, Generating, Thinking').classes(
						'w-full')
					self.throttle_strings = ui.input('Throttled text',
													 value='Something went wrong, Try again later').classes('w-full')
			# RIGHT COLUMN - LOGS AND ACTIONS
			with ui.column().classes('w-1/2 min-w-[400px] flex-grow'):
				with ui.card().classes('w-full'):
					ui.label('Run').classes('text-xl font-bold mb-2')
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
					ui.label('Status').classes('font-bold')

					self.progress_label = ui.label('Progress: 0/0')
					self.progress_bar = ui.linear_progress(value=0, show_value=False).classes('w-full mb-4')

					self.log_area = ui.log().classes('w-full h-full bg-gray-100 p-2 rounded text-sm font-mono')

	def update_progress(self, current, total):
		self.progress_label.set_text(f'Progress: {current}/{total}')
		self.progress_bar.set_value(current / total if total > 0 else 0)

	async def on_prepare(self):
		self.log_area.clear()
		self.btn_prepare.disable()

		# Handle the temporary query file if user used text area
		query_file = self.file_input.value
		if self.input_method.value == 'Insert Queries':
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
			self.log_area.push(f"Shuffling rows to {shuffled_filename}")
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
			queries=self.text_input.value if self.input_method.value == self.label_insert else None,
			query_file=query_file if self.input_method.value == self.label_queryfile else None,
			offset=int(self.offset.value),
			generating_strings=self.gen_strings.value,
			throttled_strings=self.throttle_strings.value,
			progress_callback=self.update_progress,
			log_callback=self.log_area.push  # Send all scraper prints straight to the GUI
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

	def on_stop(self):
		self.btn_stop.disable()
		if self.scraper:
			self.scraper.stop()


# Run the NiceGUI app
app = GUI()
ui.run(title='Google AI Scraper', native=True, reload=True, window_size=(500, 800))
