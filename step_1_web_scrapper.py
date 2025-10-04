import os
import json
import time
import hashlib
import logging
import traceback
import re
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver import ActionChains
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
    WebDriverException,
    MoveTargetOutOfBoundsException,
    ElementNotInteractableException,
)
from bs4 import BeautifulSoup

# --- logging setup (ensure directory exists) ---
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/testing.log",
    encoding="utf-8",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s",
)


class WebElementFinder:
    """Web element finder with multiple strategies"""

    def __init__(self, url: str, driver: Any = None, avoid_pages: List[str] = []):
        """Use given Chrome driver, or create new if none provided"""
        if driver is None:
            self.session = requests.Session()
            chrome_options = Options()
            # chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--disable-background-timer-throttling")

            # Block images (this replaces --disable-images)
            # prefs = {"profile.managed_default_content_settings.images": 2}
            # chrome_options.add_experimental_option("prefs", prefs)
            self.driver = webdriver.Chrome(options=chrome_options)
        else:
            self.driver = driver
        self.driver.set_page_load_timeout(10)
        self.avoid_page = avoid_pages
        self.script_vivible_element="""
                        function isVisible(elem) {
                            if (!(elem instanceof Element)) return false;
                            const style = getComputedStyle(elem);
                            return style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                style.opacity !== '0' &&
                                elem.offsetWidth > 0 &&
                                elem.offsetHeight > 0;
                        }

                        function getVisibleHTML(elem) {
                            if (!isVisible(elem)) return ''; // skip invisible elements entirely
                            
                            let html = ''; 
                            for (const child of elem.children) {
                                html += getVisibleHTML(child); // only include visible children
                            }
                            
                            // if element itself has no visible children, return its outerHTML
                            if (html === '') {
                                return elem.outerHTML;
                            } else {
                                // otherwise, wrap visible children in the parent tag without duplicating invisible parts
                                const tag = elem.tagName.toLowerCase();
                                const attrs = Array.from(elem.attributes)
                                                .map(a => `${a.name}="${a.value}"`)
                                                .join(' ');
                                return `<${tag}${attrs ? ' ' + attrs : ''}>${html}</${tag}>`;
                            }
                        }

                        return getVisibleHTML(document.body);

                        """
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

        self.scraped_urls: List[str] = []
        self._seen_hashes: List[str] = []
        self.whole_website = []
        self.base_url = url
        parsed = urlparse(url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

        logging.info(f"step 1 - base url is set {url}")

    # --- small helpers ---

    def wait_for_page_load(self, timeout: int = 8):
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

    def wait_for_dynamic_content(self, timeout: int = 8):
        self.driver.execute_script(
            """
        window.activeRequests = 0;
        (function(open) {
            XMLHttpRequest.prototype.open = function() {
                window.activeRequests++;
                this.addEventListener('loadend', function() {
                    window.activeRequests--;
                });
                open.apply(this, arguments);
            };
        })(XMLHttpRequest.prototype.open);

        (function(fetch) {
            window.fetch = function() {
                window.activeRequests++;
                return fetch.apply(this, arguments).finally(() => {
                    window.activeRequests--;
                });
            };
        })(window.fetch);
        """
        )
        start = time.time()
        # Wait until all API calls are done
        while True:
            active = self.driver.execute_script("return window.activeRequests")
            logging.info(f"active api the pages have {active}")
            if active == 0:
                break
            if time.time() - start > timeout:
                logging.warning("Timeout waiting for dynamic content")
                break
            time.sleep(0.2)  # Wait a little before checking again

        logging.info("All API calls completed!")

    def wait_for_dom_changes(self, timeout: int = 8):
        """Wait until DOM mutations settle (for React/Vue/SPA apps)."""
        self.driver.execute_script(
            """
        if (!window.__pendingMutations) {
            window.__pendingMutations = 0;
            const observer = new MutationObserver(() => {
                window.__pendingMutations++;
                clearTimeout(window.__mutationTimeout);
                window.__mutationTimeout = setTimeout(() => {
                    window.__pendingMutations = 0;
                }, 300); // settle time
            });
            observer.observe(document.body, { childList: true, subtree: true });
        }
        """
        )

        start = time.time()
        while True:
            pending = self.driver.execute_script("return window.__pendingMutations")
            logging.info(f"dom mutation waiting for  {pending}")
            if pending == 0:
                break
            if time.time() - start > timeout:
                logging.warning("Timeout waiting for DOM changes")
                break
            time.sleep(0.2)

    def wait_until_ready(self, timeout: int = 8):
        """Convenience method: wait for page + dynamic content."""
        self.wait_for_page_load(timeout)
        self.wait_for_dynamic_content(timeout)
        self.wait_for_dom_changes(timeout)

    def remove_duplicate_elements(self, elements):
        """Remove visually duplicate elements by hashing key attributes."""
        unique_elements = []
        for el in elements:
            try:

                if el.tag_name in ["style", "noscript", "script"]:
                    continue

                if not el.is_displayed():
                    continue

                tag_name = el.tag_name
                text = (el.text or "").strip()[:100]
                classes = el.get_attribute("class") or ""
                element_id = el.get_attribute("id") or ""
                href = el.get_attribute("href") or ""
                onclick = el.get_attribute("onclick") or ""
                placeholder = el.get_attribute("placeholder") or ""
                name = el.get_attribute("name") or ""
                value = el.get_attribute("value") or ""
                title = el.get_attribute("title") or ""
                alt = el.get_attribute("alt") or ""
                src = el.get_attribute("src") or ""
                data_testid = el.get_attribute("data-testid") or ""
                aria_label = el.get_attribute("aria-label") or ""
                type_attr = el.get_attribute("type") or ""
                role = el.get_attribute("role") or ""

                parent_form_id = ""
                parent_form_class = ""
                try:
                    parent_form = el.find_element(By.XPATH, "./ancestor::form[1]")
                    parent_form_id = parent_form.get_attribute("id") or ""
                    parent_form_class = parent_form.get_attribute("class") or ""
                except Exception:
                    pass

                # Get element's position/location as additional differentiator
                # try:
                #     location = el.location
                #     position = f"{location['x']},{location['y']}"
                # except Exception:
                #     position = ""

                element_string = "|".join(
                    [
                        tag_name,
                        text,
                        classes,
                        element_id,
                        href,
                        onclick,
                        placeholder,
                        name,
                        value,
                        title,
                        alt,
                        src,
                        data_testid,
                        aria_label,
                        type_attr,
                        role,
                        parent_form_id,
                        parent_form_class,
                        # position
                    ]
                )
                hash_code = hashlib.md5(element_string.encode()).hexdigest()
                if hash_code not in self._seen_hashes:
                    self._seen_hashes.append(hash_code)
                    unique_elements.append(el)
                else:
                    logging.info(f"removing duplicate element - {text!r}")
            except StaleElementReferenceException:
                # If element became stale during hashing, just skip
                continue
        return unique_elements

    def should_skip_link(self, current_url: str, href: Optional[str]) -> bool:
        """
        Decide whether to skip clicking a link.
        Returns True when we should SKIP (invalid/external/already visited).
        """
        if not href:
            return True

        # Check if href contains any word in avoid_page
        if any(word and word in href for word in self.avoid_page):
            logging.info(f"Skipping link due to avoid_page match: {href}")
            return True

        href = href.strip()
        # Non-navigational schemes or anchors
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return True

        # Make absolute
        abs_url = urljoin(current_url, href)

        # removeing query params
        abs_url = abs_url.split("?")[0]

        abs_url = abs_url.split("#")[0]

        parsed_abs = urlparse(abs_url)

        # External origin? skip
        if f"{parsed_abs.scheme}://{parsed_abs.netloc}" != self.origin:
            logging.info(f"Skipping external link: {abs_url}")
            return True

        # Already visited? skip
        if abs_url in self.scraped_urls:
            logging.info(f"Skipping already visited URL: {abs_url}")
            return True

        return False  # OK to click

    def safe_click(self, element):
        """Try a normal click, then move+click, then JS click."""
        try:
            element.click()
            return
        except (ElementClickInterceptedException, WebDriverException):
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", element
                )
                ActionChains(self.driver).move_to_element(element).pause(
                    0.1
                ).click().perform()
                return
            except Exception:
                self.driver.execute_script("arguments[0].click();", element)

    def has_dom_changed(self, previous_dom: str) -> bool:
        """
        Compare the previous DOM with the current DOM of the page, ignoring dynamic content.

        Args:
            previous_dom: The DOM captured before an action (as a string).

        Returns:
            bool: True if the DOM has meaningfully changed, False otherwise.
        """
        # Get current DOM
        current_dom = self.driver.execute_script(self.script_vivible_element)

        # Normalize both DOMs
        prev_normalized = self._normalize_dom(previous_dom)
        curr_normalized = self._normalize_dom(current_dom)

        # Compare using hash
        prev_hash = hashlib.md5(prev_normalized.encode("utf-8")).hexdigest()
        curr_hash = hashlib.md5(curr_normalized.encode("utf-8")).hexdigest()

        return prev_hash != curr_hash

    def _normalize_dom(self, dom: str) -> str:
        """
        Normalize DOM by removing or standardizing dynamic content.

        Args:
            dom: Raw DOM string

        Returns:
            Normalized DOM string
        """
        # Remove timestamps and dates (various formats)
        dom = re.sub(
            r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?",
            "TIMESTAMP",
            dom,
        )
        dom = re.sub(r"\d{1,2}:\d{2}(:\d{2})?(\s*[AP]M)?", "TIME", dom)
        dom = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}", "DATE", dom)

        # Remove dynamic IDs and classes that contain timestamps or random strings
        dom = re.sub(r'id="[^"]*\d{10,}[^"]*"', 'id="DYNAMIC_ID"', dom)
        dom = re.sub(r'data-id="[^"]*"', 'data-id="DYNAMIC_ID"', dom)
        dom = re.sub(r'data-reactid="[^"]*"', 'data-reactid="DYNAMIC_ID"', dom)

        # Remove inline styles that might change (like animations)
        dom = re.sub(r'style="[^"]*"', 'style="REMOVED"', dom)

        # Remove nonce attributes (security tokens)
        dom = re.sub(r'nonce="[^"]*"', 'nonce="REMOVED"', dom)

        # Remove CSRF tokens
        dom = re.sub(
            r'csrf[-_]token["\s:=]+[^"\s<>]+',
            'csrf_token="REMOVED"',
            dom,
            flags=re.IGNORECASE,
        )

        # Remove session IDs
        dom = re.sub(
            r'session[-_]id["\s:=]+[^"\s<>]+',
            'session_id="REMOVED"',
            dom,
            flags=re.IGNORECASE,
        )

        # Remove random/UUID patterns
        dom = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "UUID",
            dom,
            flags=re.IGNORECASE,
        )

        # Remove script tags with timestamps or dynamic content
        dom = re.sub(
            r"<script[^>]*>.*?</script>",
            "<script>REMOVED</script>",
            dom,
            flags=re.DOTALL,
        )
         # Replace numbers in element text with placeholder
        # e.g., <span>Price: 299</span> → <span>Price: NUMBER</span>
        dom = re.sub(
            r">(.*?\d+.*?)<",
            lambda m: ">" + re.sub(r"\d+", "NUMBER", m.group(1)) + "<",
            dom,
        )

        # Normalize whitespace
        dom = re.sub(r"\s+", " ", dom)
        dom = dom.strip()

        return dom

    def click_element_and_scrape_child(
        self, element, element_data, url_before_click, is_popup=False
    ):
        """
        Click the element, handle same-tab navigation or new-tab,
        scrape the child page, then return to the original page.
        """
        wait = WebDriverWait(self.driver, 10)

        handles_before = self.driver.window_handles[:]
        current_url = self.driver.current_url
        dom_before = self.driver.execute_script(self.script_vivible_element)
        self.safe_click(element)
        self.wait_until_ready()
        time.sleep(0.5)  # brief settle

        # --- Check if alert popped up immediately ---
        try:
            alert = self.driver.switch_to.alert
            text = alert.text
            logging.info(f"Alert detected after click: {text}")

            element_data["popup"] = {"type": "alert", "text": text}

            # Default: dismiss (use accept() if required)
            alert.dismiss()
            return element_data, False
        except Exception:
            # No alert → continue with navigation logic
            pass
        if not is_popup:
            try:
                popup_data = self.detect_and_handle_popups()
                if popup_data:
                    element_data["popup"] = popup_data
                    # return element_data
            except Exception:
                # No alert → continue with poplogic logic
                pass

        # Wait for either URL change or a new tab
        def nav_or_tab_opened(d):
            return (d.current_url != url_before_click) or (
                len(d.window_handles) > len(handles_before)
            )

        try:
            wait.until(nav_or_tab_opened)
        except TimeoutException:
            if self.has_dom_changed(dom_before):
                logging.info("internal routes happen")
                internal_elements = self.get_element_detail(3, True)
                element_data["internal_elements"] = internal_elements
                return element_data, True
            else:
                logging.info("No navigation or new tab detected after click")
                return element_data, False  # nothing to do

        # New tab?
        handles_after = self.driver.window_handles[:]
        if len(handles_after) > len(handles_before):
            new_handle = [h for h in handles_after if h not in handles_before][0]
            self.driver.switch_to.window(new_handle)
            self.wait_until_ready()
            new_url = self.driver.current_url.split("?", 1)[0].split("#", 1)[0]
            element_data["navigate_to"] = new_url
            logging.info(f"New tab opened: {new_url}")

            if not self.should_skip_link(current_url, new_url):
                self.scraped_urls.append(new_url)
                self.get_element_detail(wait_time=5)

            # Close child tab and switch back
            self.driver.close()
            self.driver.switch_to.window(handles_before[0])
            try:
                wait.until(EC.url_to_be(url_before_click))
            except TimeoutException:
                # Last resort: refresh to recover
                logging.warning("Back navigation timeout; refreshing original page")
                self.driver.get(url_before_click)
                self.wait_until_ready()
            return element_data, False

        # Same tab navigation
        if self.driver.current_url != url_before_click:
            self.wait_until_ready()
            new_url = self.driver.current_url.split("?", 1)[0].split("#", 1)[0]
            element_data["navigate_to"] = new_url
            logging.info(f"Navigated to new page: {new_url}")

            if not self.should_skip_link(current_url, new_url):
                self.scraped_urls.append(new_url)
                self.get_element_detail(wait_time=5)

            # Go back to original page
            self.driver.back()
            logging.info(f"navigating back to {url_before_click}")
            self.wait_until_ready()
            try:
                wait.until(EC.url_to_be(url_before_click))
            except TimeoutException:
                # Last resort: refresh to recover
                logging.warning("Back navigation timeout; refreshing original page")
                self.driver.get(url_before_click)
                self.wait_until_ready()

        # here to write the logic for drop down,popup

        return element_data, False

    # --- main routines ---

    def find_all_elements_dynamic(self, url=None, wait_time: int = 8) -> Dict[str, Any]:
        """Find all elements including dynamically loaded content (Selenium)"""

        if url is None:
            url = self.base_url
        if url not in self.scraped_urls:
            self.scraped_urls.append(url)
            logging.info(f"step 2 - marking {url} as not yet scraped")

        try:
            self.driver.get(url)
            self.wait_until_ready(15)
            return self.get_element_detail(wait_time, home_page=True)
        except Exception as e:
            logging.error({"error": f"Failed to process page with Selenium: {str(e)}"})
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return {"error": f"Failed to process page with Selenium: {str(e)}"}

    def get_element_detail(
        self, wait_time: int, internal_route=False, home_page=False
    ) -> Dict[str, Any]:
        WebDriverWait(self.driver, wait_time).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        body = self.driver.find_element(By.TAG_NAME, "body")
        all_elements = body.find_elements(By.XPATH, ".//*")

        logging.info("step 3 - filtering unique elements")
        unique_elements = self.remove_duplicate_elements(all_elements)

        popup_data = None
        # handles popups on loading pages
        if not internal_route and home_page:
            try:
                popup_data = self.detect_and_handle_popups()
            except Exception:
                # No alert → continue with poplogic logic
                pass

        # IMPORTANT: work with XPaths to avoid stale references after navigation
        xpaths = []
        for el in unique_elements:
            try:
                xpaths.append(self.get_element_xpath(el))
            except StaleElementReferenceException:
                logging.error(f"staleElement error of {el}")
                continue

        logging.info("step 4 - generating elements metadata")
        result = {
            "metadata": {
                "url": self.driver.current_url,
                "title": self.driver.title,
                "total_elements": len(all_elements),
                "unique_elements": len(xpaths),
            },
            "web_elements": [],
        }
        if popup_data:
            result["metadata"]["popup"] = popup_data

        logging.info("step 5 - iterating elements safely by XPath")
        result["web_elements"].append(self.details_using_xpath(xpaths))
        if internal_route:
            return result["web_elements"]
        self.whole_website.append(result)
        return result

    def details_using_xpath(
        self,
        xpaths,
    ):
        previous_element = None
        first_nav_element = None
        web_elements = []
        for xp in xpaths:
            try:
                WebDriverWait(self.driver, 3).until(
                    EC.presence_of_element_located((By.XPATH, xp))
                )
                element = self.driver.find_element(By.XPATH, xp)

                # Build element snapshot BEFORE clicking
                element_data = {
                    "tag": element.tag_name,
                    "text": self.driver.execute_script(
                        """
                            var element = arguments[0];
                            var directText = '';
                            for (var i = 0; i < element.childNodes.length; i++) {
                            var node = element.childNodes[i];
                            if (node.nodeType === Node.TEXT_NODE) {
                            directText += node.textContent;
                            }
                            }return directText.trim();""",
                        element,
                    ),
                    "attributes": self.driver.execute_script(
                        """
                        const el = arguments[0];
                        const items = {};
                        for (let i = 0; i < el.attributes.length; i++) {
                            items[el.attributes[i].name] = el.attributes[i].value;
                        }
                        return items;
                        """,
                        element,
                    ),
                    # "location": element.location,
                    # "size": element.size,
                    "is_displayed": "true" if element.is_displayed() else "false",
                    "is_enabled": "true" if element.is_enabled() else "false",
                    "xpath": xp,
                }

                if element_data["tag"] in ("div") and element_data["text"] == "":
                    continue
                # Handle hover interactions for specific elements
                if self.is_probably_clickable(element) and self.is_element_visible(element):
                    previous_dom=self.driver.execute_script(self.script_vivible_element)
                    hover_success = self.safe_hover(element)
                    if hover_success:
                        self.wait_for_dom_changes(2)  # Wait for any dropdown/popup to appear
                        # Check for dropdowns after hover
                        dropdown_data = self.detect_dropdown_after_hover(previous_dom)
                        if dropdown_data:
                            element_data["dropdown"] = dropdown_data

                clickable = element.is_enabled() and self.is_probably_clickable(element)

                if clickable:
                    current_url = self.driver.current_url

                    if element.tag_name == "a":
                        href = element.get_attribute("href")
                        if self.should_skip_link(current_url, href):
                            web_elements.append(element_data)
                            continue

                    # check for logout ,delete etc button to avoid clicking
                    processed_text = element.text.lower().strip()[:100]
                    # converting avoid pages to lower
                    self.avoid_buttons = [k.lower() for k in self.avoid_buttons]
                    # Check if the processed text is in the list
                    if any(keyword in processed_text for keyword in self.avoid_buttons):
                        web_elements.append(element_data)
                        continue
                    element_data, internal_element = (
                        self.click_element_and_scrape_child(
                            element, element_data, url_before_click=current_url
                        )
                    )
                    web_elements.append(element_data)
                    if not first_nav_element and internal_element:
                        first_nav_element = previous_element
                    if internal_element:
                        if first_nav_element:
                            try:
                                # First attempt to find and click
                                first_element = self.driver.find_element(
                                    By.XPATH, first_nav_element
                                )
                                # Click safely
                                self.safe_click(first_element)
                                logging.info(
                                    f"find the the previous element in the {first_nav_element} and click the element"
                                )
                                self.wait_until_ready()
                                if self.driver.current_url != current_url:
                                    logging.info(
                                        "navigating to wrong page returning to current page"
                                    )
                                    self.driver.get(current_url)
                                    self.wait_until_ready()
                            except NoSuchElementException:
                                # If not found, refresh the page and try again
                                logging.error(
                                    f"unable to find the the previous element in the {first_nav_element}"
                                )
                                self.driver.refresh()
                                self.wait_until_ready()
                        else:
                            # If not found, refresh the page and try again
                            logging.info("no previous element let refresh the page")
                            self.driver.refresh()
                            self.wait_until_ready()

                    previous_element = xp
                else:
                    web_elements.append(element_data)

            except (NoSuchElementException, StaleElementReferenceException) as e:
                logging.error(f"Element vanished or stale for xpath={xp}: {e}")
                continue

            except TimeoutException as e:
                logging.error(f"unable to find element in {xp} :{e}")
            except Exception as e:
                logging.error(f"Unexpected error for xpath={xp}: {e}")
                logging.error(f"Traceback:\n{traceback.format_exc()}")
                continue

        return web_elements

    def is_probably_clickable(self, element):
        if element.get_attribute("onclick"):
            return True
        if element.tag_name in ("a", "button"):
            return True
        if element.get_attribute("role") == "button":
            return True
        if element.value_of_css_property("cursor") == "pointer":
            return True
        return False

    def is_element_visible(self, element) -> bool:
        """Check if element is truly visible"""
        try:
            if not element.is_displayed() or not element.is_enabled():
                return False

            # Check if element has zero size
            size = element.size
            if size["height"] == 0 or size["width"] == 0:
                return False

            # Check if element is covered by another element
            location = element.location
            if location["x"] < 0 or location["y"] < 0:
                return False

            return True
        except (StaleElementReferenceException, WebDriverException):
            return False

    def safe_hover(self, element):
        """Safely hover over an element with fallback strategies and better stability."""
        try:
            # ✅ Strategy 1: Scroll element into view and hover using ActionChains
            self.driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'instant', block: 'center', inline: 'center'});",
                element,
            )
            time.sleep(0.2)  # Small delay to stabilize scroll

            # Ensure element is interactable after scroll
            if not element.is_displayed():
                logging.warning(f"Element not displayed after scroll: {element}")
                return False

            actions = ActionChains(self.driver)
            actions.move_to_element(element).pause(0.8).perform()
            logging.info(f"Hovered over element (ActionChains): <{element.tag_name}> {element.text[:40]!r}")
            return True

        except (MoveTargetOutOfBoundsException, ElementNotInteractableException) as e:
            logging.warning(f"Hover failed with move/interact error: {e}")

            # ✅ Strategy 2: Try JavaScript hover (more reliable for hidden overlays or offscreen elements)
            try:
                self.driver.execute_script(
                    """
                    const elem = arguments[0];
                    const rect = elem.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;

                    const event = new MouseEvent('mouseover', {
                        bubbles: true,
                        cancelable: true,
                        view: window
                    });
                    elem.dispatchEvent(event);
                    return true;
                    """,
                    element,
                )
                time.sleep(0.3)
                logging.info(f"Hovered using JavaScript dispatch: <{element.tag_name}>")
                return True
            except Exception as js_e:
                logging.error(f"JavaScript hover failed: {js_e}")
                return False

        except (StaleElementReferenceException, WebDriverException) as e:
            logging.error(f"Hover failed (stale/webdriver): {e}")
            return False

        except Exception as e:
            logging.error(f"Unexpected hover error: {type(e).__name__}: {e}")
            return False


    def detect_dropdown_after_hover(self,previous_dom):
        """Detect dropdowns that appear after hovering"""
        try:

            def get_xpath_bs(el):
                path = []
                while el is not None and el.name is not None:
                    # Only consider element siblings at the same level
                    if el.parent:
                        siblings = [sib for sib in el.parent.find_all(el.name, recursive=False)]
                        if len(siblings) > 1:
                            index = siblings.index(el) + 1  # XPath is 1-based
                            path.append(f"{el.name}[{index}]")
                        else:
                            path.append(el.name)
                    else:
                        path.append(el.name)
                    el = el.parent

                path.reverse()
                # Replace [document] with html if somehow it sneaks in
                if path[0] == '[document]':
                    path[0] = 'html'
                return '/' + '/'.join(path)

            def extract_dom_structure(soup):
                elements = []
                for element in soup.find_all(True):  # True = all tags
                    data = {
                        "tag": element.name,
                        "attrs": dict(element.attrs),
                        "text": element.get_text(strip=True),
                        "xpath":get_xpath_bs(element)
                    }
                    elements.append(data)
                return elements

            def compare_doms(old, new):
                # Filter out <body> elements
                old_filtered = [e for e in old if e['tag'].lower() != 'body']
                new_filtered = [e for e in new if e['tag'].lower() != 'body']

                old_set = {(e['xpath']) for e in old_filtered}

                # Keep the order of new elements
                added = [ (e['xpath']) for e in new_filtered
                        if ( e['xpath']) not in old_set ]

                return {"added": added}
            
            current_dom=self.driver.execute_script(self.script_vivible_element)
            
            soup_old = BeautifulSoup(previous_dom, "html.parser")
            soup_new=BeautifulSoup(current_dom,"html.parser")

            old_elements = extract_dom_structure(soup_old)
            new_elements = extract_dom_structure(soup_new)

            changes = compare_doms(old_elements, new_elements)

            logging.info(f"changed xpath{changes['added']}")
            if changes["added"]:
                logging.info("dropdown detected ")
                xpath=changes["added"]
                dropdown_data = self.details_using_xpath(xpath)
                return dropdown_data
            logging.info("no dropdown detected")
            return None
        except Exception as e:
            logging.error(f"Error detecting dropdown after hover: {e}")
            return None

    def get_element_xpath(self, element) -> str:
        """Generate an absolute XPath for a Selenium WebElement (with SVG support)"""
        return self.driver.execute_script(
            """
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
            return getXPath(arguments[0]);
            """,
            element,
        )

    def detect_and_handle_popups(self):
        """
        Detect various types of popups and collect their details.
        Returns popup data if found, None otherwise.
        """
        popup_data = None

        try:

            # 1. Modal dialogs
            modal_selectors = [
                "//div[contains(@class,'modal') and contains(@style,'display: block')]",
                "//div[@role='dialog']",
                "//div[contains(@class,'popup')]",
                "//div[contains(@class,'overlay')]",
                "//div[contains(@class,'lightbox')]",
                "//div[contains(@id,'modal')]",
                "//div[contains(@class,'modal-dialog')]",
            ]

            for selector in modal_selectors:
                modals = self.driver.find_elements(By.XPATH, selector)
                visible_modals = [m for m in modals if m.is_displayed()]

                if visible_modals:
                    modal = visible_modals[0]
                    logging.info(f"Modal dialog detected using selector: {selector}")

                    popup_data = self.extract_popup_details(modal, "modal")
                    self.close_popup(modal, "modal")
                    return popup_data

            # # 2. Dropdown menus
            # dropdown_selectors = [
            #     "//div[contains(@class,'dropdown')]",
            #     "//ul[contains(@class,'dropdown-menu') and contains(@style,'display: block')]",
            #     "//ul[contains(@class,'sub-menu') and contains(@style,'display: block')]",
            #     "//div[@aria-expanded='true']",
            #     "//div[@role='menu' and @aria-hidden='false']",
            # ]

            # for selector in dropdown_selectors:
            #     dropdowns = self.driver.find_elements(By.XPATH, selector)
            #     visible_dropdowns = [d for d in dropdowns if d.is_displayed()]

            #     if visible_dropdowns:
            #         dropdown = visible_dropdowns[0]
            #         logging.info(f"Dropdown detected using selector: {selector}")

            #         popup_data = self.extract_popup_details(dropdown, "dropdown")
            #         self.close_popup(dropdown, "dropdown")
            #         return popup_data

            # 3. Tooltips
            tooltip_selectors = [
                "//div[contains(@class,'tooltip') and contains(@style,'display: block')]",
                "//div[@role='tooltip']",
                "//div[contains(@class,'popover')]",
            ]

            for selector in tooltip_selectors:
                tooltips = self.driver.find_elements(By.XPATH, selector)
                visible_tooltips = [t for t in tooltips if t.is_displayed()]

                if visible_tooltips:
                    tooltip = visible_tooltips[0]
                    logging.info(f"Tooltip detected using selector: {selector}")

                    popup_data = self.extract_popup_details(tooltip, "tooltip")
                    self.close_popup(tooltip, "tooltip")
                    return popup_data

            # 4. Custom overlays
            overlay_selectors = [
                "//div[contains(@style,'z-index') and contains(@style,'position: fixed')]",
                "//div[contains(@style,'z-index') and contains(@style,'position: absolute')]",
            ]

            for selector in overlay_selectors:
                overlays = self.driver.find_elements(By.XPATH, selector)
                visible_overlays = [
                    o for o in overlays if o.is_displayed() and o.size["height"] > 50
                ]

                if visible_overlays:
                    overlay = visible_overlays[0]
                    logging.info(f"Custom overlay detected using selector: {selector}")

                    popup_data = self.extract_popup_details(overlay, "overlay")
                    self.close_popup(overlay, "overlay")

                    return popup_data
        except Exception as e:
            logging.error(f"Error while detecting popups: {e}")

        return popup_data

    def extract_popup_details(self, popup_element, popup_type):
        """
        Extract detailed information from a popup element.
        """
        try:
            popup_data = {
                "type": popup_type,
                "text": popup_element.text.strip(),
                "classes": popup_element.get_attribute("class"),
                "id": popup_element.get_attribute("id"),
                "elements": [],
            }

            # Get interactive elements within the popup
            interactive_elements = popup_element.find_elements(
                By.XPATH,
                ".//button | .//a | .//input | .//select | .//textarea | .//*[@onclick] | .//*[@role='button']",
            )

            for elem in interactive_elements:
                if self.is_element_visible(elem):
                    elem_info = {
                        "tag": elem.tag_name,
                        "text": elem.text.strip(),
                        "type": elem.get_attribute("type"),
                        "href": elem.get_attribute("href"),
                        "onclick": elem.get_attribute("onclick"),
                        "class": elem.get_attribute("class"),
                        "id": elem.get_attribute("id"),
                        "xpath": self.get_element_xpath(elem),
                    }
                    if elem_info["tag"] in ("button", "a") and popup_type == "dropdown":
                        elem_info, _ = self.click_element_and_scrape_child(
                            elem, elem_info, self.driver.current_url, True
                        )

                    popup_data["elements"].append(elem_info)

            # Get form elements if any
            forms = popup_element.find_elements(By.TAG_NAME, "form")
            if forms:
                popup_data["forms"] = []
                for form in forms:
                    form_data = {
                        "action": form.get_attribute("action"),
                        "method": form.get_attribute("method"),
                        "fields": [],
                    }

                    fields = form.find_elements(
                        By.XPATH, ".//input | .//select | .//textarea"
                    )
                    for field in fields:
                        field_info = {
                            "name": field.get_attribute("name"),
                            "type": field.get_attribute("type"),
                            "placeholder": field.get_attribute("placeholder"),
                            "required": field.get_attribute("required") is not None,
                        }
                        form_data["fields"].append(field_info)

                    popup_data["forms"].append(form_data)

            return popup_data

        except Exception as e:
            logging.error(f"Error extracting popup details: {e}")
            return {
                "type": popup_type,
                "text": "Error extracting details",
                "error": str(e),
            }

    def close_popup(self, popup_element, popup_type):
        """
        Attempt to close the popup using various methods.
        """
        try:

            # Method 1: Press Escape key
            if popup_type in ["modal", "dropdown"]:
                try:
                    popup_element.send_keys(Keys.ESCAPE)
                    logging.info(f"Closed {popup_type} using Escape key")
                    time.sleep(0.5)
                    return True
                except Exception:
                    pass

            # Method 2: Look for close button
            close_selectors = [
                ".//button[contains(text(),'×')]",
                ".//button[contains(text(),'Close')]",
                ".//button[contains(text(),'close')]",
                ".//span[contains(@class,'close')]",
                ".//div[contains(@class,'close')]",
                ".//*[@aria-label='Close']",
                ".//*[contains(@class,'btn-close')]",
                ".//*[contains(@data-dismiss,'modal')]",
                ".//button[.//svg]",
            ]

            for selector in close_selectors:
                try:
                    close_btn = popup_element.find_element(By.XPATH, selector)
                    if close_btn.is_displayed() and close_btn.is_enabled():
                        self.safe_click(close_btn)
                        logging.info(f"Closed {popup_type} using close button")
                        time.sleep(0.5)
                        return True
                except Exception:
                    continue

            # Method 3: Click outside for dropdowns/tooltips
            if popup_type in ["dropdown", "tooltip"]:
                try:
                    body = self.driver.find_element(By.TAG_NAME, "body")
                    self.driver.execute_script("arguments[0].click();", body)
                    logging.info(f"Closed {popup_type} by clicking outside")
                    time.sleep(0.5)
                    return True
                except Exception:
                    pass

            # Method 4: Click overlay background
            try:
                overlay = self.driver.find_element(
                    By.XPATH,
                    "//div[contains(@class,'modal-backdrop') or contains(@class,'overlay')]",
                )
                if overlay.is_displayed():
                    self.safe_click(overlay)
                    logging.info(f"Closed {popup_type} by clicking overlay")
                    time.sleep(0.5)
                    return True
            except Exception:
                pass

            logging.warning(f"Could not close {popup_type}")
            return False

        except Exception as e:
            logging.error(f"Error closing {popup_type}: {e}")
            return False


# --- run ---
if __name__ == "__main__":
    finder = WebElementFinder(
        "http://localhost:3000/",
    )
    try:
        finder.find_all_elements_dynamic()
        result = finder.whole_website
        with open("output.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logging.info("output saved")
        print("✅ Output saved to output.json")
        print(finder.scraped_urls)
    finally:
        finder.driver.quit()
