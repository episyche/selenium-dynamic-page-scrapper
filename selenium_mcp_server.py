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
    """Navigate to a URL using the persistent browser instance."""
    return get_browser().navigate(url)

@mcp.tool()
def click_element(xpath: str) -> Dict[str, Any]:
    """Click an element on the current page."""
    return get_browser().click_element(xpath)

@mcp.tool()
def send_keys_to_element(xpath: str, text: str, clear_first: bool = True) -> Dict[str, Any]:
    """Send text to an input element."""
    return get_browser().send_keys(xpath, text, clear_first)

@mcp.tool()
def find_element(xpath: str) -> Dict[str, Any]:
    """Find and get information about an element."""
    return get_browser().find_element(xpath)

@mcp.tool()
def get_current_url() -> Dict[str, Any]:
    """Get the current URL of the browser."""
    return get_browser().get_current_url()

@mcp.tool()
def take_screenshot(filename: str = None) -> Dict[str, Any]:
    """Take a screenshot of the current page."""
    return get_browser().take_screenshot(filename)

@mcp.tool()
def press_enter_on_element(xpath: str) -> Dict[str, Any]:
    """Press Enter key on an element."""
    return get_browser().press_enter(xpath)

@mcp.tool()
def refresh_page() -> Dict[str, Any]:
    """Refresh the current page."""
    return get_browser().refresh_page()

@mcp.tool()
def go_back() -> Dict[str, Any]:
    """Navigate back in browser history."""
    return get_browser().go_back()

@mcp.tool()
def go_forward() -> Dict[str, Any]:
    """Navigate forward in browser history."""
    return get_browser().go_forward()

@mcp.tool()
def get_element_text(xpath: str) -> Dict[str, Any]:
    """Get the text content of an element."""
    return get_browser().get_text_of_element(xpath)

@mcp.tool()
def get_element_attribute(xpath: str, attribute: str) -> Dict[str, Any]:
    """Get an attribute value from an element."""
    return get_browser().get_attribute(xpath, attribute)

@mcp.tool()
def hover_over_element(xpath: str) -> Dict[str, Any]:
    """Hover over an element."""
    return get_browser().hover_element(xpath)

@mcp.tool()
def upload_file(xpath: str, file_path: str) -> Dict[str, Any]:
    """Upload a file to a file input element."""
    return get_browser().upload_file_direct(xpath, file_path)

@mcp.tool()
def upload_file_drag_drop(xpath: str, file_path: str) -> Dict[str, Any]:
    """Upload a file using drag and drop."""
    return get_browser().upload_file_drag_drop(xpath, file_path)

@mcp.tool()
def press_escape() -> Dict[str, Any]:
    """Press ESC key to close modals/popups."""
    return get_browser().press_escape()

@mcp.tool()
def close_browser() -> Dict[str, Any]:
    """Close the current window."""
    return get_browser().close_current_window()

@mcp.tool()
def quit_browser() -> Dict[str, Any]:
    """Quit the entire browser and cleanup resources."""
    global browser
    result = get_browser().quit_browser()
    browser = None  # Reset so it can be recreated if needed
    return result

if __name__ == "__main__":
    mcp.run()
    