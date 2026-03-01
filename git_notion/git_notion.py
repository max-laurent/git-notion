"""Main module."""
import hashlib
import os
import glob
import json
import time
from configparser import ConfigParser
import re
import requests

NOTION_TOKEN = os.getenv("NOTION_TOKEN_V2", "")
NOTION_VERSION = "2022-06-28"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": NOTION_VERSION,
}

# ── Low-level Notion API helpers ────────────────────────────────────────────

def notion_get(path):
    r = requests.get(f"https://api.notion.com/v1/{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()

def notion_post(path, data):
    r = requests.post(f"https://api.notion.com/v1/{path}", headers=HEADERS, json=data)
    r.raise_for_status()
    return r.json()

def notion_patch(path, data):
    r = requests.patch(f"https://api.notion.com/v1/{path}", headers=HEADERS, json=data)
    r.raise_for_status()
    return r.json()

def get_block_children(block_id):
    results = []
    cursor = None
    while True:
        params = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        data = notion_get(f"blocks/{block_id}/children{params}")
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return results

def delete_block(block_id):
    requests.delete(f"https://api.notion.com/v1/blocks/{block_id}", headers=HEADERS)

def append_blocks(block_id, children):
    """Append blocks in chunks of 100 (Notion API limit)."""
    for i in range(0, len(children), 100):
        chunk = children[i:i+100]
        notion_patch(f"blocks/{block_id}/children", {"children": chunk})
        time.sleep(0.3)  # rate limit safety

# ── Markdown → Notion blocks ────────────────────────────────────────────────

def rich_text(text):
    """Convert text with basic inline markdown to Notion rich_text array."""
    segments = []
    # Pattern for bold, italic, inline code
    pattern = re.compile(r'(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`|(.+?)(?=\*\*|\*|`|$))', re.DOTALL)
    remaining = text
    while remaining:
        bold = re.match(r'\*\*(.+?)\*\*', remaining, re.DOTALL)
        italic = re.match(r'\*(.+?)\*', remaining, re.DOTALL)
        code = re.match(r'`(.+?)`', remaining, re.DOTALL)
        if bold:
            segments.append({"type": "text", "text": {"content": bold.group(1)}, "annotations": {"bold": True}})
            remaining = remaining[bold.end():]
        elif italic:
            segments.append({"type": "text", "text": {"content": italic.group(1)}, "annotations": {"italic": True}})
            remaining = remaining[italic.end():]
        elif code:
            segments.append({"type": "text", "text": {"content": code.group(1)}, "annotations": {"code": True}})
            remaining = remaining[code.end():]
        else:
            # Find next special marker
            next_marker = re.search(r'\*\*|\*|`', remaining)
            if next_marker:
                plain = remaining[:next_marker.start()]
                if plain:
                    segments.append({"type": "text", "text": {"content": plain}})
                remaining = remaining[next_marker.start():]
            else:
                if remaining:
                    segments.append({"type": "text", "text": {"content": remaining}})
                break
    return segments or [{"type": "text", "text": {"content": text}}]

def parse_table(table_lines):
    """Parse markdown table lines into a Notion table block."""
    rows = []
    for line in table_lines:
        if re.match(r'^\s*\|[-|\s:]+\|\s*$', line):
            continue  # skip separator
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        rows.append(cells)
    if not rows:
        return None
    col_count = max(len(r) for r in rows)
    table_rows = []
    for row in rows:
        cells = row + [''] * (col_count - len(row))  # pad
        table_rows.append({
            "type": "table_row",
            "table_row": {
                "cells": [[{"type": "text", "text": {"content": cell}}] for cell in cells]
            }
        })
    return {
        "type": "table",
        "table": {
            "table_width": col_count,
            "has_column_header": True,
            "has_row_header": False,
            "children": table_rows
        }
    }

def md_to_notion_blocks(md_content: str):
    """Convert markdown string to list of Notion API block dicts."""
    blocks = []
    lines = md_content.split('\n')
    i = 0
    code_buffer = []
    in_code_block = False
    code_language = "plain text"

    while i < len(lines):
        line = lines[i]

        # Code block
        if line.startswith('```'):
            if not in_code_block:
                in_code_block = True
                code_language = line[3:].strip() or "plain text"
                code_buffer = []
            else:
                in_code_block = False
                blocks.append({
                    "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": '\n'.join(code_buffer)}}],
                        "language": code_language
                    }
                })
                code_buffer = []
            i += 1
            continue

        if in_code_block:
            code_buffer.append(line)
            i += 1
            continue

        # Table
        if re.match(r'^\s*\|.*\|\s*$', line):
            table_lines = []
            while i < len(lines) and re.match(r'^\s*\|.*\|\s*$', lines[i]):
                table_lines.append(lines[i])
                i += 1
            table_block = parse_table(table_lines)
            if table_block:
                blocks.append(table_block)
            continue

        # Headings
        if line.startswith('### '):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": rich_text(line[4:])}})
        elif line.startswith('## '):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": rich_text(line[3:])}})
        elif line.startswith('# '):
            blocks.append({"type": "heading_1", "heading_1": {"rich_text": rich_text(line[2:])}})

        # Bullet list
        elif line.startswith('- ') or line.startswith('* '):
            blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich_text(line[2:])}})

        # Numbered list
        elif re.match(r'^\d+\. ', line):
            text = re.sub(r'^\d+\. ', '', line)
            blocks.append({"type": "numbered_list_item", "numbered_list_item": {"rich_text": rich_text(text)}})

        # Blockquote
        elif line.startswith('> '):
            blocks.append({"type": "quote", "quote": {"rich_text": rich_text(line[2:])}})

        # Divider
        elif line.strip() in ('---', '***', '___'):
            blocks.append({"type": "divider", "divider": {}})

        # Checkbox
        elif line.startswith('- [ ] ') or line.startswith('- [x] '):
            checked = line.startswith('- [x] ')
            text = line[6:]
            blocks.append({"type": "to_do", "to_do": {"rich_text": rich_text(text), "checked": checked}})

        # Paragraph
        elif line.strip():
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": rich_text(line)}})

        i += 1

    return blocks

