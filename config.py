import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = os.getenv("API_ID", "0")
API_ID = int(API_ID) if API_ID.isdigit() else 0
API_HASH = os.getenv("API_HASH", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
STORE_ADMIN_IDS = ADMIN_IDS.copy()
SOURCING_ADMIN_IDS = ADMIN_IDS.copy()
SELLER_BOT_TOKEN = BOT_TOKEN


# Database
# Priority: /app/data (User defined volume) > /data (Railway default) > local
if os.path.exists("/app/data"):
    DATABASE_URL = "sqlite+aiosqlite:////app/data/app.db"
elif os.path.exists("/data"):
    DATABASE_URL = "sqlite+aiosqlite:////data/app.db"
else:
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///app.db")

# WebApp URLs
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://tg-test-plus3414-production.up.railway.app")
STORE_URL = f"{WEBAPP_URL}/store?v=3"
SELLER_URL = f"{WEBAPP_URL}/seller?v=3"

# Binance Payment Config
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
DEPOSIT_ADDRESS = os.getenv("DEPOSIT_ADDRESS", "")
