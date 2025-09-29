import requests
import xml.etree.ElementTree as ET
from typing import List, Dict
from urllib.parse import urlparse
import json
import argparse
import os
from pathlib import Path
from models.database import (
    Page, initialize_database, get_database_name,
    get_session, Session
)

# Default debug setting
DEBUG = False

def setup_debug_directories(sitemap_source: str) -> tuple[Path, Path, Path]:
    """
    Create debug directories for storing output files.
    
    Args:
        sitemap_source (str): URL or path to sitemap file
        
    Returns:
        tuple[Path, Path, Path]: Paths to base, json, and markdown directories
    """
    # Generate base directory name from sitemap source
    if sitemap_source.startswith(('http://', 'https://')):
        domain = urlparse(sitemap_source).netloc.replace('www.', '').replace('.', '_')
    else:
        domain = os.path.splitext(os.path.basename(sitemap_source))[0]
    
    # Create debug directory structure
    base_dir = Path('debug') / domain
    json_dir = base_dir / 'json'
    markdown_dir = base_dir / 'markdown'
    
    if DEBUG:
        base_dir.mkdir(parents=True, exist_ok=True)
        json_dir.mkdir(parents=True, exist_ok=True)
        markdown_dir.mkdir(parents=True, exist_ok=True)
    
    return base_dir, json_dir, markdown_dir

def create_nested_structure(urls: List[str]) -> Dict:
    """
    Create a nested dictionary structure from a list of URLs with name, depth, children_depth, and url fields.
    Every node (including intermediate) will have a URL.
    """
    def calculate_children_depth(node: Dict) -> int:
        if not node["children"]:
            return 0
        return 1 + max(calculate_children_depth(child) for child in node["children"])

    structure = {
        "name": "root",
        "depth": 0,
        "children_depth": 0,
        "url": None,
        "children": []
    }

    temp_nodes = {}

    for url in urls:
        parsed = urlparse(url)
        path_parts = [part for part in parsed.path.split('/') if part]

        current = structure
        current_path = []
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        for i, part in enumerate(path_parts):
            current_path.append(part)
            path_key = '/'.join(current_path)

            # Construct full URL up to this part
            node_url = f"{base_url}/{'/'.join(current_path)}"

            if path_key not in temp_nodes:
                new_node = {
                    "name": part,
                    "depth": i + 1,
                    "children_depth": 0,
                    "url": node_url,
                    "children": []
                }
                temp_nodes[path_key] = new_node
                current["children"].append(new_node)

            current = temp_nodes[path_key]

    # Update children_depth
    def update_children_depth(node: Dict) -> None:
        for child in node["children"]:
            update_children_depth(child)
        node["children_depth"] = calculate_children_depth(node)

    update_children_depth(structure)
    return structure

def get_sitemap_urls(sitemap_source: str) -> List[str]:
    """
    Fetch and parse sitemap.xml from a given URL or local file and return all URLs found in it.
    
    Args:
        sitemap_source (str): The URL of the sitemap.xml file or path to local sitemap file
        
    Returns:
        List[str]: List of URLs found in the sitemap
    """
    try:
        # Check if the source is a URL or local file
        if sitemap_source.startswith(('http://', 'https://')):
            # Fetch the sitemap content from URL
            response = requests.get(sitemap_source)
            response.raise_for_status()
            content = response.content
        else:
            # Read from local file
            with open(sitemap_source, 'rb') as f:
                content = f.read()
        
        # Parse the XML content
        root = ET.fromstring(content)
        
        # Define the namespace (sitemaps typically use this namespace)
        namespace = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        
        # Find all URL elements and extract their locations
        urls = [url.text for url in root.findall('.//ns:loc', namespace)]
        
        return urls
    
    except requests.RequestException as e:
        print(f"Error fetching sitemap from URL: {e}")
        return []
    except ET.ParseError as e:
        print(f"Error parsing XML: {e}")
        return []
    except FileNotFoundError:
        print(f"Error: Sitemap file not found at {sitemap_source}")
        return []
    except Exception as e:
        print(f"Unexpected error: {e}")
        return []

def save_debug_files(structure: Dict, base_dir: Path, json_dir: Path) -> None:
    """
    Save debug files if DEBUG is enabled.
    
    Args:
        structure (Dict): The nested structure to save
        base_dir (Path): Base directory for debug files
        json_dir (Path): Directory for JSON files
    """    
    # Save URL structure
    url_structure_file = base_dir / "url_structure.json"
    with open(url_structure_file, 'w', encoding='utf-8') as f:
        json.dump(structure, f, indent=2)

def store_pages_in_db(structure: Dict, parent_id: int = None, session=None) -> None:
    """
    Store pages in SQLite database with parent-child relationships.
    Stores every node (home + intermediate paths + leaves) with full URLs.
    """
    if session is None:
        session = get_session()

    # Create landing page
    if parent_id is None:
        # Pick domain root
        for child in structure["children"]:
            if child.get("url"):
                parsed = urlparse(child["url"])
                domain = f"{parsed.scheme}://{parsed.netloc}"
                root = Page(
                    name="home",
                    url=domain,
                    depth=0,
                    parent_id=None
                )
                session.add(root)
                session.commit()
                parent_id = root.id
                break

    # Store all children
    for child in structure["children"]:
        page = Page(
            name=child["name"],
            url=child.get("url"),
            depth=child["depth"],
            parent_id=parent_id
        )
        session.add(page)
        session.commit()

        # Recurse
        store_pages_in_db(child, page.id, session)



def main(urls: List[str]):
    # Initialize database (use any name since we don’t have sitemap source)
    initialize_database("whole_website")

    if not urls:
        print("No URLs found in the list. Exiting...")
        return

    print(f"Found {len(urls)} URLs in the array")
    print(f"Using database: {get_database_name('whole_website')}")

    # Build nested structure
    nested_structure = create_nested_structure(urls)

    # Optional: Save debug
    if DEBUG:
        base_dir = Path("debug") / "urls_array"
        json_dir = base_dir / "json"
        markdown_dir = base_dir / "markdown"
        save_debug_files(nested_structure, base_dir, json_dir)

    # Store in DB
    print("\nStoring pages in database...")
    store_pages_in_db(nested_structure)
    print("Pages stored successfully!")