# ── Database / page helpers ─────────────────────────────────────────────────

def get_or_create_page(parent_id, title):
    """Get or create a child page by title under parent_id."""
    children = get_block_children(parent_id)
    for child in children:
        if child["type"] == "child_page" and child["child_page"]["title"] == title:
            return child["id"]
    result = notion_post("pages", {
        "parent": {"page_id": parent_id},
        "properties": {"title": {"title": [{"text": {"content": title}}]}}
    })
    return result["id"]

def get_or_create_database(parent_id, title):
    """Get or create an inline database under parent_id."""
    children = get_block_children(parent_id)
    for child in children:
        if child["type"] == "child_database" and child["child_database"]["title"] == title:
            return child["id"]
    result = notion_post("databases", {
        "parent": {"page_id": parent_id},
        "title": [{"text": {"content": title}}],
        "is_inline": True,
        "properties": {
            "Name": {"title": {}},
            "MD5":  {"rich_text": {}}
        }
    })
    return result["id"]

def get_or_create_db_row(db_id, page_title):
    """Get or create a page (row) in the database by title."""
    result = notion_post("databases/" + db_id + "/query", {
        "filter": {"property": "Name", "title": {"equals": page_title}}
    })
    if result["results"]:
        page = result["results"][0]
        md5 = ""
        if page["properties"].get("MD5", {}).get("rich_text"):
            md5 = page["properties"]["MD5"]["rich_text"][0]["plain_text"]
        return page["id"], md5
    new_page = notion_post("pages", {
        "parent": {"database_id": db_id},
        "properties": {
            "Name": {"title": [{"text": {"content": page_title}}]},
            "MD5":  {"rich_text": [{"text": {"content": ""}}]}
        }
    })
    return new_page["id"], ""

def set_page_md5(page_id, md5):
    notion_patch(f"pages/{page_id}", {
        "properties": {
            "MD5": {"rich_text": [{"text": {"content": md5}}]}
        }
    })

def clear_page_content(page_id):
    children = get_block_children(page_id)
    for child in children:
        delete_block(child["id"])
        time.sleep(0.1)

# ── Main upload ─────────────────────────────────────────────────────────────

def upload_file_to_db(db_id, filename: str):
    page_title = os.path.basename(filename).replace(".md", "")

    hasher = hashlib.md5()
    with open(filename, "rb") as f:
        hasher.update(f.read())
    new_md5 = hasher.hexdigest()

    page_id, existing_md5 = get_or_create_db_row(db_id, page_title)

    if existing_md5 == new_md5:
        print(f"  {filename} unchanged, skipping.")
        return

    clear_page_content(page_id)

    with open(filename, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = md_to_notion_blocks(content)
    append_blocks(page_id, blocks)
    set_page_md5(page_id, new_md5)
    print(f"  {filename} uploaded.")

def sync_to_notion(repo_root: str = "."):
    os.chdir(repo_root)
    config = ConfigParser()
    config.read(os.path.join(repo_root, "setup.cfg"))

    root_page_url = os.getenv("NOTION_ROOT_PAGE") or config.get('git-notion', 'notion_root_page')
    ignore_regex = os.getenv("NOTION_IGNORE_REGEX") or config.get('git-notion', 'ignore_regex', fallback=None)

    # Extract page ID from URL
    root_page_id = root_page_url.rstrip('/').split('-')[-1].split('?')[0]
    # Format as UUID if needed
    if len(root_page_id) == 32:
        root_page_id = f"{root_page_id[:8]}-{root_page_id[8:12]}-{root_page_id[12:16]}-{root_page_id[16:20]}-{root_page_id[20:]}"

    folder_files = {}
    for file in glob.glob("**/*.md", recursive=True):
        if ignore_regex and re.match(ignore_regex, file):
            continue
        folder = os.path.dirname(file) or "root"
        folder_files.setdefault(folder, []).append(file)

    for folder, files in folder_files.items():
        print(f"\nProcessing folder: {folder}")
        page_id = root_page_id
        for part in folder.split(os.sep):
            page_id = get_or_create_page(page_id, part)
        db_id = get_or_create_database(page_id, os.path.basename(folder))
        for file in files:
            upload_file_to_db(db_id, file)