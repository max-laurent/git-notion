"""Main module."""
import hashlib
import io
import os
import glob
from configparser import ConfigParser
import re
from notion.block import PageBlock, TextBlock, CollectionViewBlock
from notion.client import NotionClient
from md2notion.upload import upload

TOKEN = os.getenv("NOTION_TOKEN_V2", "")
_client = None

def get_client():
    global _client
    if not _client:
        _client = NotionClient(token_v2=TOKEN)
    return _client

def get_or_create_page(base_page, title):
    """Get or create a subpage."""
    for child in base_page.children.filter(PageBlock):
        if child.title == title:
            return child
    return base_page.children.add_new(PageBlock, title=title)

def get_or_create_database(base_page, title):
    """Get or create a table/database inside a subpage."""
    for child in base_page.children:
        if isinstance(child, CollectionViewBlock) and child.collection.name == title:
            return child

    db = base_page.children.add_new(CollectionViewBlock)
    db.collection = get_client().get_collection(
        get_client().create_record("collection", parent=db, schema={
            "title": {"name": "Name", "type": "title"},
            "hash":  {"name": "MD5",  "type": "text"},
        })
    )
    db.views.add_new(view_type="table")
    db.collection.name = title
    return db

def get_or_create_row(db, page_title):
    """Get or create a row in the database."""
    for row in db.collection.get_rows():
        if row.title == page_title:
            return row, False
    row = db.collection.add_row()
    row.title = page_title
    return row, True

def strip_md_tables(md_content: str) -> str:
    """Replace markdown tables with a plain text notice to avoid nested collection errors."""
    table_pattern = re.compile(r'(\|.+\|\n)+', re.MULTILINE)
    return table_pattern.sub("[Table: see source file in GitHub]\n", md_content)

def upload_file_to_db(db, filename: str):
    """Upload a markdown file as a row in the database."""
    page_title = os.path.basename(filename).replace(".md", "")

    hasher = hashlib.md5()
    with open(filename, "rb") as mdFile:
        hasher.update(mdFile.read())

    row, is_new = get_or_create_row(db, page_title)

    # Skip if unchanged
    if not is_new and row.hash == hasher.hexdigest():
        print(f"  {filename} unchanged, skipping.")
        return

    # Clear and re-upload content
    for child in row.children:
        child.remove()

    row.hash = hasher.hexdigest()

    with open(filename, "r", encoding="utf-8") as mdFile:
        content = mdFile.read()

    cleaned_content = strip_md_tables(content)
    cleaned_file = io.StringIO(cleaned_content)
    cleaned_file.name = filename  # md2notion needs a .name attribute

    upload(cleaned_file, row)
    print(f"  {filename} uploaded.")

def sync_to_notion(repo_root: str = "."):
    os.chdir(repo_root)
    config = ConfigParser()
    config.read(os.path.join(repo_root, "setup.cfg"))

    root_page_url = os.getenv("NOTION_ROOT_PAGE") or config.get('git-notion', 'notion_root_page')
    ignore_regex = os.getenv("NOTION_IGNORE_REGEX") or config.get('git-notion', 'ignore_regex', fallback=None)

    root_page = get_client().get_block(root_page_url)

    # Group files by their folder
    folder_files = {}
    for file in glob.glob("**/*.md", recursive=True):
        if ignore_regex and re.match(ignore_regex, file):
            continue
        folder = os.path.dirname(file) or "root"
        folder_files.setdefault(folder, []).append(file)

    # For each folder: create subpage → create table → upload files as rows
    for folder, files in folder_files.items():
        print(f"\nProcessing folder: {folder}")

        # Support nested folders (e.g. sops/hr → subpage "sops" > subpage "hr")
        page = root_page
        for part in folder.split(os.sep):
            page = get_or_create_page(page, part)

        # One table per subpage, named after the folder
        db_title = os.path.basename(folder)
        db = get_or_create_database(page, db_title)

        for file in files:
            upload_file_to_db(db, file)