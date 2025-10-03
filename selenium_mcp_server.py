from mcp.server.fastmcp import FastMCP
from browser import Browser
from typing import Dict, Any

mcp = FastMCP("selenium MCP Server")

browser = None


def get_browser() -> Browser:
    """Get or create the browser instance."""
    global browser
    if browser is None:
        browser = Browser()
    return browser


@mcp.tool()
def navigate_to_page(url: str) -> Dict[str, Any]:
    """
    Navigate to a URL using the persistent browser instance.

    Args:
        url (str): The URL to navigate to.

    Returns:
        Dict[str, Any]: Information about the navigation result, such as success status and current URL.
    """
    return get_browser().navigate(url)


@mcp.tool()
def click_element(xpath: str) -> Dict[str, Any]:
    """
    Click an element on the current page.

    Args:
        xpath (str): The XPath of the element to click.

    Returns:
        Dict[str, Any]: Result of the click action, including success status.
    """
    return get_browser().click_element(xpath)


@mcp.tool()
def send_keys_to_element(
    xpath: str, text: str, clear_first: bool = True
) -> Dict[str, Any]:
    """
    Send text to an input element.

    Args:
        xpath (str): The XPath of the input element.
        text (str): The text to send.
        clear_first (bool, optional): Whether to clear existing text before sending. Defaults to True.

    Returns:
        Dict[str, Any]: Result of the send keys action, including success status.
    """
    return get_browser().send_keys(xpath, text, clear_first)


@mcp.tool()
def find_element(xpath: str) -> Dict[str, Any]:
    """
    Find and get information about an element.

    Args:
        xpath (str): The XPath of the element to find.

    Returns:
        Dict[str, Any]: Information about the found element (text, attributes, visibility, etc.).
    """
    return get_browser().find_element(xpath)


@mcp.tool()
def get_current_url() -> Dict[str, Any]:
    """
    Get the current URL of the browser.

    Returns:
        Dict[str, Any]: The current URL.
    """
    return get_browser().get_current_url()


@mcp.tool()
def take_screenshot(filename: str = None) -> Dict[str, Any]:
    """
    Take a screenshot of the current page.

    Args:
        filename (str, optional): The file path to save the screenshot. If None, a default path is used.

    Returns:
        Dict[str, Any]: Information about the screenshot, including file path and success status.
    """
    return get_browser().take_screenshot(filename)


@mcp.tool()
def press_enter_on_element(xpath: str) -> Dict[str, Any]:
    """
    Press Enter key on an element.

    Args:
        xpath (str): The XPath of the element.

    Returns:
        Dict[str, Any]: Result of pressing Enter, including success status.
    """
    return get_browser().press_enter(xpath)


@mcp.tool()
def refresh_page() -> Dict[str, Any]:
    """
    Refresh the current page.

    Returns:
        Dict[str, Any]: Result of the refresh action, including success status.
    """
    return get_browser().refresh_page()


@mcp.tool()
def go_back() -> Dict[str, Any]:
    """
    Navigate back in browser history.

    Returns:
        Dict[str, Any]: Result of the navigation, including current URL and success status.
    """
    return get_browser().go_back()


@mcp.tool()
def go_forward() -> Dict[str, Any]:
    """
    Navigate forward in browser history.

    Returns:
        Dict[str, Any]: Result of the navigation, including current URL and success status.
    """
    return get_browser().go_forward()


@mcp.tool()
def get_element_text(xpath: str) -> Dict[str, Any]:
    """
    Get the text content of an element.

    Args:
        xpath (str): The XPath of the element.

    Returns:
        Dict[str, Any]: Text content of the element and success status.
    """
    return get_browser().get_text_of_element(xpath)


@mcp.tool()
def get_element_attribute(xpath: str, attribute: str) -> Dict[str, Any]:
    """
    Get an attribute value from an element.

    Args:
        xpath (str): The XPath of the element.
        attribute (str): The attribute name to retrieve.

    Returns:
        Dict[str, Any]: Attribute value and success status.
    """
    return get_browser().get_attribute(xpath, attribute)


@mcp.tool()
def hover_over_element(xpath: str) -> Dict[str, Any]:
    """
    Hover over an element.

    Args:
        xpath (str): The XPath of the element to hover over.

    Returns:
        Dict[str, Any]: Result of the hover action, including success status.
    """
    return get_browser().hover_element(xpath)


@mcp.tool()
def upload_file(xpath: str, file_path: str) -> Dict[str, Any]:
    """
    Upload a file to a file input element.

    Args:
        xpath (str): The XPath of the file input element.
        file_path (str): Path to the file to upload.

    Returns:
        Dict[str, Any]: Result of the upload, including success status.
    """
    return get_browser().upload_file_direct(xpath, file_path)


@mcp.tool()
def upload_file_drag_drop(xpath: str, file_path: str) -> Dict[str, Any]:
    """
    Upload a file using drag and drop.

    Args:
        xpath (str): The XPath of the drop zone element.
        file_path (str): Path to the file to upload.

    Returns:
        Dict[str, Any]: Result of the upload, including success status.
    """
    return get_browser().upload_file_drag_drop(xpath, file_path)


@mcp.tool()
def press_escape() -> Dict[str, Any]:
    """
    Press ESC key to close modals/popups.

    Returns:
        Dict[str, Any]: Result of pressing ESC, including success status.
    """
    return get_browser().press_escape()


@mcp.tool()
def close_browser() -> Dict[str, Any]:
    """
    Close the current window.

    Returns:
        Dict[str, Any]: Result of closing the window, including success status.
    """
    return get_browser().close_current_window()


@mcp.tool()
def quit_browser() -> Dict[str, Any]:
    """
    Quit the entire browser and cleanup resources .

    Returns:
        Dict[str, Any]: Result of quitting the browser, including success status.
    """
    global browser
    result = get_browser().quit_browser()
    browser = None  # Reset so it can be recreated if needed
    return result


if __name__ == "__main__":
    mcp.run()
