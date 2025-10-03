import os
import requests
import time
from datetime import datetime
from zoneinfo import ZoneInfo 
import base64
from typing import Dict, Any
from selenium.webdriver.chrome.options import Options
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver import ActionChains
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    WebDriverException,
)


class Browser:
    def __init__(self):
        """Initialize the Chrome WebDriver."""
        chrome_options = Options()
        chrome_options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"  # Update path if different
        chrome_options.add_argument("--start-maximized")
        self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        self.driver.set_page_load_timeout(60)

    def wait_for_page_load(self, timeout: int = 20):
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

    def ensure_driver(self):
        """Ensure WebDriver is initialized."""
        if self.driver is None:
            raise RuntimeError("WebDriver not initialized. Please try again.")

    def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate to a URL."""
        self.ensure_driver()
        try:
            # Ensure valid URL
            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            self.driver.get(url)
            self.wait_for_page_load()
            return {
                "success": True,
                "url": self.driver.current_url,
                "title": self.driver.title,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def find_element(self, xpath: str) -> Dict[str, Any]:
        """Find an element on the page."""
        self.ensure_driver()
        try:
            element = self.driver.find_element(By.XPATH, xpath)
            return {
                "success": True,
                "found": True,
                "text": element.text,
                "tag_name": element.tag_name,
                "is_displayed": element.is_displayed(),
                "is_enabled": element.is_enabled(),
                "location": element.location,
                "size": element.size,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def click_element(self, xpath: str) -> Dict[str, Any]:
        """Click an element."""
        self.ensure_driver()
        """Try a normal click, then move+click, then JS click."""
        try:
            element = self.driver.find_element(By.XPATH, xpath)
            element.click()
            return {"success": True, "message": "Element clicked successfully"}
        except (ElementClickInterceptedException, WebDriverException):
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", element
                )
                ActionChains(self.driver).move_to_element(element).pause(
                    0.1
                ).click().perform()
                return {"success": True, "message": "Element clicked successfully"}
            except Exception as e:
                return {"success": False, "error": str(e)}

    def take_screenshot(self, filename: str = None) -> Dict[str, Any]:
        """Take a screenshot of the current page."""
        self.ensure_driver()
        try:
            ts = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d_%H%M%S")
            if filename is None:
                filename = f"screenshot_{ts}.png"
            self.driver.save_screenshot(filename)
            return {"success": True, "filename": filename}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_text_of_element(self, xpath: str) -> Dict[str, Any]:
        """get the text present in the element"""
        self.ensure_driver()
        try:
            element = self.driver.find_element(By.XPATH, xpath)
            text = self.driver.execute_script(
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
            )
            return {"success": True, "text": text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def send_keys(
        self, xpath: str, text: str, clear_first: bool = True
    ) -> Dict[str, Any]:
        """Send keys to an input element."""
        self.ensure_driver()
        time.sleep(2)
        try:
            element = self.driver.find_element(By.XPATH, xpath)
            if clear_first:
                element.clear()
            element.send_keys(text)
            return {"success": True, "action": "keys_sent", "text": text}
        except Exception as e:
            return {"success": False, "error": str(e)}
        
    def press_enter(self, xpath: str) -> Dict[str, Any]:
        """Press the Enter key on an element."""
        self.ensure_driver()
        try:
            element = self.driver.find_element(By.XPATH, xpath)
            element.send_keys(Keys.ENTER) 
            return {"success": True, "action": "enter_pressed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_attribute(self, xpath: str, attribute: str) -> Dict[str, Any]:
        """Get an attribute value from an element."""
        self.ensure_driver()
        try:
            element = self.driver.find_element(By.XPATH, xpath)
            value = element.get_attribute(attribute)
            return {"success": True, "attribute": attribute, "value": value}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def hover_element(self, xpath: str) -> Dict[str, Any]:
        """Hover over an element."""
        self.ensure_driver()
        try:
            element = self.driver.find_element(By.XPATH, xpath)
            actions = ActionChains(self.driver)
            actions.move_to_element(element).perform()
            return {"success": True, "action": "hovered"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def refresh_page(self) -> Dict[str, Any]:
        """Refresh the current page."""
        self.ensure_driver()
        try:
            self.driver.refresh()
            self.wait_for_page_load()
            return {"success": True, "action": "refreshed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def go_back(self) -> Dict[str, Any]:
        """Navigate back in browser history."""
        self.ensure_driver()
        try:
            self.driver.back()
            self.wait_for_page_load()
            return {"success": True, "action": "back"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def go_forward(self) -> Dict[str, Any]:
        """Navigate forward in browser history."""
        self.ensure_driver()
        try:
            self.driver.forward()
            self.wait_for_page_load()
            return {"success": True, "action": "forward"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_current_url(self) -> Dict[str, Any]:
        """get the current url of the page"""
        self.ensure_driver()
        try:
            currnet_url = self.driver.current_url
            return {"success": True, "current_url": currnet_url}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # def get_window_handles(self) -> Dict[str, Any]:
    #     """Get all window handles."""
    #     self.ensure_driver()
    #     try:
    #         handles = self.driver.window_handles
    #         return {"success": True, "handles": handles, "current": self.driver.current_window_handle}
    #     except Exception as e:
    #         return {"success": False, "error": str(e)}

    # def switch_to_next_handle(self) -> Dict[str, Any]:
    #     """Switch to the next window handle in the list."""
    #     self.ensure_driver()
    #     try:
    #         current_handle = self.driver.current_window_handle
    #         all_handles = self.driver.window_handles

    #         if len(all_handles) <= 1:
    #             return {"success": False, "error": "Only one window available"}

    #         current_index = all_handles.index(current_handle)
    #         next_index = (current_index + 1) % len(all_handles)  # Wrap around to first if at end
    #         next_handle = all_handles[next_index]

    #         self.driver.switch_to.window(+1)
    #         return {
    #             "success": True,
    #             "previous_handle": current_handle,
    #             "current_handle": next_handle,
    #             "window_index": next_index + 1,
    #             "total_windows": len(all_handles)
    #         }
    #     except Exception as e:
    #         return {"success": False, "error": str(e)}

    # def switch_to_previous_handle(self) -> Dict[str, Any]:
    #     """Switch to the previous window handle in the list."""
    #     self.ensure_driver()
    #     try:
    #         current_handle = self.driver.current_window_handle
    #         all_handles = self.driver.window_handles

    #         if len(all_handles) <= 1:
    #             return {"success": False, "error": "Only one window available"}

    #         current_index = all_handles.index(current_handle)
    #         previous_index = (current_index - 1) % len(all_handles)  # Wrap around to last if at beginning
    #         previous_handle = all_handles[previous_index]

    #         self.driver.switch_to.window(-1)
    #         return {
    #             "success": True,
    #             "previous_handle": current_handle,
    #             "current_handle": previous_handle,
    #             "window_index": previous_index + 1,
    #             "total_windows": len(all_handles)
    #         }
    #     except Exception as e:
    #         return {"success": False, "error": str(e)}

    # def switch_to_window(self, handle: str) -> Dict[str, Any]:
    #     """Switch to a specific window."""
    #     self.ensure_driver()
    #     try:
    #         self.driver.switch_to.window(handle)
    #         return {"success": True, "current_handle": handle}
    #     except Exception as e:
    #         return {"success": False, "error": str(e)}

    def close_current_window(self) -> Dict[str, Any]:
        """Close the current window."""
        self.ensure_driver()
        try:
            self.driver.close()
            return {"success": True, "action": "window_closed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def quit_browser(self) -> Dict[str, Any]:
        """Quit the entire browser (close all windows and terminate browser process)."""
        self.ensure_driver()
        try:
            self.driver.quit()
            return {"success": True, "action": "browser_quit"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def upload_file_direct(self, xpath: str, file_path: str) -> Dict[str, Any]:
        """
        Upload file by directly sending file path to input element.
        This is the most reliable method for standard file inputs.
        """
        self.ensure_driver()
        try:
            # Verify file exists
            if not os.path.exists(file_path):
                return {"success": False, "error": f"File not found: {file_path}"}

            # Get absolute path
            absolute_path = os.path.abspath(file_path)

            # Find file input element
            element = self.driver.find_element(By.XPATH, xpath)
            if not element:
                return {"success": False, "error": "File input element not found"}

            # Verify it's a file input
            if element.get_attribute("type") != "file":
                return {"success": False, "error": "Element is not a file input"}

            # Send file path directly to input
            element.send_keys(absolute_path)

            return {
                "success": True,
                "action": "file_uploaded_direct",
                "file_path": absolute_path,
                "file_name": os.path.basename(absolute_path),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
        
    def press_escape(self) -> Dict[str, Any]:
        """Press ESC key on the page (useful for closing modals/popups)."""
        self.ensure_driver()
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.ESCAPE)
            return {"success": True, "action": "escape_pressed_on_page"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def upload_file_drag_drop(
        self, xpath: str, file_path: str
    ) -> Dict[str, Any]:
        """
        Upload file using drag and drop with actual file content.
        This creates a proper File object with real content.
        """
        self.ensure_driver()
        try:
            if not os.path.exists(file_path):
                return {"success": False, "error": f"File not found: {file_path}"}

            # Read file content and encode as base64
            with open(file_path, "rb") as f:
                file_content = f.read()

            file_b64 = base64.b64encode(file_content).decode("utf-8")
            file_name = os.path.basename(file_path)
            file_type = self._get_mime_type(file_path)

            # Find drop zone element
            drop_zone = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )

            # JavaScript to create file with actual content and simulate drop
            js_drop_files = """
            var target = arguments[0];
            var fileName = arguments[1];
            var fileType = arguments[2];
            var fileContent = arguments[3];
            
            // Convert base64 to binary
            var binaryString = atob(fileContent);
            var bytes = new Uint8Array(binaryString.length);
            for (var i = 0; i < binaryString.length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }
            
            // Create file with actual content
            var file = new File([bytes], fileName, { type: fileType });
            var dt = new DataTransfer();
            dt.items.add(file);
            
            // Create and dispatch drop event
            var dropEvent = new DragEvent('drop', {
                bubbles: true,
                cancelable: true,
                dataTransfer: dt
            });
            
            target.dispatchEvent(dropEvent);
            """

            # Execute the drop with actual file content
            self.driver.execute_script(
                js_drop_files, drop_zone, file_name, file_type, file_b64
            )

            return {
                "success": True,
                "action": "file_uploaded_drag_drop_with_content",
                "file_name": file_name,
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    def _get_mime_type(self, file_path: str) -> str:
        """Get MIME type based on file extension."""
        extension = os.path.splitext(file_path)[1].lower()
        mime_types = {
            ".txt": "text/plain",
            ".pdf": "application/pdf",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xls": "application/vnd.ms-excel",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".zip": "application/zip",
            ".csv": "text/csv",
        }
        return mime_types.get(extension, "application/octet-stream")


# if __name__ == "__main__":
#     Browser.run()

# # Example usage
if __name__ == "__main__":
    browser = Browser()
    print(browser.navigate("https://www.cricbuzz.com/"))
    time.sleep(1)
    print(browser.find_element("/html/body/header/div/nav/a[2]"))
    print(browser.hover_element("/html/body/header/div/nav/a[2]"))
    time.sleep(10)
    print(browser.click_element("/html/body/header/div/nav/a[2]"))
#     # print(browser.send_keys("/html/body/div[2]/div/div/div/div[2]/div/div/div/form/div[1]/div/input","lionel messi"))
#     # print(
#     #     browser.upload_file_direct(
#     #         "/html/body/div[1]/div[1]/div[1]/div[4]/div[3]/input",
#     #         r"C:\Users\sabar\Downloads\wallpaper\1081458.jpg",
#     #     )
#     # )
#     # print(browser.press_escape())
#     time.sleep(2)
#     # print(browser.press_enter("/html/body/div[2]/div[4]/form/div[1]/div[1]/div[1]/div[1]/div[2]/textarea"))

#     # print(browser.find_element('/html/body/div[1]/div[1]/div[1]/div[1]/div[2]/input[1]'))
#     # time.sleep(1)
#     # print(browser.click_element('/html/body/div[1]/div[1]/div[1]/div[1]/div[2]/input[1]'))
#     # time.sleep(1)
#     # print(browser.find_element('/html/body/div[1]/div[1]/div[1]/div[1]/div[3]/form[1]/button[1]'))
#     # time.sleep(1)
#     # print(browser.click_element('/html/body/div[1]/div[1]/div[1]/div[1]/div[3]/form[1]/button[1]'))
#     # time.sleep(1)
#     # print(browser.get_current_url())
#     # print(browser.take_screenshot("login-page.png"))

#     # time.sleep(20)
