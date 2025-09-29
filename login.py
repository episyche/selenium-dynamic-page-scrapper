import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By



def login_to_website(url,username, password, username_selector, password_selector, login_button_selector):
        """
        Generic login function using Selenium
        
        Args:
            url: Website URL
            login:button to the login page
            username: Your username/email
            password: Your password
            username_selector: CSS selector or XPath for username field
            password_selector: CSS selector or XPath for password field
            login_button_selector: CSS selector or XPath for login button
        """
        
        try:
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
            driver=webdriver.Chrome(options=chrome_options)
            # Navigate to the website
            driver.get(url)
        
            time.sleep(3)
            # Wait for page to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, username_selector))
            )
            
            # Find and fill username field
            username_field = driver.find_element(By.XPATH, username_selector)
            username_field.clear()
            username_field.send_keys(username)
            
            # Find and fill password field
            password_field = driver.find_element(By.XPATH, password_selector)
            password_field.clear()
            password_field.send_keys(password)
            
            # Find and click login button
            login_button = driver.find_element(By.XPATH, login_button_selector)
            login_button.click()
            
            # Wait for login to complete (adjust timeout as needed)
            WebDriverWait(driver, 10).until(
                EC.url_changes(url)  # Wait for URL to change after login
            )
            
            print("Login successful!")
            return driver
            
        except Exception as e:
            print(f"Login failed: {str(e)}")
            driver.quit()
            return None