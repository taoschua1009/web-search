"""
Google Drive Tools - List files and read file content.
"""

from src.configs.tool_loader import tool_config
from src.registry.models import ToolDefinition
from src.services import drive_service
from src.services.file_reader import read_file

# ============================================================
# Tool 1: LIST DRIVE FILES
# ============================================================

def list_drive_files(folder_id: str = "") -> dict:
    """List all files in Google Drive."""
    fid = folder_id if folder_id else None
    files = drive_service.list_files(folder_id=fid)
    return {
        "total_files": len(files),
        "files": files,
    }


list_files_tool = ToolDefinition(
    **tool_config("list_drive_files"),
    handler=list_drive_files,
)


# ============================================================
# Tool 2: READ DRIVE FILE
# ============================================================

def read_drive_file(file_id: str) -> dict:
    """Download a file from Google Drive and read its content."""
    import os
    download = drive_service.download_file(file_id=file_id)
    temp_path = download["temp_path"]
    try:
        result = read_file(temp_path)
    finally:
        os.unlink(temp_path)
    return {
        "file_id": download["file_id"],
        "file_name": download["file_name"],
        "mime_type": download["mime_type"],
        "content": result["content"],
    }


read_file_tool = ToolDefinition(
    **tool_config("read_drive_file"),
    handler=read_drive_file,
)


ALL_DRIVE_TOOLS = [list_files_tool, read_file_tool]
