"""Tool definitions for Gmail search and read-only email access."""

from src.configs.tool_loader import tool_config
from src.registry.models import ToolDefinition
from src.services.gmail_service import (
    list_messages,
    read_message,
    search_and_read_messages,
)

search_and_read_gmail_messages_tool = ToolDefinition(
    **tool_config("search_and_read_gmail_messages"),
    handler=search_and_read_messages,
)


list_gmail_messages_tool = ToolDefinition(
    **tool_config("list_gmail_messages"),
    handler=list_messages,
)

read_gmail_message_tool = ToolDefinition(
    **tool_config("read_gmail_message"),
    handler=read_message,
)

ALL_GMAIL_TOOLS = [search_and_read_gmail_messages_tool, list_gmail_messages_tool, read_gmail_message_tool]
