"""
pip install mcp-use
pip install langchain-openai
"""

import asyncio
import os
from datetime import datetime
import json

from langchain_openai import ChatOpenAI
from mcp_use import MCPAgent, MCPClient


async def main():
    current_date = datetime.now().strftime("%Y-%m-%d")

    config = {
        "mcpServers": {
            "selenium_testing_server": {
                "command": "python",
                "args": ["selenium_mcp_server.py"],
            },
        }
    }

    client = MCPClient.from_dict(config)
    llm = ChatOpenAI(
        model="gpt-4o-mini", api_key=os.getenv("OPENAI_API_KEY")
    )  # or any LLM supported by LangChain

    system_rules = """
You are a JSON Test Executor AI that runs automated tests and checks if they pass or fail.

HOW IT WORKS:

1) READ THE TEST:
- The input JSON contains:
  - test_id: unique test identifier
  - test_name: name of the test
  - test_description: what the test does
  - test_steps: list of actions to perform in order
  - test_expected_result: what should happen if the test passes

2) USE THE RIGHT TOOLS:
- First, list all available MCP tools you can use
- Only use tools that actually exist - don't make up tool names
- If a tool isn't available, mark that step as "simulated"

3) RUN THE TEST STEPS:
- Execute each step in order, one at a time
- Special rule: Always run "Close_browser" last, even if it appears earlier in the list
- Do not perform any action after executing "Close_browser"
- For clicking or navigating:
  - Find elements using the XPath provided
  - Check if the page URL changes
  - Record the new URL
  - Take a screenshot to validate the action
- For waiting:
  - Use wait tools if available
  - Otherwise, check periodically with a timeout

4) HANDLE ERRORS:
- If something goes wrong, put the error message in actual_output
- Mark the step as failed

5) RETURN THE RESULT:
- Return one JSON object with:
  - test_id
  - test_name
  - input_json (the original test you received)
  - expected_output (what should have happened)
  - actual_output (what actually happened)
  - test_result ("correct" if it matches, "wrong" if it doesn't)
  - screenshot_files (list of all screenshot file names captured during test execution)
- Do not include code fences in your output (no ```json).
- No extra explanations - just the JSON

KEY POINTS:
- "Wait for page to load" means check that the URL changed or page is stable
- Extract URLs from expected results to compare properly
- Always close the browser last, no matter where it appears in the steps
- Don't exceed the maximum number of tool calls allowed
"""

    agent = MCPAgent(llm=llm, client=client, max_steps=2, system_prompt=system_rules)

    test_case = {
        "test_id": "TC001",
        "test_name": "Navigate to Blog Page",
        "test_description": "Verify that clicking the Blog button navigates the user to the Blog page.",
        "test_steps": [
            "Step 1: Open the browser.",
            "Step 2: Navigate to the 'https://dende.ai/' website.",
            "Step 3: Locate and click the Blog button using XPath '/html/body/header/div/nav[1]/div/ul/li[4]/a'.",
            "Step 4: Wait for the Blog page to load.",
            "Step 5: Close the browser window.",
        ],
        "test_expected_result": "The user is successfully navigated to the Blog page.",
    }

    test_case_1 = {
        "test_id": "TC003",
        "test_name": "multi-page navigation",
        "test_description": "Verify navigation from Home to Page1, then Page1 to Page2, Page3 back to Home.",
        "test_steps": [
            "Step 1: Open the browser.",
            "Step 2: Navigate to the Home page at 'http://localhost:3000/'.",
            "Step 3: Click the Page1 link/button using XPath '/html/body/div/nav/div[1]/a[2]'.",
            "Step 4: Wait for the page to load.",
            "Step 5: Close the browser window.",
        ],
        "test_expected_result": "User successfully navigates http://localhost:3000/page1.",
    }

    test_case_2={
    "test_id": "TC002",
    "test_name": "Login with Valid Email and Password",
    "test_description": "Verify that the user can log in successfully using a valid email and password.",
    "test_steps": [
        "Step 1: Open the browser.",
        "Step 2: Navigate to the 'https://v2.dende.ai/login' website.",
        "Step 3: Locate the Email input field using XPath '/html/body/div[1]/div[1]/main/div[1]/div/form/div[1]/div/input' and enter a 'melissa-rogers@powerscrews.com'.",
        "Step 4: Locate the Password input field using XPath '/html/body/div[1]/div[1]/main/div[1]/div/form/div[2]/div/input' and enter a 'Test123!'.",
        "Step 5: Locate and click the Login button using XPath '/html/body/div[1]/div[1]/main/div[1]/div/form/button>'.",
        "Step 6: Wait for the home/dashboard page to load after login.",
        "Step 7: Close the browser window."
    ],
    "test_expected_result": "The user is successfully logged in and redirected to the home/dashboard page."
}
    
    test_case_3={
    "test_id": "TC005",
    "test_name": "Form Interaction Test",
    "test_description": "Verify that the user can navigate to a URL, fill input fields, select options, and interact with checkboxes successfully.",
    "test_steps": [
        "Step 1: Open the browser.",
        "Step 2: Navigate to 'https://demoqa.com/automation-practice-form'.",
        "Step 3: Locate the First Name input field using XPath '/html/body/div[2]/div/div/div/div[2]/div[2]/form/div[1]/div[2]/input' and enter 'Lionel'.",
        "Step 4: Locate the Last Name input field using XPath '/html/body/div[2]/div/div/div/div[2]/div[2]/form/div[1]/div[4]/input' and enter 'Messi'.",
        "Step 5: Locate the Email input field using XPath '/html/body/div[2]/div/div/div/div[2]/div[2]/form/div[2]/div[2]/input' and enter 'goat@football.com'.",
        "Step 6: Locate and click the desired Radio button using XPath '/html/body/div[2]/div/div/div/div[2]/div[2]/form/div[3]/div[2]/div[1]/input'.",
        "Step 7: Locate the Mobile Number input field using XPath '/html/body/div[2]/div/div/div/div[2]/div[2]/form/div[4]/div[2]/input' and enter '00000000000 '.",
        "Step 8: Locate and click the required Checkbox using XPath '/html/body/div[2]/div/div/div/div[2]/div[2]/form/div[7]/div[2]/div[1]/input'.",
        "Step 9: (Optional) Click the Submit button using XPath '/html/body/div[2]/div/div/div/div[2]/div[2]/form/div[11]/div/button'.",
        "Step 10: Wait for confirmation, next page, or any expected outcome.",
        "Step 11: Close the browser window."
    ],
    "test_expected_result": "The user is able to fill all input fields, select radio buttons, check the checkbox, and perform the required actions successfully."
}

    test_case_4={
    "test_id": "TC006",
    "test_name": "Navigate to Page1 and Return",
    "test_description": "Verify that clicking the Page1 button navigates to Page1 and using the browser back button returns to the previous page.",
    "test_steps": [
        "Step 1: Open the browser.",
        "Step 2: Navigate to 'http://localhost:3000/'.",
        "Step 3: Capture the current URL as the 'previous URL'.",
        "Step 4: Locate and click the Page1 button using XPath '/html/body/div/nav/div[1]/a[2]'.",
        "Step 5: Wait for Page1 to load completely.",
        "Step 6: The expected Page1 URL.",
        "Step 7: Use the browser back button to return to the previous page.",
        "Step 8: Wait for the previous page to load completely.",
        "Step 9: Verify that the current URL matches the previously captured 'previous URL'.",
        "Step 10: Close the browser window."
    ],
    "test_expected_result": "The user successfully navigates to Page1 after clicking the button, and using the browser back button returns to the previous page with the correct URL."
}
    test_case_5={
    "test_id": "TC010",
    "test_name": "Verify Buttons Using XPaths",
    "test_description": "Verify that each button identified by its XPath is visible, clickable, and performs the expected action.",
    "test_steps": [
        "Step 1: Open the browser.",
        "Step 2: Navigate to 'http://localhost:3000/'.",
        "Step 3: For each button XPath in the list ['/html/body/div/nav/div[1]/a[2]', '/html/body/div/nav/div[1]/a[3]', '/html/body/div/nav/div[2]/a[1]', '/html/body/div/nav/div[2]/a[2]']:",
        "    a) Locate the button using its XPath.",
        "    d) Click the button and verify the expected behavior (e.g., page navigation, modal opens, action performed).",
        "    e) If the button navigates away, return to the original page to continue testing other buttons.",
        "Step 4: Close the browser window."
    ],
    "test_expected_result": "All buttons identified by their XPaths are visible, enabled, clickable, and perform the expected actions correctly."
}
    test_case_6={
    "test_id": "TC011",
    "test_name": "Verify Button Text",
    "test_description": "Verify that the button displays the correct text as expected.",
    "test_steps": [
        "Step 1: Open the browser.",
        "Step 2: Navigate to 'https://www.cricbuzz.com/'.",
        "Step 3: Locate the button using XPath '/html/body/header/div/nav/a[2]'.",
        "Step 4: Retrieve the text of the button.",
        "Step 5: Verify that the button text matches the expected text 'Live Scores'.",
        "Step 6: Close the browser window."
    ],
    "test_expected_result": "The button displays the correct text as specified."
}
    
    test_case_7={
    "test_id": "TC002_NEG",
    "test_name": "Login with Invalid Email or Password",
    "test_description": "Verify that the user cannot log in using invalid email or password and receives an appropriate error message.",
    "test_steps": [
        "Step 1: Open the browser.",
        "Step 2: Navigate to the 'https://v2.dende.ai/login' website.",
        "Step 3: Locate the Email input field using XPath '/html/body/div[1]/div[1]/main/div[1]/div/form/div[1]/div/input' and enter an invalid email 'invalid@example.com'.",
        "Step 4: Locate the Password input field using XPath '/html/body/div[1]/div[1]/main/div[1]/div/form/div[2]/div/input' and enter an invalid password 'wrongpassword'.",
        "Step 5: Locate and click the Login button using XPath '/html/body/div[1]/div[1]/main/div[1]/div/form/button'.",
        "Step 6: Wait for the error message to appear indicating invalid credentials in xpath '/html/body/div[1]/div[2]/div/div/div[1]/div[2]'.",
        "Step 7: Verify that the error message is displayed correctly.",
        "Step 8: Close the browser window."
    ],
    "test_expected_result": "The login attempt fails, and an error message is displayed indicating that the email or password is incorrect."
}
    test_case_8={
    "test_id": "TC012_SEC",
    "test_name": "Login Form Security - SQL Injection",
    "test_description": "Verify that the login form is protected against SQL injection attacks and does not allow unauthorized access.",
    "test_steps": [
        "Step 1: Open the browser.",
        "Step 2: Navigate to the 'https://v2.dende.ai/login' website.",
        "Step 3: Locate the Email input field using XPath '/html/body/div[1]/div[1]/main/div[1]/div/form/div[1]/div/input' and enter an SQL injection string \"'hero@gmail.com OR '1'='1\".",
        "Step 4: Locate the Password input field using XPath '/html/body/div[1]/div[1]/main/div[1]/div/form/div[2]/div/input' and enter any value, e.g., 'password'.",
        "Step 5: Locate and click the Login button using XPath '/html/body/div[1]/div[1]/main/div[1]/div/form/button'.",
        "Step 6: Wait for the response from the server.",
        "Step 7: Verify that login fails.",
        "Step 8: Close the browser window."
    ],
    "test_expected_result": "The login attempt fails, the application does not allow access, and no sensitive information (like database errors) is exposed."
}






    # Google search console
    result = await agent.run(
        json.dumps(test_case_1),
        max_steps=20,
    )
    print("Result:", result)


if __name__ == "__main__":
    asyncio.run(main())
