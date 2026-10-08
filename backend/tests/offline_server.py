"""Import backend/server.py for offline tests without real secrets or network.

Dummy env is set before import (load_dotenv won't override these), so no live
Mongo/Gemini/Printify credentials are needed or used.
"""
import os
import sys
from pathlib import Path

for _k, _v in {
    "MONGO_URL": "mongodb://localhost:1",
    "DB_NAME": "offline_test",
    "GEMINI_API_KEY": "offline-dummy",
    "ADMIN_API_KEY": "offline-dummy",
    "CORS_ORIGINS": "http://localhost:3000",
    "PRINTIFY_API_TOKEN": "offline-dummy",
}.items():
    os.environ.setdefault(_k, _v)
# Blank (not unset) so load_dotenv can't pull real Etsy credentials from backend/.env;
# tests that need them set them with monkeypatch.
for _k in ("ETSY_API_KEY", "ETSY_SHARED_SECRET", "ETSY_ACCESS_TOKEN", "ETSY_REFRESH_TOKEN", "ETSY_SHOP_ID"):
    os.environ[_k] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402,F401
