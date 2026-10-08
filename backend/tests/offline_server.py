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
}.items():
    os.environ.setdefault(_k, _v)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402,F401
