import hashlib
import io
import os
import glob
from configparser import ConfigParser
import re
from notion.block import PageBlock, TextBlock, CollectionViewBlock, TableBlock, TableRowBlock
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
    for child in base_page.children.filter(PageBlock):
        if child.title == title:
            return child
    return base_page.children.add_new(PageBlock, title=title)

def get_or_create_database(base_page, title):
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
    for row in db.collection.get_rows():
        if row.title == page_title:
            return row, False
    row = db.collection.add_row()
    row.title = page_title
    return row, True

def parse_md_tables(md_content: str):
    """
    Split markdown content into segments: plain text or tables.
    Returns a list of dicts: {"type": "text", "content": "..."} 
                          or {"type": "table", "rows": [[cell, cell], ...]}
    """
    segments = []
    lines = md_content.split('\n')
    current_text = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if re.match(r'^\s*\|.*\|\s*$', line):
            # Flush accumulated text
            if current_text:
                segments.append({"type": "text", "content": '\n'.join(current_text)})
                current_text = []
            # Collect table lines
            table_lines = []
            while i < len(lines) and re.match(r'^\s*\|.*\|\s*$', lines[i]):
                table_lines.append(lines[i])
                i += 1
            # Parse rows, skip separator lines
            rows = []
            for tline in table_lines:
                if re.match(r'^\s*\|[-|\s:]+\|\s*$', tline):
                    continue
                cells = [c.strip() for c in tline.strip().strip('|').split('|')]
                rows.append(cells)
            if rows:
                segments.append({"type": "table", "rows": rows})
        else:
            current_text.append(line)
            i += 1

    if current_text:
        segments.append({"type": "text", "content": '\n'.join(current_text)})

    return segments

def upload_table_to_notion(page, rows):
    """Create a Notion TableBlock from parsed table rows."""
    if not rows:
        return
    col_count = max(len(row) for row in rows)
    table = page.children.add_new(TableBlock, columns=col_count)
    for row_data in rows:
        tr = table.children.add_new(TableRowBlock)
        for j, cell in enumerate(row_data):
            tr.set_cell(j, cell)

def upload_file_to_db(db, filename: str):
    page_title = os.path.basename(filename).replace(".md", "")

    hasher = hashlib.md5()
    with open(filename, "rb") as mdFile:
        hasher.update(mdFile.read())

    row, is_new = get_or_create_row(db, page_title)

    if not is_new and row.hash == hasher.hexdigest():
        print(f"  {filename} unchanged, skipping.")
        return

    for child in row.children:
        child.remove()

    row.hash = hasher.hexdigest()

    with open(filename, "r", encoding="utf-8") as mdFile:
        content = mdFile.read()

    segments = parse_md_tables(content)

    for segment in segments:
        if segment["type"] == "text":
            text = segment["content"].strip()
            if text:
                f = io.StringIO(text)
                f.name = filename
                upload(f, row)
        elif segment["type"] == "table":
            upload_table_to_notion(row, segment["rows"])

    print(f"  {filename} uploaded.")

def sync_to_notion(repo_root: str = "."):
    os.chdir(repo_root)
    config = ConfigParser()
    config.read(os.path.join(repo_root, "setup.cfg"))

    root_page_url = os.getenv("NOTION_ROOT_PAGE") or config.get('git-notion', 'notion_root_page')
    ignore_regex = os.getenv("NOTION_IGNORE_REGEX") or config.get('git-notion', 'ignore_regex', fallback=None)

    root_page = get_client().get_block(root_page_url)

    folder_files = {}
    for file in glob.glob("**/*.md", recursive=True):
        if ignore_regex and re.match(ignore_regex, file):
            continue
        folder = os.path.dirname(file) or "root"
        folder_files.setdefault(folder, []).append(file)

    for folder, files in folder_files.items():
        print(f"\nProcessing folder: {folder}")
        page = root_page
        for part in folder.split(os.sep):
            page = get_or_create_page(page, part)
        db_title = os.path.basename(folder)
        db = get_or_create_database(page, db_title)
        for file in files:
            upload_file_to_db(db, file)