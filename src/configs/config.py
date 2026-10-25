import os
from pathlib import Path
from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONFIG_DIR.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
STATIC_DIR = SRC_DIR / "static"

ENV_FILE = Path(os.getenv("AGENT_ENV_FILE") or PROJECT_ROOT / ".env").expanduser()
if not ENV_FILE.is_absolute():
    ENV_FILE = PROJECT_ROOT / ENV_FILE
load_dotenv(ENV_FILE)


def config_path(value: str) -> str:
    """Resolve relative credential/token paths against this config directory."""
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else CONFIG_DIR / path)

# LLM
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-5-5")
LLM_EFFORT = os.getenv("LLM_EFFORT", "low")

# Embeddings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# Qdrant
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
MEMORY_COLLECTION = "agent_memory"

# Google Drive
GOOGLE_SERVICE_ACCOUNT_FILE = config_path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json"))
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")

# Google Calendar (OAuth user login)
GOOGLE_OAUTH_CLIENT_FILE = config_path(os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "credential.json"))
GOOGLE_TOKEN_FILE = config_path(os.getenv("GOOGLE_TOKEN_FILE", "token.json"))
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_OAUTH_PORT = int(os.getenv("GOOGLE_OAUTH_PORT", "8080"))
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "Asia/Ho_Chi_Minh")

# Gmail uses a separate read-only authorization from Calendar.
GOOGLE_GMAIL_CLIENT_FILE = config_path(os.getenv("GOOGLE_GMAIL_CLIENT_FILE", GOOGLE_OAUTH_CLIENT_FILE))
GOOGLE_GMAIL_TOKEN_FILE = config_path(os.getenv("GOOGLE_GMAIL_TOKEN_FILE", "gmail_token.json"))
GOOGLE_GMAIL_OAUTH_PORT = int(os.getenv("GOOGLE_GMAIL_OAUTH_PORT", "8000"))

# Web login returns to the already registered root URI on port 9004.
GOOGLE_WEB_CLIENT_FILE = config_path(os.getenv("GOOGLE_WEB_CLIENT_FILE", "client_secret.json"))
GOOGLE_WEB_REDIRECT_URI = os.getenv("GOOGLE_WEB_REDIRECT_URI", "http://localhost:9004")

# OpenTelemetry
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "ai-agent")
# Set OTEL_EXPORTER_OTLP_ENDPOINT to enable OTLP export (e.g., http://localhost:4318)
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
