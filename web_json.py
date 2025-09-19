import os
import time
import hashlib
import logging
import traceback
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

# --- logging setup (ensure directory exists) ---
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/testing.log",
    encoding="utf-8",
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s",
)


class WebElementFinder:
    """Web element finder with multiple strategies"""

    def __init__(self, url: str, driver=None, aviod_page: List[str] = []):

        if driver:
            self.driver = driver
        else:
            self.session = requests.Session()
            chrome_options = Options()
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--disable-background-timer-throttling")

            # Block images (this replaces --disable-images)
            prefs = {"profile.managed_default_content_settings.images": 2}
            chrome_options.add_experimental_option("prefs", prefs)
            self.driver = webdriver.Chrome(options=chrome_options)

        self.driver.set_page_load_timeout(8)

        self.scraped_urls: List[str] = []
        self._seen_hashes: List[str] = []
        self.avoided_links = aviod_page
        self.base_url = url
        parsed = urlparse(url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

        logging.info(f"step 1 - base url is set {url}")

    # --- small helpers ---
    def wait_for_page_load(self, timeout: int = 8):
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

    def remove_duplicate_elements(self, elements):
        """Remove visually duplicate elements by hashing key attributes."""
        unique_elements = []
        for el in elements:
            try:
                tag_name = el.tag_name
                text = (el.text or "").strip()[:50]
                classes = el.get_attribute("class") or ""
                element_id = el.get_attribute("id") or ""
                href = el.get_attribute("href") or ""
                src = el.get_attribute("src") or ""

                element_string = "|".join(
                    [
                        tag_name,
                        text,
                        classes,
                        element_id,
                        href,
                        src,
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

        href = href.strip()
        # Non-navigational schemes or anchors
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return True

        # avoid blog to run fastly
        if any(keyword in href for keyword in self.avoided_links):
            return True

        # Make absolute
        abs_url = urljoin(current_url, href)

        # removeing query params
        abs_url = abs_url.split("?")[0]

        abs_url = abs_url.split("#")[0]

        parsed_abs = urlparse(abs_url)

        # External origin? skip  hardcoded for this website
        if (
            f"{parsed_abs.scheme}://{parsed_abs.netloc}" != self.origin
            and parsed_abs.netloc not in ["v2.dende.ai", "app.dende.ai"]
        ):

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
                    0.05
                ).click().perform()
                return
            except Exception:
                self.driver.execute_script("arguments[0].click();", element)

    def click_element_and_scrape_child(
        self,
        element,
        url_before_click,
    ):
        """
        Click the element, handle same-tab navigation or new-tab,
        scrape the child page, then return to the original page.
        """
        wait = WebDriverWait(self.driver, 8)

        handles_before = self.driver.window_handles[:]
        current_url = self.driver.current_url
        self.safe_click(element)
        time.sleep(0.2)  # brief settle

        # --- Check if alert popped up immediately ---
        try:
            alert = self.driver.switch_to.alert
            text = alert.text
            logging.info(f"Alert detected after click: {text}")

            # Default: dismiss (use accept() if required)
            alert.dismiss()
            return
        except Exception:
            # No alert → continue with navigation logic
            pass
        if url_before_click == current_url:
            try:
                self.detect_and_handle_popups()
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
            logging.info("No navigation or new tab detected after click")
            return  # nothing to do

        # New tab?
        handles_after = self.driver.window_handles[:]
        if len(handles_after) > len(handles_before):
            new_handle = [h for h in handles_after if h not in handles_before][0]
            self.driver.switch_to.window(new_handle)
            self.wait_for_page_load()
            new_url = self.driver.current_url
            logging.info(f"New tab opened: {new_url}")

            if not self.should_skip_link(current_url, new_url):
                self.scraped_urls.append(new_url.split("?", 1)[0].split("#", 1)[0])
                self.get_element_detail(wait_time=5)
            # Close child tab and switch back
            self.driver.close()
            self.driver.switch_to.window(handles_before[0])
            wait.until(EC.url_to_be(url_before_click))
            return

        # Same tab navigation
        if self.driver.current_url != url_before_click:
            self.wait_for_page_load()
            new_url = self.driver.current_url
            logging.info(f"Navigated to new page: {new_url}")

            if not self.should_skip_link(current_url, new_url):
                self.scraped_urls.append(new_url.split("?", 1)[0].split("#", 1)[0])
                self.get_element_detail(wait_time=5)

            # Go back to original page
            self.driver.back()
            logging.info(f"navigating back to {url_before_click}")
            try:
                wait.until(EC.url_to_be(url_before_click))
            except TimeoutException:
                # Last resort: refresh to recover
                logging.warning("Back navigation timeout; refreshing original page")
                self.driver.get(url_before_click)
                self.wait_for_page_load()

    # --- main routines ---

    def find_all_Urls_dynamic(self, url=None, wait_time: int = 5) -> Dict[str, Any]:
        """Find all elements including dynamically loaded content (Selenium)"""

        if url is None:
            url = self.base_url
        if url not in self.scraped_urls:
            self.scraped_urls.append(url)
            logging.info(f"step 2 - marking {url} as not yet scraped")

        try:
            self.driver.get(url)
            self.wait_for_page_load()
            self.get_element_detail(5)
            return self.scraped_urls
        except Exception as e:
            logging.error({"error": f"Failed to process page with Selenium: {str(e)}"})
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return {"error": f"Failed to process page with Selenium: {str(e)}"}

    def get_element_detail(self, wait_time: int) -> Dict[str, Any]:
        WebDriverWait(self.driver, wait_time).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(1)  # allow dynamic content to settle a bit

        body = self.driver.find_element(By.TAG_NAME, "body")
        all_elements = body.find_elements(By.XPATH, ".//*")

        logging.info("step 3 - filtering unique elements")
        unique_elements = self.remove_duplicate_elements(all_elements)

        # handles popups on loading pages
        try:
            self.detect_and_handle_popups()
        except Exception:
            # No alert → continue with poplogic logic
            pass

        # IMPORTANT: work with XPaths to avoid stale references after navigation
        xpaths = []
        for el in unique_elements:
            try:
                if el.tag_name in ("style", "noscript", "script"):
                    continue
                    
                # Quick clickability check
                if el.tag_name in ("a", "button") and el.is_enabled():
                    xpaths.append(self.get_element_xpath(el))
                elif el.tag_name in ("div", "li") and self.is_element_clickable(el):
                    xpaths.append(self.get_element_xpath(el))
                    
            except StaleElementReferenceException:
                continue
        logging.info("step 5 - iterating elements safely by XPath")
        current_url = self.driver.current_url
        for xp in xpaths:
            try:
                element = self.driver.find_element(By.XPATH, xp)

                # Handle hover interactions for specific elements
                if element.tag_name in (
                    "a",
                    "button",
                    "div",
                    "li",
                ):
                    hover_success = self.safe_hover(element)
                    if hover_success:
                        time.sleep(0.5)  # Wait for any dropdown/popup to appear

                        # Check for dropdowns after hover
                        self.detect_dropdown_after_hover()

                
                

                if element.tag_name == "a":
                    href = element.get_attribute("href")
                    if not self.should_skip_link(current_url, href):
                        self.click_element_and_scrape_child(
                            element, url_before_click=current_url
                        )

                elif element.tag_name == "button":
                    self.click_element_and_scrape_child(
                        element, url_before_click=current_url
                    )

            except (NoSuchElementException, StaleElementReferenceException) as e:
                logging.error(f"Element vanished or stale for xpath={xp}: {e}")
                continue
            except Exception as e:
                logging.error(f"Unexpected error for xpath={xp}: {e}")
                logging.error(f"Traceback:\n{traceback.format_exc()}")
                continue

        return

    def is_element_clickable(self, element) -> bool:
        """Check if element is truly clickable"""
        try:
            if not element.is_displayed() or not element.is_enabled():
                return False

            # Quick size and location check
            size = element.size
            location = element.location
            
            return (size["height"] > 0 and size["width"] > 0 and 
                   location["x"] >= 0 and location["y"] >= 0)

        except (StaleElementReferenceException, WebDriverException):
            return False

    def safe_hover(self, element):
        """Hover over an element safely with multiple fallback strategies."""
        if not self.is_element_clickable(element):
            return False
        try:
            # Strategy 1: Scroll element into view and hover
            self.driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});",
                element,
            )
            time.sleep(0.2)  # Wait for scroll to complete

            actions = ActionChains(self.driver)
            actions.move_to_element(element).pause(0.1).perform()
            logging.info(
                f"Hovered over element: {element.tag_name} {element.text[:30]}"
            )
            return True

        except (MoveTargetOutOfBoundsException, ElementNotInteractableException) as e:
            logging.warning(f"Hover failed with move error: {e}")
            try:
                # Strategy 2: Use JavaScript to trigger mouse events
                self.driver.execute_script(
                    """
                    var element = arguments[0];
                    var event = new MouseEvent('mouseover', {
                        view: window,
                        bubbles: true,
                        cancelable: true
                    });
                    element.dispatchEvent(event);
                """,
                    element,
                )
                time.sleep(0.1)
                logging.info(f"Hovered using JavaScript: {element.tag_name}")
                return True
            except Exception as js_e:
                logging.error(f"JavaScript hover also failed: {js_e}")
                return False

        except (StaleElementReferenceException, WebDriverException) as e:
            logging.error(f"Hover failed with stale/webdriver error: {e}")
            return False
        except Exception as e:
            logging.error(f"Unexpected hover error: {e}")
            return False

    def detect_dropdown_after_hover(self):
        """Quick dropdown detection"""
        try:
            # Simplified selectors for common dropdowns
            dropdowns = self.driver.find_elements(
                By.XPATH, 
                "//div[contains(@class,'dropdown-menu') and contains(@style,'display: block')] | "
                "//ul[contains(@class,'dropdown-menu')] | "
                "//div[@aria-expanded='true']"
            )
            
            visible_dropdowns = [d for d in dropdowns if d.is_displayed()]
            if visible_dropdowns:
                self.close_popup(visible_dropdowns[0], "dropdown")
                
        except Exception as e:
            logging.error(f"Error detecting dropdown: {e}")
    def get_element_xpath(self, element) -> str:
        """Generate an absolute XPath for a Selenium WebElement"""
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
                        return getXPath(el.parentNode) + '/' + el.tagName.toLowerCase() + '[' + ix + ']';
                    }
                }
            }
            return getXPath(arguments[0]);
            """,
            element,
        )

    def detect_and_handle_popups(self):
        """Faster popup detection with combined selectors"""
        try:
            # Combined selector for all popup types
            all_popup_selector = (
                "//div[contains(@class,'modal') and contains(@style,'display: block')] | "
                "//div[@role='dialog'] | "
                "//div[contains(@class,'popup')] | "
                "//div[contains(@class,'dropdown-menu') and contains(@style,'display: block')] | "
                "//div[contains(@class,'tooltip') and contains(@style,'display: block')] | "
                "//div[@role='tooltip']"
            )
            
            popups = self.driver.find_elements(By.XPATH, all_popup_selector)
            visible_popups = [p for p in popups if p.is_displayed()]

            if visible_popups:
                popup = visible_popups[0]
                popup_class = popup.get_attribute("class") or ""
                
                # Determine popup type from class
                if "modal" in popup_class or popup.get_attribute("role") == "dialog":
                    popup_type = "modal"
                elif "dropdown" in popup_class:
                    popup_type = "dropdown"
                elif "tooltip" in popup_class or popup.get_attribute("role") == "tooltip":
                    popup_type = "tooltip"
                else:
                    popup_type = "popup"
                
                self.close_popup(popup, popup_type)
                return True
                
        except Exception as e:
            logging.error(f"Error detecting popups: {e}")
        
        return False

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
                    time.sleep(0.1)
                    return True
                except Exception:
                    pass

            # Quick close button search with single combined selector
            close_btn = popup_element.find_elements(
                By.XPATH,
                ".//button[contains(text(),'×') or contains(text(),'Close') or contains(@class,'close') or contains(@class,'btn-close')] | "
                ".//span[contains(@class,'close')] | "
                ".//*[@aria-label='Close']"
            )
            
            if close_btn:
                for btn in close_btn:
                    if btn.is_displayed() and btn.is_enabled():
                        self.safe_click(btn)
                        time.sleep(0.1)
                        return True

            # Method 3: Click outside for dropdowns/tooltips
            if popup_type in ["dropdown", "tooltip"]:
                try:
                    body = self.driver.find_element(By.TAG_NAME, "body")
                    self.driver.execute_script("arguments[0].click();", body)
                    logging.info(f"Closed {popup_type} by clicking outside")
                    time.sleep(0.1)
                    return True
                except Exception:
                    pass

            logging.warning(f"Could not close {popup_type}")
            return False

        except Exception as e:
            logging.error(f"Error closing {popup_type}: {e}")
        return False

    def close(self):
        """Clean up and close the WebDriver session."""
        if self.driver:
            self.driver.quit()
            logging.info("WebDriver session closed.")


finder=WebElementFinder("https://dende.ai/",aviod_page=["blog"])
urls=finder.find_all_Urls_dynamic()
print(urls)
print(len(urls))