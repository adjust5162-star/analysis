from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_analyzer import load_env
from web_app import AppHandler, init_db

load_env(ROOT / ".env")
init_db()


class handler(AppHandler):
    pass
