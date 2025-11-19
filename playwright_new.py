import asyncio
from playwright.async_api import (
    async_playwright,
    Page,
    Locator,
    ElementHandle,
    TimeoutError as PlaywrightTimeoutError,
)
import json
import logging
import os
from urllib.parse import urlparse, urljoin
from typing import Optional, Set, List, Dict
import traceback
import time
from dataclasses import dataclass, field
import hashlib
import re

# import sqlite3
import aiosqlite

# --- logging setup (ensure directory exists) ---
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/playwright1.log",
    encoding="utf-8",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s",
)


@dataclass
class ThreadSafeState:
    """Thread-safe state container using asyncio locks"""

    scraped_urls: Set[str] = field(default_factory=set)
    results_all: List[Dict] = field(default_factory=list)
    unique_element_hashes: Set[str] = field(default_factory=set)
    navigatable_elements: Dict[str, str] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add_scraped_url(self, url: str, helper_id) -> bool:
        """
        Add URL to scraped set. Returns True if added, False if already exists.
        Thread-safe operation.
        """
        async with self._lock:
            if url in self.scraped_urls:
                return False
            self.scraped_urls.add(url)
            logging.info(
                f"helper {helper_id} Added URL to scraped set: {url} (Total: {len(self.scraped_urls)})"
            )
            return True

    async def has_scraped_url(self, url: str) -> bool:
        """Check if URL has been scraped. Thread-safe operation."""
        async with self._lock:
            return url in self.scraped_urls

    async def add_result(self, result: Dict, helper_id) -> None:
        """Add scraping result. Thread-safe operation."""
        async with self._lock:
            self.results_all.append(result)
            logging.info(
                f"helper {helper_id} Added result for URL: {result.get('url', 'unknown')} (Total results: {len(self.results_all)})"
            )

    async def add_unique_hash(self, key_hash: str) -> bool:
        """
        Add element hash to set. Returns True if added, False if already exists.
        Thread-safe operation.
        """
        async with self._lock:
            if key_hash in self.unique_element_hashes:
                return False
            self.unique_element_hashes.add(key_hash)
            return True

    async def get_navigation(self, key_hash: str) -> Optional[str]:
        """Get a cached navigation URL for a given element hash. Thread-safe."""
        async with self._lock:
            return self.navigatable_elements.get(key_hash)

    async def add_navigation(self, key_hash: str, url: str):
        """Cache a navigation URL for a given element hash. Thread-safe."""
        async with self._lock:
            self.navigatable_elements[key_hash] = url

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
        os.makedirs("databases", exist_ok=True)
        self.db_path = os.path.join("databases", db_path)
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

    async def insert_page(self, url: str, title: str, elements: dict, helper_id: int):
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
            logging.info(f"helper {helper_id} [DB] Saved page: {url}")
        except Exception as e:
            logging.error(f"helper {helper_id} [DB] Error inserting page {url}: {e}")

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
        self.helper_pages: Dict[int, Set] = {}
        # self.unique_element = []

        # self.navigatable_elements = {}

        # Replace shared state with thread-safe container
        self.state = ThreadSafeState()
        netloc = re.sub(r"[^a-zA-Z0-9._-]", "_", parsed.netloc)
        self.db = Database_Handler(f"{netloc}.db")
        self.avoid_page = avoid_page or []
        self.avoid_buttons = [
            "logout",
            "log out",
            "sign out",
            "signout",
            "delete",
            "remove",
            "cancel",
            "close account",
            "deactivate",
        ]

        # Add statistics tracking
        self.stats_lock = asyncio.Lock()
        self.stats = {
            "urls_queued": 0,
            "urls_processed": 0,
            "urls_skipped": 0,
            "errors": 0,
        }

        # Optional: Add click lock for extra safety
        # self.click_lock = asyncio.Lock()

    async def update_stats(self, key: str, increment: int = 1):
        """Update statistics in a thread-safe manner"""
        async with self.stats_lock:
            self.stats[key] = self.stats.get(key, 0) + increment

    async def should_skip_link(
        self, current_url: str, href: Optional[str], helper_id: int
    ) -> bool:
        """Return True if the link should be skipped."""
        if not href:
            logging.info(f"helper {helper_id} Skipping link due to empty href")
            return True

        # Check if href contains avoid words
        if any(word and word in href for word in self.avoid_page):
            logging.info(
                f"helper {helper_id} Skipping link due to avoid_page match: {href}"
            )
            return True

        href = href.strip()
        # Skip non-navigational links
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return True

        # Convert relative to absolute
        abs_url = urljoin(current_url, href)
        # abs_url = abs_url.split("?")[0]
        abs_url = abs_url.split("#")[0]

        parsed_abs = urlparse(abs_url)

        # External link?
        if f"{parsed_abs.scheme}://{parsed_abs.netloc}" != self.origin:
            logging.info(f"helper {helper_id} Skipping external link: {abs_url}")
            return True

        return False

    async def wait_until_ready(
        self, page, timeout: int = 5, settle_time: float = 1, helper_id: int = 0
    ):
        """Wait for full page + JS/XHR + DOM mutations to settle (async Playwright version)"""
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
                f"helper {helper_id} Active requests: {active_requests}, ms since last DOM mutation: {elapsed_since_last_mutation:.0f}"
            )
            if (
                active_requests == 0
                and elapsed_since_last_mutation > settle_time * 1000
            ):
                logging.info(f"helper {helper_id} Page is ready")
                break

            if time.time() - start > timeout:
                logging.warning(
                    f"helper {helper_id} Timeout waiting for page readiness"
                )
                break

            await asyncio.sleep(0.1)

    get_xpath_js = """
(el) => {
  function getXPath(el) {
    if (!el) return '';
    if (el.nodeType === Node.DOCUMENT_NODE) return '';
    // If it's the body element, return a nice canonical path
    if (el === document.body) return '/html/body';

    // Only work with elements
    if (el.nodeType !== Node.ELEMENT_NODE) {
      // fallback: use parent element
      return getXPath(el.parentElement);
    }

    const tagIsSVG = el.namespaceURI === 'http://www.w3.org/2000/svg';
    const tagName = tagIsSVG ? "*[name()='" + el.tagName.toLowerCase() + "']" : el.tagName.toLowerCase();

    // Count how many previous sibling elements of the same tag exist,
    // then index = previousCount + 1 (1-based)
    let index = 1;
    for (let sib = el.previousElementSibling; sib; sib = sib.previousElementSibling) {
      if (tagIsSVG) {
        // for SVG we compare lowercased tagName to be safe
        if (sib.tagName && sib.tagName.toLowerCase() === el.tagName.toLowerCase()) {
          index++;
        }
      } else {
        if (sib.tagName === el.tagName) index++;
      }
    }

    const parentPath = getXPath(el.parentElement);
    return parentPath + '/' + tagName + '[' + index + ']';
  }

  return getXPath(el);
}
"""

    async def scrape_tab(self, context, url, helper_id: int):
        """Scrape visible elements and click interactive pointer elements recursively."""
        page = None
        try:
            await self.pause_event.wait()
            page = await context.new_page()
            self.helper_pages[helper_id].add(page)

            success = False
            for attempt in range(3):  # Try 3 times
                try:
                    await page.goto(url, timeout=20000)  # Increased timeout
                    await self.wait_until_ready(
                        page,
                        timeout=20,
                        settle_time=5,
                        helper_id=helper_id,  # Increased timeout
                    )
                    success = True
                    break  # Success!
                except Exception as e:
                    logging.warning(
                        f"[helper {helper_id}] Attempt {attempt + 1}/3 failed for {url}: {e}"
                    )
                    if attempt == 2:  # Last attempt failed
                        raise  # Re-raise the exception to be caught by outer block
                    await asyncio.sleep(3)  # Wait 3 seconds before retrying

            if not success:
                logging.error(
                    f"[helper {helper_id}] is unable to open the link {url} by 3 Attempts"
                )
                return

            page_details = {
                "title": await page.title(),
                "url": page.url,
                "elements": [],
            }
            await self.remove_widget(page=page, helper_id=helper_id)

            popups = await self.detect_and_handle_popups(page, helper_id)
            if popups:
                page_details["popups"] = popups

            visible_elements_xpaths = await self.get_visible_elements_xpaths(
                page=page, helper_id=helper_id, purpose="scrape_tab"
            )
            logging.info(
                f"helper {helper_id}: Found {len(visible_elements_xpaths)} visible elements on {url}"
            )

            visible_elements = await self.details_from_thre_xpath(
                visible_elements_xpaths, page, url, helper_id, visible_elements_xpaths
            )

            page_details["elements"] = visible_elements
            await self.state.add_result(page_details, helper_id)
            # Save page details in SQLite DB
            try:
                await self.db.insert_page(
                    url=page_details["url"],
                    title=page_details["title"],
                    elements=page_details["elements"],
                    helper_id=helper_id,
                )
                logging.info(
                    f"[helper {helper_id}] Page saved to database: {page_details['url']}"
                )
            except Exception as e:
                logging.error(f"[helper {helper_id}] Error saving to DB: {e}")

            await self.update_stats("urls_processed")

        except Exception as e:
            logging.error(
                f"[helper {helper_id}] Fatal error in scrape_tab for {url}: {e}"
            )
            logging.error(traceback.format_exc())
            await self.update_stats("errors")

        finally:
            if page:
                try:
                    await page.close()
                    self.helper_pages[helper_id].discard(page)
                except Exception as e:
                    logging.error(f"[helper {helper_id}] Error closing page: {e}")

    async def details_from_thre_xpath(
        self,
        xpaths,
        page,
        url,
        helper_id,
        visible_elements_xpaths,
        hover_element=None,
        internal_route_element=None,
        internal_clicked_elements=[],
    ) -> List[Dict]:
        """
        Given a list of xpaths, return detailed info about each element"""
        # to avoid click both parent and child elements
        previous_url = page.url
        parent_xpath = None
        duplicate_parent_xpath = None
        previous_element_xpath = None
        visible_elements = []
        clciked_xpaths = []
        internal_route_detected_element = []
        current_url = page.url
        for xpath in xpaths:
            is_dropdown_detected = False
            await self.pause_event.wait()
            try:
                if hover_element:
                    await self.safe_hover(
                        page,
                        hover_element,
                        helper_id=helper_id,
                    )
                    logging.info(
                        f"[helper {helper_id}] Hovered over hover_element before processing: {await hover_element.evaluate(self.get_xpath_js)} in {url} for {xpath}"
                    )
                element = page.locator(f"xpath={xpath}")
                if not await element.count():
                    logging.warning(
                        f"[helper {helper_id}] Element not found for XPath: {xpath} in {url} let refresh the page and retrying..."
                    )
                    await page.goto(current_url)
                    await page.wait_for_load_state("domcontentloaded")
                    await self.wait_until_ready(
                        page, timeout=15, settle_time=2, helper_id=helper_id
                    )
                    await self.remove_widget(page=page, helper_id=helper_id)
                    # conditional hover abd click back internal route element
                    if internal_route_element:
                        for internal_clicked_element in internal_clicked_elements:
                            if internal_clicked_element:
                                await self.safe_click(
                                    internal_clicked_element,
                                    helper_id=helper_id,
                                    xpath=await internal_clicked_element.evaluate(
                                        self.get_xpath_js
                                    ),
                                )
                    if hover_element:
                        await self.safe_hover(
                            page,
                            hover_element,
                            helper_id=helper_id,
                        )
                        logging.info(
                            f"[helper {helper_id}] Hovered over hover_element before processing: {await hover_element.evaluate(self.get_xpath_js)} in {url} for {xpath}"
                        )
                    element = page.locator(f"xpath={xpath}")
                    if not await element.count():
                        logging.error(
                            f"[helper {helper_id}] Element not found for XPath: {xpath} in {url} after refresh the page"
                        )
                        continue

                tag = (await element.evaluate("(el) => el.tagName")).lower()
                if tag in ["script", "style", "meta", "link", "noscript"]:
                    logging.info(
                        f"[helper {helper_id}] Skipping non-relevant tag: {tag} for XPath: {xpath} in {url}"
                    )
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
                    "tag": tag,
                    "text": direct_text,
                    "attributes": attrs,
                    "xpath": xpath,
                }

                all_text = await element.evaluate("(el) => el.innerText || ''")

                # comparing all the text including child elements to avoid duplicates and check modifications
                element_data_include_child_text = {
                    "tag": tag,
                    "text": direct_text,
                    "attributes": attrs,
                    "xpath": xpath,
                    "all_text": all_text,
                }

                attrs_sorted = sorted(attrs.items())
                # Combine all element info into a list
                key_list = [tag, xpath, direct_text, attrs_sorted, all_text]

                # Serialize to JSON string
                key_str = json.dumps(key_list, sort_keys=True)

                # Create hashcode
                key_hash = hashlib.sha256(key_str.encode()).hexdigest()
                if "?" not in page.url:
                    if duplicate_parent_xpath and await self.is_child_xpath(
                        duplicate_parent_xpath, xpath
                    ):
                        logging.info(
                            f"[helper {helper_id}] Skipping element inside duplicate parent: {xpath} in {url} for {element_data_include_child_text}"
                        )
                        navigation = await self.state.get_navigation(key_hash)
                        if navigation:
                            logging.info(
                                f"[helper {helper_id}] Found previous navigation for duplicate element: {navigation}"
                            )
                            element_data["navigated_to"] = navigation
                        visible_elements.append(element_data)
                        continue
                    was_added = await self.state.add_unique_hash(key_hash)
                    if not was_added:
                        duplicate_parent_xpath = xpath
                        logging.info(
                            f"[helper {helper_id}] Duplicate element skipped: {xpath} in {url} for {element_data_include_child_text} "
                        )
                        # Check if we have navigation info for this duplicate
                        navigation = await self.state.get_navigation(key_hash)
                        if navigation:
                            logging.info(
                                f"[helper {helper_id}] Found previous navigation for duplicate element: {navigation}"
                            )
                            element_data["navigated_to"] = navigation
                        visible_elements.append(element_data)
                        continue
                    else:
                        # Not a duplicate, so clear the duplicate parent tracker
                        duplicate_parent_xpath = None

                # clickable check with clickable, enabled and not child of previously clicked
                if (
                    await self.is_probably_clickable(element, helper_id)
                    and await element.is_enabled()
                    and not await self.is_child_xpath(parent_xpath, xpath)
                ):
                    map_before_hover = await self.get_visible_content_map(
                        page, visible_elements_xpaths, helper_id
                    )
                    hash_before_hover = await self.get_hash_from_map(map_before_hover)

                    processed_text = (
                        await element.evaluate("(el) => el.innerText || ''")
                    )[:30].lower()
                    if any(word in processed_text for word in self.avoid_buttons):
                        logging.info(
                            f"[helper {helper_id}] Skipping click on avoid button element: {xpath} in {url}"
                        )
                        visible_elements.append(element_data)
                        parent_xpath = xpath
                        continue
                    await self.wait_until_ready(
                        page, timeout=10, settle_time=1.0, helper_id=helper_id
                    )
                    visible_elements_before_hover = (
                        await self.get_visible_elements_xpaths(
                            page=page, helper_id=helper_id, purpose="before_hover"
                        )
                    )
                    hovered = await self.safe_hover(page, element, helper_id)
                    if not hovered:
                        logging.info(
                            f"[helper {helper_id}] Could not hover over element: {xpath} in {url}"
                        )
                    else:
                        logging.info(
                            f"[helper {helper_id}] Hovered over element: {xpath} in {url}"
                        )
                        # detect dropdowns after hover
                        dropdown_xpaths = await self.detect_dropdown_after_hover(
                            page,
                            map_before_hover,
                            hash_before_hover,
                            helper_id,
                            visible_elements_before_hover,
                            element,
                        )
                        if dropdown_xpaths:
                            element_data["dropdowns_after_hover"] = dropdown_xpaths
                            is_dropdown_detected = True
                            await asyncio.sleep(1.0)
                            await self.wait_until_ready(page, helper_id=helper_id)

                    href = attrs.get("href")
                    if tag.lower() == "a" and await self.should_skip_link(
                        previous_url, href, helper_id
                    ):
                        logging.info(
                            f"[helper {helper_id}] Skipping click on link element: {xpath} in {url} with href: {href}"
                        )
                        visible_elements.append(element_data)
                        parent_xpath = xpath
                        continue

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

                        map_before = await self.get_visible_content_map(
                            page, visible_elements_xpaths, helper_id
                        )
                        hash_before = await self.get_hash_from_map(map_before)

                        # getting the dom details before clicking

                        # Click the element
                        logging.info(
                            f"[helper {helper_id}] Clicking element: {xpath} in {url} "
                        )
                        clicked = await self.safe_click(
                            locator=element, helper_id=helper_id, xpath=xpath
                        )
                        if not clicked:
                            logging.info(
                                f"[helper {helper_id}] Click failed for element: {xpath} in {url} "
                            )
                            visible_elements.append(element_data)
                            continue

                        parent_xpath = xpath
                        clciked_xpaths.append(xpath)

                        # Wait for popup with timeout
                        try:
                            new_tab = await asyncio.wait_for(
                                new_page_future, timeout=2.0
                            )
                            self.helper_pages[helper_id].add(new_tab)

                            await new_tab.wait_for_load_state("domcontentloaded")
                            new_url = new_tab.url
                            logging.info(
                                f"[helper {helper_id}] Detected new tab: {new_url}"
                            )
                            element_data["opened_new_tab"] = new_url
                            await self.state.add_navigation(key_hash, new_url)
                            if not await self.should_skip_link(
                                previous_url, new_url, helper_id
                            ):
                                already_scraped = await self.state.has_scraped_url(
                                    new_url
                                )
                                if not already_scraped:
                                    await self.to_visit.put(new_url)
                                    await self.update_stats("urls_queued")
                                    logging.info(
                                        f"[helper {helper_id}] Queued new tab: {new_url}"
                                    )

                            try:
                                await new_tab.close()
                                self.helper_pages[helper_id].discard(new_tab)
                                logging.info(
                                    f"[helper {helper_id}] Closed new tab: {new_url}"
                                )
                            except Exception as e:
                                logging.warning(
                                    f"[helper {helper_id}] Could not close tab: {e}"
                                )

                            await page.bring_to_front()
                            await self.wait_until_ready(
                                page, timeout=10, settle_time=1.0, helper_id=helper_id
                            )
                            if internal_route_element:
                                await self.safe_click(
                                    internal_route_element,
                                    helper_id=helper_id,
                                    xpath=await internal_route_element.evaluate(
                                        self.get_xpath_js
                                    ),
                                )

                        except asyncio.TimeoutError:
                            # No popup opened, check for same-tab navigation
                            logging.info(
                                f"[helper {helper_id}] No new tab was detected, checking for navigation"
                            )

                            await page.wait_for_load_state(
                                "domcontentloaded", timeout=30000
                            )
                            # await self.wait_until_ready(
                            #     page, timeout=15, settle_time=1, helper_id=helper_id
                            # )

                            if page.url != previous_url:
                                # new_url = page.url.split("?")[0].split("#")[0]
                                new_url = page.url.split("#")[0]
                                element_data["navigated_to"] = new_url
                                await self.state.add_navigation(key_hash, new_url)
                                logging.info(
                                    f"[helper {helper_id}] Detected same-tab navigation to: {new_url}"
                                )
                                if not await self.should_skip_link(
                                    previous_url, new_url, helper_id
                                ):
                                    already_scraped = await self.state.has_scraped_url(
                                        new_url
                                    )
                                    if not already_scraped:
                                        await self.to_visit.put(new_url)
                                        await self.update_stats("urls_queued")
                                        logging.info(
                                            f"[helper {helper_id}] Queued navigation: {new_url}"
                                        )
                                    else:
                                        logging.info(
                                            f"[helper {helper_id}] Navigation URL already scraped: {new_url}"
                                        )

                                await self.safe_go_back(
                                    page, helper_id, previous_url=previous_url
                                )
                                if internal_route_element:
                                    await self.safe_click(
                                        internal_route_element,
                                        helper_id=helper_id,
                                        xpath=await internal_route_element.evaluate(
                                            self.get_xpath_js
                                        ),
                                    )

                            else:
                                popup_data = await self.detect_and_handle_popups(
                                    page, helper_id
                                )
                                if popup_data:
                                    element_data["popups"] = popup_data
                                    logging.info(
                                        f"[helper {helper_id}] Handled popup after click on {xpath} in {url}."
                                    )
                                    await self.wait_until_ready(
                                        page,
                                        timeout=15,
                                        settle_time=1,
                                        helper_id=helper_id,
                                    )
                                    # continue

                                logging.info(
                                    f"[helper {helper_id}] No navigation occurred after click."
                                )

                                await self.wait_until_ready(
                                    page,
                                    timeout=5,
                                    settle_time=0.5,
                                    helper_id=helper_id,
                                )
                                visible_xpaths_after = (
                                    await self.get_visible_elements_xpaths(
                                        page, helper_id, purpose="after_click"
                                    )
                                )
                                map_after = await self.get_visible_content_map(
                                    page, visible_xpaths_after, helper_id
                                )
                                hash_after = await self.get_hash_from_map(map_after)

                                if (
                                    hash_before != hash_after
                                ) and not is_dropdown_detected:
                                    internal_route_detected_element.append(xpath)
                                    if previous_element_xpath is None:
                                        previous_element_xpath = (
                                            clciked_xpaths[
                                                clciked_xpaths.index(xpath) - 1
                                            ]
                                            if clciked_xpaths.index(xpath) > 0
                                            and await self.have_same_parents(
                                                page=page,
                                                xpath1=clciked_xpaths[
                                                    clciked_xpaths.index(xpath) - 1
                                                ],
                                                xpath2=xpath,
                                            )
                                            and clciked_xpaths[
                                                clciked_xpaths.index(xpath) - 1
                                            ]
                                            not in internal_route_detected_element
                                            else None
                                        )
                                    logging.info(
                                        f"[helper {helper_id}] DOM changed after click on {xpath} in {url}."
                                    )
                                    new_xpaths = [
                                        x
                                        for x in visible_xpaths_after
                                        if x not in visible_elements_xpaths
                                    ]

                                    if new_xpaths:
                                        logging.info(
                                            f"[helper {helper_id}] Found {len(new_xpaths)} new elements internal routes {new_xpaths}."
                                        )
                                        internal_clicked_elements.append(element)
                                        element_data["internal_routes"] = (
                                            await self.details_from_thre_xpath(
                                                new_xpaths,  # Process only the new elements
                                                page,
                                                url,
                                                helper_id,
                                                visible_xpaths_after,  # Pass the new full list
                                                internal_route_element=element,
                                                internal_clicked_elements=internal_clicked_elements,
                                            )
                                        )
                                        internal_clicked_elements.remove(element)
                                        managed_ir = await self.manage_internal_navigation(
                                            page=page,
                                            element=element,
                                            helper_id=helper_id,
                                            previous_element_xpath=previous_element_xpath,
                                            previous_url=previous_url,
                                            hash_before=hash_before,
                                            internal_clicked_elements=internal_clicked_elements,
                                        )
                                        if managed_ir:
                                            logging.info(
                                                f"[Helper {helper_id}]: managed the internal routes in {page.url}"
                                            )
                                        else:
                                            logging.info(
                                                f"[helper {helper_id}] unable to manage internal routes"
                                            )

                                    else:
                                        modified_xpath = (
                                            self._get_modified_elements_data(
                                                map_before, map_after
                                            )
                                        )
                                        if modified_xpath:
                                            # No new XPaths, so the change must be in-place.
                                            logging.info(
                                                f"[helper {helper_id}] DOM changed: Content was modified in-place {modified_xpath}."
                                            )
                                            internal_clicked_elements.append(element)
                                            element_data["internal_routes"] = (
                                                await self.details_from_thre_xpath(
                                                    modified_xpath,  # Process only the new elements
                                                    page,
                                                    url,
                                                    helper_id,
                                                    visible_xpaths_after,
                                                    internal_route_element=element,  # Pass the new full list
                                                    internal_clicked_elements=internal_clicked_elements,
                                                )
                                            )
                                            internal_clicked_elements.remove(element)

                                        else:
                                            logging.info(
                                                f"[helper {helper_id}] DOM changed but no new elements detected or some elements removed after click on {xpath} in {url}."
                                            )
                                        managed_ir = await self.manage_internal_navigation(
                                            page=page,
                                            element=element,
                                            helper_id=helper_id,
                                            previous_element_xpath=previous_element_xpath,
                                            previous_url=previous_url,
                                            hash_before=hash_before,
                                            internal_clicked_elements=internal_clicked_elements,
                                        )
                                        if managed_ir:
                                            logging.info(
                                                f"[Helper {helper_id}]: managed the internal routes in {page.url}"
                                            )
                                        else:
                                            logging.warning(
                                                f"[helper {helper_id}] unable to manage internal routes in {page.url}"
                                            )
                                    # if internal_route_element:
                                    #     await self.safe_click(internal_route_element,helper_id=helper_id,xpath=await internal_route_element.evaluate(self.get_xpath_js))

                                else:
                                    logging.info(
                                        f"[helper {helper_id}] DOM did not change after click on {xpath} in {url}."
                                    )
                                    previous_element_xpath = None

                    finally:
                        # Remove the popup listener
                        page.remove_listener("popup", on_popup)
                else:
                    if not await self.is_probably_clickable(element, helper_id):
                        logging.info(
                            f"[helper {helper_id}] {xpath} is not probably clickable in {url}"
                        )
                    elif not await element.is_enabled():
                        logging.info(
                            f"[helper {helper_id}] {xpath} is not enabled in {url}"
                        )
                    else:
                        logging.info(
                            f"[helper {helper_id}] {xpath} a child of previously clicked: {parent_xpath} in {url}"
                        )
                visible_elements.append(element_data)

            except Exception as e:
                logging.error(
                    f"[helper {helper_id}] Error processing element {xpath}: {e}"
                )
                logging.error(f"Traceback:\n{traceback.format_exc()}")
                await self.update_stats("errors")
                continue

        return visible_elements

    async def have_same_parents(self, page, xpath1: str, xpath2: str) -> bool:
        logging.info(f"xpath 1: {xpath1}  ,xpath 2 :{xpath2}")
        js = r"""
    ({ xp1, xp2 }) => {
        function getNode(xp) {
            return document.evaluate(
                xp, document, null,
                XPathResult.FIRST_ORDERED_NODE_TYPE,
                null
            ).singleNodeValue;
        }
        function buildAncestors(node) {
            const arr = [];
            let cur = node;
            while (cur && cur.nodeType === 1) {
                arr.unshift(cur); // root first
                cur = cur.parentElement;
            }
            return arr;
        }
        function tagPathFromAncestor(ancestor, node) {
            const tags = [];
            let cur = node;
            while (cur && cur !== ancestor) {
                if (cur.nodeType === 1) tags.unshift(cur.nodeName.toLowerCase());
                cur = cur.parentElement;
            }
            return tags;
        }

        const n1 = getNode(xp1);
        const n2 = getNode(xp2);
        if (!n1 || !n2) return false;

        const anc1 = buildAncestors(n1);
        const anc2 = buildAncestors(n2);
        if (anc1.length !== anc2.length) return false; // require same depth

        // QUICK sibling check: same immediate parent => True
        const p1 = n1.parentElement;
        const p2 = n2.parentElement;
        if (p1 && p2 && p1 === p2) {
            // optional: ensure tag-path from parent down to nodes is same length & tags
            const path1 = tagPathFromAncestor(p1, n1); // from parent (exclusive) => n1
            const path2 = tagPathFromAncestor(p2, n2);
            if (path1.length !== path2.length) return false;
            for (let i = 0; i < path1.length; i++) {
                if (path1[i] !== path2[i]) return false;
            }
            return true;
        }

        const depth = anc1.length;
        for (let k = 1; k < depth; k++) {
            const idx = depth - 1 - k;
            const a1 = anc1[idx];
            const a2 = anc2[idx];
            if (!a1 || !a2) continue;
            if (a1 === a2) continue; // same ancestor at this level — skip

            const pa1 = a1.parentElement;
            const pa2 = a2.parentElement;
            if (!pa1 || !pa2) continue;
            if (pa1 !== pa2) continue; // ancestors must be siblings

            const path1 = tagPathFromAncestor(a1, n1);
            const path2 = tagPathFromAncestor(a2, n2);
            if (path1.length !== path2.length) continue;

            let same = true;
            for (let x = 0; x < path1.length; x++) {
                if (path1[x] !== path2[x]) { same = false; break; }
            }
            if (same) return true;
        }
        return false;
    }
    """

        return await page.evaluate(js, {"xp1": xpath1, "xp2": xpath2})

    async def safe_click(
        self,
        locator: Locator,
        helper_id: str,
        xpath: Optional[str] = None,
        timeout: float = 5000,
    ) -> bool:
        """
        Try to click a locator robustly:
        1. wait for it to be visible & enabled
        2. try locator.click()
        3. fallback to JS click on the element
        Returns True on success, False on failure.
        """
        try:
            # Wait for the element to be visible and actionable
            await locator.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeoutError:
            logging.warning(
                f"[helper {helper_id}] element not visible (xpath={xpath}) after {timeout}ms."
            )
            return False

        try:
            await locator.click()
            logging.info(
                f"[helper {helper_id}] clicked element (xpath={xpath}) via locator.click()."
            )
            return True
        except Exception as exc:
            logging.warning(
                f"[helper {helper_id}] locator.click failed for (xpath={xpath}): {exc}. Trying JS click fallback."
            )
            # fallback to JS click using evaluate on an element handle
            try:
                await locator.evaluate("(el) => el.click()")
                logging.info(
                    f"[helper {helper_id}] clicked element (xpath={xpath}) via JS fallback."
                )
                return True
            except Exception as exc2:
                logging.error(
                    f"[helper {helper_id}] JS click fallback failed for (xpath={xpath}): {exc2}"
                )
                return False

    async def manage_internal_navigation(
        self,
        page: Page,
        element: Locator,
        helper_id: str,
        previous_element_xpath: Optional[str],
        previous_url: str,
        hash_before: str,
        internal_clicked_elements=[],
    ) -> bool:
        """
        Try to return to a previous element (if provided) after a DOM change,
        otherwise click the given element again. After clicking, wait until ready,
        verify URL and DOM hash. Returns True if returned to original state (hash matches),
        False otherwise.
        """

        async def refresh_and_check() -> bool:
            await page.goto(previous_url)
            await self.wait_until_ready(
                page=page, timeout=15, settle_time=5, helper_id=helper_id
            )
            await self.remove_widget(page=page, helper_id=helper_id)
            if internal_clicked_elements:
                for internal_clicked_element in internal_clicked_elements:
                    await self.safe_click(
                        internal_clicked_element,
                        helper_id=helper_id,
                        xpath=await internal_clicked_element.evaluate(
                            self.get_xpath_js
                        ),
                    )
            xpaths_here = await self.get_visible_elements_xpaths(
                page=page,
                helper_id=helper_id,
                purpose="comparing the dom for after inter routes detected",
            )
            map_here = await self.get_visible_content_map(
                page=page, xpaths=xpaths_here, helper_id=helper_id
            )
            hash_here = await self.get_hash_from_map(content_map=map_here)

            if hash_before == hash_here:
                logging.info(
                    f"[helper {helper_id}] returned to the original state in {page.url} after refreshing the page"
                )
                return True
            else:
                logging.info(
                    f"[helper {helper_id}] state changed: previous hash != current hash after refreshing the page (page.url={page.url})"
                )
                return False

        async def post_click_checks() -> bool:
            # common post-click sequence
            await self.wait_until_ready(
                page, timeout=15, settle_time=1, helper_id=helper_id
            )

            # If click navigated to wrong URL, navigate back and remove widget
            if previous_url != page.url:
                logging.info(
                    f"[helper {helper_id}] clicked wrong element; navigating to {previous_url}"
                )
                try:
                    await page.goto(previous_url)
                    await self.wait_until_ready(
                        page=page, timeout=15, settle_time=2, helper_id=helper_id
                    )
                    await self.remove_widget(page=page, helper_id=helper_id)
                    if internal_clicked_elements:
                        for internal_clicked_element in internal_clicked_elements:
                            await self.safe_click(
                                internal_clicked_element,
                                helper_id=helper_id,
                                xpath=await internal_clicked_element.evaluate(
                                    self.get_xpath_js
                                ),
                            )
                except Exception as exc:
                    logging.error(
                        f"[helper {helper_id}] failed to navigate back to previous_url {previous_url}: {exc}"
                    )

            # compute current visible map & hash
            xpaths_here = await self.get_visible_elements_xpaths(
                page=page,
                helper_id=helper_id,
                purpose="comparing the dom for after inter routes detected",
            )
            map_here = await self.get_visible_content_map(
                page=page, xpaths=xpaths_here, helper_id=helper_id
            )
            hash_here = await self.get_hash_from_map(content_map=map_here)

            if hash_before == hash_here:
                logging.info(
                    f"[helper {helper_id}] returned to the original state in {page.url}"
                )
                return True
            else:
                logging.info(
                    f"[helper {helper_id}] state changed: previous hash != current hash (page.url={page.url})"
                )
                return False

        # Main flow: try to go back to previous element if provided, else click the provided element
        if previous_element_xpath:
            previous_element = page.locator(f"xpath={previous_element_xpath}")
            logging.info(
                f"[helper {helper_id}] Returning to previous element {previous_element_xpath} after DOM change."
            )
            clicked = await self.safe_click(
                previous_element, helper_id, xpath=previous_element_xpath
            )
            if not clicked:
                logging.warning(
                    f"[helper {helper_id}] failed to click previous element ({previous_element_xpath}); attempting to continue."
                )
            if await post_click_checks():
                return True
            return await refresh_and_check()

        else:
            logging.info(
                f"[helper {helper_id}] No previous element to return to after DOM change — clicking the same element."
            )
            # Try to extract the xpath for better logging if available
            xpath_for_log = await element.evaluate(self.get_xpath_js)

            clicked = await self.safe_click(
                element, helper_id, xpath=xpath_for_log, timeout=5000
            )
            if not clicked:
                logging.warning(
                    f"[helper {helper_id}] failed to click the element (xpath={xpath_for_log}); continuing to checks."
                )
            if await post_click_checks():
                return True
            return await refresh_and_check()

    async def safe_go_back(self, page, helper_id, previous_url):
        """Safely go back in browser history or reload previous URL if needed."""

        async def ensure_correct_url():
            """Verify and correct the URL if mismatch."""
            if previous_url and page.url != previous_url:
                logging.warning(
                    f"[helper {helper_id}] URL mismatch after go_back(). "
                    f"Expected: {previous_url}, Got: {page.url}. Fixing..."
                )
                await page.goto(previous_url, wait_until="domcontentloaded")
                await self.wait_until_ready(
                    page, timeout=15, settle_time=1, helper_id=helper_id
                )
                await self.remove_widget(page=page, helper_id=helper_id)
                logging.info(f"[helper {helper_id}] URL corrected successfully.")

        try:
            # attempt normal browser history back
            await page.go_back(timeout=10000, wait_until="domcontentloaded")
            await page.wait_for_load_state("domcontentloaded")
            await self.wait_until_ready(
                page, timeout=15, settle_time=1, helper_id=helper_id
            )
            await self.remove_widget(page=page, helper_id=helper_id)

            logging.info(
                f"[helper {helper_id}] Went back successfully using go_back()."
            )

            # verify URL after go_back
            await ensure_correct_url()

        except Exception:
            logging.warning(
                f"[helper {helper_id}] go_back() failed, using js fallback ..."
            )

            try:
                #  JS history fallback
                await page.evaluate("window.history.back()")
                await page.wait_for_load_state("domcontentloaded")
                await self.wait_until_ready(
                    page, timeout=15, settle_time=1, helper_id=helper_id
                )
                await self.remove_widget(page=page, helper_id=helper_id)
                logging.info(
                    f"[helper {helper_id}] Went back using JS fallback successfully."
                )

            except Exception:
                logging.warning(
                    f"[helper {helper_id}] js go back failed, using js fallback ..."
                )
                # known previous URL? → direct navigation fallback
                await page.goto(previous_url, wait_until="domcontentloaded")
                await self.wait_until_ready(
                    page, timeout=15, settle_time=1, helper_id=helper_id
                )
                await self.remove_widget(page=page, helper_id=helper_id)
                logging.info(
                    f"[helper {helper_id}] Fallback navigation to previous URL succeeded."
                )

            # verify URL after fallback
            await ensure_correct_url()

    async def detect_dropdown_after_hover(
        self,
        page,
        map_before_hover,
        hash_before_hover,
        helper_id,
        visible_elements_xpaths,
        element,
    ):
        """Detect dropdowns that appear after hovering"""
        try:
            await self.wait_until_ready(
                page=page, timeout=5, settle_time=1, helper_id=helper_id
            )
            visible_xpaths_after_hover = await self.get_visible_elements_xpaths(
                page, helper_id, purpose="after_hover"
            )
            map_after_hover = await self.get_visible_content_map(
                page, visible_xpaths_after_hover, helper_id
            )
            hash_after_hover = await self.get_hash_from_map(map_after_hover)

            if hash_before_hover != hash_after_hover:
                logging.info(
                    f"[helper {helper_id}] dropdown detected after hover {await element.evaluate(self.get_xpath_js)} in {page.url}."
                )
                new_xpaths = [
                    x
                    for x in visible_xpaths_after_hover
                    if x not in visible_elements_xpaths
                ]
                if new_xpaths:
                    logging.info(f"helper {helper_id} changed xpath{new_xpaths}")
                    dropdown_data = await self.details_from_thre_xpath(
                        new_xpaths,
                        page,
                        page.url,
                        helper_id,
                        visible_xpaths_after_hover,
                        element,
                    )
                    return dropdown_data
                else:

                    modified_xpath = self._get_modified_elements_data(
                        map_before_hover, map_after_hover
                    )
                    logging.info(f"helper {helper_id} modified xpath{modified_xpath}")
                    dropdown_data = await self.details_from_thre_xpath(
                        modified_xpath,
                        page,
                        page.url,
                        helper_id,
                        visible_xpaths_after_hover,
                        element,
                    )
                    return dropdown_data
            logging.info(f"[helper {helper_id}]:no dropdown detected")
            return None
        except Exception as e:
            logging.error(
                f"[helper {helper_id}]:Error detecting dropdown after hover: {e}"
            )
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return None

    async def get_single_xpath(
        self, element_handle: ElementHandle, helper_id: int, i: int
    ) -> str | None:
        """Tries to get a single visible element's xpath. Returns None on failure."""
        try:
            if await element_handle.is_visible():
                return await element_handle.evaluate(self.get_xpath_js)
        except Exception as e:
            logging.warning(
                f" helper {helper_id} : Concurrent error on element {i} : {e}"
            )
        return None

    async def get_visible_elements_xpaths(
        self, page: Page, helper_id: int, purpose=None
    ) -> List[str]:
        """Get xpaths of all visible elements CONCURRENTLY."""
        try:
            await self.wait_until_ready(page=page, timeout=5, settle_time=1)

            # 1. Define your locator
            selector = "body *:not(script):not(style):not(meta):not(noscript):not(link):not(title)"

            # 2. Get the stable "snapshot" list of locators
            try:
                all_handles = await page.query_selector_all(selector)
            except PlaywrightTimeoutError:
                logging.warning(
                    f" helper {helper_id} : Timed out getting initial element list in  {purpose}."
                )
                return []

            total = len(all_handles)
            if total == 0:
                logging.info(
                    f" helper {helper_id} : No elements found to process in {page.url} in {purpose}"
                )
                return []

            logging.info(
                f" helper {helper_id} : found {total} elements in {purpose}. Processing CONCURRENTLY."
            )

            # 3.Create a list of tasks (coroutines) to run
            tasks = []
            for i, element_handle in enumerate(all_handles):
                tasks.append(
                    # Call our helper for each element
                    self.get_single_xpath(element_handle, helper_id, i)
                )

            # 4. Run all tasks concurrently and get results
            # This is where the speed comes from!
            results = await asyncio.gather(*tasks)

            # 5. Filter out the None values (from failed/invisible elements)
            xpaths = [xpath for xpath in results if xpath is not None]

            logging.info(
                f" helper {helper_id} : Successfully extracted {len(xpaths)} visible xpaths in {purpose}."
            )
            return xpaths

        except Exception as e:
            logging.error(
                f" helper {helper_id} Error in get_visible_elements_xpaths: {e}"
            )
            logging.error(traceback.format_exc())
            return []

    async def get_visible_content_map(
        self, page: Page, xpaths: List[str], helper_id: int
    ) -> Dict[str, str]:
        """
        Gets a map of {xpath: direct_text} for all given xpaths.
        This is the raw data used for hashing and comparison.
        """
        try:
            # This JS function takes the list of xpaths, finds each element,
            # and returns an object of {xpath: direct_text}.
            content_map = await page.evaluate(
                """
                (xpaths) => {
                    
                    // --- HELPER FUNCTION TO GET DIRECT TEXT ---
                    function getDirectText(element) {
                        if (!element) return "";
                        return Array.from(element.childNodes)
                            .filter(n => n.nodeType === Node.TEXT_NODE) // Get only text nodes
                            .map(n => n.textContent.trim())           // Get their text and trim whitespace
                            .filter(t => t.length > 0)                // Filter out empty strings
                            .join(' ');                               // Join them with a space
                    }
                    // --- END HELPER FUNCTION ---

                    const signatures = {};
                    for (const xpath of xpaths) {
                        try {
                            const el = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                            if (el) {
                                // --- THIS IS THE MODIFIED PART ---
                                // Use the helper function instead of el.innerText
                                const text = getDirectText(el);
                                signatures[xpath] = text;
                            } else {
                                signatures[xpath] = null; // Mark as not found (e.g., detached)
                            }
                        } catch (e) {
                            signatures[xpath] = null; // Mark as error
                        }
                    }
                    return signatures;
                }
                """,
                xpaths,
            )
            return content_map
        except Exception as e:
            logging.warning(f"helper {helper_id} Could not generate content map: {e}")
            return {"error": str(e)}

    async def get_hash_from_map(self, content_map: Dict[str, str]) -> str:
        """Generates a stable hash from a content map."""
        try:
            # Use json.dumps with sort_keys=True for a canonical representation
            canonical_string = json.dumps(content_map, sort_keys=True)
            return hashlib.sha256(canonical_string.encode()).hexdigest()
        except Exception as e:
            logging.error(f"Could not hash content map: {e}")
            # Return a unique hash to force a "change" state on error
            return hashlib.sha256(f"error_{time.time()}".encode()).hexdigest()

    def _get_modified_elements_data(
        self, map_before: Dict[str, str], map_after: Dict[str, str]
    ) -> List[str]:
        """
        Compares two content maps and returns a list of dictionaries
        for elements with modified text.
        """
        modified_xpath = []
        # We only care about xpaths present in both, but with different text
        common_xpaths = set(map_before.keys()) & set(map_after.keys())

        for x in sorted(list(common_xpaths)):
            text_before = map_before.get(x)
            text_after = map_after.get(x)

            # Check if text is not None for both and they are different
            if (
                text_before is not None
                and text_before != ""
                and text_after is not None
                and text_after != ""
                and text_before != text_after
            ):
                modified_xpath.append(x)
        return modified_xpath

    async def safe_hover(
        self,
        page,
        element: Locator,
        helper_id,
        *,
        timeout: int = 3000,  # ms for individual waits
        overall_timeout: int = 8000,  # ms total budget for hover attempts
    ):
        """
        Robust hover with multiple fallbacks:
        1. ensure visible & scroll into view
        2. normal hover()
        3. move mouse to element center (page.mouse.move) then mouseover
        4. force hover
        5. temporarily disable overlay intercepting element (pointer-events:none) and retry
        6. dispatch PointerEvent / MouseEvent via JS (with coordinates)
        Returns True on success, False otherwise.
        """
        start = time.time()
        xpath_for_log = "[xpath_unavailable]"  # Fallback log
        try:
            xpath_for_log = await element.evaluate(self.get_xpath_js)
        except Exception:
            logging.error("Xpath of the element is unable to find")
            pass  # Element might be gone, logging will use fallback

            # 1) Ensure visible & scrolled into view
        try:
            await element.scroll_into_view_if_needed(timeout=timeout)
        except Exception as e:
            logging.debug(
                f"[helper {helper_id}] scroll_into_view_if_needed failed: {e}"
            )

        # Helper to abort if overall timeout passed
        def timed_out():
            return (time.time() - start) * 1000 > overall_timeout

        # 2) Try normal hover via Playwright API
        try:
            await element.hover(timeout=timeout)
            logging.info(f"[helper {helper_id}] Hovered normally: {xpath_for_log}")
            await page.wait_for_timeout(150)
            return True
        except Exception as e:
            logging.warning(
                f"[helper {helper_id}] Normal hover failed for {xpath_for_log}: {e}"
            )

        if timed_out():
            logging.warning(
                f"[helper {helper_id}] safe_hover: overall timeout after normal hover for {xpath_for_log}"
            )
            return False

        # 3) Try moving mouse to center using bounding box + page.mouse.move + mouseover
        try:
            box = await element.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                # Move there (Playwright mouse uses page coordinates)
                await page.mouse.move(cx, cy, steps=8)
                # Fire pointer/mouse events at that coordinate
                await page.mouse.down()
                await page.mouse.up()
                # small pause to let site react
                await page.wait_for_timeout(120)
                logging.info(
                    f"[helper {helper_id}] Moved mouse to center for {xpath_for_log} at ({cx:.1f},{cy:.1f})"
                )
                # After moving, try hover() again (sometimes now it works)
                try:
                    await element.hover(timeout=1000)
                    logging.info(
                        f"[helper {helper_id}] Hover succeeded after mouse.move: {xpath_for_log}"
                    )
                    await page.wait_for_timeout(120)
                    return True
                except Exception:
                    logging.debug(
                        f"[helper {helper_id}] hover still failed after mouse.move for {xpath_for_log}"
                    )
            else:
                logging.debug(
                    f"[helper {helper_id}] bounding_box is None for {xpath_for_log}"
                )
        except Exception as e:
            logging.debug(
                f"[helper {helper_id}] mouse.move approach failed for {xpath_for_log}: {e}"
            )

        if timed_out():
            logging.warning(
                f"[helper {helper_id}] safe_hover: overall timeout after mouse.move for {xpath_for_log}"
            )
            return False

        # 4) Try force hover (shorter timeout)
        try:
            await element.hover(force=True, timeout=1000)
            logging.info(
                f"[helper {helper_id}] Forced hover succeeded for {xpath_for_log}"
            )
            await page.wait_for_timeout(120)
            return True
        except Exception as e:
            logging.warning(
                f"[helper {helper_id}] Forced hover failed for {xpath_for_log}: {e}"
            )

        if timed_out():
            logging.warning(
                f"[helper {helper_id}] safe_hover: overall timeout after force hover for {xpath_for_log}"
            )
            return False

        # 5) Check for intercepting element at center and temporarily disable it (pointer-events: none)
        try:
            intercept_info = await page.evaluate(
                """
                (el) => {
                    try {
                        const rect = el.getBoundingClientRect();
                        const x = rect.left + rect.width/2;
                        const y = rect.top + rect.height/2;
                        const topEl = document.elementFromPoint(x, y);
                        if (!topEl) return null;
                        if (topEl === el || el.contains(topEl)) return null;
                        // collect a short descriptor so we can log it
                        const desc = { tag: topEl.tagName, cls: topEl.className ? topEl.className.toString().slice(0,200) : '', id: topEl.id || '' };
                        // store original pointer-events so we can revert
                        const original = topEl.style.pointerEvents || '';
                        topEl.style.pointerEvents = 'none';
                        return { desc, original: original };
                    } catch (err) { return null; }
                }
                """,
                await element.element_handle(),
            )
            if intercept_info:
                logging.warning(
                    f"[helper {helper_id}] Interceptor found for {xpath_for_log}: {intercept_info.get('desc')}, temporarily disabled it."
                )
                # retry hover after disabling overlay
                try:
                    await element.hover(timeout=1200)
                    logging.info(
                        f"[helper {helper_id}] Hover succeeded after disabling interceptor for {xpath_for_log}"
                    )
                    # revert pointer-events
                    await element.evaluate(
                        """
                        (el) => {
                            const rect = el.getBoundingClientRect();
                            const x = rect.left + rect.width/2;
                            const y = rect.top + rect.height/2;
                            const topEl = document.elementFromPoint(x, y);
                            if (topEl) topEl.style.pointerEvents = '';
                        }
                        """
                    )
                    await page.wait_for_timeout(120)
                    return True
                except Exception as e:
                    logging.debug(
                        f"[helper {helper_id}] hover failed after disabling interceptor: {e}"
                    )
                    # attempt to revert anyway
                    try:
                        await element.evaluate(
                            """
                            (el) => {
                                const rect = el.getBoundingClientRect();
                                const x = rect.left + rect.width/2;
                                const y = rect.top + rect.height/2;
                                const topEl = document.elementFromPoint(x, y);
                                if (topEl) topEl.style.pointerEvents = '';
                            }
                            """
                        )
                    except Exception:
                        pass
        except Exception as overlay_e:
            logging.debug(
                f"[helper {helper_id}] overlay disable attempt failed: {overlay_e}"
            )

        if timed_out():
            logging.warning(
                f"[helper {helper_id}] safe_hover: overall timeout after overlay attempt for {xpath_for_log}"
            )
            return False

        # 6) Final fallback: dispatch PointerEvent / MouseEvent at center with coordinates
        try:
            # compute coords again if possible
            box = await element.bounding_box()
            coords = {"x": 0, "y": 0}
            if box:
                coords["x"] = box["x"] + box["width"] / 2
                coords["y"] = box["y"] + box["height"] / 2

            await element.evaluate(
                """
                (el, cx, cy) => {
                    function dispatch(type, x, y) {
                        let ev;
                        try {
                            ev = new PointerEvent(type, { bubbles: true, cancelable: true, pointerType: 'mouse', clientX: x, clientY: y });
                        } catch(e) {
                            ev = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y });
                        }
                        el.dispatchEvent(ev);
                    }
                    dispatch('pointerover', cx, cy);
                    dispatch('pointerenter', cx, cy);
                    dispatch('mouseover', cx, cy);
                    dispatch('mouseenter', cx, cy);
                }
                """,
                coords["x"],
                coords["y"],
            )
            await page.wait_for_timeout(150)
            logging.info(
                f"[helper {helper_id}] Dispatched pointer/mouse events for {xpath_for_log}"
            )
            return True
        except Exception as js_e:
            logging.error(
                f"[helper {helper_id}] JS dispatch hover failed for {xpath_for_log}: {js_e}"
            )
            logging.debug(traceback.format_exc())
            return False

    async def is_child_xpath(self, parent_xpath, child_xpath):
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

    async def is_probably_clickable(self, element, helper_id):
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

            cursor = (
                await element.evaluate("(el) => getComputedStyle(el).cursor")
            ).lower()
            if cursor == "pointer":
                return True

            return False
        except Exception as e:
            logging.error(
                f"helper {helper_id} Error checking if element is clickable: {e}"
            )
            return False

    async def detect_and_handle_popups(self, page: Page, helper_id: int):
        """
        Detect various types of popups and collect their details.
        Returns popup data if found, None otherwise.
        """
        popup_data = None

        try:
            # --- 1. Modal dialogs ---
            modal_selectors = [
                # # --- Headless UI / similar (explicit) ---
                # "//div[contains(@id,'headlessui-dialog') or contains(@id,'headlessui-dialog-panel')]",
                # "//*[@data-headlessui-state='open' or contains(@data-headlessui-state,'open')]",
                # --- Radix / data-state pattern (open) ---
                "//*[@data-state='open' and (contains(@id,'dialog') or contains(@class,'dialog') or @role='dialog')]",
                # --- explicit ARIA / roles (best practice) ---
                "//*[@role='dialog']",
                "//*[@role='alertdialog']",
                "//*[@aria-modal='true']",
                # --- id-based common tokens (dialog/modal/popup/lightbox) ---
                "//div[contains(translate(@id,'MODAL','modal'),'modal') or contains(translate(@id,'DIALOG','dialog'),'dialog') "
                "or contains(translate(@id,'POPUP','popup'),'popup') or contains(translate(@id,'LIGHTBOX','lightbox'),'lightbox')]",
                # --- class-based common tokens (framework-agnostic) ---
                "//div[contains(@class,'modal') or contains(@class,'dialog') or contains(@class,'popup') or contains(@class,'lightbox')]",
                # --- popular frameworks (Ant / Material / Chakra / Mantine / Bootstrap / Shadcn) ---
                "//div[contains(@class,'ant-modal') or contains(@class,'ant-drawer')]",
                # "//div[contains(@class,'MuiDialog') or contains(@class,'MuiModal')]",
                "//div[contains(@class,'chakra-modal') or contains(@class,'chakra-alert-dialog')]",
                "//div[contains(@class,'mantine-Modal') or contains(@class,'mantine-Drawer')]",
                "//div[contains(@class,'bootbox') or (contains(@class,'fade') and contains(@class,'show'))]",
                "//div[contains(@class,'shadcn') or contains(@class,'radix') or contains(@class,'headlessui')]",
                # --- visible / shown heuristics: elements with inline display:block / non-zero size & transform hints ---
                "//div[(contains(@style,'display:block') or contains(@style,'display: block') or contains(@style,'visibility: visible')) and (contains(@class,'modal') or contains(@id,'dialog') or @role='dialog')]",
                "//div[(number(string-length(normalize-space(@innerHTML))) > 0) and (contains(@style,'position: fixed') or contains(@style,'position: absolute'))]",
                # --- fallback: container with form / title typical for modals (short heuristic) ---
                "//div[.//button and (.//h1 or .//h2 or .//p) and (contains(@class,'modal') or contains(@id,'dialog') or @role='dialog')]",
                # --- generic dialog-like containers using data-state/data-open attributes ---
                "//*[@data-state='open' or @data-open='true' or @data-open='']",
                "//*[@data-open='' or @data-open='true' or contains(@data-open,'open')]",
                # --- last-resort generic patterns (keeps false positives down by requiring 'dialog' in id/class or role) ---
                "//div[(contains(@class,'dialog') or contains(@id,'dialog') or contains(@class,'popup') or contains(@id,'popup'))]",
            ]

            for selector in modal_selectors:
                elements = await page.locator(f"xpath={selector}").element_handles()
                for el in elements:
                    try:
                        is_visible = await el.is_visible()
                        if not is_visible:
                            continue

                        # Check rendered width and height
                        size = await el.evaluate(
                            "(node) => ({width: node.offsetWidth || 0, height: node.offsetHeight || 0})"
                        )
                        if size["width"] > 0 and size["height"] > 0:
                            logging.info(
                                f"helper {helper_id} Modal size: {size} for selector: {selector}"
                            )
                            modal = el
                            logging.info(
                                f"helper {helper_id} Modal detected using selector: {selector}of  {await modal.evaluate(self.get_xpath_js)}"
                            )
                            popup_data = await self.extract_popup_details(
                                page, modal, "modal", helper_id
                            )
                            await self.close_popup(page, modal, "modal", helper_id)
                            return popup_data
                    except Exception:
                        continue

            # --- 2. Tooltips ---
            tooltip_selectors = [
                # --- Base tooltip patterns ---
                "//div[contains(@class,'tooltip') and (contains(@style,'display: block') or contains(@style,'opacity: 1'))]",
                "//div[@role='tooltip']",
                "//div[contains(@class,'popover') or contains(@class,'hint') or contains(@class,'balloon')]",
                # --- Framework-specific ---
                "//div[contains(@class,'ant-tooltip') or contains(@class,'ant-popover')]",  # Ant Design
                "//div[contains(@class,'MuiTooltip') or contains(@class,'MuiPopover')]",  # Material UI
                "//div[contains(@class,'chakra-tooltip') or contains(@class,'chakra-popover')]",  # Chakra UI
                "//div[contains(@class,'mantine-Tooltip') or contains(@class,'mantine-Popover')]",  # Mantine UI
                # --- Visibility / position-based hints ---
                "//div[contains(@style,'position: absolute') and contains(@style,'z-index') and (contains(@class,'tooltip') or contains(@class,'popover'))]",
                "//div[contains(@id,'tooltip') or contains(@id,'popover')]",
            ]

            for selector in tooltip_selectors:
                elements = await page.locator(f"xpath={selector}").element_handles()

                for el in elements:
                    try:
                        is_visible = await el.is_visible()
                        if not is_visible:
                            continue

                        # Check rendered width and height
                        size = await el.evaluate(
                            "(node) => ({width: node.offsetWidth || 0, height: node.offsetHeight || 0})"
                        )
                        if size["width"] > 0 and size["height"] > 0:
                            logging.info(
                                f"helper {helper_id} Tooltip size: {size} for selector: {selector}"
                            )
                            tooltip = el
                            logging.info(
                                f"helper {helper_id} Tooltip detected using selector :{selector} of {await tooltip.evaluate(self.get_xpath_js)}"
                            )
                            popup_data = await self.extract_popup_details(
                                page, tooltip, "tooltip", helper_id
                            )
                            await self.close_popup(page, tooltip, "tooltip", helper_id)
                            return popup_data
                    except Exception:
                        continue

            # --- 3. Overlays ---
            overlay_selectors = [
                # --- Framework-specific masks / backdrops (specific first) ---
                # --- overlays / backdrops (visible modal overlays) ---
                "//div[contains(@style,'position: fixed') and (contains(@class,'overlay') or contains(@class,'backdrop') or contains(@class,'modal'))]",
                "//*[contains(@class,'backdrop') or contains(@class,'overlay')][contains(@style,'opacity') or contains(@style,'background')]",
                "//*[contains(@data-backdrop,'true') or contains(@data-overlay,'true')]",
                "//div[contains(@class,'ant-modal-mask') or contains(@class,'ant-drawer-mask')]",
                "//div[contains(@class,'MuiBackdrop-root') or contains(@class,'MuiModal-backdrop')]",
                "//div[contains(@class,'chakra-modal__overlay') or contains(@class,'chakra-popover__popper')]",
                "//div[contains(@class,'mantine-Overlay') or contains(@class,'mantine-Modal-overlay')]",
                # --- popular libs / generic tokens ---
                "//div[contains(@class,'overlay') or contains(@class,'backdrop') or contains(@class,'modal-backdrop') or contains(@class,'backdrop-root')]",
                # --- id-based tokens ---
                "//div[contains(@id,'overlay') or contains(@id,'backdrop') or contains(@id,'modal-backdrop')]",
                # --- data-state / data-open patterns (Radix / HeadlessUI / custom) ---
                "//*[(@data-state='open' or @data-open='true' or @data-open='') and (contains(@class,'overlay') or contains(@id,'overlay') or contains(@class,'backdrop') or contains(@role,'presentation'))]",
                # --- style-based visibility + position + z-index heuristics (common) ---
                # "//div[(contains(@style,'position: fixed') or contains(@style,'position: absolute')) and (contains(@style,'z-index') or contains(@style,'opacity') or contains(@style,'background'))]",
                # "//div[contains(@style,'position: fixed') and (contains(@style,'rgba') or contains(@style,'opacity') or contains(@style,'background')) and not(contains(@style,'opacity: 0'))]",
                # --- elements that are full-screen / near-full-screen by bounding heuristics (fallback) ---
                # Note: these use visible heuristics in runtime code (not pure XPath) but left here for discovery
                # "//div[(contains(@style,'top: 0') or contains(@style,'left: 0')) and (contains(@style,'width: 100%') or contains(@style,'height: 100%') or contains(@style,'inset: 0'))]",
                # --- semantic / ARIA fallbacks (rare for overlays but useful) ---
                "//*[@role='presentation' and (contains(@class,'overlay') or contains(@id,'backdrop'))]",
                "//*[contains(@aria-hidden,'true') and (contains(@class,'backdrop') or contains(@id,'overlay'))]",
                # --- last-resort: section/aside that looks like an overlay/backdrop by tokens ---
                "//section[contains(@class,'overlay') or contains(@class,'backdrop') or contains(@id,'overlay')]",
                "//aside[contains(@class,'overlay') or contains(@class,'backdrop') or contains(@id,'overlay')]",
            ]

            for selector in overlay_selectors:
                elements = await page.locator(f"xpath={selector}").element_handles()

                for el in elements:
                    try:
                        is_visible = await el.is_visible()
                        if not is_visible:
                            continue

                        # Check rendered width and height
                        size = await el.evaluate(
                            "(node) => ({width: node.offsetWidth || 0, height: node.offsetHeight || 0})"
                        )
                        if size["width"] > 0 and size["height"] > 0:
                            logging.info(
                                f"helper {helper_id} Overlay size: {size} for selector: {selector}"
                            )
                            overlay = el
                            logging.info(
                                f"helper {helper_id} Overlay detected using selector: {selector} of {await overlay.evaluate(self.get_xpath_js)}"
                            )
                            popup_data = await self.extract_popup_details(
                                page, overlay, "overlay", helper_id
                            )
                            await self.close_popup(page, overlay, "overlay", helper_id)
                            return popup_data
                    except Exception:
                        continue

        except Exception as e:
            logging.error(f"Error detecting popups: {e}")

        return popup_data

    async def extract_popup_details(
        self, page: Page, popup_element, popup_type: str, helper_id: int
    ):
        """
        Extract detailed information from a popup element.
        """
        try:
            text = await popup_element.inner_text()
            popup_data = {
                "type": popup_type,
                "text": text.strip(),
                "classes": await popup_element.get_attribute("class"),
                "id": await popup_element.get_attribute("id"),
                "elements": [],
            }

            # Interactive elements inside popup
            interactive_xpath = (
                ".//button | .//a | .//input | .//select | .//textarea | "
                ".//*[@onclick] | .//*[@role='button']"
            )
            elements = await popup_element.query_selector_all(
                f"xpath={interactive_xpath}"
            )

            for elem in elements:
                if await elem.is_visible():
                    tag = await elem.evaluate("(el) => el.tagName.toLowerCase()")
                    elem_info = {
                        "tag": tag,
                        "text": (await elem.inner_text()).strip(),
                        "type": await elem.get_attribute("type"),
                        "href": await elem.get_attribute("href"),
                        "onclick": await elem.get_attribute("onclick"),
                        "class": await elem.get_attribute("class"),
                        "id": await elem.get_attribute("id"),
                    }
                    popup_data["elements"].append(elem_info)

            # Extract forms
            forms = await popup_element.query_selector_all("form")
            if forms:
                popup_data["forms"] = []
                for form in forms:
                    form_data = {
                        "action": await form.get_attribute("action"),
                        "method": await form.get_attribute("method"),
                        "fields": [],
                    }
                    fields = await form.query_selector_all("input, select, textarea")
                    for field in fields:
                        field_info = {
                            "name": await field.get_attribute("name"),
                            "type": await field.get_attribute("type"),
                            "placeholder": await field.get_attribute("placeholder"),
                            "required": await field.get_attribute("required")
                            is not None,
                        }
                        form_data["fields"].append(field_info)
                    popup_data["forms"].append(form_data)

            return popup_data

        except Exception as e:
            logging.error(f"helper {helper_id} Error extracting popup details: {e}")
            return {
                "type": popup_type,
                "text": "Error extracting details",
                "error": str(e),
            }

    async def _is_locator_visible_safe(self, loc: Locator) -> bool:
        try:
            # count + is_visible reduces detached-element races
            if await loc.count() == 0:
                return False
            vis = await loc.is_visible()
            if not vis:
                return False
            box = await loc.bounding_box()
            return bool(box and box["width"] > 0 and box["height"] > 0)
        except Exception:
            return False

    async def close_popup(
        self, page: Page, popup_element, popup_type: str, helper_id: int
    ):
        """
        Attempt to close the popup using various methods.
        """
        try:
            popup_element = page.locator(
                f"xpath={await popup_element.evaluate(self.get_xpath_js)}"
            )
            close_selectors = [
                # === 1️⃣ Text-based close buttons ===
                ".//button[contains(normalize-space(.), '×')]",
                ".//button[contains(normalize-space(.), '✕')]",
                ".//button[contains(normalize-space(.), 'Close')]",
                ".//button[normalize-space()='Cancel']",
                ".//a[contains(text(),'Close') or contains(text(),'Dismiss') or contains(text(),'No Thanks')]",
                # === 2️⃣ Accessibility attributes ===
                ".//*[contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'close')]",
                ".//*[contains(@aria-label, 'close') or contains(@aria-label, 'Close Button')]",
                ".//*[contains(@title, 'Close') or contains(@title, 'Dismiss') or contains(@title, 'Cancel')]",
                ".//*[@role='button' and (contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'close') or contains(translate(@title,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'close'))]",
                # === 3️⃣ Common class patterns ===
                ".//*[contains(@class, 'close-button') or contains(@class, 'close-btn') or contains(@class, 'modal-close')]",
                ".//*[contains(@class, 'btn-close') or contains(@class, 'icon-close') or contains(@class, 'popup-close')]",
                ".//*[contains(@class, 'toggle-x') or contains(@class, 'modal__close') or contains(@class, 'toggle-x--close')]",
                ".//*[contains(@class, 'dismiss-btn') or contains(@class, 'exit-button') or contains(@class,'overlay-close')]",
                # === 4️⃣ Data attributes ===
                ".//*[@data-dismiss='modal']",
                ".//*[contains(@data-dismiss, 'modal') or contains(@data-action, 'close') or contains(@data-bs-dismiss, 'modal')]",
                # === 5️⃣ SVG or icon-based “X” buttons ===
                ".//*[.//svg[contains(@class,'close') or contains(@class,'xmark') or contains(@class,'times')]]",
                ".//*[.//svg//*[contains(@d,'M') and contains(@d,'L') and (contains(@d,'18') or contains(@d,'12'))]]",  # SVG path X
                ".//*[.//svg[contains(@data-icon,'xmark') or contains(@data-icon,'times')]]",
                # === 6️⃣ Positional / floating top-right close controls ===
                ".//*[contains(@class,'top-') and contains(@class,'right-') and (.//svg or .//*[contains(@class,'close')])]",
                ".//div[contains(@class,'top-') and contains(@class,'right-') and (.//svg or .//*[contains(@class,'close')])]",
                ".//button[contains(@class,'top-') and contains(@class,'right-')]",
                ".//*[contains(@class,'absolute') and contains(@class,'right') and contains(@class,'top') and (.//svg or .//span)]",
                # === 7️⃣ Generic clickable wrappers (fallbacks) ===
                # ".//*[contains(@class,'cursor-pointer') and (.//svg or .//span or .//path)]",
                # ".//*[@role='button' and (.//svg or .//span)][contains(@class,'close') or contains(@class,'toggle') or contains(@class,'top-')]",
                # ".//div[(contains(@class,'pointer') or contains(@class,'click')) and (.//svg or .//*[contains(@class,'close')])]",
                # === 8️⃣ Special cases (newsletter, cookie, chat, overlay, GDPR) ===
                ".//*[contains(text(),'No thanks') or contains(text(),'no thanks') or contains(text(),'Got it') or contains(text(),'Accept all')]",
                ".//*[contains(@id,'close') or contains(@id,'dismiss') or contains(@id,'cancel') or contains(@id,'exit')]",
                # === 9️⃣ Shadow DOM / inline SVG generic fallback ===
                ".//*[local-name()='svg' and (contains(@class,'close') or contains(@class,'xmark'))]",
            ]

            # Try close buttons first
            for selector in close_selectors:
                btn = popup_element.locator(f"xpath={selector}")
                # try repeatedly clicking the first visible matching locator until either popup closes or nothing visible
                try:
                    while await btn.count() and await btn.first.is_visible():
                        await btn.first.click()
                        await asyncio.sleep(0.5)
                        if not await self._is_locator_visible_safe(popup_element):
                            logging.info(
                                f"helper {helper_id} {popup_type} closed after clicking {selector}"
                            )
                            return True
                    logging.info(
                        f"helper {helper_id} no visible candidates left for {selector}"
                    )
                except Exception as e:
                    logging.debug(f"helper {helper_id} selector {selector} failed: {e}")

            # Escape key for modals
            if popup_type in ["modal", "dropdown"]:
                await page.keyboard.press("Escape")
                logging.info(f"helper {helper_id} Closed {popup_type} using Escape key")
                await asyncio.sleep(0.5)
                if not await self._is_locator_visible_safe(popup_element):
                    logging.info(
                        f"helper {helper_id} {popup_type} closed after pressing Escape key"
                    )
                    return True
                else:
                    logging.info(
                        f"helper {helper_id} {popup_type} still visible after pressing Escape key"
                    )

            # Click outside for tooltips/dropdowns
            if popup_type in ["modal", "overlay", "dropdown"]:
                try:
                    # Get bounding box of the popup element
                    box = await popup_element.bounding_box()

                    if not box:
                        logging.warning(
                            f"helper {helper_id} Could not get bounding box for {popup_type}"
                        )
                        logging.warning(
                            f"helper {helper_id} Could not close {popup_type}"
                        )
                        return False

                    # Get viewport size (fallback to evaluate if page.viewport_size is None)
                    viewport = page.viewport_size
                    if not viewport:
                        viewport = await page.evaluate(
                            "() => ({ width: window.innerWidth, height: window.innerHeight })"
                        )

                    vw = viewport["width"]
                    vh = viewport["height"]

                    x, y, w, h = box["x"], box["y"], box["width"], box["height"]

                    # Candidate points just outside the bounding box (10px padding)
                    padding = 10
                    candidates = [
                        (x + w + padding, y + h / 2),  # right middle
                        (x - padding, y + h / 2),  # left middle
                        (x + w / 2, y - padding),  # top middle
                        (x + w / 2, y + h + padding),  # bottom middle
                        (x - padding, y - padding),  # top-left corner outside
                        (x + w + padding, y - padding),  # top-right
                        (x - padding, y + h + padding),  # bottom-left
                        (x + w + padding, y + h + padding),  # bottom-right
                    ]

                    # Clamp candidate points into viewport and filter valid ones
                    def clamp(px, py):
                        px = max(1, min(px, vw - 1))
                        py = max(1, min(py, vh - 1))
                        return px, py

                    candidates = [clamp(px, py) for px, py in candidates]

                    clicked = False
                    for idx, (click_x, click_y) in enumerate(candidates):
                        try:
                            await page.mouse.click(click_x, click_y)
                            logging.info(
                                f"helper {helper_id} Tried clicking outside overlay at ({click_x:.1f},{click_y:.1f}) (candidate {idx})"
                            )
                            clicked = True

                            # short wait for any close animation / DOM change
                            await asyncio.sleep(0.25)

                            if not await self._is_locator_visible_safe(popup_element):
                                logging.info(
                                    f"helper {helper_id} {popup_type} closed after clicking outside at ({click_x:.1f},{click_y:.1f})"
                                )
                                return True
                            else:
                                logging.info(
                                    f"helper {helper_id} {popup_type} still visible after clicking candidate {idx}"
                                )
                        except Exception as e:
                            logging.debug(
                                f"helper {helper_id} Error clicking candidate {idx} at ({click_x},{click_y}): {e}"
                            )
                            # continue to next candidate

                    if not clicked:
                        logging.warning(
                            f"helper {helper_id} No clickable candidate found for {popup_type}"
                        )
                    else:
                        logging.warning(
                            f"helper {helper_id} Could not close {popup_type} by clicking outside"
                        )

                except Exception as e:
                    logging.error(
                        f"helper {helper_id} Error clicking outside overlay: {e}"
                    )

            logging.warning(f"helper {helper_id} Could not close {popup_type}")
            return False

        except Exception as e:
            logging.error(f"helper {helper_id} Error closing {popup_type}: {e}")
            return False

    async def remove_widget(self, page, helper_id):
        # Locate the widget
        widget = page.locator("#agentzee-widget-root")

        # Check if it exists
        if await widget.count() > 0:
            logging.info(f"Helper :{helper_id} Widget found, removing...")

            # Remove from DOM
            await page.evaluate(
                """
                const el = document.querySelector('#agentzee-widget-root');
                if (el) el.remove();
            """
            )

            logging.info(f"Helper :{helper_id} Widget removed! ")
        else:
            logging.info(f"Helper :{helper_id} Widget not found")

        widget = page.locator("#headlessui-portal-root")

        # Check if it exists
        if await widget.count() > 0:
            logging.info(f"Helper :{helper_id} Widget found, removing...")

            # Remove from DOM
            await page.evaluate(
                """
                const el = document.querySelector('#headlessui-portal-root');
                if (el) el.remove();
            """
            )

            logging.info(f"Helper :{helper_id} Widget removed!")
        else:
            logging.info(f"Helper :{helper_id} Widget not found")

    async def helper(self, context, helper_id: int):
        """helper that takes URLs from queue"""
        logging.info(f"helper {helper_id} started")
        self.helper_pages[helper_id] = set()

        while True:

            await self.pause_event.wait()
            try:
                url = await self.to_visit.get()

                if url is None:
                    logging.info(f"helper {helper_id} received stop signal.")
                    self.to_visit.task_done()
                    break

                was_added = await self.state.add_scraped_url(url, helper_id)
                if not was_added:
                    logging.info(f"helper {helper_id}: URL already scraped: {url}")
                    await self.update_stats("urls_skipped")
                    self.to_visit.task_done()
                    continue

                logging.info(f"helper {helper_id}: Processing URL: {url}")
                await self.scrape_tab(context, url, helper_id)
                self.to_visit.task_done()

            except asyncio.CancelledError:
                logging.info(f"helper {helper_id} cancelled")
                try:
                    self.to_visit.task_done()
                except Exception:
                    pass
                break

            except Exception as e:
                logging.error(f"helper {helper_id}: Unexpected error: {e}")
                logging.error(f"Traceback:\n{traceback.format_exc()}")
                await self.update_stats("errors")
                try:
                    self.to_visit.task_done()
                except Exception:
                    pass
                continue

    # file: playwright_login.py

    async def login_to_website(
        self,
        url: str,
        login_link_selector: str,
        username: str,
        password: str,
        username_selector: str,
        password_selector: str,
        login_button_selector: str,
        wait_for_url_change: bool = True,
        page: Page = None,
    ):
        """
        Generic async login function using Playwright.

        Handles navigation reloads safely and returns a single Page object.
        """
        try:
            logging.info(f"Navigating to: {url}")
            await page.goto(url, timeout=20000)
            await page.wait_for_load_state("domcontentloaded")

            if login_link_selector:
                logging.info(f"clicking the login button {login_button_selector}")
                await page.click(f"xpath={login_link_selector}")
                await page.wait_for_load_state("domcontentloaded")

            # Ensure input fields exist before typing
            await page.wait_for_selector(f"xpath={username_selector}", timeout=5000)
            await page.wait_for_selector(f"xpath={password_selector}", timeout=5000)

            # Type slowly to mimic human behavior
            await page.fill(f"xpath={username_selector}", "")
            await page.type(f"xpath={username_selector}", username, delay=50)

            await page.fill(f"xpath={password_selector}", "")
            await page.type(f"xpath={password_selector}", password, delay=50)

            # Wait before clicking login to avoid context loss
            await page.wait_for_timeout(500)

            # Wait for navigation triggered by login
            async with page.expect_navigation(
                wait_until="domcontentloaded", timeout=15000
            ):
                await page.click(f"xpath={login_button_selector}")

            await self.wait_until_ready(page)

            # Optionally wait for URL change
            if wait_for_url_change:
                old_url = url
                try:
                    await page.wait_for_function(
                        f"window.location.href !== '{old_url}'", timeout=10000
                    )
                except PlaywrightTimeoutError:
                    logging.warning(" Login completed, but URL did not change.")

            logging.info("✅ Login successful!")
            return page

        except Exception as e:
            print(f"❌ Login failed: {str(e)}")
            return None

    async def run(
        self,
        headless: bool = False,
        login_url: str = None,
        login_link_selector: str = None,
        username: str = None,
        password: str = None,
        username_selector: str = None,
        password_selector: str = None,
        login_button_selector: str = None,
    ):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--start-maximized" if not headless else "",
                ],
            )
            if headless:
                logging.info("Running in headless mode.")
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (...) Chrome/120 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    screen={"width": 1920, "height": 1080},
                )
            else:
                logging.info("Running in headed mode with no viewport.")
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (...) Chrome/120 Safari/537.36",
                    no_viewport=True,
                )
            if login_url:
                logging.info(f"Logging in to {login_url}...")
                page = await self.login_to_website(
                    url=login_url,
                    login_link_selector=login_link_selector,
                    username=username,
                    password=password,
                    username_selector=username_selector,
                    password_selector=password_selector,
                    login_button_selector=login_button_selector,
                    page=await context.new_page(),
                )
                if page is None:
                    logging.error("Login failed, aborting scraper.")
                    return
                await context.add_cookies(await page.context.cookies())
                await page.close()
                logging.info("Login successful, starting scraper...")

            # Add initial URL to queue
            await self.to_visit.put(self.start_url)
            await self.update_stats("urls_queued")

            # Create helpers with IDs for debugging
            helpers = [
                asyncio.create_task(self.helper(context, i))
                for i in range(self.max_concurrency)
            ]

            try:
                # Wait for all enqueued items to be processed
                await self.to_visit.join()
                logging.info("Queue is empty — sending stop signals to helpers...")

                # Send one sentinel None per helper so they exit gracefully
                for _ in range(self.max_concurrency):
                    await self.to_visit.put(None)

                # Wait for helpers to exit after receiving sentinel
                await asyncio.gather(*helpers)

                logging.info("All helpers finished processing.")

            except KeyboardInterrupt:
                logging.info("Received keyboard interrupt, shutting down early...")
                # On early shutdown, cancel helpers
                for w in helpers:
                    w.cancel()
                await asyncio.gather(*helpers, return_exceptions=True)

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
    asyncio.run(
        RecursiveScraper(
            "https://webopt.ai/seo/report?id=cc6089cd-8516-418c-af77-3bf949805cde&pageId=598a9835-ded6-4291-9a2d-6f7d628ea1e6", max_concurrency=1, avoid_page=["tools", "blog"]
        ).run(
            True,
            "https://webopt.ai/",
            "/html/body/div[1]/header/nav/div[3]/div/div/p",
            "vahak77251@bllibl.com",
            "Test@12345",
            "/html/body/div[4]/div/div/div/div[2]/div/div/form/div/div/div[3]/input",
            "/html/body/div[4]/div/div/div/div[2]/div/div/form/div/div/div[3]/div[1]/input",
            "/html/body/div[4]/div/div/div/div[2]/div/div/form/div/div/div[4]/button",
        )
    )

    # asyncio.run(
    #     RecursiveScraper(
    #         "https://app.instantly.ai/app/accounts", max_concurrency=1
    #     ).run(
    #         headless=False,
    #         login_url="https://app.instantly.ai/auth/login",
    #         username="sabarish@episyche.com",
    #         password="Test@123!",
    #         username_selector="/html/body/div[1]/section/div[2]/div/div/div[2]/div/div/form/div/div[1]/div[1]/input",
    #         password_selector="/html/body/div[1]/section/div[2]/div/div/div[2]/div/div/form/div/div[1]/div[2]/input",
    #         login_button_selector="/html/body/div[1]/section/div[2]/div/div/div[2]/div/div/form/div/div[2]/div[1]/button",
    #     )
    # )
    # asyncio.run(
    #     RecursiveScraper(
    #         "https://v2.dende.ai/dashboard", max_concurrency=1).run(
    #         headless=False,
    #         login_url="https://v2.dende.ai/login",
    #         username="melissa-rogers@powerscrews.com",
    #         password="Test123!",
    #         username_selector="/html/body/div[1]/div[1]/main/div[1]/div/form/div[1]/div/input",
    #         password_selector="/html/body/div[1]/div[1]/main/div[1]/div/form/div[2]/div/input",
    #         login_button_selector="/html/body/div[1]/div[1]/main/div[1]/div/form/button/span",
    #     )
    # )

    # asyncio.run(
    #     RecursiveScraper("http://localhost:3000/", max_concurrency=5).run(
    #         headless=True
    #     )
    # )
