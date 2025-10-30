import asyncio
from playwright.async_api import async_playwright
import json
import logging
import os
from urllib.parse import urlparse, urljoin
from typing import Optional, Set, List, Dict
import traceback
import time
from dataclasses import dataclass, field
import hashlib

# import sqlite3
import aiosqlite
from bs4 import BeautifulSoup
from datetime import datetime

# --- logging setup (ensure directory exists) ---
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/playwright.log",
    encoding="utf-8",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s",
)


@dataclass
class ThreadSafeState:
    """Thread-safe state container using asyncio locks"""

    scraped_urls: Set[str] = field(default_factory=set)
    results_all: List[Dict] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add_scraped_url(self, url: str) -> bool:
        """
        Add URL to scraped set. Returns True if added, False if already exists.
        Thread-safe operation.
        """
        async with self._lock:
            if url in self.scraped_urls:
                return False
            self.scraped_urls.add(url)
            logging.info(
                f"Added URL to scraped set: {url} (Total: {len(self.scraped_urls)})"
            )
            return True

    async def has_scraped_url(self, url: str) -> bool:
        """Check if URL has been scraped. Thread-safe operation."""
        async with self._lock:
            return url in self.scraped_urls

    async def add_result(self, result: Dict) -> None:
        """Add scraping result. Thread-safe operation."""
        async with self._lock:
            self.results_all.append(result)
            logging.info(
                f"Added result for URL: {result.get('url', 'unknown')} (Total results: {len(self.results_all)})"
            )

    async def get_results_copy(self) -> List[Dict]:
        """Get a copy of all results. Thread-safe operation."""
        async with self._lock:
            return self.results_all.copy()

    async def get_stats(self) -> Dict[str, int]:
        """Get statistics about scraping progress. Thread-safe operation."""
        async with self._lock:
            return {
                "scraped_urls": len(self.scraped_urls),
                "results_count": len(self.results_all),
            }


