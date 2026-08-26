import sys
from pathlib import Path

# Them thu muc src vao python path de import duoc app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.server import mcp

app = mcp.http_app()
