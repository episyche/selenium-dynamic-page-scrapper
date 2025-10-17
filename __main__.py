import argparse
from step_1_web_scrapper import WebElementFinder
from step_2_create_database import main as step2_main
from step_3_store_json import main as step3_main
from step_4_create_mini_json import summarize
import json
import time
from login import login_to_website
from login_in_homepage import login_to_website as homepage_login


def main():
    # Set up argument parser
    parser = argparse.ArgumentParser(description="Process URL")
    parser.add_argument("website_source", help="URL")
    args = parser.parse_args()


    # # login to dende hardcoded
    # driver=login_to_website("https://v2.dende.ai/login","melissa-rogers@powerscrews.com",'Test123!',"/html/body/div[1]/div[1]/main/div[1]/div/form/div[1]/div/input","/html/body/div[1]/div[1]/main/div[1]/div/form/div[2]/div/input","/html/body/div[1]/div[1]/main/div[1]/div/form/button")
    # time.sleep(5)
    # print("running Scrapping whole page")

    # login to instaenly  hardcoded
    # driver=login_to_website("https://app.instantly.ai/auth/login","sabarish@episyche.com",'Test@123!',"/html/body/div[1]/section/div[2]/div/div/div[2]/div/div/form/div/div[1]/div[1]/input","/html/body/div[1]/section/div[2]/div/div/div[2]/div/div/form/div/div[1]/div[2]/input","/html/body/div[1]/section/div[2]/div/div/div[2]/div/div/form/div/div[2]/div[1]/button")
    # time.sleep(5)
    # print("running Scrapping whole page")

    driver = homepage_login(
        "https://webopt.ai/",
        "/html/body/div[1]/header/nav/div[3]/div/div/p",
        "vahak77251@bllibl.com",
        "Test@12345",
        "emailId",
        "passwordId",
        "//button[@type='submit' and contains(@class, 'bg-gradient-to-r') and contains(text(), 'Log In')]",
    )
    time.sleep(5)
    print("running Scrapping whole page")

    finder = WebElementFinder(args.website_source,driver=driver,avoid_pages=["tools"]) #include driver from login for same driver
    urls=[]
    finder.find_all_elements_dynamic()
    website=finder.whole_website
    # print("website:",json.dumps(website, ensure_ascii=False, indent=2))
    for page in website:
        urls.append(page["metadata"]["url"])
    # Run step 2
    print("Running Step 1: Reading sitemap and creating database structure...")
    step2_main(urls)

    # Run step 3
    print("\nRunning Step 2: Converting HTML to text...")
    step3_main(website)

    # # Run step 4
    # print("summarize the json")
    # summarize()

if __name__ == "__main__":
    main()