class Database_Handler:
    """Manages SQLite database operations for scraping results"""

    def __init__(self, db_path: str = "scraper_data.db"):
        self.db_path = db_path
        self._initialized = False

    async def _init_db(self):
        """Ensure the table exists (run once)."""
        if self._initialized:
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE,
                    title TEXT,
                    elements_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
            await db.commit()
        self._initialized = True
        logging.info("[DB] Initialized database and ensured table exists.")

    async def insert_page(self, url: str, title: str, elements: dict):
        """Insert or update a page record asynchronously."""
        try:
            await self._init_db()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO pages (url, title, elements_json)
                    VALUES (?, ?, ?)
                """,
                    (url, title, json.dumps(elements, ensure_ascii=False)),
                )
                await db.commit()
            logging.info(f"[DB] Saved page: {url}")
        except Exception as e:
            logging.error(f"[DB] Error inserting page {url}: {e}")

    async def fetch_all_pages(self):
        """Fetch all saved pages asynchronously."""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id, url, title, elements_json FROM pages"
            ) as cursor:
                rows = await cursor.fetchall()
        return rows


class RecursiveScraper:
    def __init__(self, start_url, max_concurrency=1, avoid_page=None):
        self.start_url = start_url
        parsed = urlparse(start_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.max_concurrency = max_concurrency
        self.to_visit = asyncio.Queue()
        self.pause_event = asyncio.Event()
        self.pause_event.set()  # Initially not paused
        self.worker_pages: Dict[int, Set] = {}
        self.unique_element = []

        self.navigatable_elements = {}

        # Replace shared state with thread-safe container
        self.state = ThreadSafeState()
        self.db = Database_Handler("scraper_data.db")

        self.avoid_page = avoid_page or []

        # Add statistics tracking
        self.stats_lock = asyncio.Lock()
        self.stats = {
            "urls_queued": 0,
            "urls_processed": 0,
            "urls_skipped": 0,
            "errors": 0,
        }

        # Optional: Add click lock for extra safety
        self.click_lock = asyncio.Lock()

    async def update_stats(self, key: str, increment: int = 1):
        """Update statistics in a thread-safe manner"""
        async with self.stats_lock:
            self.stats[key] = self.stats.get(key, 0) + increment

    def should_skip_link(self, current_url: str, href: Optional[str]) -> bool:
        """Return True if the link should be skipped."""
        if not href:
            logging.info("Skipping link due to empty href")
            return True

        # Check if href contains avoid words
        if any(word and word in href for word in self.avoid_page):
            logging.info(f"Skipping link due to avoid_page match: {href}")
            return True

        href = href.strip()
        # Skip non-navigational links
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return True

        # Convert relative to absolute
        abs_url = urljoin(current_url, href)
        abs_url = abs_url.split("?")[0]
        abs_url = abs_url.split("#")[0]

        parsed_abs = urlparse(abs_url)

        # External link?
        if f"{parsed_abs.scheme}://{parsed_abs.netloc}" != self.origin:
            logging.info(f"Skipping external link: {abs_url}")
            return True

        return False

    async def wait_until_ready(
        self, page, timeout: int = 5, settle_time: float = 1, previous_url=None
    ):
        """Wait for full page + JS/XHR + DOM mutations to settle (async Playwright version)"""

        if previous_url and page.url != previous_url:
            logging.info(
                f"Page URL changed from {previous_url} to {page.url} during wait_until_ready"
            )
            start = time.time()
            while time.time() - start < timeout:
                if page.url.split("?")[0].split("#")[0] == previous_url:
                    break
                await asyncio.sleep(0.3)
                logging.info(
                    f"Waiting for page to stabilize at {previous_url}, current URL: {page.url}"
                )

        # Wait for document readyState
        await page.wait_for_function(
            "document.readyState === 'complete'", timeout=timeout * 1000
        )

        # Hook XHR/fetch and MutationObserver
        await page.evaluate(
            """
            if (!window.__activeRequestsHooked) {
                window.__activeRequests = 0;
                window.__activeRequestsHooked = true;

                // XHR
                (function(open) {
                    XMLHttpRequest.prototype.open = function() {
                        window.__activeRequests++;
                        this.addEventListener('loadend', function() {
                            window.__activeRequests--;
                        });
                        open.apply(this, arguments);
                    };
                })(XMLHttpRequest.prototype.open);

                // Fetch
                (function(fetch) {
                    window.fetch = function() {
                        window.__activeRequests++;
                        return fetch.apply(this, arguments).finally(() => {
                            window.__activeRequests--;
                        });
                    };
                })(window.fetch);
            }

            if (!window.__lastMutationTime) {
                window.__lastMutationTime = Date.now();
                const observer = new MutationObserver(() => {
                    window.__lastMutationTime = Date.now();
                });
                observer.observe(document, { childList: true, subtree: true, attributes: true, characterData: true });
            }
        """
        )

        # Wait for both network + DOM to settle
        start = time.time()
        while True:
            active_requests = await page.evaluate("window.__activeRequests")
            last_mutation = await page.evaluate("window.__lastMutationTime")
            elapsed_since_last_mutation = (time.time() * 1000) - last_mutation
            logging.info(
                f"Active requests: {active_requests}, ms since last DOM mutation: {elapsed_since_last_mutation:.0f}"
            )
            if (
                active_requests == 0
                and elapsed_since_last_mutation > settle_time * 1000
            ):
                logging.info("Page is ready")
                break

            if time.time() - start > timeout:
                logging.warning("Timeout waiting for page readiness")
                break

            await asyncio.sleep(0.1)

    get_xpath_js = """
