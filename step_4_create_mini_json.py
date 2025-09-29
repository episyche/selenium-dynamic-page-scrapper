from sqlalchemy import func
from models.database import get_session, Page, initialize_database
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

def create_direct_chain(json_array):
    """
    Create a direct chain that processes an array of webpage JSON objects
    and returns a summarized JSON format .
    """
    llm = get_llm()

    # Convert array of JSONs to string
    json_content = json.dumps(json_array, separators=(",", ":"))

    action_prompt = """
        Website Data:
        {json_content}

        You are a website structure parser. 
        You will receive raw JSON dumps of web pages containing elements with attributes such as xpath, class, style, aria, etc. 
        Don't use own data only use the json data

        Your task:
        1. Extract only the meaningful hierarchy of the website:
        - always start with the page_id lower in the page for eg . 1 for topmost,2 for second topmost
        - Keep: page_name, page_url, and essential elements (headers, main sections, navigation links, buttons, headings, paragraphs).
        - For navigation elements (like <a>, <button>), preserve their text and linked_page (if it has a URL).
        - For text elements (<h1>, <h2>, <p>), keep only the text content.
        - all the element must contains xpath 
        - For stats, lists, or unique values, summarize them into key-value pairs.

        2. Remove all unnecessary details:
        - Remove, style, aria, class, and other low-level attributes.
        - Ignore invisible or empty elements unless they represent navigation or modal dialogs.

        3. Output a simplified JSON in this format:
        [{{
            "page_name": "string",
            "page_url": "string",
            "elements": [
            {{
                "tag": "string",
                "text": "string (optional)",
                "xpath": "string",
                "url": "string (optional) only url present in the json donot added own json",
                "elements": [nested elements] 
            }}
            ]
        }}]

        4. The result should clearly show:
        - Website navigation hierarchy
        - Linked pages from buttons/links
        - Important visible text (headings, paragraphs, stats)

        Do not include code fences in your output (no ```json).
        Only return the cleaned JSON.
        """

    prompt = ChatPromptTemplate.from_template(action_prompt)
    chain = prompt | llm

    return chain, json_content


def ask_question(json_array):
    """Load JSON file, create direct chain, ask a question, and save json."""

    try:
        chain, json_content = create_direct_chain(json_array)

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

        return answer_text
    except Exception as e:
        print(f"Error asking question: {e}")
        return None
    
def split_json_array_conditionally(json_array, max_len=3):
    """
    Split array into parts of length <= max_len **only if length > 5**.
    Otherwise, return the array as a single part.
    """
    
    parts = []
    for i in range(0, len(json_array), max_len):
        parts.append(json_array[i:i + max_len])
    return parts

def summarize_backwards_conditional(json_array):
    """
    Summarize chunks from last to first (rolling), 
    splitting only if length > 5.
    """
    # Step 1: Conditional split
    chunks = split_json_array_conditionally(json_array, max_len=3)

    # Step 2: Start from last chunk
    summary = ask_question(chunks[-1])

    # Step 3: Iterate backwards and summarize with previous
    for i in range(len(chunks) - 2, -1, -1):
        combined = chunks[i] + [summary]
        summary = ask_question(combined)

    return summary


def summarize():
    """
    Summarize hierarchical website pages stored in the database.

    This function processes all pages in the `Page` table, starting from the
    deepest depth and moving upward. For each parent page, it collects the 
    text or JSON content of its children, generates a summarized representation 
    using `ask_question()`, and stores that summary back into the parent's 
    `text_content` field.

    """
    initialize_database("whole_website")
    session = get_session()

    # Get the maximum depth value
    last_depth = session.query(func.max(Page.depth)).scalar()

    if last_depth is None:
        print("No pages found in the table.")
    else:
        summarized_parent = []
        while last_depth > 0:
            seen = set()

            # Get all pages at this depth
            pages_at_last_depth = (
                session.query(Page).filter(Page.depth == last_depth).all()
            )

            for page in pages_at_last_depth:
                if page.parent_id and page.parent_id not in seen:
                    seen.add(page.parent_id)

                    # Get the parent
                    parent_page = (
                        session.query(Page).filter(Page.id == page.parent_id).first()
                    )
                    if parent_page:
                        children = []
                        print(f"\nParent Page ID: {parent_page.id}")

                        # Print all children of this parent (siblings included)
                        for child in (
                            session.query(Page)
                            .filter(Page.parent_id == page.parent_id)
                            .all()
                        ):
                            print(f"   Child Page ID: {child.id}")
                            children.append(child.id)
                        total = [parent_page.id] + children
                        print(f"     {total}")
                        common = list(set(children) & set(summarized_parent))
                        json_array = []
                        if common:
                            total = list(set(total) - set(common))
                            for webpage in (
                                session.query(Page).filter(Page.id.in_(common)).all()
                            ):
                                json_array.append(json.dumps(json.loads(webpage.text_content), separators=(',', ':')))
                            for webpage in (
                                session.query(Page).filter(Page.id.in_(total)).all()
                            ):
                                json_array.append(json.dumps(json.loads(webpage.json_content), separators=(',', ':')))
                            summarized_parent.append(parent_page.id)

                        else:
                            for webpage in (
                                session.query(Page).filter(Page.id.in_(total)).all()
                            ):
                                json_array.append(json.dumps(json.loads(webpage.json_content), separators=(',', ':')))
                            summarized_parent.append(parent_page.id)
                        if len(json_array)<4:
                            summarized_json = ask_question(json_array)
                            parent_page.text_content = summarized_json
                        else:
                            parent_page.text_content=summarize_backwards_conditional(json_array)

            session.commit()
            last_depth -= 1

    session.close()


if __name__ == "__main__":
    summarize()
