"""
Read File Tool - Read and convert files to Markdown using MarkItDown.
"""

from src.configs.tool_loader import tool_config
from src.registry.models import ToolDefinition
from src.services.file_reader import read_file


def read_local_file(file_path: str) -> dict:
    """Read a local file and convert to Markdown."""
    return read_file(file_path)


read_file_tool = ToolDefinition(
    **tool_config("read_file"),
    handler=read_local_file,
)


ALL_READ_FILE_TOOLS = [read_file_tool]
