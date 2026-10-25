"""
File Reader Service - Convert files to Markdown using MarkItDown.
"""

import os
from markitdown import MarkItDown

MAX_CHARS = 15000


def read_file(file_path: str) -> dict:
    """Read a local file and convert its content to Markdown."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: '{file_path}'")

    md = MarkItDown()
    result = md.convert(file_path)
    text = result.text_content

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n... [truncated, total {len(text)} chars]"

    return {
        "file_name": os.path.basename(file_path),
        "content": text,
    }
