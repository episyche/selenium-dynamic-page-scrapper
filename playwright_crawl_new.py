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
from threading import Lock as ThreadLock

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


class RecursiveScraper:
    def __init__(self, start_url, max_concurrency=1, avoid_page=None):
        self.start_url = start_url
        parsed = urlparse(start_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.max_concurrency = max_concurrency
        self.to_visit = asyncio.Queue()

        # Replace shared state with thread-safe container
        self.state = ThreadSafeState()

        self.avoid_page = avoid_page or []

        # Add statistics tracking
        self.stats_lock = asyncio.Lock()
        self.stats = {
            "urls_queued": 0,
            "urls_processed": 0,
            "urls_skipped": 0,
            "errors": 0,
        }

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
        self, page, timeout: int = 5, settle_time: float = 1.5, previous_url=None
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

    async def scrape_tab(self, context, url):
        """Scrape visible elements and click interactive pointer elements recursively."""
        page = None
        try:
            page = await context.new_page()
            await page.goto(url, timeout=10000)
            await page.wait_for_load_state("networkidle")
            await self.wait_until_ready(page, timeout=15, settle_time=1.5)

            body = await page.query_selector("body")
            if not body:
                print("No body tag found on the page.")
                return

            all_elements = await body.query_selector_all("*")
            page_details = {
                "title": await page.title(),
                "url": page.url,
                "elements": [],
            }
            logging.info(
                f"Scraping URL: {page.url} with {len(all_elements)} elements found."
            )

            visible_elements = []
            previous_url = page.url
            xpaths = []
            parent_xpath = None

            for element in all_elements:
                if await element.is_visible():
                    xpath = await element.evaluate(self.get_xpath_js)
                    xpaths.append(xpath)

            for xpath in xpaths:
                try:
                    element = await page.query_selector(f"xpath={xpath}")
                    if not element:
                        logging.warning(
                            f"Element not found for xpath: {xpath} at {previous_url}"
                        )
                        await page.goto(previous_url)
                        await self.wait_until_ready(page, timeout=15, settle_time=1.5)
                        element = await page.query_selector(f"xpath={xpath}")
                        if not element:
                            logging.error(
                                f"Element still not found after reloading page: {xpath} at {previous_url}"
                            )
                            continue
                        logging.info(
                            f"Successfully reloaded and found element for xpath: {xpath} at {previous_url}"
                        )

                    tag = await element.evaluate("(el) => el.tagName")
                    if tag.lower() in [
                        "script",
                        "style",
                        "meta",
                        "link",
                        "noscript",
                    ]:
                        continue

                    # Get only direct text, not from children
                    direct_text = await element.evaluate(
                        """
                    (el) => Array.from(el.childNodes)
                        .filter(node => node.nodeType === Node.TEXT_NODE)
                        .map(node => node.textContent.trim())
                        .filter(text => text.length > 0)
                        .join(' ')
                    """
                    )

                    attrs = await element.evaluate(
                        "(el) => { let a = {}; for (let attr of el.attributes) a[attr.name] = attr.value; return a; }"
                    )

                    element_data = {
                        "tag": tag.lower(),
                        "text": direct_text,
                        "attributes": attrs,
                        "xpath": xpath,
                    }

                    if (
                        await self.is_probably_clickable(element)
                        and await element.is_enabled()
                        and not self.is_child_xpath(parent_xpath, xpath)
                    ):
                        if tag == "a":
                            href = attrs.get("href")
                            urljoined_href = (
                                urljoin(previous_url, href) if href else None
                            )
                            element_data["link_to"] = urljoined_href
                            if self.should_skip_link(previous_url, href):
                                logging.info(f"Skipping link with href: {href}")
                                continue

                        logging.info(
                            f"Clicking element: <{tag.lower()}> with xpath: {xpath} on {page.url}"
                        )

                        try:
                            element_handle = page.locator(f"xpath={xpath}")
                            element_handle = await element_handle.first.element_handle()
                            if element_handle:
                                await element_handle.evaluate("(el) => el.click()")
                                parent_xpath = xpath

                                # Wait for navigation or DOM content loaded
                                await page.wait_for_load_state(
                                    "domcontentloaded", timeout=30000
                                )
                                await self.wait_until_ready(
                                    page, timeout=15, settle_time=1.5
                                )

                                if page.url != previous_url:
                                    element_data["navigated_to"] = page.url
                                    if not self.should_skip_link(
                                        previous_url, page.url
                                    ):
                                        current_url = page.url.split("?")[0].split("#")[
                                            0
                                        ]

                                        # Thread-safe check and add
                                        already_scraped = (
                                            await self.state.has_scraped_url(
                                                current_url
                                            )
                                        )
                                        if not already_scraped:
                                            await self.to_visit.put(current_url)
                                            await self.update_stats("urls_queued")
                                            logging.info(
                                                f"Queued new URL: {current_url}"
                                            )

                                        logging.info(
                                            f"Navigated to new URL: {current_url}, returning to {previous_url}"
                                        )

                                        can_go_back = await page.evaluate(
                                            "window.history.length > 1"
                                        )
                                        if can_go_back:
                                            await page.go_back()
                                            logging.info(
                                                f"Returned to {previous_url} after navigation"
                                            )
                                            await page.wait_for_load_state(
                                                "domcontentloaded", timeout=30000
                                            )
                                            await self.wait_until_ready(
                                                page,
                                                timeout=15,
                                                settle_time=1.5,
                                                previous_url=previous_url,
                                            )
                                        else:
                                            if page.url != previous_url:
                                                logging.info(
                                                    f"No history available, reloading {previous_url}"
                                                )
                                                await page.goto(
                                                    previous_url,
                                                    wait_until="domcontentloaded",
                                                    timeout=30000,
                                                )
                                                await self.wait_until_ready(
                                                    page, timeout=15, settle_time=1.5
                                                )
                                            else:
                                                logging.info(
                                                    f"Already at {previous_url}, no need to reload"
                                                )
                                                await asyncio.sleep(1)
                        except Exception as e:
                            logging.error(f"Error clicking element: {e}")
                            await self.update_stats("errors")
                            continue

                    visible_elements.append(element_data)

                except Exception as e:
                    logging.error(f"Error processing element: {e}")
                    logging.error(f"Traceback:\n{traceback.format_exc()}")
                    await self.update_stats("errors")
                    continue

            page_details["elements"] = visible_elements

            # Thread-safe result addition
            await self.state.add_result(page_details)
            await self.update_stats("urls_processed")

            logging.info(f"Finished scraping URL: {url}")

        except Exception as e:
            logging.error(f"Fatal error in scrape_tab for {url}: {e}")
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            await self.update_stats("errors")

        finally:
            if page:
                try:
                    await page.close()
                except Exception as e:
                    logging.error(f"Error closing page: {e}")

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

        while True:
            try:
                url = await self.to_visit.get()

                # Thread-safe check and add URL
                was_added = await self.state.add_scraped_url(url)

                if not was_added:
                    # URL was already scraped by another worker
                    logging.info(f"Worker {worker_id}: URL already scraped: {url}")
                    await self.update_stats("urls_skipped")
                    self.to_visit.task_done()
                    continue

                logging.info(f"Worker {worker_id}: Processing URL: {url}")
                await self.scrape_tab(context, url)

            except asyncio.CancelledError:
                logging.info(f"Worker {worker_id} cancelled")
                break
            except Exception as e:
                logging.error(f"Worker {worker_id}: Unexpected error: {e}")
                logging.error(f"Traceback:\n{traceback.format_exc()}")
                await self.update_stats("errors")
            finally:
                self.to_visit.task_done()

    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
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
                # Wait for queue to be empty
                await self.to_visit.join()
                logging.info("Queue is empty, all URLs processed")

            except KeyboardInterrupt:
                logging.info("Received keyboard interrupt, shutting down...")
            finally:
                # Cancel workers
                for w in workers:
                    w.cancel()

                # Wait for workers to finish cancellation
                await asyncio.gather(*workers, return_exceptions=True)

                await browser.close()

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
