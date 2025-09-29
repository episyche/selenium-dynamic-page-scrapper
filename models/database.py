from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from typing import Optional

# Database setup
Base = declarative_base()
engine = None
Session = None

class Page(Base):
    """SQLAlchemy model for storing sitemap pages and their content"""
    __tablename__ = 'pages'

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=True)
    depth = Column(Integer, nullable=False)
    parent_id = Column(Integer, ForeignKey('pages.id'), nullable=True)
    json_content = Column(Text, nullable=True)
    text_content = Column(Text, nullable=True)
    
    # Self-referencing relationship
    children = relationship("Page", backref="parent", remote_side=[id])

    def __repr__(self):
        return f"<Page(name='{self.name}', url='{self.url}', depth={self.depth})>"

def initialize_database(name:str) -> None:
    """
    Initialize database connection and session.
    
    Args:
        sitemap_source (str): URL or path to sitemap file
    """
    global engine, Session
    
    # Generate database name and create engine
    db_name = get_database_name(name)
    db_path = f'sqlite:///{db_name}'
    engine = create_engine(db_path)
    Session = sessionmaker(bind=engine)
    
    # Create database tables
    Base.metadata.create_all(engine)

def get_database_name(sitemap_source: str) -> str:
    """
    Generate a database name based on the sitemap source.
    For URLs: uses the domain name
    For files: uses the filename without extension
    
    Args:
        sitemap_source (str): URL or path to sitemap file
        
    Returns:
        str: Database name
    """
    from urllib.parse import urlparse
    import os
    
    if sitemap_source.startswith(('http://', 'https://')):
        # For URLs, use the domain name
        parsed_url = urlparse(sitemap_source)
        domain = parsed_url.netloc
        # Remove 'www.' if present and replace dots with underscores
        domain = domain.replace('www.', '').replace('.', '_')
        return f"{domain}_pages.db"
    else:
        # For files, use the filename without extension
        filename = os.path.basename(sitemap_source)
        name_without_ext = os.path.splitext(filename)[0]
        return f"{name_without_ext}_pages.db"

def get_session() -> Optional[Session]:
    """
    Get the current database session.
    
    Returns:
        Optional[Session]: The current database session or None if not initialized
    """
    return Session() if Session else None 