import os
import json
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate

load_dotenv()


def get_llm():
    """Initialize and return the LLM."""
    return ChatOpenAI(
        model="gpt-4o-mini", temperature=0.2, api_key=os.getenv("OPENAI_API_KEY")
    )


def load_markdown_file(file_path):
    """Load markdown data from a given file path."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"Error loading markdown file: {e}")
        return None


def create_test_case_chain(markdown_content):
    """Create a chain that generates test cases from markdown features."""
    llm = get_llm()

    test_case_prompt = """
    You are a QA expert who creates comprehensive test cases from feature documentation.

    Feature Documentation:
    {markdown_content}


    Your task:
1. Analyze all features described in the documentation.
2. For each feature, generate **separate individual test cases** (one JSON object per test case). Each test case must include:
   - test_id
   - test_name
   - test_description
   - test_steps (include XPath of the elements, and describe each step in detail e.g., 
       "Step 1: Open the browser
        Step 2: Navigate to page_url
        Step 3: Locate 'Sign Up' button using and click
        Step 4: Enter email etc.)
   - test_expected_result
3. Do **not** group test cases by feature. Each test case must be a standalone JSON object.
4. Format the output as a JSON array of individual test case objects.
5. Do not include code fences in the output. Only return the cleaned JSON with test cases.

Make sure each test case is complete and self-contained.
"""

    prompt = ChatPromptTemplate.from_template(test_case_prompt)
    chain = prompt | llm

    return chain


def generate_test_cases(markdown_file_path, output_json="test_cases.json"):
    """Load markdown file, create chain, generate test cases, and save to file."""
    markdown_content = load_markdown_file(markdown_file_path)
    if not markdown_content:
        return None

    try:
        chain = create_test_case_chain(markdown_content)

        # Invoke the chain with markdown content
        response = chain.invoke(
            {
                "markdown_content": markdown_content,
            }
        )

        # Extract text content from response
        if hasattr(response, "content"):
            test_cases_text = response.content
        else:
            test_cases_text = str(response)

        print("LLM Output:\n", test_cases_text)

        # Write the test cases to a file
        with open(output_json, "w", encoding="utf-8") as f:
            f.write(test_cases_text)

        print(f"Test cases file saved locally at '{output_json}'")

        return test_cases_text
    except Exception as e:
        print(f"Error generating test cases: {e}")
        return None


if __name__ == "__main__":
    markdown_file = "features.md"

    generate_test_cases(markdown_file, "test_cases.json")
