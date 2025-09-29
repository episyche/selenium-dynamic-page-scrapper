import requests
import traceback
import argparse
# from bs4 import BeautifulSoup
import json
from typing import List, Dict, Tuple
from models.database import (
    Page, initialize_database, get_database_name,
    get_session, Session
)

def store_content_in_db(session, page_id: int, json_content: List[Dict], text_content: str):
    """Store the extracted content in the database using SQLAlchemy."""
    # Get the page
    page = session.query(Page).filter_by(id=page_id).first()
    
    if page:
        # Update page content
        page.json_content = json.dumps(json_content)
        session.commit()
    else:
        print(f"Warning: Page with ID {page_id} not found")

def find_non_matching_entries(parent_data, child_data):
    """
    Find entries in child_data that don't exist in parent_data.
    Compares entire dictionary entries for exact matching.
    """
    # Create a set of JSON string representations from parent_data for quick lookup
    parent_entries = {
        json.dumps(item, sort_keys=True)
        for item in parent_data
    }
    
    # Find entries in child_data that don't exist in parent_data
    non_matching = [
        item for item in child_data 
        if json.dumps(item, sort_keys=True) not in parent_entries
    ]
    
    return non_matching

def get_all_parent_pages(session, page: Page) -> List[Page]:
    """
    Get all parent pages up to the root (depth 0) for a given page.
    
    Args:
        session: SQLAlchemy session
        page: Current page object
        
    Returns:
        List of parent pages ordered from immediate parent to root parent
    """
    parent_pages = []
    current_page = page
    
    while current_page.parent_id is not None:
        parent = session.query(Page).filter_by(id=current_page.parent_id).first()
        if parent:
            parent_pages.append(parent)
            current_page = parent
        else:
            break
            
    return parent_pages
def get_page_by_url(website, target_url):
    for site in website:
        if site.get("metadata", {}).get("url").rstrip("/") == target_url.rstrip("/"):
            return site
    return None

def main(website):
    initialize_database("whole_website")
    
    # Create a new session
    session = get_session()
    
    #Get all the depth level available in the database and store it in a list
    depth_levels = []
    for depth in session.query(Page.depth).distinct().order_by(Page.depth).all():
        depth_levels.append(depth[0])    
    print(depth_levels) 
   
    try:       
        for depth in depth_levels:
            print(f"Processing depth: {depth}")
            print(f"Processing depth type: {type(depth)}")
        
            pages = session.query(Page).filter_by(depth=depth).order_by(Page.id).all()
            
            for page in pages:
                print(f"Processing page: {page.url}")
                try:
                    page_data = get_page_by_url(website, page.url)
                    filtered_json_content =page_data                                                
                    # if depth == 0:
                    #     None                       
                    #     # text_content = process_json_to_markdown(filtered_json_content)                             
                    # else: 
                    #     # Get all parent pages up to root
                    #     parent_pages = get_all_parent_pages(session, page)
                        
                    #     if parent_pages:
                    #         # Compare with each parent page's content
                    #         for parent_page in parent_pages:
                    #             if parent_page.json_content:
                    #                 parent_json_content = json.loads(parent_page.json_content)
                    #                 # Find content that's not in this parent page
                    #                 filtered_json_content = find_non_matching_entries(
                    #                     parent_json_content, 
                    #                     filtered_json_content
                    #                 )
                                    
                    # text_content = process_json_to_markdown(filtered_json_content)
                            
                    store_content_in_db(session, page.id, filtered_json_content, "text_content")                    
                    
                except Exception as e:
                    print(f"Error processing {page.url}: {str(e)}")
                    print(traceback.print_exc())
    
    finally:
        session.close()


