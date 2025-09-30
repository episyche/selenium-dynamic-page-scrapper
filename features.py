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


def load_json_file(file_path):
    """Load JSON data from a given file path."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading JSON file: {e}")
        return None


def create_direct_chain(json_data):
    """Create a direct chain that processes JSON data without vector storage."""
    llm = get_llm()

    # Convert JSON to string for direct inclusion in prompt
    json_content = json.dumps(json_data, separators=(",", ":"))

    action_prompt = """    
    Your are an JSON reading expert, who understands the JSON and answer the questions asked

    Website Data:
    {json_content}

    Your task:
    1.Find all the possible features of the website and return in markdown
     Do not include code fences in your output (no ```md).
        Only return the cleaned markdown.
"""

    prompt = ChatPromptTemplate.from_template(action_prompt)
    chain = prompt | llm

    return chain, json_content


def generate_features(json_file_path, output_md="features.md"):
    """Load JSON file, create direct chain, ask a question, and save md."""
    json_data = load_json_file(json_file_path)
    if not json_data:
        return None

    try:
        chain, json_content = create_direct_chain(json_data)

        # Invoke the chain with both JSON content and user input
        response = chain.invoke(
            {
                "json_content": json_content,
            }
        )

        # Extract text content from response
        if hasattr(response, "content"):
            answer_text = response.content
        else:
            answer_text = str(response)

        print("LLM Output:\n", answer_text)

        # File path to save md
        file_path = output_md

        # Write the md string to a file
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(answer_text)

        print(f"md file saved locally at '{file_path}'")

        return answer_text
    except Exception as e:
        print(f"Error asking question: {e}")
        return None


if __name__ == "__main__":
    json_file = "min_json.json"

    generate_features(json_file, "features.md")
