import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.okx.com"
JOURNAL = "signals.csv"
MAX_MARKETS = 100
PAUSE = 0.04

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FIELDS = [
    "time", "strategy", "symbol", "side", "timeframe", "setup_type",
    "entry", "stop", "tp1", "tp2", "tp3", "planned_rr",
    "trigger", "status", "last_checked"
]


def now():
    return datetime.now(timezone.utc).isoformat()


def num(x):
    return format(float(x), ".12g")
