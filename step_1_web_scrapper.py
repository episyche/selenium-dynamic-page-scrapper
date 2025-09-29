import os
import json
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
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s",
)


class WebElementFinder:
    """Web element finder with multiple strategies"""

    def __init__(self, url: str,driver:Any=None,avoid_pages:List[str]=[]):
        if driver is None:
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
            # prefs = {"profile.managed_default_content_settings.images": 2}
            # chrome_options.add_experimental_option("prefs", prefs)
            self.driver = webdriver.Chrome(options=chrome_options)
        else:
            self.driver=driver
        self.driver.set_page_load_timeout(10)
        self.avoid_page=avoid_pages

        self.scraped_urls: List[str] = []
        self._seen_hashes: List[str] = []
        self.whole_website=[]
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
                
                if el.tag_name in ["style", "noscript", "script"]:
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
        abs_url=abs_url.split('?')[0]

        abs_url=abs_url.split('#')[0]

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
        self.safe_click(element)
        time.sleep(0.5)  # brief settle

        # --- Check if alert popped up immediately ---
        try:
            alert = self.driver.switch_to.alert
            text = alert.text
            logging.info(f"Alert detected after click: {text}")

            element_data["popup"] = {"type": "alert", "text": text}

            # Default: dismiss (use accept() if required)
            alert.dismiss()
            return element_data
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
            logging.info("No navigation or new tab detected after click")
            return element_data  # nothing to do

        # New tab?
        handles_after = self.driver.window_handles[:]
        if len(handles_after) > len(handles_before):
            new_handle = [h for h in handles_after if h not in handles_before][0]
            self.driver.switch_to.window(new_handle)
            self.wait_for_page_load()
            new_url = self.driver.current_url.split("?", 1)[0].split("#", 1)[0]
            element_data["navigate_to"]=new_url
            logging.info(f"New tab opened: {new_url}")

            if not self.should_skip_link(current_url, new_url):
                self.scraped_urls.append(new_url)
                child_data = self.get_element_detail(wait_time=5)

            # Close child tab and switch back
            self.driver.close()
            self.driver.switch_to.window(handles_before[0])
            wait.until(EC.url_to_be(url_before_click))
            return element_data

        # Same tab navigation
        if self.driver.current_url != url_before_click:
            self.wait_for_page_load()
            new_url = self.driver.current_url.split("?", 1)[0].split("#", 1)[0]
            element_data["navigate_to"]=new_url
            logging.info(f"Navigated to new page: {new_url}")

            if not self.should_skip_link(current_url, new_url):
                self.scraped_urls.append(new_url)
                logging.info(new_url)
                child_data = self.get_element_detail(wait_time=5)
                

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

        # here to write the logic for drop down,popup

        return element_data

    # --- main routines ---

    def find_all_elements_dynamic(
        self, url=None, wait_time: int = 8
    ) -> Dict[str, Any]:
        """Find all elements including dynamically loaded content (Selenium)"""

        if url is None:
            url=self.base_url
        if url not in self.scraped_urls:
            self.scraped_urls.append(url)
            logging.info(f"step 2 - marking {url} as not yet scraped")

        try:
            self.driver.get(url)
            self.wait_for_page_load()
            return self.get_element_detail(wait_time)
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

        #     all_dropdowns = []

        #     dropdown_selectors = [
        #     "//div[contains(@class,'dropdown')]",
        #     "//ul[contains(@class,'dropdown-menu') and contains(@style,'display: block')]",
        #     "//div[@aria-expanded='true']",
        #     "//div[@role='menu' and @aria-hidden='false']",
        # ]
        #     for selector in dropdown_selectors:
        #         all_dropdowns.extend(self.driver.find_elements(By.XPATH, selector))

        #     dropdown_xpaths = [self.get_element_xpath(d) for d in all_dropdowns]

        logging.info("step 5 - iterating elements safely by XPath")
        for xp in xpaths:
            try:
                logging.info(xp)
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
                    "is_displayed":"true" if element.is_displayed() else "false",
                    "is_enabled": "true" if element.is_displayed() else "false",
                    "xpath": xp,
                }

                if element_data["tag"] in ("div") and element_data["text"]=="":
                    continue
                # Handle hover interactions for specific elements
                if element.tag_name in (
                    "a",
                    "button",
                    "div",
                    "li",
                ) and self.is_element_clickable(element):
                    hover_success = self.safe_hover(element)
                    if hover_success:
                        time.sleep(0.5)  # Wait for any dropdown/popup to appear

                        # Check for dropdowns after hover
                        dropdown_data = self.detect_dropdown_after_hover()
                        if dropdown_data:
                            element_data["dropdown"] = dropdown_data

                # dropdown_selectors = [
                #     "//div[contains(@class,'dropdown')]",
                #     "//ul[contains(@class,'dropdown-menu') and contains(@style,'display: block')]",
                #     "//div[@aria-expanded='true']",
                #     "//div[@role='menu' and @aria-hidden='false']",
                # ]

                # all_dropdowns = []
                # for selector in dropdown_selectors:
                #     all_dropdowns.extend(self.driver.find_elements(By.XPATH, selector))

                # visible_dropdowns = [d for d in all_dropdowns if d.is_displayed()]

                # if element in visible_dropdowns:
                #     popup_data = self.extract_popup_details(element, "dropdown")
                #     self.close_popup(element, "dropdown")
                #     element_data["dropdown"] = popup_data

                clickable = element.is_enabled() and self.is_probably_clickable(element)

                if clickable:
                    current_url = self.driver.current_url

                    if element.tag_name == "a":
                        href = element.get_attribute("href")
                        if self.should_skip_link(current_url, href):
                            result["web_elements"].append(element_data)
                            continue
                    if element.text.replace(" ", "").lower() == "logout":
                        result["web_elements"].append(element_data)
                        continue
                    element_data = self.click_element_and_scrape_child(
                    element, element_data, url_before_click=current_url
                    )                        
                    result["web_elements"].append(element_data)
                else:
                    result["web_elements"].append(element_data)

            except (NoSuchElementException, StaleElementReferenceException) as e:
                logging.error(f"Element vanished or stale for xpath={xp}: {e}")
                continue
            except Exception as e:
                logging.error(f"Unexpected error for xpath={xp}: {e}")
                logging.error(f"Traceback:\n{traceback.format_exc()}")
                continue
        self.whole_website.append(result)
        return result
    
    def is_probably_clickable(self,element):
        if element.get_attribute("onclick"):
            return True
        if element.tag_name in ("a", "button"):
            return True
        if element.get_attribute("role") == "button":
            return True
        if element.value_of_css_property("cursor") == "pointer":
            return True
        return False


    def is_element_clickable(self, element) -> bool:
        """Check if element is truly clickable"""
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
        """Hover over an element safely with multiple fallback strategies."""
        if not self.is_element_clickable(element):
            return False

        try:
            # Strategy 1: Scroll element into view and hover
            self.driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center', inline: 'center'});",
                element,
            )
            time.sleep(0.5)  # Wait for scroll to complete

            actions = ActionChains(self.driver)
            actions.move_to_element(element).pause(0.5).perform()
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
                time.sleep(0.5)
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
        """Detect dropdowns that appear after hovering"""
        try:
            dropdown_selectors = [
                "//div[contains(@class,'dropdown-menu') and contains(@style,'display: block')]",
                "//ul[contains(@class,'dropdown-menu') and @style and contains(@style,'display') and not(contains(@style,'none'))]",
                "//div[@aria-expanded='true']",
                "//div[@role='menu' and @aria-hidden='false']",
            ]

            for selector in dropdown_selectors:
                dropdowns = self.driver.find_elements(By.XPATH, selector)
                visible_dropdowns = [d for d in dropdowns if d.is_displayed()]

                if visible_dropdowns:
                    dropdown = visible_dropdowns[0]
                    dropdown_data = self.extract_popup_details(dropdown, "dropdown")
                    self.close_popup(dropdown, "dropdown")
                    return dropdown_data
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

            # 2. Dropdown menus
            dropdown_selectors = [
                "//div[contains(@class,'dropdown')]",
                "//ul[contains(@class,'dropdown-menu') and contains(@style,'display: block')]",
                "//ul[contains(@class,'sub-menu') and contains(@style,'display: block')]",
                "//div[@aria-expanded='true']",
                "//div[@role='menu' and @aria-hidden='false']",
            ]

            for selector in dropdown_selectors:
                dropdowns = self.driver.find_elements(By.XPATH, selector)
                visible_dropdowns = [d for d in dropdowns if d.is_displayed()]

                if visible_dropdowns:
                    dropdown = visible_dropdowns[0]
                    logging.info(f"Dropdown detected using selector: {selector}")

                    popup_data = self.extract_popup_details(dropdown, "dropdown")
                    self.close_popup(dropdown, "dropdown")
                    return popup_data

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
                if elem.is_displayed():
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
                        elem_info = self.click_element_and_scrape_child(
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
                ".//button[.//svg]"
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
    finder = WebElementFinder("http://localhost:3000/",)
    try:
        finder.find_all_elements_dynamic()
        result=finder.whole_website
        # https://www.mockaroo.com/
        # http://localhost:3000/
        # https://www.behold.cam/
        # https://unlovedai.com/
        # https://bandbooker.com/
        with open("output.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logging.info("output saved")
        print("✅ Output saved to output.json")
        print(finder.scraped_urls)
    finally:
        finder.driver.quit()