(el) => {
    function getXPath(el) {
        if (el.nodeType === Node.DOCUMENT_NODE) return '';
        if (el === document.body) return '/html/body';
        let ix = 0;
        let siblings = el.parentNode ? el.parentNode.childNodes : [];
        for (let i = 0; i < siblings.length; i++) {
            let sib = siblings[i];
            if (sib.nodeType === 1 && sib.tagName === el.tagName) ix++;
            if (sib === el) {
                // use *[name()='tag'] for svg elements
                const tagName = (el.namespaceURI === 'http://www.w3.org/2000/svg')
                    ? "*[name()='" + el.tagName.toLowerCase() + "']"
                    : el.tagName.toLowerCase();
                return getXPath(el.parentNode) + '/' + tagName + '[' + ix + ']';
            }
        }
    }
    // use the provided parameter `el` (arrow functions don't have `arguments`)
    return getXPath(el);
}
"""

    async def scrape_tab(self, context, url, worker_id: int):
        """Scrape visible elements and click interactive pointer elements recursively."""
        page = None
        try:
            await self.pause_event.wait()
            page = await context.new_page()
            self.worker_pages[worker_id].add(page)

            await page.goto(url, timeout=15000)
            # await page.wait_for_load_state("networkidle")
            await self.wait_until_ready(page, timeout=15, settle_time=1)

            body = await page.query_selector("body")
            if not body:
                logging.warning(f"Worker {worker_id}: No body tag found on {url}")
                return

            all_elements = await body.query_selector_all("*")
            page_details = {
                "title": await page.title(),
                "url": page.url,
                "elements": [],
            }
            logging.info(
                f"Worker {worker_id}: Found {len(all_elements)} elements on {url}"
            )

            visible_elements = []
            previous_url = page.url
            xpaths = []

            # collect all the xpath of visible elements
            for element in all_elements:
                if await element.is_visible():
                    xpath = await element.evaluate(self.get_xpath_js)
                    xpaths.append(xpath)

            # to avoid click both parent and child elements
            parent_xpath = None
            duplicate_parent_xpath = None
            previous_element_xpath = None
            clciked_xpaths = []
            for xpath in xpaths:
                await self.pause_event.wait()
                try:
                    element = await page.query_selector(f"xpath={xpath}")
                    if not element:
                        continue

                    tag = await element.evaluate("(el) => el.tagName")
                    if tag.lower() in ["script", "style", "meta", "link", "noscript"]:
                        continue

                    attrs = await element.evaluate(
                        "(el) => { let a = {}; for (let attr of el.attributes) a[attr.name] = attr.value; return a; }"
                    )
                    direct_text = await element.evaluate(
                        """
                        (el) => Array.from(el.childNodes)
                            .filter(n => n.nodeType === Node.TEXT_NODE)
                            .map(n => n.textContent.trim())
                            .filter(t => t.length > 0)
                            .join(' ')
                        """
                    )

                    element_data = {
                        "tag": tag.lower(),
                        "text": direct_text,
                        "attributes": attrs,
                        "xpath": xpath,
                    }

                    all_text = await element.evaluate("(el) => el.innerText || ''")

                    # comparing all the text including child elements to avoid duplicates and check modifications
                    element_data_include_child_text = {
                        "tag": tag.lower(),
                        "text": direct_text,
                        "attributes": attrs,
                        "xpath": xpath,
                        "all_text": all_text,
                    }

                    attrs_sorted = sorted(attrs.items())
                    # Combine all element info into a list
                    key_list = [tag.lower(), xpath, direct_text, attrs_sorted]

                    # Serialize to JSON string
                    key_str = json.dumps(key_list, sort_keys=True)

                    # Create hashcode
                    key_hash = hashlib.sha256(key_str.encode()).hexdigest()
                    if duplicate_parent_xpath and self.is_child_xpath(
                        duplicate_parent_xpath, xpath
                    ):
                        logging.info(
                            f"[Worker {worker_id}] Skipping element inside duplicate parent: {xpath} in {url}"
                        )
                        navigation = self.navigatable_elements.get(key_hash)
                        if navigation:
                            logging.info(
                                f"[Worker {worker_id}] Found previous navigation for duplicate element: {navigation}"
                            )
                            element_data["navigated_to"] = navigation
                        visible_elements.append(element_data)
                        continue
                    elif element_data_include_child_text in self.unique_element:
                        duplicate_parent_xpath = xpath
                        logging.info(
                            f"[Worker {worker_id}] Duplicate element skipped: {xpath} in {url}"
                        )
                        # Check if we have navigation info for this duplicate
                        navigation = self.navigatable_elements.get(key_hash)
                        if navigation:
                            logging.info(
                                f"[Worker {worker_id}] Found previous navigation for duplicate element: {navigation}"
                            )
                            element_data["navigated_to"] = navigation
                        visible_elements.append(element_data)
                        continue
                    else:
                        self.unique_element.append(element_data_include_child_text)

                    # clickable check
                    if (
                        await self.is_probably_clickable(element)
                        and await element.is_enabled()
                        and not self.is_child_xpath(parent_xpath, xpath)
                    ):
                        href = attrs.get("href")
                        if tag.lower() == "a" and self.should_skip_link(
                            previous_url, href
                        ):
                            continue

                        element_handle = page.locator(f"xpath={xpath}").first
                        handle = await element_handle.element_handle()
                        if not handle:
                            continue

                        logging.info(f"[Worker {worker_id}] Clicking element: {xpath}")

                        # Set up new page detection BEFORE clicking
                        new_page_future = asyncio.Future()

                        def on_popup(popup):
                            if not new_page_future.done():
                                new_page_future.set_result(popup)

                        # Register the popup listener
                        page.on("popup", on_popup)

                        try:
                            # Optional: Use click lock for extra safety
                            # async with self.click_lock:

                            # getting the dom details before clicking
                            dom_before = await page.evaluate(
                                "document.documentElement.outerHTML"
                            )

                            # Click the element
                            await handle.evaluate("(el) => el.click()")
                            parent_xpath = xpath
                            clciked_xpaths.append(xpath)

                            # Wait for popup with timeout
                            try:
                                new_tab = await asyncio.wait_for(
                                    new_page_future, timeout=2.0
                                )
                                self.worker_pages[worker_id].add(new_tab)

                                await new_tab.wait_for_load_state("domcontentloaded")
                                new_url = new_tab.url
                                logging.info(
                                    f"[Worker {worker_id}] Detected new tab: {new_url}"
                                )
                                element_data["opened_new_tab"] = new_url
                                self.navigatable_elements[key_hash] = new_url
                                if not self.should_skip_link(previous_url, new_url):
                                    already_scraped = await self.state.has_scraped_url(
                                        new_url
                                    )
                                    if not already_scraped:
                                        await self.to_visit.put(new_url)
                                        await self.update_stats("urls_queued")
                                        logging.info(
                                            f"[Worker {worker_id}] Queued new tab: {new_url}"
                                        )

                                try:
                                    await new_tab.close()
                                    self.worker_pages[worker_id].discard(new_tab)
                                    logging.info(
                                        f"[Worker {worker_id}] Closed new tab: {new_url}"
                                    )
                                except Exception as e:
                                    logging.warning(
                                        f"[Worker {worker_id}] Could not close tab: {e}"
                                    )

                                await page.bring_to_front()
                                await self.wait_until_ready(
                                    page, timeout=10, settle_time=1.0
                                )

                            except asyncio.TimeoutError:
                                # No popup opened, check for same-tab navigation
                                logging.info(
                                    f"[Worker {worker_id}] No popup detected, checking for navigation"
                                )

                                await page.wait_for_load_state(
                                    "domcontentloaded", timeout=30000
                                )
                                await self.wait_until_ready(
                                    page, timeout=15, settle_time=1
                                )

                                if page.url != previous_url:
                                    new_url = page.url.split("?")[0].split("#")[0]
                                    element_data["navigated_to"] = new_url
                                    self.navigatable_elements[key_hash] = new_url
                                    logging.info(
                                        f"[Worker {worker_id}] Detected same-tab navigation to: {new_url}"
                                    )
                                    if not self.should_skip_link(previous_url, new_url):
                                        already_scraped = (
                                            await self.state.has_scraped_url(new_url)
                                        )
                                        if not already_scraped:
                                            await self.to_visit.put(new_url)
                                            await self.update_stats("urls_queued")
                                            logging.info(
                                                f"[Worker {worker_id}] Queued navigation: {new_url}"
                                            )
                                        else:
                                            logging.info(
                                                f"[Worker {worker_id}] Navigation URL already scraped: {new_url}"
                                            )

                                    await page.go_back()
                                    await self.wait_until_ready(
                                        page, timeout=15, settle_time=1
                                    )
                                else:
                                    logging.info(
                                        f"[Worker {worker_id}] No navigation occurred after click."
                                    )
                                    dom_after = await page.evaluate(
                                        "document.documentElement.outerHTML"
                                    )
                                    if dom_before != dom_after:
                                        if previous_element_xpath is None:
                                            previous_element_xpath = (
                                                clciked_xpaths[
                                                    clciked_xpaths.index(xpath) - 1
                                                ]
                                                if clciked_xpaths.index(xpath) > 0
                                                else None
                                            )
                                        logging.info(
                                            f"[Worker {worker_id}] DOM changed after click on {xpath} in {url}."
                                        )
                                        soup_old = BeautifulSoup(
                                            dom_before, "html.parser"
                                        )
                                        soup_new = BeautifulSoup(
                                            dom_after, "html.parser"
                                        )

                                        old_elements = self.extract_dom_structure(
                                            soup_old
                                        )
                                        new_elements = self.extract_dom_structure(
                                            soup_new
                                        )

                                        changes = self.compare_doms(
                                            old_elements, new_elements, True
                                        )

                                        if changes["added"]:
                                            logging.info("internal routes detected ")
                                            element_data["internal_routes"] = changes[
                                                "added"
                                            ]

                                        previous_element = (
                                            await page.query_selector(
                                                f"xpath={previous_element_xpath}"
                                            )
                                            if previous_element_xpath
                                            else None
                                        )
                                        if previous_element:
                                            logging.info(
                                                f"[Worker {worker_id}] Returning to previous element {previous_element_xpath} after DOM change."
                                            )
                                            await previous_element.evaluate(
                                                "(el) => el.click()"
                                            )
                                            await self.wait_until_ready(
                                                page, timeout=15, settle_time=1
                                            )

                                        else:
                                            logging.info(
                                                f"[Worker {worker_id}] No previous element to return to after DOM change so clciking the same elements."
                                            )
                                            await element.evaluate("(el) => el.click()")
                                            await self.wait_until_ready(
                                                page, timeout=15, settle_time=1
                                            )
                                    else:
                                        logging.info(
                                            f"[Worker {worker_id}] DOM did not change after click on {xpath} in {url}."
                                        )
                                        previous_element_xpath = None

                        finally:
                            # Remove the popup listener
                            page.remove_listener("popup", on_popup)
                    visible_elements.append(element_data)

                except Exception as e:
                    logging.error(
                        f"[Worker {worker_id}] Error processing element {xpath}: {e}"
                    )
                    await self.update_stats("errors")
                    continue

            page_details["elements"] = visible_elements
            await self.state.add_result(page_details)
            # Save page details in SQLite DB
            try:
                await self.db.insert_page(
                    url=page_details["url"],
                    title=page_details["title"],
                    elements=page_details["elements"],
                )
                logging.info(
                    f"[Worker {worker_id}] Page saved to database: {page_details['url']}"
                )
            except Exception as e:
                logging.error(f"[Worker {worker_id}] Error saving to DB: {e}")

            await self.update_stats("urls_processed")

        except Exception as e:
            logging.error(
                f"[Worker {worker_id}] Fatal error in scrape_tab for {url}: {e}"
            )
            logging.error(traceback.format_exc())
            await self.update_stats("errors")

        finally:
            if page:
                try:
                    await page.close()
                    self.worker_pages[worker_id].discard(page)
                except Exception as e:
                    logging.error(f"[Worker {worker_id}] Error closing page: {e}")

    def extract_dom_structure(self, soup):
        elements = []
        for element in soup.find_all(True):  # True = all tags
            xpath = self.get_xpath_bs(element)

            normalized_attrs = {}
            for k, v in element.attrs.items():
                if isinstance(v, list):
                    normalized_attrs[k] = tuple(v)
                else:
                    normalized_attrs[k] = v

            direct_texts = element.find_all(string=True, recursive=False)
            direct_text = " ".join(t.strip() for t in direct_texts if t.strip())
            data = {
                "tag": element.name,
                "attrs": normalized_attrs,
                "xpath": xpath,
                "text": direct_text,
            }
            elements.append(data)
        return elements

    def get_xpath_bs(self, el):
        parts = []
        while el and getattr(el, "name", None):
            parent = el.parent

            # Stop before the document root
            if not parent or getattr(parent, "name", None) in (
                None,
                "[document]",
            ):
                parts.append(el.name)
                break

            # Collect tag siblings in DOM order (ignore strings/comments)
            same_tag_siblings = [
                sib for sib in parent.children if getattr(sib, "name", None) == el.name
            ]

            # Determine index among siblings (1-based like XPath)
            if len(same_tag_siblings) > 1:
                try:
                    index = same_tag_siblings.index(el) + 1
                    parts.append(f"{el.name}[{index}]")
                except ValueError:
                    # fallback if BS made a copy or parsing glitch
                    parts.append(f"{el.name}[1]")
            else:
                parts.append(el.name)

            el = parent

        parts.reverse()

        # Fix duplicate /html/html case
        if len(parts) >= 2 and parts[0] == "html" and parts[1] == "html":
            parts = parts[1:]

        return "/" + "/".join(parts)

    def compare_doms(self, old, new, is_include_text=False):
        # Filter out <body> elements
        logging.info("comparing doms to detect changes")
        old_filtered = [e for e in old if e["tag"].lower() != "body"]
        new_filtered = [e for e in new if e["tag"].lower() not in ["body", "style"]]

        if is_include_text:

            def serialize_attrs(attrs):
                return tuple(sorted((k, v) for k, v in attrs.items()))

            logging.info("comparing doms with text included")
            old_set = {(e["xpath"], e["text"]) for e in old_filtered}

            # Keep the order of new elements
            added = [
                e["xpath"]
                for e in new_filtered
                if (e["xpath"], e["text"]) not in old_set
            ]
            logging.info(f"added elements with text {added}")
            return {"added": added}

        old_set = {(e["xpath"]) for e in old_filtered}

        # Keep the order of new elements
        added = [(e["xpath"]) for e in new_filtered if (e["xpath"]) not in old_set]
        logging.info(f"added elements {added}")
        return {"added": added}

    def is_child_xpath(self, parent_xpath, child_xpath):
        """
        Returns True if parent_xpath is a prefix of child_xpath
        (i.e., child_xpath is inside parent_xpath)
        """
        if parent_xpath is None:
            return False
        # Normalize: remove trailing slashes
        parent_xpath = parent_xpath.rstrip("/")
        child_xpath = child_xpath.rstrip("/")

        # Check if child starts with parent + "/" or is exactly equal
        return child_xpath == parent_xpath or child_xpath.startswith(parent_xpath + "/")

    async def is_probably_clickable(self, element):
        """Return True if element is likely clickable."""
        try:
            onclick = await element.get_attribute("onclick")
            if onclick:
                return True

            tag = (await element.evaluate("(el) => el.tagName")).lower()
            if tag in ("a", "button"):
                return True

            role = await element.get_attribute("role")
            if role == "button":
                return True

            cursor = await element.evaluate("(el) => getComputedStyle(el).cursor")
            if cursor == "pointer":
                return True

            return False
        except Exception as e:
            logging.error(f"Error checking if element is clickable: {e}")
            return False

    async def worker(self, context, worker_id: int):
        """Worker that takes URLs from queue"""
        logging.info(f"Worker {worker_id} started")
        self.worker_pages[worker_id] = set()

        while True:

            await self.pause_event.wait()
            try:
                url = await self.to_visit.get()

                if url is None:
                    logging.info(f"Worker {worker_id} received stop signal.")
                    self.to_visit.task_done()
                    break

                was_added = await self.state.add_scraped_url(url)
                if not was_added:
                    logging.info(f"Worker {worker_id}: URL already scraped: {url}")
                    await self.update_stats("urls_skipped")
                    self.to_visit.task_done()
                    continue

                logging.info(f"Worker {worker_id}: Processing URL: {url}")
                await self.scrape_tab(context, url, worker_id)
                self.to_visit.task_done()

            except asyncio.CancelledError:
                logging.info(f"Worker {worker_id} cancelled")
                try:
                    self.to_visit.task_done()
                except Exception:
                    pass
                break

            except Exception as e:
                logging.error(f"Worker {worker_id}: Unexpected error: {e}")
                logging.error(f"Traceback:\n{traceback.format_exc()}")
                await self.update_stats("errors")
                try:
                    self.to_visit.task_done()
                except Exception:
                    pass
                continue

    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--disable-web-security", "--window-size=1920,1080"],
            )
            context = await browser.new_context()

            # Add initial URL to queue
            await self.to_visit.put(self.start_url)
            await self.update_stats("urls_queued")

            # Create workers with IDs for debugging
            workers = [
                asyncio.create_task(self.worker(context, i))
                for i in range(self.max_concurrency)
            ]

            try:
                # Wait for all enqueued items to be processed
                await self.to_visit.join()
                logging.info("Queue is empty — sending stop signals to workers...")

                # Send one sentinel None per worker so they exit gracefully
                for _ in range(self.max_concurrency):
                    await self.to_visit.put(None)

                # Wait for workers to exit after receiving sentinel
                await asyncio.gather(*workers)

                logging.info("All workers finished processing.")

            except KeyboardInterrupt:
                logging.info("Received keyboard interrupt, shutting down early...")
                # On early shutdown, cancel workers
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

            finally:
                # Close the browser and save results
                try:
                    await browser.close()
                except Exception as e:
                    logging.error(f"Error closing browser: {e}")

                # Thread-safe retrieval of results
                results = await self.state.get_results_copy()
                stats = await self.state.get_stats()

                # Print final statistics
                print("\n" + "=" * 50)
                print("SCRAPING COMPLETE")
                print("=" * 50)
                print(f"URLs Queued: {self.stats['urls_queued']}")
                print(f"URLs Processed: {self.stats['urls_processed']}")
                print(f"URLs Skipped: {self.stats['urls_skipped']}")
                print(f"Errors: {self.stats['errors']}")
                print(f"Unique URLs Scraped: {stats['scraped_urls']}")
                print(f"Results Collected: {stats['results_count']}")
                print("=" * 50 + "\n")

                # Save results to file
                try:
                    with open("website.json", "w", encoding="utf-8") as f:
                        json.dump(results, f, ensure_ascii=False, indent=4)
                    logging.info(
                        f"Results saved to website.json ({len(results)} pages)"
                    )
                except Exception as e:
                    logging.error(f"Error saving results: {e}")
                    # Try to save to backup file
                    try:
                        backup_file = f"website_backup_{int(time.time())}.json"
                        with open(backup_file, "w", encoding="utf-8") as f:
                            json.dump(results, f, ensure_ascii=False, indent=4)
                        logging.info(f"Results saved to backup file: {backup_file}")
                    except Exception as backup_error:
                        logging.error(f"Failed to save backup: {backup_error}")


if __name__ == "__main__":
    asyncio.run(RecursiveScraper("http://localhost:3000/", max_concurrency=5).run())
