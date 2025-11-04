import asyncio
from playwright.async_api import (
    async_playwright,
    Page,
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
from bs4 import BeautifulSoup

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
        self.unique_element = []

        self.navigatable_elements = {}

        # Replace shared state with thread-safe container
        self.state = ThreadSafeState()
        netloc = re.sub(r"[^a-zA-Z0-9._-]", "_", parsed.netloc)
        self.db = Database_Handler(f"{netloc}.db")
        self.script_visible_element = """
            () => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, {
                    acceptNode(node) {
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                            return NodeFilter.FILTER_REJECT;
                        }
                        const rect = node.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) {
                            return NodeFilter.FILTER_REJECT;
                        }
                        return NodeFilter.FILTER_ACCEPT;
                    }
                });
                const visibleNodes = [];
                let currentNode;
                while ((currentNode = walker.nextNode())) {
                    visibleNodes.push(currentNode.outerHTML);
                }
                return visibleNodes.join("\\n");
            }
            """
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
        self.click_lock = asyncio.Lock()

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

    async def scrape_tab(self, context, url, helper_id: int):
        """Scrape visible elements and click interactive pointer elements recursively."""
        page = None
        try:
            await self.pause_event.wait()
            page = await context.new_page()
            self.helper_pages[helper_id].add(page)

            await page.goto(url, timeout=15000)
            # await page.wait_for_load_state("networkidle")
            await self.wait_until_ready(
                page, timeout=15, settle_time=1, helper_id=helper_id
            )

            body = await page.query_selector("body")
            if not body:
                logging.warning(f"helper {helper_id}: No body tag found on {url}")
                return

            all_elements = await body.query_selector_all("*")
            page_details = {
                "title": await page.title(),
                "url": page.url,
                "elements": [],
            }
            logging.info(
                f"helper {helper_id}: Found {len(all_elements)} elements on {url}"
            )
            popups = await self.detect_and_handle_popups(page, helper_id)
            if popups:
                page_details["popups"] = popups

            xpaths = []

            # collect all the xpath of visible elements
            for element in all_elements:
                if await element.is_visible():
                    xpath = await element.evaluate(self.get_xpath_js)
                    xpaths.append(xpath)
            visible_elements_xpaths = xpaths
            logging.info(
                f"helper {helper_id}: Found {len(xpaths)} visible elements on {url}"
            )
            visible_elements = await self.details_from_thre_xpath(
                xpaths, page, url, helper_id, visible_elements_xpaths
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
        self, xpaths, page, url, helper_id, visible_elements_xpaths, hover_element=None
    ):
        # to avoid click both parent and child elements
        previous_url = page.url
        parent_xpath = None
        duplicate_parent_xpath = None
        previous_element_xpath = None
        visible_elements = []
        clciked_xpaths = []
        for xpath in xpaths:
            await self.pause_event.wait()
            try:
                if hover_element:
                    await self.safe_hover(page, hover_element, xpath, helper_id)
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
                if duplicate_parent_xpath and await self.is_child_xpath(
                    duplicate_parent_xpath, xpath
                ):
                    logging.info(
                        f"[helper {helper_id}] Skipping element inside duplicate parent: {xpath} in {url}"
                    )
                    navigation = self.navigatable_elements.get(key_hash)
                    if navigation:
                        logging.info(
                            f"[helper {helper_id}] Found previous navigation for duplicate element: {navigation}"
                        )
                        element_data["navigated_to"] = navigation
                    visible_elements.append(element_data)
                    continue
                elif element_data_include_child_text in self.unique_element:
                    duplicate_parent_xpath = xpath
                    logging.info(
                        f"[helper {helper_id}] Duplicate element skipped: {xpath} in {url}"
                    )
                    # Check if we have navigation info for this duplicate
                    navigation = self.navigatable_elements.get(key_hash)
                    if navigation:
                        logging.info(
                            f"[helper {helper_id}] Found previous navigation for duplicate element: {navigation}"
                        )
                        element_data["navigated_to"] = navigation
                    visible_elements.append(element_data)
                    continue
                else:
                    self.unique_element.append(element_data_include_child_text)

                # clickable check
                if (
                    await self.is_probably_clickable(element, helper_id)
                    and await element.is_enabled()
                    and not await self.is_child_xpath(parent_xpath, xpath)
                ):
                    dom_before_hover = await page.evaluate(
                        "document.documentElement.outerHTML"
                    )
                    dom_before_hover_visible = await page.evaluate(
                        self.script_visible_element
                    )
                    processed_text = (
                        await element.evaluate("(el) => el.innerText || ''")
                    )[:100].lower()
                    if any(word in processed_text for word in self.avoid_buttons):
                        logging.info(
                            f"[helper {helper_id}] Skipping click on avoid button element: {xpath} in {url}"
                        )
                        visible_elements.append(element_data)
                        parent_xpath = xpath
                        continue
                    hovered = await self.safe_hover(page, element, xpath, helper_id)
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
                            dom_before_hover,
                            dom_before_hover_visible,
                            helper_id,
                            visible_elements_xpaths,
                            element,
                        )
                        if dropdown_xpaths:
                            element_data["dropdowns_after_hover"] = dropdown_xpaths

                    href = attrs.get("href")
                    if tag.lower() == "a" and await self.should_skip_link(
                        previous_url, href, helper_id
                    ):
                        continue

                    element_handle = page.locator(f"xpath={xpath}").first
                    handle = await element_handle.element_handle()
                    if not handle:
                        continue

                    logging.info(
                        f"[helper {helper_id}] Clicking element: {xpath} in {url}"
                    )

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
                        dom_before_visible = await page.evaluate(
                            self.script_visible_element
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
                            self.helper_pages[helper_id].add(new_tab)

                            await new_tab.wait_for_load_state("domcontentloaded")
                            new_url = new_tab.url
                            logging.info(
                                f"[helper {helper_id}] Detected new tab: {new_url}"
                            )
                            element_data["opened_new_tab"] = new_url
                            self.navigatable_elements[key_hash] = new_url
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

                        except asyncio.TimeoutError:
                            # No popup opened, check for same-tab navigation
                            logging.info(
                                f"[helper {helper_id}] No popup detected, checking for navigation"
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
                                self.navigatable_elements[key_hash] = new_url
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

                                await page.go_back()
                                await self.wait_until_ready(
                                    page, timeout=15, settle_time=1, helper_id=helper_id
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
                                else:
                                    logging.info(
                                        f"[helper {helper_id}] No navigation occurred after click."
                                    )
                                    dom_after = await page.evaluate(
                                        "document.documentElement.outerHTML"
                                    )
                                    dom_after_visible = await page.evaluate(
                                        self.script_visible_element
                                    )

                                    dom_before_visible = await self._normalize_dom(
                                        dom_before_visible
                                    )
                                    dom_after_visible = await self._normalize_dom(
                                        dom_after_visible
                                    )

                                    if dom_before_visible != dom_after_visible:
                                        if previous_element_xpath is None:
                                            previous_element_xpath = (
                                                clciked_xpaths[
                                                    clciked_xpaths.index(xpath) - 1
                                                ]
                                                if clciked_xpaths.index(xpath) > 0
                                                else None
                                            )
                                        logging.info(
                                            f"[helper {helper_id}] DOM changed after click on {xpath} in {url}."
                                        )
                                        soup_old = BeautifulSoup(
                                            dom_before, "html.parser"
                                        )
                                        soup_new = BeautifulSoup(
                                            dom_after, "html.parser"
                                        )

                                        old_elements = await self.extract_dom_structure(
                                            soup_old
                                        )
                                        new_elements = await self.extract_dom_structure(
                                            soup_new
                                        )

                                        changes = await self.compare_doms(
                                            old_elements, new_elements, helper_id, True
                                        )

                                        if changes["added"]:
                                            logging.info(
                                                f"helper {helper_id}:  internal routes detected in {url} "
                                            )
                                            changed_xpaths = changes["added"]
                                            element_data["internal_routes"] = (
                                                await self.details_from_thre_xpath(
                                                    changed_xpaths,
                                                    page,
                                                    url,
                                                    helper_id,
                                                    await self.get_visible_elements_xpaths(
                                                        page
                                                    ),
                                                )
                                            )
                                            if previous_element_xpath:
                                                previous_element = await page.query_selector(
                                                    f"xpath={previous_element_xpath}"
                                                )
                                                logging.info(
                                                    f"[helper {helper_id}] Returning to previous element {previous_element_xpath} after DOM change."
                                                )
                                                await previous_element.evaluate(
                                                    "(el) => el.click()"
                                                )
                                                await self.wait_until_ready(
                                                    page,
                                                    timeout=15,
                                                    settle_time=1,
                                                    helper_id=helper_id,
                                                )

                                            else:
                                                logging.info(
                                                    f"[helper {helper_id}] No previous element to return to after DOM change so clciking the same elements."
                                                )
                                                await element.evaluate(
                                                    "(el) => el.click()"
                                                )
                                                await self.wait_until_ready(
                                                    page,
                                                    timeout=15,
                                                    settle_time=1,
                                                    helper_id=helper_id,
                                                )
                                        else:
                                            logging.info(
                                                f"[helper {helper_id}] DOM changed but no new elements detected after click on {xpath} in {url}."
                                            )
                                            previous_element_xpath = None
                                    else:
                                        logging.info(
                                            f"[helper {helper_id}] DOM did not change after click on {xpath} in {url}."
                                        )
                                        previous_element_xpath = None

                    finally:
                        # Remove the popup listener
                        page.remove_listener("popup", on_popup)
                visible_elements.append(element_data)

            except Exception as e:
                logging.error(
                    f"[helper {helper_id}] Error processing element {xpath}: {e}"
                )
                logging.error(f"Traceback:\n{traceback.format_exc()}")
                await self.update_stats("errors")
                continue

        return visible_elements

    async def _normalize_dom(self, dom: str) -> str:
        """
        Normalize DOM by removing or standardizing dynamic content.
        Useful for comparing DOM snapshots across navigations or reloads.

        Args:
            dom: Raw DOM string

        Returns:
            Normalized DOM string
        """

        # --- 1. Normalize timestamps, dates, and times ---
        dom = re.sub(
            r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?",
            "TIMESTAMP",
            dom,
        )
        dom = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?(\s*[AP]M)?\b", "TIME", dom)
        dom = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", "DATE", dom)

        # --- 2. Remove or standardize dynamic attributes ---
        dom = re.sub(r'\s+id="[^"]*\d{8,}[^"]*"', ' id="DYNAMIC_ID"', dom)
        dom = re.sub(r'\s+data-(id|reactid)="[^"]*"', ' data-\\1="DYNAMIC_ID"', dom)
        dom = re.sub(r'\s+nonce="[^"]*"', ' nonce="REMOVED"', dom)
        dom = re.sub(r'\s+style="[^"]*"', ' style="REMOVED"', dom)

        # Remove inline event handlers like onclick="..."
        dom = re.sub(r'\son\w+="[^"]*"', ' onEVENT="REMOVED"', dom)

        # Normalize any numeric data-* attributes (e.g. data-number, data-count)
        dom = re.sub(r'data-[a-zA-Z_-]+="\d+"', 'data-ATTR="DYNAMIC_VALUE"', dom)

        # --- 3. Remove security or session identifiers ---
        dom = re.sub(
            r'csrf[-_]token["\s:=]+[^"\s<>]+', 'csrf_token="REMOVED"', dom, flags=re.I
        )
        dom = re.sub(
            r'session[-_]id["\s:=]+[^"\s<>]+', 'session_id="REMOVED"', dom, flags=re.I
        )

        # --- 4. Replace UUIDs / hashes ---
        dom = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "UUID",
            dom,
            flags=re.I,
        )
        dom = re.sub(
            r'id="[A-Za-z0-9_\-]{12,}"', 'id="DYNAMIC_ID"', dom
        )  # long alphanumeric IDs

        # --- 5. Remove script and comment blocks ---
        dom = re.sub(
            r"<script[^>]*>.*?</script>",
            "<script>REMOVED</script>",
            dom,
            flags=re.DOTALL,
        )
        dom = re.sub(r"<!--.*?-->", "", dom, flags=re.DOTALL)

        # --- 6. Replace visible numeric content ---
        # Handles numbers with separators (’, ', comma, dot, underscore, or space)
        dom = re.sub(
            r">(.*?\d[\d’'`,._\s]*\d.*?)<",
            lambda m: ">"
            + re.sub(
                r"\d+", "NUMBER", re.sub(r"(NUMBER[’'`,._\s]*)+", "NUMBER", m.group(1))
            )
            + "<",
            dom,
        )

        # Optional: simplify patterns like "NUMBER’NUMBER’NUMBER" → "NUMBER"
        dom = re.sub(r"(NUMBER[’'`,._\s]*)+", "NUMBER", dom)

        # --- 7. Normalize whitespace ---
        dom = re.sub(r"\s+", " ", dom).strip()

        return dom

    async def detect_dropdown_after_hover(
        self,
        page,
        previous_dom,
        previous_dom_visible,
        helper_id,
        visible_elements_xpaths,
        element,
    ):
        """Detect dropdowns that appear after hovering"""
        try:

            # Get current DOM after hover
            await self.wait_until_ready(
                page=page, timeout=5, settle_time=1, helper_id=helper_id
            )
            current_dom = await page.evaluate("document.documentElement.outerHTML")
            current_dom_visible = await page.evaluate(self.script_visible_element)
            previous_dom = await self._normalize_dom(previous_dom)
            current_dom = await self._normalize_dom(current_dom)

            soup_old = BeautifulSoup(previous_dom, "html.parser")
            soup_new = BeautifulSoup(current_dom, "html.parser")

            old_elements = await self.extract_dom_structure(soup_old)
            new_elements = await self.extract_dom_structure(soup_new)

            changes = await self.compare_doms(old_elements, new_elements, helper_id)

            if (
                len(changes["added"]) == 0
                and previous_dom_visible != current_dom_visible
            ):
                logging.info(
                    f"[helper {helper_id}]: no changes detected in full dom, checking visible dom only"
                )
                xpaths = await self.get_visible_elements_xpaths(page)
                changes["added"] = [
                    x for x in xpaths if x not in visible_elements_xpaths
                ]
            logging.info(f"helper {helper_id} changed xpath{changes['added']}")
            if changes["added"]:
                logging.info(f"[helper {helper_id}]:dropdown detected ")
                xpath = changes["added"]
                dropdown_data = await self.details_from_thre_xpath(
                    xpath,
                    page,
                    page.url,
                    helper_id,
                    await self.get_visible_elements_xpaths(page),
                    element,
                )
                # dropdown_data = self.details_using_xpath(xpath, hover_element)
                return dropdown_data
            logging.info(f"[helper {helper_id}]:no dropdown detected")
            return None
        except Exception as e:
            logging.error(
                f"[helper {helper_id}]:Error detecting dropdown after hover: {e}"
            )
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return None

    async def get_visible_elements_xpaths(self, page):
        """Get xpaths of all visible elements on the page."""
        body = await page.query_selector("body")
        if not body:
            return []

        all_elements = await body.query_selector_all("*")
        xpaths = []
        for element in all_elements:
            if await element.is_visible():
                xpath = await element.evaluate(self.get_xpath_js)
                xpaths.append(xpath)
        return xpaths

    async def safe_hover(self, page, element, xpath, helper_id):
        """Safely hover over an element using Playwright with multiple fallback strategies."""
        try:
            # ✅ Step 1: Ensure the element is visible and stable
            await element.scroll_into_view_if_needed(timeout=3000)
            await page.wait_for_selector(
                f"xpath={xpath}", state="visible", timeout=3000
            )
            await page.wait_for_timeout(300)

            # ✅ Step 2: Attempt normal hover
            try:
                await element.hover(timeout=3000)
                logging.info(
                    f"[helper {helper_id}] Hovered over element normally: {xpath}"
                )
                await page.wait_for_timeout(300)
                return True
            except Exception as e:
                logging.warning(
                    f"[helper {helper_id}] Normal hover failed for {xpath}: {e}"
                )

            # ✅ Step 3: Check for overlay elements that might intercept pointer events
            try:
                intercepting_el = await page.evaluate(
                    """
                    (el) => {
                        const rect = el.getBoundingClientRect();
                        const x = rect.left + rect.width / 2;
                        const y = rect.top + rect.height / 2;
                        const elAtPoint = document.elementFromPoint(x, y);
                        if (elAtPoint && elAtPoint !== el && !el.contains(elAtPoint)) {
                            return elAtPoint.outerHTML.slice(0, 200);
                        }
                        return null;
                    }
                    """,
                    element,
                )
                if intercepting_el:
                    logging.warning(
                        f"[helper {helper_id}] Element {xpath} is intercepted by: {intercepting_el}"
                    )
                    await page.wait_for_timeout(500)
            except Exception as overlay_e:
                logging.debug(f"[helper {helper_id}] Overlay check failed: {overlay_e}")

            # ✅ Step 4: Force hover if safe
            try:
                await element.hover(force=True, timeout=2000)
                logging.info(f"[helper {helper_id}] Forced hover succeeded for {xpath}")
                await page.wait_for_timeout(300)
                return True
            except Exception as e:
                logging.warning(
                    f"[helper {helper_id}] Forced hover failed for {xpath}: {e}"
                )

            # ✅ Step 5: Final fallback - simulate hover with JS dispatch
            try:
                await page.evaluate(
                    """
                    (el) => {
                        ['mouseover', 'mouseenter'].forEach(eventType => {
                            const event = new MouseEvent(eventType, {
                                bubbles: true,
                                cancelable: true,
                                view: window
                            });
                            el.dispatchEvent(event);
                        });
                    }
                    """,
                    element,
                )
                await page.wait_for_timeout(300)
                logging.info(
                    f"[helper {helper_id}] JS-dispatched hover succeeded: {xpath}"
                )
                return True
            except Exception as js_e:
                logging.error(f"[helper {helper_id}] JS hover fallback failed: {js_e}")
                return False

        except Exception as outer_e:
            logging.error(
                f"[helper {helper_id}] Unexpected error in safe_hover: {outer_e}"
            )
            return False

    async def get_xpath_bs(self, el):
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

    async def extract_dom_structure(self, soup, created=True):
        elements = []
        for element in soup.find_all(True):  # True = all tags
            xpath = await self.get_xpath_bs(element)

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

    async def compare_doms(self, old, new, helper_id, is_include_text=False):
        # Filter out <body> elements
        logging.info(f"helper {helper_id}: comparing doms to detect changes")
        old_filtered = [e for e in old if e["tag"].lower() != "body"]
        new_filtered = [
            e
            for e in new
            if e["tag"].lower()
            not in ["body", "style", "script", "meta", "link", "noscript"]
        ]

        if is_include_text:

            def serialize_attrs(attrs):
                return tuple(sorted((k, v) for k, v in attrs.items()))

            logging.info(f"helper {helper_id} comparing doms with text included")
            old_set = {(e["xpath"], e["text"]) for e in old_filtered}

            # Keep the order of new elements
            added = [
                e["xpath"]
                for e in new_filtered
                if (e["xpath"], e["text"]) not in old_set
            ]
            logging.info(f"helper {helper_id}: Found added elements with text {added}")
            return {"added": added}

        old_set = {(e["xpath"]) for e in old_filtered}

        # Keep the order of new elements
        added = [(e["xpath"]) for e in new_filtered if (e["xpath"]) not in old_set]
        logging.info(f"helper {helper_id}: Found added elements {added}")
        return {"added": added}

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

            cursor = await element.evaluate("(el) => getComputedStyle(el).cursor")
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
                # --- Headless UI / similar (explicit) ---
                "//div[contains(@id,'headlessui-dialog') or contains(@id,'headlessui-dialog-panel')]",
                "//*[@data-headlessui-state='open' or contains(@data-headlessui-state,'open')]",
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
                "//div[contains(@class,'MuiDialog') or contains(@class,'MuiModal')]",
                "//div[contains(@class,'chakra-modal') or contains(@class,'chakra-alert-dialog')]",
                "//div[contains(@class,'mantine-Modal') or contains(@class,'mantine-Drawer')]",
                "//div[contains(@class,'bootbox') or (contains(@class,'fade') and contains(@class,'show'))]",
                "//div[contains(@class,'shadcn') or contains(@class,'radix') or contains(@class,'headlessui')]",
                # --- overlays / backdrops (visible modal overlays) ---
                "//div[contains(@style,'position: fixed') and (contains(@class,'overlay') or contains(@class,'backdrop') or contains(@class,'modal'))]",
                "//*[contains(@class,'backdrop') or contains(@class,'overlay')][contains(@style,'opacity') or contains(@style,'background')]",
                "//*[contains(@data-backdrop,'true') or contains(@data-overlay,'true')]",
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
                visible_modals = [el for el in elements if await el.is_visible()]

                if visible_modals:
                    modal = visible_modals[0]
                    logging.info(
                        f"helper {helper_id} Modal detected using selector: {selector}of  {await modal.evaluate(self.get_xpath_js)}"
                    )
                    popup_data = await self.extract_popup_details(
                        page, modal, "modal", helper_id
                    )
                    await self.close_popup(page, modal, "modal", helper_id)
                    return popup_data

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
                visible_tooltips = [el for el in elements if await el.is_visible()]

                if visible_tooltips:
                    tooltip = visible_tooltips[0]
                    logging.info(
                        f"helper {helper_id} Tooltip detected using selector :{selector} of {await tooltip.evaluate(self.get_xpath_js)}"
                    )
                    popup_data = await self.extract_popup_details(
                        page, tooltip, "tooltip", helper_id
                    )
                    await self.close_popup(page, tooltip, "tooltip", helper_id)
                    return popup_data

            # --- 3. Overlays ---
            overlay_selectors = [
                # --- Framework-specific masks / backdrops (specific first) ---
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
                "//div[(contains(@style,'position: fixed') or contains(@style,'position: absolute')) and (contains(@style,'z-index') or contains(@style,'opacity') or contains(@style,'background'))]",
                "//div[contains(@style,'position: fixed') and (contains(@style,'rgba') or contains(@style,'opacity') or contains(@style,'background')) and not(contains(@style,'opacity: 0'))]",
                # --- elements that are full-screen / near-full-screen by bounding heuristics (fallback) ---
                # Note: these use visible heuristics in runtime code (not pure XPath) but left here for discovery
                "//div[(contains(@style,'top: 0') or contains(@style,'left: 0')) and (contains(@style,'width: 100%') or contains(@style,'height: 100%') or contains(@style,'inset: 0'))]",
                # --- semantic / ARIA fallbacks (rare for overlays but useful) ---
                "//*[@role='presentation' and (contains(@class,'overlay') or contains(@id,'backdrop'))]",
                "//*[contains(@aria-hidden,'true') and (contains(@class,'backdrop') or contains(@id,'overlay'))]",
                # --- last-resort: section/aside that looks like an overlay/backdrop by tokens ---
                "//section[contains(@class,'overlay') or contains(@class,'backdrop') or contains(@id,'overlay')]",
                "//aside[contains(@class,'overlay') or contains(@class,'backdrop') or contains(@id,'overlay')]",
            ]

            for selector in overlay_selectors:
                elements = await page.locator(f"xpath={selector}").element_handles()
                visible_overlays = []
                for el in elements:
                    if await el.is_visible():
                        box = await el.bounding_box()
                        if box and box["height"] > 50:
                            visible_overlays.append(el)

                if visible_overlays:
                    overlay = visible_overlays[0]
                    logging.info(
                        f"helper {helper_id} Overlay detected using selector: {selector} of {await overlay.evaluate(self.get_xpath_js)}"
                    )
                    popup_data = await self.extract_popup_details(
                        page, overlay, "overlay", helper_id
                    )
                    await self.close_popup(page, overlay, "overlay", helper_id)
                    return popup_data

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

    async def close_popup(
        self, page: Page, popup_element, popup_type: str, helper_id: int
    ):
        """
        Attempt to close the popup using various methods.
        """
        try:
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
                ".//*[contains(@class,'cursor-pointer') and (.//svg or .//span or .//path)]",
                ".//*[@role='button' and (.//svg or .//span)][contains(@class,'close') or contains(@class,'toggle') or contains(@class,'top-')]",
                ".//div[(contains(@class,'pointer') or contains(@class,'click')) and (.//svg or .//*[contains(@class,'close')])]",
                # === 8️⃣ Special cases (newsletter, cookie, chat, overlay, GDPR) ===
                ".//*[contains(text(),'No thanks') or contains(text(),'no thanks') or contains(text(),'Got it') or contains(text(),'Accept all')]",
                ".//*[contains(@id,'close') or contains(@id,'dismiss') or contains(@id,'cancel') or contains(@id,'exit')]",
                # === 9️⃣ Shadow DOM / inline SVG generic fallback ===
                ".//*[local-name()='svg' and (contains(@class,'close') or contains(@class,'xmark'))]",
            ]

            # Try close buttons first
            for selector in close_selectors:
                btn = await popup_element.query_selector(f"xpath={selector}")
                if btn and await btn.is_visible():
                    await btn.click()
                    logging.info(
                        f"helper {helper_id} Closed {popup_type} using {selector}"
                    )
                    await asyncio.sleep(0.5)
                    return True

            # Escape key for modals
            if popup_type in ["modal", "dropdown"]:
                await page.keyboard.press("Escape")
                logging.info(f"helper {helper_id} Closed {popup_type} using Escape key")
                await asyncio.sleep(0.5)
                return True

            # Click outside for tooltips/dropdowns
            if popup_type in ["tooltip", "dropdown"]:
                await page.mouse.click(10, 10)
                logging.info(
                    f"helper {helper_id} Closed {popup_type} by clicking outside"
                )
                await asyncio.sleep(0.5)
                return True

            # Click overlay background
            overlay = await page.query_selector(
                "xpath=//div[contains(@class,'modal-backdrop') or contains(@class,'overlay')]"
            )
            if overlay and await overlay.is_visible():
                await overlay.click()
                logging.info(
                    f"helper {helper_id} Closed {popup_type} by clicking overlay"
                )
                await asyncio.sleep(0.5)
                return True

            logging.warning(f"helper {helper_id} Could not close {popup_type}")
            return False

        except Exception as e:
            logging.error(f"helper {helper_id} Error closing {popup_type}: {e}")
            return False

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
        Generic login function using Playwright (async)

        Args:
            url: Website login URL
            login_link_selector: CSS or XPath selector for login link/button
            username: Your username/email
            password: Your password
            username_selector: CSS or XPath selector for username field
            password_selector: CSS or XPath selector for password field
            login_button_selector: CSS or XPath selector for login button
            wait_for_url_change: Wait for URL to change after login (default: True)
        """
        try:
            print(f"Navigating to: {url}")
            await page.goto(url, timeout=20000)
            await page.wait_for_load_state("domcontentloaded")

            await page.click(f"xpath={login_link_selector}")
            await page.wait_for_load_state("domcontentloaded")

            # Fill username and password fields
            await page.fill(f"xpath={username_selector}", username)
            await page.fill(f"xpath={password_selector}", password)

            # Click the login button
            await page.click(f"xpath={login_button_selector}")

            # Optional: Wait for URL to change after login
            if wait_for_url_change:
                try:
                    await page.wait_for_url(
                        lambda current: current != url, timeout=10000
                    )
                except PlaywrightTimeoutError:
                    print("⚠️ Login completed, but URL did not change.")

            print("✅ Login successful!")
            return page  # Keep browser open for reuse

        except Exception as e:
            print(f"❌ Login failed: {str(e)}")
            return None, None

    async def run(
        self,
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
                headless=False,
                args=["--disable-web-security", "--window-size=1920,1080"],
            )
            context = await browser.new_context()
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
            "https://webopt.ai/", max_concurrency=5, avoid_page=["tools"]
        ).run(
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
    #        "http://localhost:3000/", max_concurrency=5, avoid_page=["tools", "blog"]
    #     ).run()
    # )
