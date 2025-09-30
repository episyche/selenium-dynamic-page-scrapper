import asyncio
import argparse
import json
import os
import sys
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()



def get_server_script_path() -> str:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, "selenium_mcp_server.py")


async def call_tool(session: ClientSession, name: str, **arguments) -> str:
    result = await session.call_tool(name=name, arguments=arguments)

    # Try to extract human-readable text from the result's content items
    try:
        content_items = getattr(result, "content", []) or []
        if content_items:
            first = content_items[0]
            text = getattr(first, "text", None)
            if text is not None:
                return text
        return str(result)
    except Exception:
        return str(result)


def ensure_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set. Export OPENAI_API_KEY.")
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "OpenAI SDK not installed. Install 'openai' or run 'pip install -r requirements.txt'."
        ) from exc
    return OpenAI()


def llm_route_question(
    question: str, model: str | None = None
) -> tuple[str | None, dict]:
    client = ensure_openai_client()
    model_name = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    system_prompt = (
        "You are an intelligent Web Element Finder AI.\n"
        "Your task is to help automation systems (like Selenium) interact with web pages.\n"
        "When the user asks a question:\n"
        "- Select the most relevant tool from the available tools.\n"
        "- Generate the correct arguments for that tool.\n"
        "- Respond ONLY with a JSON object containing:\n"
        "  - 'method': the tool name\n"
        "  - 'arguments': a dictionary of parameters\n"
        "Available tools:\n"
        "1. navigate_to_page(url: str) → Open the given website URL.\n"
    )

    user_prompt = f"User prompt: {question}"

    try:
        # Using Chat Completions with JSON response

        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        print(f"Content: {content}")
        data = json.loads(content)
        return data.get("method"), data.get("arguments", {})
    except Exception:
        return None, {}


async def main() -> None:
    parser = argparse.ArgumentParser(description="MCP Selenium client")
    parser.add_argument(
        "--question",
        "-q",
        nargs="+",
        help="Natural language question, e.g. 'navigate to the google.com'",
        required=False,
    )
    parser.add_argument(
        "--model",
        "-m",
        default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        help="LLM model name for agent mode",
    )
    args = parser.parse_args()

    server_script = get_server_script_path()
    if not os.path.exists(server_script):
        raise FileNotFoundError(f"Server script not found at: {server_script}")

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[server_script],
        env=None,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            if not args.question:
                print(
                    "Please provide a question with --question/-q, e.g. --question 'what is my username'"
                )
                sys.exit(1)

            qtext = " ".join(args.question)
            method, arguments = llm_route_question(qtext, model=args.model)
            if method:
                result_text = await call_tool(session, method, **arguments)
                print(result_text)
            else:
                print(
                    "LLM could not parse the question. Please rephrase and try again."
                )


if __name__ == "__main__":
    asyncio.run(main())
