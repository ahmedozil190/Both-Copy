import os
import logging
import math
import traceback
from datetime import datetime, timedelta
import phonenumbers
from phonenumbers import geocoder
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.future import select
from sqlalchemy import select, delete, update, func, text, or_, cast, String
from database.engine import async_session
from database.models import User, Account, Transaction, AccountStatus, TransactionType, CountryPrice, WithdrawalRequest, WithdrawalStatus, UserCountryPrice, Deposit, AppSetting, UserStorePrice, ApiServer, SubscriptionChannel
from urllib.parse import parse_qsl
import re
import pycountry
import hmac
import hashlib
import time
import requests
from pydantic import BaseModel
from typing import List
# Delayed imports inside functions to avoid pyrogram event loop issues
from services.external_provider import ExternalProvider
from services.i18n import get_text
import pycountry
import re
import urllib.request
import json
import asyncio
from datetime import datetime
import random
import string

class AdminAuthRequest(BaseModel):
    user_id: int
    init_data: str

class SellerDataRequest(BaseModel):
    user_id: int

class SellerOTPRequest(BaseModel):
    user_id: int
    phone: str
    init_data: str # Added for security

class SellerOTPSubmit(BaseModel):
    user_id: int
    phone: str
    hash: str
    code: str
    country: str
    buy_price: float
    init_data: str # Added for security

class WithdrawSubmit(BaseModel):
    user_id: int
    amount: float
    method: str
    address: str
    init_data: str # Added for security verification

class WithdrawAction(AdminAuthRequest):
    request_id: int
    action: str # 'approve' or 'reject'

async def check_and_alert_missing_price(country_name: str, phone_number: str, session):
    from config import BOT_TOKEN, ADMIN_IDS
    import aiohttp
    import asyncio
    try:
        cp_stmt = select(CountryPrice).where(CountryPrice.country_name == country_name)
        cp_list = (await session.execute(cp_stmt)).scalars().all()
        sell_price = cp_list[0].price if cp_list else 0
        
        if sell_price <= 0:
            alert_msg = (
                f"⚠️ <b>Missing Price: {country_name}</b>\n"
                f"Stock added ({phone_number}) but price is $0.00.\n"
                f"Status: <b>HIDDEN</b> from store."
            )
            
            async def notify_admins():
                async with aiohttp.ClientSession() as http_session:
                    for admin_id in ADMIN_IDS:
                        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                        payload = {"chat_id": admin_id, "text": alert_msg, "parse_mode": "HTML"}
                        try:
                            await http_session.post(url, json=payload, timeout=5)
                        except Exception: pass
            
            asyncio.create_task(notify_admins())
    except Exception as e:
        logger.error(f"Error checking missing price alert: {e}")

class DepositSubmit(BaseModel):
    user_id: int
    txid: str
    method: str = "Binance Pay"

def normalize_provider_countries(srv_countries):
    """Normalizes various API provider responses into a standard list of country dicts."""
    countries_list = []
    
    # 1. Super Parser: Find the node that actually contains country data
    def find_country_node(node):
        if isinstance(node, dict):
            # Case A: Dict with country keys (EG, PS, etc.)
            if any(k in node for k in ["EG", "PS", "SA", "US", "20", "966", "970"]):
                return node
            # Case B: Dict that contains common keys like price/count
            if any(k in node for k in ["price", "count", "rate", "cost", "stock"]):
                return node
            # Otherwise, drill down
            for k, v in node.items():
                res = find_country_node(v)
                if res: return res
        elif isinstance(node, list):
            # Case C: List of objects - check first few items
            for item in node[:3]:
                if isinstance(item, dict):
                    if any(k in item for k in ["price", "count", "rate", "cost", "stock"]):
                        return node # Return the whole list
                    res = find_country_node(item)
                    if res: return node # Return the whole list if children are good
        return None

    # Special handling for Spider Service typo-prone and split structure
    # result: { countries: {1: {ISO: price}}, cuantity: {1: {ISO: count}} }
    spider_prices = {}
    spider_counts = {}
    
    if isinstance(srv_countries, dict) and "result" in srv_countries:
        res = srv_countries["result"]
        if isinstance(res, dict):
            # Try to find prices
            p_node = res.get("countries")
            if isinstance(p_node, dict) and "1" in p_node: p_node = p_node["1"]
            if isinstance(p_node, dict): spider_prices = p_node
            
            # Try to find quantities (handling the 'cuantity' typo)
            q_node = res.get("cuantity") or res.get("quantity")
            if isinstance(q_node, dict) and "1" in q_node: q_node = q_node["1"]
            if isinstance(q_node, dict): spider_counts = q_node

    if spider_prices:
        # If we found Spider-specific split data, merge it
        for code, price in spider_prices.items():
            try:
                countries_list.append({
                    "country": code,
                    "price": float(price),
                    "count": int(spider_counts.get(code, 999))
                })
            except: continue
    else:
        # Fallback to Super Parser for TG-Lion and others
        data_node = find_country_node(srv_countries)
        if not data_node:
            data_node = srv_countries
            for key in ["result", "data", "countries_info", "countries"]:
                if isinstance(data_node, dict) and key in data_node:
                    data_node = data_node[key]
                    break
        
        if isinstance(data_node, dict):
            for code, val in data_node.items():
                if code.lower() in ["status", "message", "error", "ok", "msg", "currency", "success", "rate", "price", "count", "stock", "quantity", "qty", "server_time"]: continue
                if isinstance(val, dict):
                    entry = val.copy()
                    entry["country"] = code
                    countries_list.append(entry)
                elif isinstance(val, (int, float, str)):
                    try:
                        # Use a helper to clean price string if it's not a direct float
                        def clean_p(v):
                            if isinstance(v, (int, float)): return float(v)
                            try: return float(str(v).replace('$', '').replace('USD', '').strip().split()[0])
                            except: return 0.0

                        price_val = clean_p(val)
                        countries_list.append({
                            "country": code,
                            "count": 999,
                            "price": price_val
                        })
                    except: continue
        elif isinstance(data_node, list):
            # Normalize list items to have 'country' key
            for item in data_node:
                if not isinstance(item, dict): continue
                normalized = item.copy()
                if "country" not in normalized:
                    # Try to find country code in common keys
                    for k in ["id", "iso", "code", "name"]:
                        if k in normalized:
                            normalized["country"] = normalized[k]
                            break
                countries_list.append(normalized)
    
    return countries_list


class StoreSettingsSubmit(AdminAuthRequest):
    binance_api_key: str
    binance_api_secret: str
    binance_pay_id: str
    trx_address: str
    usdt_bep20_address: str

class GeneralSettingsSubmit(AdminAuthRequest):
    bot_name: str
    purchase_log_channel_id: str
    deposit_log_channel_id: str = ""

class ApiServerSubmit(AdminAuthRequest):
    id: int | None = None
    name: str
    url: str
    api_key: str
    server_type: str = "standard"
    extra_id: str | None = None
    profit_margin: float
    min_profit: float = 0.0
    is_active: bool

class MaintenanceToggle(AdminAuthRequest):
    enabled: bool

class ReferralSettingsSubmit(AdminAuthRequest):
    join_bonus: float
    commission_percent: float

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# SECURITY: OTP Cooldown Tracking
otp_cooldowns = {} # {phone_number: timestamp, user_id: timestamp}
OTP_COOLDOWN_SECONDS = 15

def generate_transaction_id():
    chars = string.ascii_uppercase + string.digits
    suffix = ''.join(random.choice(chars) for _ in range(10))
    return f"TC{suffix}"

def get_flag_emoji(country_code: str):
    """Convert ISO country code to flag emoji."""
    try:
        if not country_code or not isinstance(country_code, str) or len(country_code) != 2:
            return "🌐"
        return "".join(chr(ord(c) + 127397) for c in country_code.upper())
    except:
        return "🌐"

_bot_info_cache = {}

def verify_telegram_auth(init_data: str, bot_token: str, expected_user_id: int) -> bool:
    """Verifies that the request actually comes from the claimed user using Telegram Web App Hash."""
    try:
        if not init_data: return False
        parsed_data = dict(parse_qsl(init_data))
        hash_str = parsed_data.pop('hash', None)
        if not hash_str: return False
        
        # Check if the user ID in init_data matches the claimed user_id
        user_obj = json.loads(parsed_data.get('user', '{}'))
        if int(user_obj.get('id', 0)) != expected_user_id:
            logger.warning(f"Auth Mismatch: Claims {expected_user_id} but InitData is for {user_obj.get('id')}")
            return False
            
        data_check_string = '\n'.join([f"{k}={v}" for k, v in sorted(parsed_data.items())])
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == hash_str
    except Exception as e:
        logger.error(f"Auth Verification Exception: {e}")
        return False

def verify_admin_auth_multi(init_data: str, user_id: int, bot_type: str = "any") -> bool:
    """Helper to verify admin auth against bot-specific or any admin IDs."""
    import config
    from config import BOT_TOKEN, SELLER_BOT_TOKEN
    if not init_data or not user_id: return False
    
    if bot_type == "store":
        admin_list = config.STORE_ADMIN_IDS
    elif bot_type == "sourcing":
        admin_list = config.SOURCING_ADMIN_IDS
    else:
        admin_list = list(set(config.STORE_ADMIN_IDS + config.SOURCING_ADMIN_IDS))
        
    if user_id not in admin_list: return False
    # Try main bot token first
    if verify_telegram_auth(init_data, BOT_TOKEN, user_id): return True
    # Fallback to seller bot token
    if verify_telegram_auth(init_data, SELLER_BOT_TOKEN, user_id): return True
    return False

def verify_user_auth_multi(init_data: str, user_id: int) -> bool:
    """Helper to verify user auth (any user) against seller bot token, or admin against main bot token."""
    import config
    from config import BOT_TOKEN, SELLER_BOT_TOKEN
    if not init_data or not user_id: return False
    # 1. Standard: Seller Bot Token (Any user)
    if verify_telegram_auth(init_data, SELLER_BOT_TOKEN, user_id): return True
    # 2. Admin Bypass: Main Bot Token (Only if admin in either bot)
    all_admins = list(set(config.STORE_ADMIN_IDS + config.SOURCING_ADMIN_IDS))
    if user_id in all_admins:
        if verify_telegram_auth(init_data, BOT_TOKEN, user_id): return True
    return False



async def send_purchase_log(user_id: int, country_name: str, price: float, phone: str, code: str, password: str = None):
    """Send a purchase log to the configured Telegram channel."""
    try:
        from config import BOT_TOKEN
        import requests
        
        async with async_session() as session:
            stmt = select(AppSetting).where(AppSetting.key == "purchase_log_channel_id")
            res = await session.execute(stmt)
            obj = res.scalar_one_or_none()
            if not obj or not obj.value:
                logger.info("Purchase log skipped: No channel ID configured.")
                return
            channel_id = obj.value.strip()
            logger.info(f"Resolved Purchase Log Channel ID: {channel_id}")
            # Standardize channel ID
            if channel_id.isdigit() or (channel_id.startswith('-') and channel_id[1:].isdigit()):
                if not channel_id.startswith('-100') and not channel_id.startswith('-'):
                    channel_id = f"-100{channel_id}"
            logger.info(f"Resolved Purchase Log Channel ID: {channel_id}")
            
        flag = "🌐"
        try:
            # Pass the phone to resolve_country_info to get accurate flag/name
            _, _, iso = resolve_country_info(country_name, full_phone=phone)
            if iso and iso != "XX": 
                flag = get_flag_emoji(iso)
        except: pass
        
        masked_id = str(user_id)
        if len(masked_id) > 6:
            masked_id = f"••{masked_id[2:4]}•••••"
        else:
            masked_id = f"••{masked_id[:2]}•••"
            
        masked_phone = str(phone)
        if len(masked_phone) > 7:
            # Mask like +96655890••••
            # We take the first 9 chars (usually including + and country code and some digits)
            masked_phone = f"{masked_phone[:9]}••••"
            
        # HTML escaping
        safe_country = country_name.replace('<', '&lt;').replace('>', '&gt;')
        # Simplify: "Iran, Islamic Republic of" → "Iran"
        display_country = clean_display_name(safe_country)
        display_password = password if password else "None"
        
        # Proper price formatting: 3 decimals if needed, else 2
        price_str = f"{price:.3f}" if f"{price:.3f}"[-1] != '0' else f"{price:.2f}"
        
        message = (
            "<b>• account purchased successfully .</b>\n\n"
            f"<b>• For country :- {display_country}{flag} </b>\n"
            "<b>• Application Type :- Telegram .</b>\n\n"
            f"<b>• Number :- {masked_phone} 📞.</b>\n"
            f"<b>• Activation code :- {code} 💬.</b>\n\n"
            f"<b>• Password :- {display_password} 🔑.</b>\n"
            f"<b>• Price :- ${price_str} 💵.</b>\n\n"
            f"<b>• ID buyer :- {masked_id} 👨🏻‍💻 .</b>"
        )
        
        payload = {
            "chat_id": channel_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        if not _bot_info_cache.get("username"):
            try:
                r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=5)
                if r.ok:
                    data = r.json()
                    _bot_info_cache["username"] = data["result"].get("username", "")
            except: pass
            
        bot_username = _bot_info_cache.get("username", "")
        if bot_username:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "• Buy number from bot 🖥 .", "url": f"https://t.me/{bot_username}"}]]
            }
        
        def do_send():
            try:
                r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=10)
                if not r.ok:
                    logger.error(f"Telegram API Error: {r.text} | Payload: {payload}")
            except Exception as e:
                logger.error(f"Requests Error in do_send: {e}")
            
        await asyncio.to_thread(do_send)
    except Exception as e:
        logger.error(f"Error in send_purchase_log: {e}")

async def send_sourcing_price_log(country_name: str, iso_code: str, country_code: str, buy_price: float, approve_delay: int, quantity: int = 1000):
    """Send a price update log to the configured Telegram channel."""
    import asyncio
    import urllib.request
    import json
    import html as _html
    try:
        async with async_session() as session:
            stmt = select(AppSetting).where(AppSetting.key == "sourcing_log_channel_id")
            res = await session.execute(stmt)
            obj = res.scalar_one_or_none()
            if not obj or not obj.value:
                logger.warning("send_sourcing_price_log: sourcing_log_channel_id is not configured.")
                return
            channel_id = obj.value.strip()

            # Standardize channel ID
            if channel_id.isdigit() or (channel_id.startswith('-') and not channel_id.startswith('-100')):
                if not channel_id.startswith('-'):
                    channel_id = f"-100{channel_id}"
                elif channel_id.startswith('-') and not channel_id.startswith('-100'):
                    channel_id = f"-100{channel_id[1:]}"

        from config import SELLER_BOT_TOKEN

        flag = get_flag_emoji(iso_code)
        c_name = str(country_name or "Unknown")
        for e in ["\U0001f1f8\U0001f1e6", "\U0001f1ea\U0001f1ec", "\U0001f1fa\U0001f1fe", "\U0001f310"]:
            c_name = c_name.replace(e, "")
        clean_name = _html.escape(c_name.strip())

        buy_str = f"{buy_price:.3f}".rstrip('0').rstrip('.')
        if '.' not in buy_str:
            buy_str = f"{buy_price:.2f}"

        message = (
            f"- {clean_name} - {flag} - ${buy_str}\n\n"
            f"- Quantity - {quantity} - +{_html.escape(str(country_code))} - {_html.escape(str(iso_code))}\n\n"
            f"- Confirmation time [ {approve_delay} ] second\n\n"
            "-The bot is always open. I will announce on this channel if the price goes up or down"
        )

        def _send_tg():
            _username = ""
            try:
                r0 = urllib.request.Request(f"https://api.telegram.org/bot{SELLER_BOT_TOKEN}/getMe")
                with urllib.request.urlopen(r0, timeout=5) as rr:
                    d0 = json.loads(rr.read().decode())
                    if d0.get("ok"):
                        _username = d0["result"].get("username", "")
            except Exception:
                pass

            payload = {"chat_id": channel_id, "text": message, "parse_mode": "HTML"}
            if _username:
                payload["reply_markup"] = {
                    "inline_keyboard": [[{"text": "\U0001f916 BOT \U0001f916", "url": f"https://t.me/{_username}"}]]
                }

            req = urllib.request.Request(
                f"https://api.telegram.org/bot{SELLER_BOT_TOKEN}/sendMessage",
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())

        result = await asyncio.to_thread(_send_tg)
        if not result.get("ok"):
            logger.error(f"Telegram API rejected sourcing log: {result}")
        else:
            logger.info(f"Sourcing price log sent -> channel={channel_id} country={country_name}")

    except Exception as e:
        logger.error(f"Error sending sourcing price log: {e}")

# ---- end send_sourcing_price_log ----


def resolve_country_info(country_code_str: str, full_phone: str = None):
    """Resolve ISO code and Country Name. Handles numeric codes, Alpha-2, and Alpha-3."""
    import pycountry
    import phonenumbers
    import re
    try:
        code_str = str(country_code_str).strip().upper().lstrip('+')
        if not code_str: return "Unknown", "🌐", "XX"

        # 1. Handle if it's already an ISO code (Alpha-2 or Alpha-3)
        if not code_str.isdigit() and len(code_str) in [2, 3]:
            try:
                country = None
                if len(code_str) == 2:
                    country = pycountry.countries.get(alpha_2=code_str)
                else:
                    country = pycountry.countries.get(alpha_3=code_str)
                
                if country:
                    name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', country.name).strip()
                    iso = country.alpha_2
                    return name, get_flag_emoji(iso), iso
            except: pass

        # 2. Handle if full_phone is provided
        if full_phone:
            try:
                parsed = phonenumbers.parse(full_phone if full_phone.startswith('+') else f"+{full_phone}")
                iso_code = phonenumbers.region_code_for_number(parsed)
                country = pycountry.countries.get(alpha_2=iso_code)
                name = country.name if country else iso_code
                name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', name).strip()
                return name, get_flag_emoji(iso_code), iso_code
            except: pass

        # 3. Handle numeric calling code prefix
        if code_str.isdigit():
            try:
                numeric_code = int(code_str)
                iso_code = phonenumbers.region_code_for_country_code(numeric_code)
                flag = get_flag_emoji(iso_code)
                
                name = f"Country {numeric_code}"
                country = pycountry.countries.get(alpha_2=iso_code)
                if country:
                    name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', country.name).strip()
                return name, flag, iso_code
            except: pass
        
        return f"Code {code_str}", "🌐", "XX"
    except Exception as e:
        logger.error(f"Error resolving country {country_code_str}: {e}")
        return f"Code {country_code_str}", "🌐", "XX"

def clean_display_name(raw_name: str) -> str:
    """Removes trailing ISO codes like EG, (EG), or [EG], and resolves standalone codes."""
    if not raw_name: return raw_name
    
    # Standalone code resolution map
    codes_map = {
        "EG": "Egypt",
        "US": "United States",
        "UK": "United Kingdom",
        "SA": "Saudi Arabia",
        "RU": "Russia",
        "UA": "Ukraine"
    }
    
    # If the name itself is just a code, resolve it
    trimmed = raw_name.strip().upper()
    if trimmed in codes_map:
        return codes_map[trimmed]
    
    # Split by comma and parenthesis to take the first part
    raw_name = raw_name.split(',')[0].split('(')[0]
    
    removals = [
        "Islamic Republic of",
        "Province of China",
        "Republic of",
        "Federation",
        "United Republic of",
        "Plurinational State of",
        "Bolivarian Republic of",
        "People's Democratic Republic",
        "Arab Republic",
        "Democratic "
    ]
    for r in removals:
        raw_name = raw_name.replace(r, "")
        
    # Handle formats like "Egypt EG", "Egypt (EG)", "Egypt [EG]"
    clean = re.sub(r'\s*[\(\[]?[A-Z]{2,3}[\)\]]?\s*$', '', raw_name)
    return clean.strip()

# ─── Babel Locale Map ───
_LANG_TO_BABEL = {
    "en": "en",
    "ar": "ar",
    "zh": "zh_Hans",
    "bn": "bn",
    "fa": "fa",
    "ru": "ru",
    "uz": "uz",
    "es": "es",
    "tr": "tr",
}

def get_localized_country_name(iso_code: str, lang: str) -> str:
    """Return country name localized to the given language using Babel."""
    if not iso_code or iso_code == 'XX':
        return "ERROR_ISO_XX"
    try:
        from babel import Locale
        from babel.core import UnknownLocaleError
        locale_str = _LANG_TO_BABEL.get(lang, "en")
        try:
            locale = Locale.parse(locale_str)
        except UnknownLocaleError:
            locale = Locale.parse("en")
        name = locale.territories.get(iso_code.upper())
        return name if name else f"ERROR_NO_NAME_FOR_{iso_code}"
    except Exception as e:
        return f"ERROR_BABEL_{str(e)}"

app = FastAPI(title="Store Admin Panel")

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    # Apply no-cache to both API and HTML pages to prevent aggressive caching in Telegram
    if request.url.path.startswith("/api/") or request.url.path.startswith("/seller") or request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/seller", response_class=HTMLResponse)
async def get_seller_panel(request: Request):
    return templates.TemplateResponse(request=request, name="seller.html")

@app.on_event("startup")
async def run_migrations():
    """Auto-migrate SQLite DB to add any missing columns and create new tables."""
    from database.engine import engine
    from database.models import Base
    import sqlalchemy
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.begin() as conn:
            # Add full_name to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN full_name TEXT"))
            except: pass
            # Add username to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN username TEXT"))
            except: pass
            # Add balance_store to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN balance_store FLOAT DEFAULT 0.0"))
                # Copy existing balance to balance_store if possible
                try:
                    await conn.execute(sqlalchemy.text("UPDATE users SET balance_store = balance"))
                except: pass
            except: pass

            # Add iso_code to user_country_prices if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE user_country_prices ADD COLUMN iso_code TEXT DEFAULT 'XX'"))
            except: pass
            # Add approve_delay to user_country_prices if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE user_country_prices ADD COLUMN approve_delay INTEGER DEFAULT 0"))
            except: pass
            
            # Add otp_code to accounts if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN otp_code VARCHAR"))
            except: pass
            # Add balance_sourcing to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN balance_sourcing FLOAT DEFAULT 0.0"))
            except: pass
            # Add is_active_store to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN is_active_store BOOLEAN DEFAULT 0"))
            except: pass
            # Add is_active_sourcing to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN is_active_sourcing BOOLEAN DEFAULT 0"))
            except: pass
            # Add is_banned_store to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN is_banned_store BOOLEAN DEFAULT 0"))
            except: pass
            # Add is_banned_sourcing to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN is_banned_sourcing BOOLEAN DEFAULT 0"))
            except: pass
            # Add referral columns to users if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN referred_by INTEGER"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN referral_earnings FLOAT DEFAULT 0.0"))
            except: pass
            
            # One-time migration: set existing users active in both if they weren't before
            # This ensures they appear in dashboards immediately after migration
            try:
                await conn.execute(sqlalchemy.text("UPDATE users SET is_active_store = 1, is_active_sourcing = 1 WHERE is_active_store = 0 AND is_active_sourcing = 0"))
            except: pass

            # Add missing columns to accounts table
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN seller_id INTEGER"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN buyer_id INTEGER"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN created_at DATETIME"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN purchased_at DATETIME"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN server_id INTEGER"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN hash_code TEXT"))
            except: pass

            # Add missing columns to country_prices table
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN country_name TEXT"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN buy_price FLOAT DEFAULT 0.0"))
            except: pass
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN approve_delay INTEGER DEFAULT 0"))
            except: pass
            
            # Add iso_code to country_prices if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN iso_code TEXT DEFAULT 'XX'"))
            except: pass
            
            # Drop unique constraint on country_code if it exists (SQLite workaround requires rebuilding table)
            try:
                table_sql_res = await conn.execute(sqlalchemy.text("SELECT sql FROM sqlite_master WHERE type='table' AND name='country_prices'"))
                table_sql = table_sql_res.scalar()
                if table_sql and 'UNIQUE' in table_sql.upper():
                    logger.info("Rebuilding country_prices to remove UNIQUE constraint")
                    await conn.execute(sqlalchemy.text("""
                        CREATE TABLE country_prices_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            country_code VARCHAR NOT NULL,
                            iso_code VARCHAR DEFAULT 'XX',
                            country_name VARCHAR NOT NULL,
                            price FLOAT NOT NULL DEFAULT 1.0,
                            buy_price FLOAT NOT NULL DEFAULT 0.5,
                            approve_delay INTEGER NOT NULL DEFAULT 0,
                            updated_at DATETIME
                        )
                    """))
                    await conn.execute(sqlalchemy.text("INSERT INTO country_prices_new (id, country_code, iso_code, country_name, price, buy_price, approve_delay, updated_at) SELECT coalesce(id, 0), coalesce(country_code, ''), coalesce(iso_code, 'XX'), coalesce(country_name, 'Unknown'), coalesce(price, 0), coalesce(buy_price, 0), coalesce(approve_delay, 0), updated_at FROM country_prices"))
                    await conn.execute(sqlalchemy.text("DROP TABLE country_prices"))
                    await conn.execute(sqlalchemy.text("ALTER TABLE country_prices_new RENAME TO country_prices"))
            except Exception as e:
                logger.error(f"Failed to rebuild country_prices table: {e}")

            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE country_prices ADD COLUMN updated_at DATETIME"))
            except: pass

            # One-time resolution: Fix 'XX' iso_codes for legacy data
            try:
                cursor = await conn.execute(sqlalchemy.text("SELECT id, country_code FROM country_prices WHERE iso_code = 'XX' OR iso_code IS NULL"))
                rows = cursor.fetchall()
                for row_id, c_code in rows:
                    try:
                        clean_code = c_code.strip().lstrip('+').lstrip('0')
                        numeric_code = int(clean_code)
                        detected_iso = phonenumbers.region_code_for_country_code(numeric_code)
                        if detected_iso:
                            await conn.execute(sqlalchemy.text("UPDATE country_prices SET iso_code = :iso WHERE id = :id"), {"iso": detected_iso, "id": row_id})
                    except: pass
            except: pass

            try:
                await conn.execute(sqlalchemy.text("UPDATE country_prices SET updated_at = '" + datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + "' WHERE updated_at IS NULL"))
            except: pass

            # Fix: Sync existing available accounts with CountryPrice selling prices
            try:
                await conn.execute(sqlalchemy.text("""
                    UPDATE accounts 
                    SET price = (
                        SELECT price FROM country_prices 
                        WHERE country_prices.country_name = accounts.country 
                        LIMIT 1
                    )
                    WHERE status = 'available' 
                    AND EXISTS (
                        SELECT 1 FROM country_prices WHERE country_prices.country_name = accounts.country
                    )
                """))
                logger.info("Successfully synced available account prices with CountryPrice table.")
            except Exception as e:
                logger.warning(f"Failed to sync account prices: {e}")

            # Add two_fa_password column to accounts table if it doesn't exist
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN two_fa_password VARCHAR;"))
                logger.info("Added two_fa_password column to accounts table.")
            except Exception as e:
                if "duplicate column name" not in str(e).lower():
                    pass

            # Add locked_buy_price to accounts if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN locked_buy_price FLOAT;"))
                logger.info("Added locked_buy_price column to accounts table.")
            except: pass
            # Add locked_approve_delay to accounts if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN locked_approve_delay INTEGER;"))
                logger.info("Added locked_approve_delay column to accounts table.")
            except: pass

            # Backfill locked values for existing pending accounts that have NULL values
            try:
                await conn.execute(sqlalchemy.text("""
                    UPDATE accounts
                    SET
                        locked_buy_price = (
                            SELECT cp.buy_price FROM country_prices cp
                            WHERE cp.country_name = accounts.country
                            LIMIT 1
                        ),
                        locked_approve_delay = (
                            SELECT cp.approve_delay FROM country_prices cp
                            WHERE cp.country_name = accounts.country
                            LIMIT 1
                        )
                    WHERE status = 'pending'
                    AND (locked_buy_price IS NULL OR locked_approve_delay IS NULL)
                """))
                logger.info("Backfilled locked values for legacy pending accounts.")
            except Exception as e:
                logger.warning(f"Backfill locked values warning: {e}")
            # Add reject_reason to accounts if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE accounts ADD COLUMN reject_reason VARCHAR;"))
                logger.info("Added reject_reason column to accounts table.")
            except: pass

            # Add min_profit to api_servers if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE api_servers ADD COLUMN min_profit FLOAT DEFAULT 0.0;"))
                logger.info("Added min_profit column to api_servers table.")
            except: pass

            # Add fee and net_amount to withdrawal_requests if missing
            try:
                await conn.execute(sqlalchemy.text("ALTER TABLE withdrawal_requests ADD COLUMN fee FLOAT NOT NULL DEFAULT 0.0;"))
                await conn.execute(sqlalchemy.text("ALTER TABLE withdrawal_requests ADD COLUMN net_amount FLOAT NOT NULL DEFAULT 0.0;"))
                logger.info("Added fee and net_amount columns to withdrawal_requests table.")
            except: pass

            # One-time migration: Update server_type based on URL if it's 'standard' or 'other'
            try:
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'max' WHERE (server_type = 'standard' OR server_type = 'other' OR server_type IS NULL) AND url LIKE '%max-tg.com%'"))
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'fast' WHERE (server_type = 'standard' OR server_type = 'other' OR server_type IS NULL) AND url LIKE '%fast-tg.com%'"))
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'lion' WHERE (server_type = 'standard' OR server_type = 'other' OR server_type IS NULL) AND (url LIKE '%TG-Lion.net%' OR url LIKE '%tg-lion%')"))
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'spider' WHERE (server_type = 'standard' OR server_type = 'other' OR server_type IS NULL) AND url LIKE '%spider-service.com%'"))
                # If still standard/other, leave as 'other' to avoid 'Standard' label
                await conn.execute(sqlalchemy.text("UPDATE api_servers SET server_type = 'other' WHERE server_type = 'standard'"))
                logger.info("Successfully migrated legacy server types based on URLs.")
            except Exception as e:
                logger.warning(f"Failed to migrate server types: {e}")

            # Load extra admin IDs from database into memory
            try:
                from database.models import AppSetting
                from sqlalchemy import select
                import config
                import os
                
                # 1. Load Store Extra Admins
                extra_admins_obj = (await conn.execute(select(AppSetting).where(AppSetting.key == "extra_admin_ids"))).scalar_one_or_none()
                if extra_admins_obj and extra_admins_obj.value:
                    extra_str = extra_admins_obj.value
                    for eid in extra_str.split(","):
                        if eid.strip().isdigit():
                            parsed_id = int(eid.strip())
                            if parsed_id not in config.STORE_ADMIN_IDS:
                                config.STORE_ADMIN_IDS.append(parsed_id)
                                
                # 2. Load Sourcing Extra Admins
                sourcing_admins_obj = (await conn.execute(select(AppSetting).where(AppSetting.key == "sourcing_extra_admin_ids"))).scalar_one_or_none()
                if sourcing_admins_obj and sourcing_admins_obj.value:
                    sourcing_str = sourcing_admins_obj.value
                    for eid in sourcing_str.split(","):
                        if eid.strip().isdigit():
                            parsed_id = int(eid.strip())
                            if parsed_id not in config.SOURCING_ADMIN_IDS:
                                config.SOURCING_ADMIN_IDS.append(parsed_id)
                                
                logger.info(f"Loaded extra admins. Active STORE_ADMIN_IDS: {config.STORE_ADMIN_IDS} | Active SOURCING_ADMIN_IDS: {config.SOURCING_ADMIN_IDS}")
            except Exception as e:
                logger.warning(f"Failed to load extra admins: {e}")
                    
        logger.info("DB migration check complete.")
    except Exception as e:
        logger.warning(f"Migration warning: {e}")

# Use absolute path for templates to avoid issues in deployment
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Models for API requests
class StockLoginStart(AdminAuthRequest):
    phone: str

class StockLoginComplete(AdminAuthRequest):
    phone: str
    code: str
    hash: str
    password: str = None
    country: str
    price: float

class BalanceUpdate(AdminAuthRequest):
    user_id_target: int
    amount: float
    type: str = "store" # "store" or "sourcing"

class BanToggle(AdminAuthRequest):
    user_id_target: int
    bot_type: str # "store" or "sourcing"
    banned: bool

class PriceUpdate(AdminAuthRequest):
    country_code: str
    country_name: str
    iso_code: str = "XX"
    price: float
    buy_price: float
    approve_delay: int

class UserPriceCreate(AdminAuthRequest):
    id: int | None = None
    user_id_target: int # Changed from user_id to avoid conflict with admin user_id
    country_code: str
    iso_code: str = "XX"
    buy_price: float
    approve_delay: int = 0

class UserStorePriceCreate(AdminAuthRequest):
    id: int | None = None
    user_id_target: int # Changed to avoid conflict
    country_code: str
    iso_code: str = "XX"
    sell_price: float

class StoreBuy(BaseModel):
    user_id: int
    country: str
    server_id: int | None = None
    init_data: str # Added for security verification

class UserSync(AdminAuthRequest):
    user_id_target: int
    bot_type: str # "store" or "sourcing"

@app.get("/admin/sourcing", response_class=HTMLResponse)
async def admin_sourcing(request: Request):
    try:
        import config
        return templates.TemplateResponse(request=request, name="admin_sourcing.html", context={"ADMIN_IDS": config.SOURCING_ADMIN_IDS})
    except Exception as e:
        logger.error(f"Error rendering sourcing dashboard: {e}")
        return HTMLResponse(content=f"<h1>Error</h1><pre>{e}</pre>", status_code=500)

@app.get("/admin/store", response_class=HTMLResponse)
async def admin_store(request: Request):
    try:
        import config
        return templates.TemplateResponse(request=request, name="admin_store.html", context={"ADMIN_IDS": config.STORE_ADMIN_IDS})
    except Exception as e:
        logger.error(f"Error rendering store dashboard: {e}")
    return templates.TemplateResponse(request=request, name="admin_store.html", context={"ADMIN_IDS": []})

@app.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/store", response_class=HTMLResponse)
async def store_page(request: Request):
    return templates.TemplateResponse(request=request, name="store.html")

@app.get("/api/store/data")
async def get_store_data(user_id: int = None, init_data: str = None):
    try:
        if user_id and init_data:
            from config import BOT_TOKEN
            if not verify_telegram_auth(init_data, BOT_TOKEN, user_id):
                raise HTTPException(status_code=401, detail="Unauthorized identity")
        
        async with async_session() as session:
            # CHECK MAINTENANCE MODE FIRST (Admins bypass)
            mnt_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "STORE_UNDER_MAINTENANCE"))).scalar_one_or_none()
            maintenance_mode = (mnt_obj.value.lower() == "true") if mnt_obj else False
            
            from config import ADMIN_IDS
            # Support & Channel settings
            support_username = (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none()
            updates_channel = (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none()

            if maintenance_mode and user_id not in ADMIN_IDS:
                return {
                    "maintenance_store": True,
                    "support_username": support_username.value if support_username else "",
                    "updates_channel": updates_channel.value if updates_channel else ""
                }

            if user_id:
                user = await session.get(User, user_id)
                if user and user.is_banned_store and user_id not in ADMIN_IDS:
                    return {
                        "is_banned": True,
                        "support_username": support_username.value if support_username else "",
                        "updates_channel": updates_channel.value if updates_channel else ""
                    }

            # 0. Global Settings
            local_enabled_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "local_server_enabled"))).scalar_one_or_none()
            local_enabled = (local_enabled_obj.value.lower() == "true") if local_enabled_obj else True

            # 1. Local Stock
            countries_map = {}
            local_results = []
            if local_enabled:
                stmt = select(Account.country, func.count(Account.id).label('cnt')).where(
                    Account.status == AccountStatus.AVAILABLE,
                    Account.server_id == None
                ).group_by(Account.country)
                
                local_results = (await session.execute(stmt)).all()
                logger.info(f"Local results: {len(local_results)} countries")
                
                for row in local_results:
                    raw_country, count = row
                    # Resolve real name and flag for local countries
                    res_name, res_flag, res_iso = resolve_country_info(raw_country)
                    # Use resolved name if it's not "Code X", otherwise keep raw_country
                    display_name = res_name if "Code " not in res_name else raw_country
                    
                    map_key = f"{display_name}|__local__"
                    countries_map[map_key] = {
                        "name": display_name, 
                        "flag": res_flag,
                        "iso": res_iso,
                        "count": count, 
                        "server_id": None, 
                        "server_name": "Server 1"
                    }

            server_names = []
            if local_enabled and len(local_results) > 0:
                server_names.append("Server 1")
                
            # 2. External Stock
            active_servers = (await session.execute(select(ApiServer).where(ApiServer.is_active == True))).scalars().all()
            for srv in active_servers:
                server_names.append(srv.name)
                
            logger.info(f"Active external servers: {len(active_servers)}")
            for srv in active_servers:
                try:
                    logger.info(f"Processing server: {srv.name} ({srv.url})")
                    provider = ExternalProvider(
                        srv.name, srv.url, srv.api_key, srv.profit_margin,
                        min_profit=getattr(srv, 'min_profit', 0.0),
                        server_type=getattr(srv, 'server_type', 'standard'),
                        extra_id=getattr(srv, 'extra_id', None)
                    )
                    srv_countries = await provider.get_countries()
                    
                    if not srv_countries:
                        logger.warning(f"Server {srv.name} returned no data.")
                        continue

                    # Handle common error formats in responses
                    if isinstance(srv_countries, dict) and srv_countries.get("status") in ["error", "fail"]:
                        logger.error(f"Server {srv.name} API Error: {srv_countries.get('message')}")
                        continue

                    # Normalize srv_countries to a list of dicts
                    countries_list = normalize_provider_countries(srv_countries)
                    
                    for c in countries_list:
                        raw_name = c.get("name") or c.get("country") or c.get("country_name") or c.get("country_code")
                        if not raw_name: continue
                        
                        # Use 'code' field (ISO Alpha-2) if available for accurate resolution
                        iso_from_data = c.get("code") or c.get("iso") or c.get("country_code")
                        if iso_from_data and len(str(iso_from_data).strip()) == 2:
                            resolved_name, resolved_flag, resolved_iso = resolve_country_info(str(iso_from_data).strip())
                        else:
                            resolved_name, resolved_flag, resolved_iso = resolve_country_info(str(raw_name))
                        
                        # Use resolved name if good, otherwise use raw name but clean emoji flags
                        if resolved_name and "Code " not in resolved_name:
                            name = resolved_name
                        else:
                            # Clean emoji flags from raw name to avoid duplication
                            import re as _re
                            name = _re.sub(r'[\U0001F1E0-\U0001F1FF]{2}', '', str(raw_name)).strip()
                            if not name: name = str(raw_name)
                        
                        try:
                            # Support all common quantity field names: count, qty, stock, quantity
                            # Default to 999 if missing or 0, to match dictionary parser behavior for "unlimited" or unknown stock
                            raw_count = c.get("count", c.get("qty", c.get("stock", c.get("quantity"))))
                            if raw_count is None:
                                count = 999
                            else:
                                try: count = int(raw_count)
                                except: count = 999
                            
                            # Support multiple price keys: price, rate, cost, amount, value
                            raw_p = c.get("price", c.get("rate", c.get("cost", c.get("amount", c.get("value", 0)))))
                            def clean_p(v):
                                if isinstance(v, (int, float)): return float(v)
                                try: return float(str(v).replace('$', '').replace('USD', '').strip().split()[0])
                                except: return 0.0
                            
                            p_price = clean_p(raw_p)
                            if count <= 0: continue
                            
                            map_key = f"{name}|{srv.name}"
                            if map_key not in countries_map:
                                countries_map[map_key] = {
                                    "name": name,
                                    "flag": resolved_flag,
                                    "iso": resolved_iso,
                                    "count": count,
                                    "server_id": srv.id,
                                    "server_name": srv.name,
                                    "p_price": p_price,
                                    "calc_price": provider.calculate_price(p_price)
                                }
                            else:
                                countries_map[map_key]["count"] += count
                        except Exception as parse_err:
                            logger.warning(f"[{srv.name}] Failed to parse entry: {c} — {parse_err}")
                            continue

                except Exception as srv_err:
                    logger.error(f"Error processing server {srv.name}: {srv_err}")
                    continue

            # 3. Final Assembly with Metadata & Pricing
            countries = []
            
            # Pre-fetch all pricing data to avoid N+1 queries
            # Pre-fetch all pricing data
            all_cp = (await session.execute(select(CountryPrice))).scalars().all()
            # Map by name, ISO, and country code for better matching
            cp_name_map = {cp.country_name: cp for cp in all_cp}
            cp_iso_map = {cp.iso_code: cp for cp in all_cp if cp.iso_code and cp.iso_code != 'XX'}
            cp_code_map = {cp.country_code.strip().replace('+', ''): cp for cp in all_cp}
            
            all_usp = []
            if user_id:
                all_usp = (await session.execute(select(UserStorePrice).where(UserStorePrice.user_id == user_id))).scalars().all()
            usp_map = {usp.country_code: usp for usp in all_usp}

            for map_key, c_data in countries_map.items():
                name = c_data["name"]
                flag = c_data.get("flag", "🌐")
                is_local = (c_data.get("server_id") is None)
                
                # 1. Determine Base Price
                if is_local:
                    # Default local price if not in DB
                    price = 1.0
                else:
                    # External: API Cost + Profit Margin
                    price = c_data.get("calc_price", 1.0)
                
                # 2. Apply CountryPrice Overrides
                # Try match by ISO first (most accurate), then by name, then by calling code
                cp = cp_iso_map.get(c_data.get("iso")) or cp_name_map.get(name) or cp_code_map.get(str(name).strip().replace('+', ''))
                
                if cp:
                    # Always use flag from DB if available (for both local and external)
                    flag = get_flag_emoji(cp.iso_code)
                    
                    # Price Override Logic:
                    if is_local:
                        # Local stock: ALWAYS use the price from the Selling Prices table
                        price = cp.price
                    # External stock: We IGNORE the Selling Prices table for price overrides,
                    # as per user request ("this page should control local inventory only").
                    # It will use the 'price' calculated above (API Cost + Profit Margin).
                
                # 3. User-Specific Price Override (highest priority)
                is_sp = False
                if is_local and cp:
                    is_sp = True

                if user_id and is_local:
                    # Match logic for UserStorePrice:
                    # 1. By ISO code (most accurate)
                    # 2. By Name
                    # 3. By Country Code (if available)
                    
                    usp = None
                    iso_key = c_data.get("iso")
                    if iso_key and iso_key != 'XX':
                        # Try to find a USP that has this ISO
                        usp = next((u for u in all_usp if u.iso_code == iso_key), None)
                    
                    if not usp:
                        # Try matching by name
                        usp = next((u for u in all_usp if u.country_code == name), None)
                    
                    if not usp and is_local:
                        # For local items, we might have the code from the CP entry
                        if cp:
                            cc_clean = cp.country_code.strip().replace('+', '')
                            usp = next((u for u in all_usp if u.country_code == cc_clean or u.country_code == f"+{cc_clean}"), None)

                    if usp:
                        price = usp.sell_price
                        is_sp = True

                if price > 0:
                    countries.append({
                        "name": name,
                        "flag": flag,
                        "iso": c_data.get("iso", "XX"),
                        "buy_price": price,
                        "count": c_data["count"],
                        "server_id": c_data.get("server_id"),
                        "server_name": c_data.get("server_name", "Server 1"),
                        "is_selling_price": is_sp
                    })
            
            # Sort by count (descending) and then name (ascending)
            countries.sort(key=lambda x: (-x["count"], x["name"]))
            
            # User balance & Stats
            balance = 0.0
            total_orders = 0
            total_spent = 0.0
            total_deposits = 0
            completed_orders = 0
            active_orders = 0
            unique_countries = 0
            referral_count = 0
            referral_earnings = 0.0
            if user_id:
                user = await session.get(User, user_id)
                if user:
                    balance = user.balance_store
                    total_orders = (await session.execute(select(func.count(Account.id)).where(Account.buyer_id == user_id))).scalar() or 0
                    
                    spent_val = (await session.execute(
                        select(func.sum(Transaction.amount)).where(
                            Transaction.user_id == user_id,
                            Transaction.type == TransactionType.BUY
                        )
                    )).scalar() or 0.0
                    total_spent = abs(float(spent_val))

                    # Personalized stats
                    total_deposits = (await session.execute(select(func.count(Deposit.id)).where(Deposit.user_id == user_id))).scalar() or 0
                    completed_orders = (await session.execute(
                        select(func.count(Account.id)).where(Account.buyer_id == user_id, Account.otp_code != None)
                    )).scalar() or 0
                    active_orders = (await session.execute(
                        select(func.count(Account.id)).where(Account.buyer_id == user_id, Account.otp_code == None)
                    )).scalar() or 0
                    unique_countries = (await session.execute(
                        select(func.count(func.distinct(Account.country))).where(Account.buyer_id == user_id)
                    )).scalar() or 0
                    
                    referral_count = (await session.execute(
                        select(func.count(User.id)).where(User.referred_by == user_id)
                    )).scalar() or 0
                    referral_earnings = user.referral_earnings or 0.0

            # Calculate Stats
            total_numbers = sum(c['count'] for c in countries)
            countries_count = len(set(c['name'] for c in countries))
            lowest_price = min((c['buy_price'] for c in countries), default=0.0)

            # Fetch bot name and username
            bot_name = "Numbers Store"
            bot_username = "BotUsername"
            try:
                from config import BOT_TOKEN
                import urllib.request
                import json
                def fetch_name():
                    try:
                        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
                        with urllib.request.urlopen(req, timeout=2) as r:
                            res_data = json.loads(r.read().decode())
                            if res_data.get("ok"):
                                return res_data["result"].get("first_name", "Numbers Store"), res_data["result"].get("username", "BotUsername")
                    except: return "Numbers Store", "BotUsername"
                bot_name, bot_username = await asyncio.to_thread(fetch_name)
            except: pass

            # Fetch Deposit Addresses
            from config import DEPOSIT_ADDRESS
            addr_keys = ["BINANCE_PAY_ID", "TRX_ADDRESS", "USDT_BEP20_ADDRESS"]
            addr_settings = {}
            for k in addr_keys:
                obj = (await session.execute(select(AppSetting).where(AppSetting.key == k))).scalar_one_or_none()
                addr_settings[k] = obj.value if obj and obj.value else ""

            final_binance_pay = addr_settings.get("BINANCE_PAY_ID") or DEPOSIT_ADDRESS
            final_trx = addr_settings.get("TRX_ADDRESS") or ""
            final_usdt_bep20 = addr_settings.get("USDT_BEP20_ADDRESS") or ""

            # Fetch Referral Settings
            ref_bonus_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "referral_join_bonus"))).scalar_one_or_none()
            ref_comm_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "referral_commission_percent"))).scalar_one_or_none()
            
            ref_bonus = float(ref_bonus_obj.value) if ref_bonus_obj and ref_bonus_obj.value else 0.005
            ref_comm = float(ref_comm_obj.value) if ref_comm_obj and ref_comm_obj.value else 1.0

        return {
            "maintenance_mode": False,
            "bot_name": bot_name,
            "bot_username": bot_username,
            "countries": countries,
            "servers": server_names,
            "referral_join_bonus": ref_bonus,
            "referral_commission_percent": ref_comm,
            "user": {
                "balance": balance,
                "total_orders": total_orders,
                "total_spent": total_spent,
                "total_deposits": total_deposits,
                "completed_orders": completed_orders,
                "active_orders": active_orders,
                "unique_countries": unique_countries,
                "referral_count": referral_count,
                "referral_earnings": referral_earnings
            },
            "stats": {
                "total_numbers": total_numbers,
                "countries_count": countries_count,
                "lowest_price": lowest_price
            },
            "deposit_methods": {
                "binance_pay": final_binance_pay,
                "trx_trc20": final_trx,
                "usdt_bep20": final_usdt_bep20
            },
            "support_username": support_username.value if support_username else "",
            "updates_channel": updates_channel.value if updates_channel else ""
        }
    except Exception as e:
        logger.error(f"Store Data Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/store/buy")
async def store_buy(data: StoreBuy):
    logger.info(f"Store Buy Request: user_id={data.user_id}, country={data.country}, server_id={data.server_id}")
    try:
        async with async_session() as session:
            # 1. AUTH VERIFICATION
            from config import BOT_TOKEN
            if not verify_telegram_auth(data.init_data, BOT_TOKEN, data.user_id):
                raise HTTPException(status_code=401, detail="Unauthorized: Telegram identity verification failed.")

            # Secure User Fetch with Row Locking
            user = await session.get(User, data.user_id, with_for_update=True)
            if not user: raise HTTPException(status_code=404, detail="User not found")
            
            # 0. Local Server Toggle
            local_enabled_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "local_server_enabled"))).scalar_one_or_none()
            local_enabled = (local_enabled_obj.value.lower() == "true") if local_enabled_obj else True

            # 1. Local Stock Check
            account = None
            if local_enabled and not data.server_id:
                stmt = select(Account).where(
                    Account.country == data.country, 
                    Account.status == AccountStatus.AVAILABLE,
                    Account.server_id == None
                ).limit(1)
                account = (await session.execute(stmt)).scalar_one_or_none()
            
            # 1. Price determination
            cp = (await session.execute(select(CountryPrice).where(CountryPrice.country_name == data.country))).scalar()
            
            # Initial default
            final_price = 1.0

            target_srv = None
            external_country_code = None
            is_local = False
            
            if account:
                is_local = True
                final_price = cp.price if cp else 1.0
            else:
                # 2. Try External Servers
                # If server_id is provided, only look at that server. Otherwise, check all active.
                if data.server_id:
                    active_servers = (await session.execute(select(ApiServer).where(ApiServer.id == data.server_id, ApiServer.is_active == True))).scalars().all()
                else:
                    active_servers = (await session.execute(select(ApiServer).where(ApiServer.is_active == True))).scalars().all()
                
                last_error = "Out of stock"
                
                for srv in active_servers:
                    provider = ExternalProvider(
                        srv.name, srv.url, srv.api_key, srv.profit_margin,
                        min_profit=getattr(srv, 'min_profit', 0.0),
                        server_type=getattr(srv, 'server_type', 'standard'),
                        extra_id=getattr(srv, 'extra_id', None)
                    )
                    srv_countries = await provider.get_countries()
                    if not srv_countries: continue
                    
                    # Use helper for normalization
                    countries_list = normalize_provider_countries(srv_countries)

                    def get_c(item):
                        rc = item.get("count", item.get("qty", item.get("stock", item.get("quantity"))))
                        try: return int(rc) if rc is not None else 999
                        except: return 999

                    server_matched = False
                    for c in countries_list:
                        if get_c(c) <= 0: continue
                        
                        raw_c = c.get("name") or c.get("country") or c.get("country_name") or c.get("country_code")
                        iso_hint = c.get("code") or c.get("iso") or c.get("country_code")
                        
                        # Resolve name for comparison
                        res_name, _, _ = resolve_country_info(str(iso_hint if (iso_hint and len(str(iso_hint))==2) else raw_c))
                        
                        if res_name == data.country or raw_c == data.country:
                            # Match found! Attempt to buy from THIS server
                            external_country_code = c.get("country")
                            cost_price = float(c.get("price", 0))
                            final_price = provider.calculate_price(cost_price)
                            
                            # Check user balance before attempting
                            if user.balance_store < final_price:
                                raise HTTPException(status_code=400, detail="Insufficient balance")

                            buy_res = await provider.buy_number(external_country_code)
                            if buy_res.get("status") == "success":
                                # SUCCESS! Record and return
                                user.balance_store -= final_price
                                new_acc = Account(
                                    phone_number=buy_res.get("number"),
                                    country=data.country,
                                    status=AccountStatus.SOLD,
                                    price=final_price,
                                    locked_buy_price=cost_price,
                                    buyer_id=user.id,
                                    purchased_at=datetime.utcnow(),
                                    server_id=srv.id,
                                    hash_code=buy_res.get("hash_code")
                                )
                                session.add(new_acc)
                                txn = Transaction(user_id=user.id, type=TransactionType.BUY, amount=-final_price)
                                session.add(txn)
                                await session.commit()
                                return {"status": "success", "phone": new_acc.phone_number, "id": new_acc.id}
                            else:
                                last_error = str(buy_res.get("message", "API provider error"))
                                logger.warning(f"Purchase failed on {srv.name}: {last_error}. Trying next server...")
                                server_matched = True # We matched the country but purchase failed
                                break # Try next server
                    # End of country loop
                
                # If we reach here, all active servers failed to provide the number
                msg_lower = last_error.lower()
                if any(word in msg_lower for word in ["balance", "رصيد", "money", "fund", "credit", "insufficient"]):
                    raise HTTPException(status_code=400, detail="Out of stock")
                else:
                    raise HTTPException(status_code=400, detail=last_error)

            # 3. Handle Local Purchase Execution
            if is_local and account:
                # Resolve Personalized Pricing
                from database.models import UserStorePrice
                _, _, res_iso = resolve_country_info(data.country)
                
                async with async_session() as inner_session:
                    stmt = select(UserStorePrice).where(UserStorePrice.user_id == data.user_id)
                    user_prices = (await inner_session.execute(stmt)).scalars().all()
                    usp = None
                    if res_iso and res_iso != 'XX':
                        usp = next((u for u in user_prices if u.iso_code == res_iso), None)
                    if not usp:
                        usp = next((u for u in user_prices if u.country_code == data.country), None)
                    if not usp and cp:
                        cc_clean = cp.country_code.strip().replace('+', '')
                        usp = next((u for u in user_prices if u.country_code == cc_clean or u.country_code == f"+{cc_clean}"), None)
                    if usp:
                        final_price = usp.sell_price
                
                if final_price <= 0:
                    raise HTTPException(status_code=400, detail="This country is currently unavailable (Price not set)")
                
                if user.balance_store < final_price:
                    raise HTTPException(status_code=400, detail="Insufficient balance")

                # Execute Local Purchase
                user.balance_store -= final_price
                account.status = AccountStatus.SOLD
                account.buyer_id = user.id
                account.otp_code = None
                account.purchased_at = datetime.utcnow()
                account.price = final_price
                txn = Transaction(user_id=user.id, type=TransactionType.BUY, amount=-final_price)
                session.add(txn)
                await session.commit()
                
                # Background cleaning: Reset authorizations and remove 2FA
                from services.session_manager import clean_account_for_buyer
                asyncio.create_task(clean_account_for_buyer(account.session_string, account.two_fa_password))
                
                return {"status": "success", "phone": account.phone_number, "id": account.id}
            
            # If we are here and not returned, it means nothing was found or bought
            raise HTTPException(status_code=400, detail="Out of stock")
    except HTTPException as e: raise e
    except Exception as e:
        logger.error(f"Store Buy Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/store/get-code")
async def store_get_code(user_id: int, phone: str, init_data: str):
    from services.session_manager import get_telegram_login_code
    try:
        from config import BOT_TOKEN
        if not verify_telegram_auth(init_data, BOT_TOKEN, user_id):
            raise HTTPException(status_code=401, detail="Unauthorized")

        async with async_session() as session:
            stmt = select(Account).where(Account.phone_number == phone, Account.buyer_id == user_id)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if not account: raise HTTPException(status_code=404, detail="Account not found")
            
            if account.otp_code:
                return {"status": "success", "code": account.otp_code}
            
            if account.server_id:
                # 1. Fetch from external server
                srv = await session.get(ApiServer, account.server_id)
                if not srv: raise HTTPException(status_code=500, detail="Server config missing")
                provider = ExternalProvider(
                    srv.name, srv.url, srv.api_key, srv.profit_margin,
                    min_profit=getattr(srv, 'min_profit', 0.0),
                    server_type=getattr(srv, 'server_type', 'standard'),
                    extra_id=getattr(srv, 'extra_id', None)
                )
                code_res = await provider.get_code(account.hash_code, number=account.phone_number)
                if code_res.get("status") == "success":
                    code = code_res.get("code")
                    account.otp_code = code
                    await session.commit()
                    await send_purchase_log(user_id, account.country, account.price, account.phone_number, code, password=code_res.get("password"))
                    return {"status": "success", "code": code}
                return {"status": "pending", "message": code_res.get("message", "بانتظار وصول الكود...")}
            else:
                # 2. Local session logic
                code = await get_telegram_login_code(
                    account.session_string, 
                    after_ts=account.purchased_at.timestamp() if account.purchased_at else None
                )
                if code:
                    account.otp_code = code
                    await session.commit()
                    await send_purchase_log(user_id, account.country, account.price, account.phone_number, code, password=account.two_fa_password)
                    
                    # Schedule bot to log out after 10 mins so the buyer is truly alone
                    from services.session_manager import logout_bot_session
                    asyncio.create_task(logout_bot_session(account.session_string, delay=600))
                    
                    return {"status": "success", "code": code}
                return {"status": "pending", "message": "Code not found yet"}
    except Exception as e:
        logger.error(f"Get Code Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/store/history")
async def get_store_history(user_id: int, init_data: str, page: int = 1, limit: int = 10):
    try:
        from config import BOT_TOKEN
        if not verify_telegram_auth(init_data, BOT_TOKEN, user_id):
            return {"orders": [], "total_pages": 0, "current_page": 1, "total_count": 0}

        async with async_session() as session:
            # Count total
            total_count = (await session.execute(
                select(func.count(Account.id)).where(Account.buyer_id == user_id)
            )).scalar() or 0
            
            total_pages = (total_count + limit - 1) // limit
            
            stmt = select(Account).where(Account.buyer_id == user_id).order_by(Account.id.desc()).offset((page - 1) * limit).limit(limit)
            results = (await session.execute(stmt)).scalars().all()
            
            history = []
            for a in results:
                # Resolve flag
                flag = "🌐"
                try:
                    cp = (await session.execute(select(CountryPrice).where(CountryPrice.country_name == a.country))).scalar()
                    if cp:
                        flag = get_flag_emoji(cp.iso_code)
                except: pass
                
                history.append({
                    "phone": a.phone_number,
                    "country": a.country,
                    "flag": flag,
                    "price": a.price,
                    "status": a.status.name if hasattr(a.status, 'name') else str(a.status),
                    "date": a.purchased_at.isoformat() if a.purchased_at else (a.created_at.isoformat() if a.created_at else None),
                    "otp_code": a.otp_code,
                    "password": a.two_fa_password
                })
            return {
                "orders": history,
                "total_pages": total_pages,
                "current_page": page,
                "total_count": total_count
            }
    except Exception as e:
        logger.error(f"Store History Error: {e}")
        return {"orders": [], "total_pages": 0, "current_page": 1, "total_count": 0}

@app.get("/api/store/deposits")
async def get_deposit_history(user_id: int, init_data: str, page: int = 1, limit: int = 10):
    try:
        from config import BOT_TOKEN
        if not verify_telegram_auth(init_data, BOT_TOKEN, user_id):
            return {"deposits": [], "total_pages": 0, "current_page": 1, "total_count": 0}

        async with async_session() as session:
            total_count = (await session.execute(
                select(func.count(Deposit.id)).where(Deposit.user_id == user_id)
            )).scalar() or 0
            
            total_pages = (total_count + limit - 1) // limit
            
            stmt = select(Deposit).where(Deposit.user_id == user_id).order_by(Deposit.id.desc()).offset((page - 1) * limit).limit(limit)
            results = (await session.execute(stmt)).scalars().all()
            
            deposits = []
            for d in results:
                deposits.append({
                    "txid": d.txid,
                    "amount": d.amount,
                    "method": d.method or "Binance Pay",
                    "date": d.created_at.isoformat() if d.created_at else None
                })
            return {
                "deposits": deposits,
                "total_pages": total_pages,
                "current_page": page,
                "total_count": total_count
            }
    except Exception as e:
        logger.error(f"Deposit History Error: {e}")
        return {"deposits": [], "total_pages": 0, "current_page": 1, "total_count": 0}

def format_usd(amount: float) -> str:
    """Format USD amount: 3 decimal places if the 3rd is non-zero, otherwise 2."""
    s3 = f"{amount:.3f}"
    if s3[-1] == '0':
        return f"{amount:.2f}"
    return s3

async def get_binance_price(coin: str):
    """Fetch current price of a coin in USDT. Falls back to CoinGecko if Binance fails."""
    coin_upper = coin.upper()
    if coin_upper == "USDT":
        return 1.0

    # --- 1. Try Binance first ---
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={coin_upper}USDT"
        response = await asyncio.to_thread(requests.get, url, timeout=5)
        if response.status_code == 200:
            price = float(response.json().get("price", 0))
            if price > 0:
                return price
    except Exception as e:
        logger.warning(f"Binance price fetch failed for {coin}: {e}")

    # --- 2. Fallback: CoinGecko ---
    # Map common symbols to CoinGecko IDs
    coingecko_ids = {
        "TRX": "tron",
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "BNB": "binancecoin",
        "SOL": "solana",
        "XRP": "ripple",
        "ADA": "cardano",
        "DOGE": "dogecoin",
        "MATIC": "matic-network",
        "LTC": "litecoin",
        "TON": "the-open-network",
    }
    cg_id = coingecko_ids.get(coin_upper, coin.lower())
    try:
        cg_url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
        cg_response = await asyncio.to_thread(requests.get, cg_url, timeout=8)
        if cg_response.status_code == 200:
            cg_data = cg_response.json()
            price = cg_data.get(cg_id, {}).get("usd", 0)
            if price and float(price) > 0:
                logger.info(f"CoinGecko fallback price for {coin}: ${price}")
                return float(price)
    except Exception as e:
        logger.warning(f"CoinGecko price fetch failed for {coin}: {e}")

    return 0

async def check_binance_pay_transaction(txid: str, api_key: str, api_secret: str):
    """Verify a Binance Pay transaction."""
    if not api_key or not api_secret:
        return False, "Binance API keys not configured", 0
        
    api_key = api_key.strip()
    api_secret = api_secret.strip()
    
    base_url = "https://api.binance.com"
    
    # Sync time
    try:
        time_res = await asyncio.to_thread(requests.get, f"{base_url}/api/v3/time", timeout=5)
        server_time = time_res.json().get("serverTime")
        timestamp = server_time if server_time else int(time.time() * 1000)
    except:
        timestamp = int(time.time() * 1000)

    endpoint = "/sapi/v1/pay/transactions"
    params = {
        "timestamp": timestamp,
        "recvWindow": 60000
    }
    
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(
        api_secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    headers = {"X-MBX-APIKEY": api_key}
    url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
    
    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers)
        data = response.json()
        
        if response.status_code == 200:
            if data.get("code") == "000000" and "data" in data:
                transactions = data.get("data", [])
                txid_clean = txid.strip().lower()
                logger.info(f"Binance Pay API checked. Retrieved {len(transactions)} transactions.")
                
                for tx in transactions:
                    # Sift through all values in the transaction to find matching txid
                    match_found = False
                    
                    # 1. Direct check of common fields
                    for k in ("orderId", "transactionId", "id", "prepayId"):
                        val = tx.get(k)
                        if val and str(val).strip().lower() == txid_clean:
                            match_found = True
                            break
                            
                    # 2. Deep recursive check fallback
                    if not match_found:
                        def search_dict(d):
                            for key, val in d.items():
                                if isinstance(val, dict):
                                    if search_dict(val):
                                        return True
                                elif isinstance(val, list):
                                    for item in val:
                                        if isinstance(item, dict) and search_dict(item):
                                            return True
                                        elif str(item).strip().lower() == txid_clean:
                                            return True
                                elif str(val).strip().lower() == txid_clean:
                                    return True
                            return False
                        match_found = search_dict(tx)
                        
                    if match_found:
                        status = tx.get("status")
                        is_status_ok = True
                        if status is not None:
                            status_str = str(status).upper()
                            if status_str not in ("SUCCESS", "1", "COMPLETED", "SUCCESSFUL"):
                                is_status_ok = False
                                
                        if is_status_ok:
                            amount = float(tx.get("amount", 0))
                            currency = tx.get("currency", "USDT")
                            
                            # Check time (24h)
                            tx_time = tx.get("transactionTime") or tx.get("time") or 0
                            current_time = int(time.time() * 1000)
                            if tx_time > 0 and (current_time - tx_time) > (24 * 60 * 60 * 1000):
                                return False, "Transaction is too old.", 0
                                
                            if currency.upper() != "USDT":
                                price = await get_binance_price(currency)
                                if price <= 0: return False, f"Price error for {currency}", 0
                                amount = amount * price
                                
                            return True, "Success", amount
                
                return False, "Transaction not found in Binance Pay history.", 0
            else:
                return False, f"Binance Pay API Error: {data.get('msg', 'Unknown')}", 0
        else:
            # If 403/400, maybe permission missing
            return False, f"Binance Pay API Error (Status {response.status_code}): {data.get('msg', 'Permission denied or invalid request')}", 0
    except Exception as e:
        return False, f"Pay Request error: {str(e)}", 0

async def check_binance_deposit(txid: str, api_key: str, api_secret: str):
    if not api_key or not api_secret:
        return False, "Binance API keys not configured", 0
        
    api_key = api_key.strip()
    api_secret = api_secret.strip()
    
    if "*" in api_secret or "Already Set" in api_secret:
        return False, "Invalid API Secret format in database.", 0
        
    base_url = "https://api.binance.com"
    
    # Sync time with Binance Server to avoid clock drift issues
    try:
        time_res = await asyncio.to_thread(requests.get, f"{base_url}/api/v3/time", timeout=5)
        server_time = time_res.json().get("serverTime")
        timestamp = server_time if server_time else int(time.time() * 1000)
    except:
        timestamp = int(time.time() * 1000)

    endpoint = "/sapi/v1/capital/deposit/hisrec"
    
    params = {
        "txId": txid.strip(),
        "recvWindow": 60000,
        "timestamp": timestamp
    }
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    
    signature = hmac.new(
        api_secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    headers = {"X-MBX-APIKEY": api_key}
    url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
    
    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers)
        data = response.json()
        if response.status_code == 200:
            if isinstance(data, list) and len(data) > 0:
                for record in data:
                    if record.get("txId") == txid:
                        status = record.get("status")
                        coin = record.get("coin", "USDT")
                        amount = float(record.get("amount", 0))
                        
                        if status == 1: # Success
                            # SECURITY: Check if transaction is too old (e.g., older than 24 hours)
                            # Binance 'insertTime' or 'updatedTime' is in ms
                            tx_time_ms = record.get("insertTime") or record.get("updatedTime") or 0
                            current_time_ms = int(time.time() * 1000)
                            
                            # 24 hours in milliseconds = 24 * 60 * 60 * 1000
                            if tx_time_ms > 0 and (current_time_ms - tx_time_ms) > (24 * 60 * 60 * 1000):
                                return False, "Transaction is too old. Only deposits from the last 24 hours are accepted.", 0

                            # Conversion Logic
                            if coin.upper() != "USDT":
                                price = await get_binance_price(coin)
                                if price <= 0:
                                    return False, f"Could not determine price for {coin}. Please contact admin.", 0
                                final_usd_amount = amount * price
                                return True, f"Success: {amount} {coin} converted to ${format_usd(final_usd_amount)}", final_usd_amount
                            else:
                                return True, "Success", amount
                        else:
                            return False, f"Deposit pending (status: {status}). Please wait.", 0
                
                # If not found in deposit history, try Pay history as fallback
                return await check_binance_pay_transaction(txid, api_key, api_secret)
            else:
                # If list is empty, try Pay history
                return await check_binance_pay_transaction(txid, api_key, api_secret)
        else:
            # If error, try Pay history as fallback if it's a 400/403 which might mean txId param was rejected or hisrec is not for this ID
            is_valid_pay, msg_pay, amt_pay = await check_binance_pay_transaction(txid, api_key, api_secret)
            if is_valid_pay: return is_valid_pay, msg_pay, amt_pay
            
            return False, f"Binance error: {data.get('msg', 'Unknown')}", 0
    except Exception as e:
        # Fallback to Pay check on any error
        try:
            is_valid_pay, msg_pay, amt_pay = await check_binance_pay_transaction(txid, api_key, api_secret)
            if is_valid_pay: return is_valid_pay, msg_pay, amt_pay
        except: pass
        return False, f"Request error: {str(e)}", 0

@app.post("/api/store/deposit/verify")
async def store_deposit_verify(req: DepositSubmit):
    try:
        txid = req.txid.strip()
        if not txid:
            return {"status": "error", "message": "TxID is empty"}
            
        async with async_session() as session:
            # Fetch Binance credentials from DB
            from config import BINANCE_API_KEY, BINANCE_API_SECRET
            
            key_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "BINANCE_API_KEY"))).scalar_one_or_none()
            sec_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "BINANCE_API_SECRET"))).scalar_one_or_none()
            
            final_key = key_obj.value if key_obj and key_obj.value else BINANCE_API_KEY
            final_sec = sec_obj.value if sec_obj and sec_obj.value else BINANCE_API_SECRET

            # Check if this txid was already processed
            existing = (await session.execute(select(Deposit).where(Deposit.txid == txid))).scalar_one_or_none()
            if existing:
                return {"status": "error", "message": "Transaction verification failed. Please check the ID or contact support."}
                
            # Verification Logic (With Test Bypass)
            is_valid, msg, amount = False, "Invalid", 0
            
            if txid.startswith("TEST-") and txid.endswith("USD"):
                try:
                    amount = float(txid.replace("TEST-", "").replace("USD", ""))
                    is_valid, msg = True, "Test Success"
                except:
                    # Fallback to normal check if parsing fails
                    if req.method == "Binance Pay":
                        is_valid, msg, amount = await check_binance_pay_transaction(txid, final_key, final_sec)
                        if not is_valid: # Try deposit history too
                            is_valid, msg, amount = await check_binance_deposit(txid, final_key, final_sec)
                    else:
                        is_valid, msg, amount = await check_binance_deposit(txid, final_key, final_sec)
            else:
                # Verify with Binance API
                if req.method == "Binance Pay":
                    is_valid, msg, amount = await check_binance_pay_transaction(txid, final_key, final_sec)
                    if not is_valid: # Try deposit history too (sometimes people enter TxID for Pay)
                        is_valid, msg, amount = await check_binance_deposit(txid, final_key, final_sec)
                else:
                    is_valid, msg, amount = await check_binance_deposit(txid, final_key, final_sec)

            if not is_valid:
                return {"status": "error", "message": f"Verification failed: {msg}"}
                
            # Update user balance
            user = (await session.execute(select(User).where(User.id == req.user_id))).scalar_one_or_none()
            if not user:
                return {"status": "error", "message": "User not found."}
                
            user.balance_store += amount
            
            # Save deposit
            new_deposit = Deposit(user_id=user.id, amount=amount, txid=txid, method=req.method)
            session.add(new_deposit)
            
            # Also log as a Transaction
            tx = Transaction(user_id=user.id, type=TransactionType.DEPOSIT, amount=amount)
            session.add(tx)
            
            # Referral Deposit Bonus
            if user.referred_by:
                referrer = (await session.execute(select(User).where(User.id == user.referred_by))).scalar_one_or_none()
                if referrer:
                    # Fetch Dynamic Commission %
                    comm_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "referral_commission_percent"))).scalar_one_or_none()
                    comm_percent = float(comm_obj.value) if comm_obj and comm_obj.value else 1.0
                    
                    bonus = amount * (comm_percent / 100.0)
                    referrer.balance_store += bonus
                    referrer.referral_earnings = (referrer.referral_earnings or 0.0) + bonus
                    tx_ref = Transaction(user_id=referrer.id, type=TransactionType.REFERRAL, amount=bonus)
                    session.add(tx_ref)
                    
                    # Commission added silently — no notification sent to referrer
            
            await session.commit()
            
            # Send notification via Bot
            try:
                # 1. Notify User (Disabled as per user request)
                # bot_buyer = app.state.bot_buyer
                # if bot_buyer:
                #     await bot_buyer.send_message(
                #         chat_id=user.id,
                #         text=f"✅ **تم الإيداع بنجاح!**\n\n💰 المبلغ: **${amount}**\n🔖 رقم المعاملة: `{txid}`\nرصيدك الحالي: **${user.balance_store:.2f}**",
                #         parse_mode="Markdown"
                #     )
                
                # 2. Notify Admin Channel
                log_ch_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "deposit_log_channel_id"))).scalar_one_or_none()
                if log_ch_obj and log_ch_obj.value:
                    from config import BOT_TOKEN
                    import aiogram
                    temp_bot = aiogram.Bot(token=BOT_TOKEN)
                    log_text = (
                        f"<b>• Received New Deposit .</b>\n\n"
                        f"<b>• User ID :- {user.id} 👤.</b>\n"
                        f"<b>• Amount: ${format_usd(amount)} 💵.</b>\n\n"
                        f"<b>• Method: {req.method} 💳.</b>\n"
                        f"<b>• Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 📅.</b>\n\n"
                        f"<b>• Transaction: {txid} 🔖</b>."
                    )
                    await temp_bot.send_message(chat_id=log_ch_obj.value, text=log_text, parse_mode="HTML")
                    await temp_bot.session.close()
            except Exception as notify_err:
                logger.error(f"Deposit Notification Error: {notify_err}")
            
            return {"status": "success", "message": f"Successfully deposited ${format_usd(amount)}", "new_balance": user.balance_store}
            
    except Exception as e:
        logger.error(f"Deposit Verify Error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/admin/sourcing/data")
async def get_sourcing_data(user_id: int, init_data: str):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        async with async_session() as session:
            total_sourced = (await session.execute(select(func.count(Account.id)))).scalar() or 0
            pending_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.PENDING))).scalar() or 0
            accepted_sourced = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.AVAILABLE))).scalar() or 0
            available_count = accepted_sourced
            sold_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.SOLD))).scalar() or 0
            rejected_sourced = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.REJECTED))).scalar() or 0
            frozen_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.REJECTED, or_(Account.reject_reason.ilike("%frozen%"), Account.reject_reason.ilike("%banned%"), Account.reject_reason.ilike("%تجميد%"), Account.reject_reason.ilike("%محظور%"), Account.reject_reason.ilike("%باند%")), Account.reject_reason.ilike("%REVOKED%") == False))).scalar() or 0
            spam_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.REJECTED, or_(Account.reject_reason.ilike("%spam%"), Account.reject_reason.ilike("%restricted%"), Account.reject_reason.ilike("%سبام%"), Account.reject_reason.ilike("%مقيد%"), Account.reject_reason.ilike("%محدود%"))))).scalar() or 0
            
            # Withdrawal stats
            withdraw_pending = (await session.execute(select(func.count(WithdrawalRequest.id)).where(WithdrawalRequest.status == WithdrawalStatus.PENDING))).scalar() or 0
            withdraw_approved = (await session.execute(select(func.count(WithdrawalRequest.id)).where(WithdrawalRequest.status == WithdrawalStatus.APPROVED))).scalar() or 0
            withdraw_rejected = (await session.execute(select(func.count(WithdrawalRequest.id)).where(WithdrawalStatus.REJECTED == WithdrawalRequest.status))).scalar() or 0
            total_paid_amount = (await session.execute(select(func.sum(WithdrawalRequest.amount)).where(WithdrawalRequest.status == WithdrawalStatus.APPROVED))).scalar() or 0
            withdraw_pending_amount = (await session.execute(select(func.sum(WithdrawalRequest.amount)).where(WithdrawalRequest.status == WithdrawalStatus.PENDING))).scalar() or 0
            
            # User stats
            total_users = (await session.execute(select(func.count(User.id)).where(User.is_active_sourcing == True))).scalar() or 0
            banned_users = (await session.execute(select(func.count(User.id)).where(User.is_active_sourcing == True, User.is_banned_sourcing == True))).scalar() or 0
            active_users = total_users - banned_users
            
            # Custom User Prices stats (Unique Users & Countries)
            from sqlalchemy import distinct
            total_custom_prices = (await session.execute(select(func.count(distinct(UserCountryPrice.user_id))))).scalar() or 0
            total_custom_countries = (await session.execute(select(func.count(distinct(UserCountryPrice.iso_code))))).scalar() or 0
            
            recent_result = await session.execute(
                select(Account).order_by(Account.id.desc()).limit(50)
            )
            recent = []
            for a in recent_result.scalars().all():
                flag = "🌐"
                try:
                    p = phonenumbers.parse(a.phone_number)
                    flag = get_flag_emoji(phonenumbers.region_code_for_number(p))
                except: pass
                
                # Fetch actual buy_price from CountryPrice
                actual_buy_price = 0
                try:
                    parsed = phonenumbers.parse(a.phone_number)
                    cc = str(parsed.country_code)
                    target_iso = phonenumbers.region_code_for_number(parsed)
                    
                    stmt = select(CountryPrice).where(
                        CountryPrice.country_code == cc,
                        CountryPrice.iso_code == target_iso
                    )
                    cp_row = (await session.execute(stmt)).scalar()
                    if not cp_row:
                         cp_row = (await session.execute(select(CountryPrice).where(CountryPrice.country_code == cc))).scalar()
                         
                    if cp_row:
                        actual_buy_price = cp_row.buy_price
                except: pass
                recent.append({
                    "phone": a.phone_number,
                    "country": f"{flag} {a.country}",
                    "buy_price": actual_buy_price,
                    "status": a.status.name,
                    "seller_id": a.seller_id,
                    "date": a.created_at.isoformat() if a.created_at else None
                })

            prices_result = await session.execute(
                select(CountryPrice).where(CountryPrice.buy_price > 0).order_by(CountryPrice.updated_at.desc())
            )
            prices = []
            for p in prices_result.scalars().all():
                iso = getattr(p, 'iso_code', None) or 'XX'
                flag = get_flag_emoji(iso)
                prices.append({
                    "code": p.country_code, 
                    "iso": iso,
                    "name": f"{flag} {clean_display_name(p.country_name)}", 
                    "buy_price": p.buy_price,
                    "price": p.price,
                    "approve_delay": p.approve_delay,
                    "log_quantity": getattr(p, 'log_quantity', 1000)
                })

            # Bot-specific user count and balance
            # Priority: AppSetting > Telegram
            bot_name = "Bot"
            try:
                bn_stmt = select(AppSetting).where(AppSetting.key == "bot_name")
                bn_res = await session.execute(bn_stmt)
                bn_obj = bn_res.scalar_one_or_none()
                if bn_obj:
                    bot_name = bn_obj.value
                else:
                    from config import SELLER_BOT_TOKEN
                    def fetch_bot_name():
                        try:
                            req = urllib.request.Request(f"https://api.telegram.org/bot{SELLER_BOT_TOKEN}/getMe")
                            with urllib.request.urlopen(req, timeout=2) as r:
                                res_data = json.loads(r.read().decode())
                                if res_data.get("ok"):
                                    return res_data["result"].get("first_name", "Bot")
                        except: return "Bot"
                    bot_name = await asyncio.to_thread(fetch_bot_name)
            except Exception as b_err:
                logger.error(f"Error fetching sourcing bot name: {b_err}")

            user_count = (await session.execute(select(func.count(User.id)).where(User.is_active_sourcing == True))).scalar() or 0
            total_sourcing_balance = (await session.execute(select(func.sum(User.balance_sourcing)).where(User.is_active_sourcing == True))).scalar() or 0.0

            users_result = await session.execute(select(User).where(User.is_active_sourcing == True).order_by(User.join_date.desc()).limit(200))
            db_users = users_result.scalars().all()
            
            # Get seller stats for these users
            u_ids = [u.id for u in db_users]
            seller_stats = {uid: {"sold": 0, "accepted": 0, "rejected": 0} for uid in u_ids}
            if u_ids:
                sold_stmt = select(Account.seller_id, func.count(Account.id)).where(Account.seller_id.in_(u_ids), Account.status == AccountStatus.SOLD).group_by(Account.seller_id)
                acc_stmt = select(Account.seller_id, func.count(Account.id)).where(Account.seller_id.in_(u_ids), Account.status == AccountStatus.AVAILABLE).group_by(Account.seller_id)
                rej_stmt = select(Account.seller_id, func.count(Account.id)).where(Account.seller_id.in_(u_ids), Account.status == AccountStatus.REJECTED).group_by(Account.seller_id)
                
                for rid, cnt in (await session.execute(sold_stmt)).all(): seller_stats[rid]["sold"] = cnt
                for rid, cnt in (await session.execute(acc_stmt)).all(): seller_stats[rid]["accepted"] = cnt
                for rid, cnt in (await session.execute(rej_stmt)).all(): seller_stats[rid]["rejected"] = cnt

            users_list = []
            for u in db_users:
                users_list.append({
                    "id": u.id,
                    "full_name": u.full_name or "N/A",
                    "username": f"@{u.username}" if u.username else "N/A",
                    "balance_sourcing": round(u.balance_sourcing or 0.0, 3),
                    "join_date": u.join_date.strftime("%Y-%m-%d") if u.join_date else "N/A",
                    "banned": u.is_banned_sourcing,
                    "sold_count": seller_stats[u.id]["sold"],
                    "accepted_count": seller_stats[u.id]["accepted"],
                    "rejected_count": seller_stats[u.id]["rejected"]
                })

            # Sourcing settings
            settings_stmt = select(AppSetting).where(AppSetting.key.in_(["sourcing_log_channel_id", "sourcing_join_log_channel_id", "min_withdraw_trx", "min_withdraw_usdt", "fee_withdraw_trx", "fee_withdraw_usdt", "sourcing_extra_admin_ids"]))
            settings_res = await session.execute(settings_stmt)
            settings_dict = {s.key: s.value for s in settings_res.scalars().all()}
            
            sourcing_log_channel_id = settings_dict.get("sourcing_log_channel_id", "")
            sourcing_join_log_channel_id = settings_dict.get("sourcing_join_log_channel_id", "")
            min_withdraw_trx = settings_dict.get("min_withdraw_trx", "4.0")
            min_withdraw_usdt = settings_dict.get("min_withdraw_usdt", "10.0")
            fee_withdraw_trx = settings_dict.get("fee_withdraw_trx", "0.2")
            fee_withdraw_usdt = settings_dict.get("fee_withdraw_usdt", "0.2")
            sourcing_extra_admin_ids = settings_dict.get("sourcing_extra_admin_ids", "")

            # Support & Channel settings
            support_username_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none()
            updates_channel_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none()
            support_username = support_username_obj.value if support_username_obj else ""
            updates_channel = updates_channel_obj.value if updates_channel_obj else ""

            return {
                "bot_name": bot_name,
                "sourcing_log_channel_id": sourcing_log_channel_id,
                "sourcing_join_log_channel_id": sourcing_join_log_channel_id,
                "support_username": support_username,
                "updates_channel": updates_channel,
                "extra_admin_ids": sourcing_extra_admin_ids,
                "min_withdraw_trx": min_withdraw_trx,
                "min_withdraw_usdt": min_withdraw_usdt,
                "fee_withdraw_trx": fee_withdraw_trx,
                "fee_withdraw_usdt": fee_withdraw_usdt,
                "stats": {
                    "total_sourced": total_sourced, 
                    "pending_count": pending_count,
                    "available_count": available_count,
                    "sold_count": sold_count,
                    "accepted_sourced": accepted_sourced,
                    "rejected_sourced": rejected_sourced,
                    "frozen_count": frozen_count, # force
                    "spam_count": spam_count, # force
                    "total_balance": round(total_sourcing_balance, 3),
                    "user_count": user_count,
                    "withdraw_pending": withdraw_pending,
                    "withdraw_approved": withdraw_approved,
                    "withdraw_rejected": withdraw_rejected,
                    "withdraw_pending_amount": float(withdraw_pending_amount),
                    "total_paid_amount": float(total_paid_amount),
                    "total_users": total_users,
                    "active_users": active_users,
                    "banned_users": banned_users,
                    "total_custom_prices": total_custom_prices,
                    "total_custom_countries": total_custom_countries
                },
                "recent": recent,
                "prices": prices,
                "users": users_list,
                "support_username": (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none().value if (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none() else "",
                "updates_channel": (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none().value if (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none() else ""
            }
    except Exception as e:
        logger.error(f"Sourcing Data Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/store/data")
async def get_admin_store_data(user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        async with async_session() as session:
            # Bot-specific user count and balance
            # Priority: AppSetting > Telegram
            bot_name = "Bot"
            try:
                bn_stmt = select(AppSetting).where(AppSetting.key == "bot_name")
                bn_obj = (await session.execute(bn_stmt)).scalar_one_or_none()
                if bn_obj:
                    bot_name = bn_obj.value
                
                log_ch_stmt = select(AppSetting).where(AppSetting.key == "purchase_log_channel_id")
                log_ch_obj = (await session.execute(log_ch_stmt)).scalar_one_or_none()
                purchase_log_channel_id = log_ch_obj.value if log_ch_obj else ""

                # Support & Channel settings
                support_username_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none()
                updates_channel_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none()
                support_username = support_username_obj.value if support_username_obj else ""
                updates_channel = updates_channel_obj.value if updates_channel_obj else ""
                
                dep_log_ch_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "deposit_log_channel_id"))).scalar_one_or_none()
                deposit_log_channel_id = dep_log_ch_obj.value if dep_log_ch_obj else ""
                
                store_join_log_ch_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "store_join_log_channel_id"))).scalar_one_or_none()
                store_join_log_channel_id = store_join_log_ch_obj.value if store_join_log_ch_obj else ""
                
                extra_admins_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "extra_admin_ids"))).scalar_one_or_none()
                store_extra_admin_ids = extra_admins_obj.value if extra_admins_obj else ""
                
                if not bn_obj:
                    from config import BOT_TOKEN
                    def fetch_bot_name_store():
                        try:
                            req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
                            with urllib.request.urlopen(req, timeout=2) as r:
                                res_data = json.loads(r.read().decode())
                                if res_data.get("ok"):
                                    return res_data["result"].get("first_name", "Bot")
                        except: return "Bot"
                    bot_name = await asyncio.to_thread(fetch_bot_name_store)
            except Exception as b_err:
                logger.error(f"Error fetching store bot name: {b_err}")

            user_count = (await session.execute(select(func.count(User.id)).where(User.is_active_store == True))).scalar() or 0
            banned_users = (await session.execute(select(func.count(User.id)).where(User.is_active_store == True, User.is_banned_store == True))).scalar() or 0
            active_users = user_count - banned_users
            stock_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.AVAILABLE))).scalar() or 0
            total_balance = (await session.execute(select(func.sum(User.balance_store)).where(User.is_active_store == True))).scalar() or 0.0

            # Sales stats
            total_sales_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.SOLD))).scalar() or 0
            total_revenue = (await session.execute(select(func.sum(Account.price)).where(Account.status == AccountStatus.SOLD))).scalar() or 0.0

            # Deposit stats
            total_deposit_requests = (await session.execute(select(func.count(Deposit.id)))).scalar() or 0
            total_deposits_amount = (await session.execute(select(func.sum(Deposit.amount)).where(Deposit.id != None))).scalar() or 0.0

            # Price stats
            active_countries_count = (await session.execute(select(func.count(CountryPrice.id)).where(CountryPrice.price > 0))).scalar() or 0
            min_price = (await session.execute(select(func.min(CountryPrice.price)).where(CountryPrice.price > 0))).scalar() or 0.0
            max_price = (await session.execute(select(func.max(CountryPrice.price)).where(CountryPrice.price > 0))).scalar() or 0.0

            # Custom User stats
            from sqlalchemy import distinct
            total_custom_users = (await session.execute(select(func.count(distinct(UserStorePrice.user_id))))).scalar() or 0
            total_custom_countries = (await session.execute(select(func.count(distinct(UserStorePrice.iso_code))))).scalar() or 0

            users_result = await session.execute(select(User).where(User.is_active_store == True).order_by(User.join_date.desc()).limit(200))
            all_users_raw = users_result.scalars().all()
            u_ids = [u.id for u in all_users_raw]

            # Optimized bulk stats for users
            bought_stats = {uid: 0 for uid in u_ids}
            spent_stats = {uid: 0.0 for uid in u_ids}

            if u_ids:
                # Count bought numbers per user
                b_stmt = select(Account.buyer_id, func.count(Account.id)).where(Account.buyer_id.in_(u_ids)).group_by(Account.buyer_id)
                for rid, cnt in (await session.execute(b_stmt)).all(): bought_stats[rid] = cnt

                # Sum spent amount per user
                s_stmt = select(Transaction.user_id, func.sum(Transaction.amount)).where(
                    Transaction.user_id.in_(u_ids),
                    Transaction.type == TransactionType.BUY
                ).group_by(Transaction.user_id)
                for rid, val in (await session.execute(s_stmt)).all(): spent_stats[rid] = abs(float(val or 0))

                # Count referrals per user
                r_stmt = select(User.referred_by, func.count(User.id)).where(User.referred_by.in_(u_ids)).group_by(User.referred_by)
                referral_stats = {uid: 0 for uid in u_ids}
                for rid, cnt in (await session.execute(r_stmt)).all(): referral_stats[rid] = cnt
            else:
                referral_stats = {}

            users = [
                {
                    "id": u.id,
                    "full_name": u.full_name or "N/A",
                    "username": f"@{u.username}" if u.username else "N/A",
                    "balance_store": round(u.balance_store or 0.0, 3),
                    "balance_sourcing": round(u.balance_sourcing or 0.0, 3),
                    "join_date": u.join_date.strftime("%Y-%m-%d") if u.join_date else "N/A",
                    "banned": u.is_banned_store,
                    "purchased_count": bought_stats[u.id],
                    "total_spent": round(spent_stats[u.id], 3),
                    "referrals_count": referral_stats.get(u.id, 0),
                }
                for u in all_users_raw
            ]
            
            tx_result = await session.execute(
                select(Account)
                .where(Account.status == AccountStatus.SOLD)
                .order_by(Account.id.desc())
                .limit(50)
            )
            transactions = []
            for acc in tx_result.scalars().all():
                flag = "🌐"
                try:
                    p = phonenumbers.parse(acc.phone_number)
                    flag = get_flag_emoji(phonenumbers.region_code_for_number(p))
                except: pass
                transactions.append({
                    "buyer_id": acc.buyer_id, 
                    "price": acc.price, 
                    "phone": acc.phone_number,
                    "password": acc.two_fa_password,
                    "country": f"{flag} {acc.country}",
                    "date": acc.purchased_at.isoformat() if acc.purchased_at else None
                })

            # Fetch all prices for store panel
            prices_result = await session.execute(
                select(CountryPrice).where(CountryPrice.price > 0).order_by(CountryPrice.updated_at.desc())
            )
            prices = []
            for p in prices_result.scalars().all():
                iso = getattr(p, 'iso_code', None) or 'XX'
                flag = get_flag_emoji(iso)
                prices.append({
                    "code": p.country_code,
                    "iso": iso,
                    "name": f"{flag} {clean_display_name(p.country_name)}",
                    "price": p.price
                })

        return {
            "bot_name": bot_name,
            "purchase_log_channel_id": purchase_log_channel_id,
            "deposit_log_channel_id": deposit_log_channel_id,
            "store_join_log_channel_id": store_join_log_channel_id,
            "support_username": support_username,
            "updates_channel": updates_channel,
            "extra_admin_ids": store_extra_admin_ids,
            "stats": {
                "user_count": user_count,
                "banned_users": banned_users,
                "active_users": active_users,
                "stock_count": stock_count,
                "total_balance": total_balance,
                "total_sales_count": total_sales_count,
                "total_revenue": total_revenue,
                "total_deposit_requests": total_deposit_requests,
                "total_deposits_amount": total_deposits_amount,
                "active_countries_count": active_countries_count,
                "total_custom_users": total_custom_users,
                "total_custom_countries": total_custom_countries,
                "min_price": min_price,
                "max_price": max_price
            },
            "users": users,
            "transactions": transactions,
            "prices": prices
        }
    except Exception as e:
        logger.error(f"Store Admin Data Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/store/sales")
async def get_admin_store_sales(
    user_id: int, 
    init_data: str,
    page: int = 1, 
    limit: int = 10,
    search: str = None
):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    try:
        async with async_session() as session:
            offset = (page - 1) * limit
            base_stmt = select(Account).where(Account.status == AccountStatus.SOLD)
            
            if search and search.strip():
                s = f"%{search.strip()}%"
                base_stmt = base_stmt.where(
                    or_(
                        Account.phone_number.ilike(s),
                        cast(Account.buyer_id, String).ilike(s),
                        Account.country.ilike(s)
                    )
                )
                
            total_count = (await session.execute(
                select(func.count()).select_from(base_stmt.subquery())
            )).scalar() or 0
            total_pages = math.ceil(total_count / limit) if total_count > 0 else 1
            
            stmt = base_stmt.order_by(Account.purchased_at.desc()).offset(offset).limit(limit)
            results = (await session.execute(stmt)).scalars().all()
            
            sales = []
            import phonenumbers
            for acc in results:
                flag = "🌐"
                try:
                    p = phonenumbers.parse(acc.phone_number)
                    flag = get_flag_emoji(phonenumbers.region_code_for_number(p))
                except: pass
                sales.append({
                    "buyer_id": acc.buyer_id, 
                    "price": acc.price, 
                    "cost": acc.locked_buy_price or 0,
                    "phone": acc.phone_number,
                    "password": acc.two_fa_password,
                    "country": f"{flag} {acc.country}",
                    "date": acc.purchased_at.isoformat() if acc.purchased_at else None,
                    "server_id": acc.server_id
                })
            
            # --- Calculate Server Stats ---
            from database.models import ApiServer
            stats_list = []
            
            # External Servers Stats
            server_stats_stmt = select(
                ApiServer.name,
                func.count(Account.id).label('total_sales'),
                func.sum(Account.price).label('total_revenue'),
                func.sum(Account.locked_buy_price).label('total_cost')
            ).join(
                ApiServer, Account.server_id == ApiServer.id
            ).where(
                Account.status == AccountStatus.SOLD,
                Account.server_id.isnot(None)
            ).group_by(ApiServer.name)
            
            stats_result = (await session.execute(server_stats_stmt)).all()
            for row in stats_result:
                revenue = row[2] or 0
                cost = row[3] or 0
                stats_list.append({
                    "server_name": row[0],
                    "total_sales": row[1],
                    "total_revenue": round(revenue, 3),
                    "total_cost": round(cost, 3),
                    "net_profit": round(revenue - cost, 3)
                })
                
            # Local App Stats
            local_stats_stmt = select(
                func.count(Account.id).label('total_sales'),
                func.sum(Account.price).label('total_revenue'),
                func.sum(Account.locked_buy_price).label('total_cost')
            ).where(
                Account.status == AccountStatus.SOLD,
                Account.server_id.is_(None)
            )
            local_row = (await session.execute(local_stats_stmt)).first()
            if local_row and local_row[0] > 0:
                revenue = local_row[1] or 0
                cost = local_row[2] or 0
                stats_list.append({
                    "server_name": "Local App",
                    "total_sales": local_row[0],
                    "total_revenue": round(revenue, 3),
                    "total_cost": round(cost, 3),
                    "net_profit": round(revenue - cost, 3)
                })

            # --- Top Countries Per Server ---
            top_c_stmt = select(
                Account.server_id,
                Account.country,
                func.count(Account.id).label('count')
            ).where(
                Account.status == AccountStatus.SOLD
            ).group_by(Account.server_id, Account.country).order_by(Account.server_id, func.count(Account.id).desc())
            
            c_res = (await session.execute(top_c_stmt)).all()
            
            server_names = {}
            for row in (await session.execute(select(ApiServer.id, ApiServer.name))).all():
                server_names[row[0]] = row[1]
                
            grouped_countries = {}
            for sid, country, count in c_res:
                sname = server_names.get(sid, "Local App") if sid is not None else "Local App"
                if sname not in grouped_countries:
                    grouped_countries[sname] = []
                if len(grouped_countries[sname]) < 4:
                    grouped_countries[sname].append({"country": country, "count": count})
            
            top_countries = [{"server_name": k, "countries": v} for k, v in grouped_countries.items()]

            return {
                "sales": sales,
                "stats": stats_list,
                "top_countries": top_countries,
                "total_pages": total_pages,
                "current_page": page
            }
    except Exception as e:
        logger.error(f"Store Sales Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/store/general-settings")
async def save_general_settings(req: GeneralSettingsSubmit):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(req.init_data, req.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        async with async_session() as session:
            updates = {
                "bot_name": req.bot_name.strip(),
                "purchase_log_channel_id": req.purchase_log_channel_id.strip(),
                "deposit_log_channel_id": req.deposit_log_channel_id.strip() if hasattr(req, 'deposit_log_channel_id') else ""
            }
            for k, v in updates.items():
                obj = (await session.execute(select(AppSetting).where(AppSetting.key == k))).scalar_one_or_none()
                if obj:
                    obj.value = v
                else:
                    session.add(AppSetting(key=k, value=v))
            await session.commit()
            return {"status": "success"}
    except Exception as e:
        logger.error(f"Save General Settings Error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/admin/store/settings")
async def get_store_settings(user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS, DEPOSIT_ADDRESS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        async with async_session() as session:
            keys = [
                "BINANCE_API_KEY", "BINANCE_API_SECRET", 
                "BINANCE_PAY_ID", "TRX_ADDRESS", "USDT_BEP20_ADDRESS",
                "referral_join_bonus", "referral_commission_percent"
            ]
            settings = {}
            for k in keys:
                obj = (await session.execute(select(AppSetting).where(AppSetting.key == k))).scalar_one_or_none()
                settings[k] = obj.value if obj else ""
            
            # Fallbacks
            from config import BINANCE_API_KEY as B_KEY, BINANCE_API_SECRET as B_SEC
            api_key = settings.get("BINANCE_API_KEY") or B_KEY
            api_secret = settings.get("BINANCE_API_SECRET") or B_SEC
            
            # Return a placeholder for the secret so the user knows it is set but cannot see it
            masked_secret = "Already Set (Leave empty to keep current)" if api_secret else ""

            return {
                "binance_api_key": api_key,
                "binance_api_secret_masked": masked_secret,
                "binance_pay_id": settings.get("BINANCE_PAY_ID") or DEPOSIT_ADDRESS,
                "trx_address": settings.get("TRX_ADDRESS") or "",
                "usdt_bep20_address": settings.get("USDT_BEP20_ADDRESS") or "",
                "referral_join_bonus": settings.get("referral_join_bonus") or "0.005",
                "referral_commission_percent": settings.get("referral_commission_percent") or "1"
            }
    except Exception as e:
        logger.error(f"Get Store Settings Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/store/settings")
async def save_store_settings(req: StoreSettingsSubmit):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(req.init_data, req.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        async with async_session() as session:
            updates = {
                "BINANCE_API_KEY": req.binance_api_key.strip(),
                "BINANCE_PAY_ID": req.binance_pay_id.strip(),
                "TRX_ADDRESS": req.trx_address.strip(),
                "USDT_BEP20_ADDRESS": req.usdt_bep20_address.strip()
            }
            if req.binance_api_secret and "Already Set" not in req.binance_api_secret:
                updates["BINANCE_API_SECRET"] = req.binance_api_secret.strip()

            for k, v in updates.items():
                obj = (await session.execute(select(AppSetting).where(AppSetting.key == k))).scalar_one_or_none()
                if obj:
                    obj.value = v
                else:
                    new_setting = AppSetting(key=k, value=v)
                    session.add(new_setting)
            
            await session.commit()
            return {"status": "success", "message": "Settings saved successfully"}
    except Exception as e:
        logger.error(f"Save Store Settings Error: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/admin/store/referral-settings")
async def save_referral_settings(req: ReferralSettingsSubmit):
    if not verify_admin_auth_multi(req.init_data, req.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        async with async_session() as session:
            updates = {
                "referral_join_bonus": str(req.join_bonus),
                "referral_commission_percent": str(req.commission_percent)
            }
            for k, v in updates.items():
                obj = (await session.execute(select(AppSetting).where(AppSetting.key == k))).scalar_one_or_none()
                if obj:
                    obj.value = v
                else:
                    session.add(AppSetting(key=k, value=v))
            await session.commit()
            return {"status": "success", "message": "Referral settings saved successfully"}
    except Exception as e:
        logger.error(f"Save Referral Settings Error: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/admin/support/settings")
async def save_support_settings(data: dict):
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        async with async_session() as session:
            allowed_keys = [
                "SUPPORT_USERNAME", "UPDATES_CHANNEL", "PURCHASE_LOG_CHANNEL_ID",
                "SOURCING_LOG_CHANNEL_ID", "purchase_log_channel_id", "sourcing_log_channel_id",
                "deposit_log_channel_id", "store_join_log_channel_id", "sourcing_join_log_channel_id",
                "extra_admin_ids", "sourcing_extra_admin_ids"
            ]
            for k, v in data.items():
                if k in ["user_id", "init_data"]: continue
                if k not in allowed_keys: continue
                obj = (await session.execute(select(AppSetting).where(AppSetting.key == k))).scalar_one_or_none()
                if obj:
                    obj.value = v.strip() if isinstance(v, str) else str(v)
                else:
                    session.add(AppSetting(key=k, value=v.strip() if isinstance(v, str) else str(v)))
            await session.commit()

            import config, os
            base_admins = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

            # Update STORE_ADMIN_IDS if extra_admin_ids changed
            if "extra_admin_ids" in data:
                config.STORE_ADMIN_IDS.clear()
                config.STORE_ADMIN_IDS.extend(base_admins)
                val = data.get("extra_admin_ids", "")
                if val and isinstance(val, str):
                    for eid in val.split(","):
                        if eid.strip().isdigit():
                            pid = int(eid.strip())
                            if pid not in config.STORE_ADMIN_IDS:
                                config.STORE_ADMIN_IDS.append(pid)
                logger.info(f"Updated STORE_ADMIN_IDS: {config.STORE_ADMIN_IDS}")

            # Update SOURCING_ADMIN_IDS if sourcing_extra_admin_ids changed
            if "sourcing_extra_admin_ids" in data:
                config.SOURCING_ADMIN_IDS.clear()
                config.SOURCING_ADMIN_IDS.extend(base_admins)
                val = data.get("sourcing_extra_admin_ids", "")
                if val and isinstance(val, str):
                    for eid in val.split(","):
                        if eid.strip().isdigit():
                            pid = int(eid.strip())
                            if pid not in config.SOURCING_ADMIN_IDS:
                                config.SOURCING_ADMIN_IDS.append(pid)
                logger.info(f"Updated SOURCING_ADMIN_IDS: {config.SOURCING_ADMIN_IDS}")

            return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/admin/system/maintenance")
async def get_maintenance(user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    async with async_session() as session:
        mnt_store = (await session.execute(select(AppSetting).where(AppSetting.key == "STORE_UNDER_MAINTENANCE"))).scalar_one_or_none()
        mnt_src = (await session.execute(select(AppSetting).where(AppSetting.key == "SOURCING_UNDER_MAINTENANCE"))).scalar_one_or_none()
        return {
            "store_enabled": (mnt_store.value.lower() == "true") if mnt_store else False,
            "sourcing_enabled": (mnt_src.value.lower() == "true") if mnt_src else False
        }

async def _update_maintenance(key: str, enabled: bool):
    async with async_session() as session:
        logger.info(f"[Maintenance] Updating {key} to {enabled}")
        setting = (await session.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
        if not setting:
            session.add(AppSetting(key=key, value="true" if enabled else "false"))
        else:
            setting.value = "true" if enabled else "false"
        await session.commit()
    return {"status": "success"}

@app.post("/api/admin/store/maintenance")
async def set_store_maintenance(data: MaintenanceToggle):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    return await _update_maintenance("STORE_UNDER_MAINTENANCE", data.enabled)

@app.post("/api/admin/sourcing/maintenance")
async def set_sourcing_maintenance(data: MaintenanceToggle):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    return await _update_maintenance("SOURCING_UNDER_MAINTENANCE", data.enabled)

@app.get("/api/admin/store/deposits")
async def get_store_deposits(user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import Deposit, User
    async with async_session() as session:
        result = await session.execute(
            select(Deposit, User)
            .join(User, Deposit.user_id == User.id)
            .order_by(Deposit.created_at.desc())
        )
        data = []
        for dep, user in result.all():
            data.append({
                "id": dep.id,
                "user_id": user.id,
                "user_name": user.full_name or "N/A",
                "user_handle": f"@{user.username}" if user.username else "N/A",
                "amount": dep.amount,
                "txid": dep.txid,
                "method": dep.method or "Binance Pay",
                "date": dep.created_at.isoformat() if dep.created_at else None
            })
        return {"deposits": data}


@app.get("/api/admin/store/user-prices")
async def get_store_user_prices(user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import UserStorePrice, User
    async with async_session() as session:
        result = await session.execute(
            select(UserStorePrice, User)
            .join(User, (UserStorePrice.user_id == User.id) & (User.is_active_store == True))
            .order_by(UserStorePrice.created_at.desc())
        )
        data = []
        for usp, user in result.all():
            flag = "🌐"
            name = f"Code {usp.country_code}"
            iso = usp.iso_code if usp.iso_code and usp.iso_code != 'XX' else None
            try:
                import pycountry
                if iso:
                    from web_admin import get_flag_emoji
                    flag = get_flag_emoji(iso)
                    country = pycountry.countries.get(alpha_2=iso)
                    if country:
                        name = country.name
                        import re
                        name = re.sub(r'\s*\(\?[A-Z]{2,3}\)?\s*$', '', name).strip()
                else:
                    n, f, _ = resolve_country_info(usp.country_code)
                    if n != "Unknown":
                        name = n
                        flag = f
            except: pass
            
            data.append({
                "id": usp.id,
                "user_id": user.id,
                "user_name": user.full_name or "N/A",
                "user_handle": f"@{user.username}" if user.username else "N/A",
                "country_code": usp.country_code,
                "iso_code": usp.iso_code,
                "country_name": f"{flag} {name}",
                "sell_price": usp.sell_price,
                "date": usp.created_at.isoformat() if usp.created_at else None
            })
        return {"prices": data}

@app.post("/api/admin/store/user-prices")
async def add_store_user_price(data: UserStorePriceCreate):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import UserStorePrice, User
    async with async_session() as session:
        user = await session.get(User, data.user_id_target)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
            
        if data.id:
            usp = await session.get(UserStorePrice, data.id)
            if not usp:
                raise HTTPException(status_code=404, detail="Price record not found")
            usp.sell_price = data.sell_price
            usp.country_code = data.country_code
            usp.iso_code = data.iso_code
        else:
            stmt = select(UserStorePrice).where(
                UserStorePrice.user_id == data.user_id_target,
                UserStorePrice.country_code == data.country_code,
                UserStorePrice.iso_code == data.iso_code
            )
            existing = (await session.execute(stmt)).scalar()
            if existing:
                raise HTTPException(status_code=400, detail="This country is already added for this user. Please edit the existing entry instead.")
            
            new_usp = UserStorePrice(
                user_id=data.user_id_target,
                country_code=data.country_code,
                iso_code=data.iso_code,
                sell_price=data.sell_price
            )
            session.add(new_usp)
        await session.commit()
        return {"status": "success"}

@app.delete("/api/admin/store/user-prices/{id}")
async def delete_store_user_price(id: int, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import UserStorePrice
    async with async_session() as session:
        usp = await session.get(UserStorePrice, id)
        if usp:
            await session.delete(usp)
            await session.commit()
            return {"status": "success"}
        raise HTTPException(status_code=404, detail="Not found")

@app.get("/api/admin/store/servers")
async def get_servers(user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        stmt = select(ApiServer).order_by(ApiServer.id.asc())
        servers = (await session.execute(stmt)).scalars().all()
        server_data = []
        for s in servers:
            # Fetch balance for each server
            provider = ExternalProvider(
                s.name, s.url, s.api_key, s.profit_margin,
                server_type=getattr(s, 'server_type', 'standard'),
                extra_id=getattr(s, 'extra_id', None)
            )
            bal_data = await provider.get_balance()
            balance_val = "Error"
            if isinstance(bal_data, dict):
                if bal_data.get("status") == "success":
                    balance_val = bal_data.get("balance", 0.0)
                else:
                    balance_val = bal_data.get("message", "Error")
            
            server_data.append({
                "id": s.id,
                "name": s.name,
                "url": s.url,
                "api_key": s.api_key,
                "server_type": getattr(s, 'server_type', 'standard'),
                "extra_id": getattr(s, 'extra_id', ''),
                "profit_margin": s.profit_margin,
                "min_profit": getattr(s, 'min_profit', 0.0),
                "is_active": s.is_active,
                "balance": balance_val
            })
            
        # Get Local Server Status
        local_status_raw = (await session.execute(select(AppSetting).where(AppSetting.key == "local_server_enabled"))).scalar_one_or_none()
        local_enabled = True if not local_status_raw or local_status_raw.value == "true" else False

        return {
            "servers": server_data,
            "local_server_enabled": local_enabled
        }

@app.post("/api/admin/store/servers")
async def save_server(data: ApiServerSubmit):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    logger.info(f"Saving server: {data.dict()}")
    async with async_session() as session:
        if data.id:
            srv = await session.get(ApiServer, data.id)
            if not srv: raise HTTPException(status_code=404, detail="Server not found")
            srv.name = data.name
            srv.url = data.url
            srv.api_key = data.api_key
            srv.server_type = data.server_type
            srv.extra_id = data.extra_id
            srv.profit_margin = data.profit_margin
            srv.min_profit = data.min_profit
            srv.is_active = data.is_active
        else:
            srv = ApiServer(
                name=data.name,
                url=data.url,
                api_key=data.api_key,
                server_type=data.server_type,
                extra_id=data.extra_id,
                profit_margin=data.profit_margin,
                min_profit=data.min_profit,
                is_active=data.is_active
            )
            session.add(srv)
        await session.commit()
        return {"status": "success"}

@app.delete("/api/admin/store/servers/{id}")
async def delete_server(id: int, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        srv = await session.get(ApiServer, id)
        if srv:
            await session.delete(srv)
            await session.commit()
            return {"status": "success"}
        raise HTTPException(status_code=404, detail="Not found")

@app.post("/api/admin/store/toggle-local")
async def toggle_local(data: dict):
    # data: {user_id, init_data, enabled}
    from config import BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    enabled = data.get("enabled", True)
    async with async_session() as session:
        setting = (await session.execute(select(AppSetting).where(AppSetting.key == "local_server_enabled"))).scalar_one_or_none()
        if not setting:
            setting = AppSetting(key="local_server_enabled", value="true" if enabled else "false")
            session.add(setting)
        else:
            setting.value = "true" if enabled else "false"
        await session.commit()
        return {"status": "success"}
@app.post("/api/admin/stock/start-login")
async def start_login(data: StockLoginStart):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from services.session_manager import request_app_code
    phone = data.phone
    if not phone.startswith("+"):
        phone = "+" + phone
        
    try:
        parsed = phonenumbers.parse(phone)
        country_code = str(parsed.country_code)
        iso_code = phonenumbers.region_code_for_number(parsed)
        flag = get_flag_emoji(iso_code)
        country_name = f"{flag} " + (geocoder.description_for_number(parsed, "en") or f"Code {country_code}")
    except Exception as e:
        logger.error(f"Phone Parse Error: {e}")
        raise HTTPException(status_code=400, detail="رقم هاتف غير صالح")
        
    async with async_session() as session:
        # Match by both code and ISO for accurate price lookup
        stmt = select(CountryPrice).where(
            CountryPrice.country_code == country_code,
            CountryPrice.iso_code == iso_code
        )
        cp = (await session.execute(stmt)).scalar()
        if not cp:
             # Fallback to code only
             cp = (await session.execute(select(CountryPrice).where(CountryPrice.country_code == country_code))).scalar()
        
        price = cp.price if cp else 1.0
        
    try:
        # Use -1 as a special ID for Admin Login
        code_hash = await request_app_code(-1, phone)
        return {
            "status": "success",
            "country": country_name,
            "price": price,
            "hash": code_hash
        }
    except Exception as e:
        logger.error(f"Login Start Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/stock/complete-login")
async def complete_login(data: StockLoginComplete):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from services.session_manager import submit_app_code
    try:
        # If 2FA is needed, the current session_manager doesn't handle it well in submit_app_code.
        # But for now, we'll try the simple path.
        submit_result = await submit_app_code(-1, data.phone, data.hash, data.code)
        
        if not submit_result:
            raise HTTPException(status_code=400, detail="فشل في جلب الجلسة. قد يكون الكود خطأ.")
            
        session_string = submit_result["session_string"]
        two_fa_password = submit_result["two_fa_password"]
            
        async with async_session() as session:
            new_acc = Account(
                phone_number=data.phone,
                country=data.country,
                price=data.price,
                session_string=session_string,
                two_fa_password=two_fa_password,
                status=AccountStatus.AVAILABLE,
                created_at=datetime.now()
            )
            session.add(new_acc)
            await session.commit()
            
            await check_and_alert_missing_price(data.country, data.phone, session)
            
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Login Complete Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/sourcing/price/update")
async def update_sourcing_price(data: dict):
    # data: {user_id, init_data, country_code, buy_price, approve_delay, iso_code, country_name}
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    code = data.get("country_code")
    iso = data.get("iso_code", "XX")
    buy_p = float(data.get("buy_price", 0))
    delay = int(data.get("approve_delay", 0))
    qty = int(data.get("quantity", 1000))
    c_name = data.get("country_name")

    # If iso/name not provided, auto-detect (legacy or basic add)
    if iso == "XX" or not c_name:
        name_only, _, detected_iso = resolve_country_info(code)
        if not c_name: c_name = name_only
        if iso == "XX": iso = detected_iso

    async with async_session() as session:
        # Match by both code and ISO to support shared prefixes like +1
        stmt = select(CountryPrice).where(
            CountryPrice.country_code == code,
            CountryPrice.iso_code == iso
        )
        cp = (await session.execute(stmt)).scalar()
        
        if cp:
            cp.buy_price = buy_p
            cp.approve_delay = delay
            cp.log_quantity = qty
            cp.updated_at = datetime.utcnow()
            if c_name: cp.country_name = c_name
        else:
            cp = CountryPrice(
                country_code=code,
                iso_code=iso,
                country_name=c_name, 
                price=0,
                buy_price=buy_p,
                approve_delay=delay,
                log_quantity=qty
            )
            session.add(cp)
        await session.commit()
        
        # Trigger price log in background if enabled
        if data.get("send_log", True):
            try:
                await send_sourcing_price_log(cp.country_name, cp.iso_code, cp.country_code, cp.buy_price, cp.approve_delay, cp.log_quantity)
            except Exception as log_err:
                logger.error(f"Failed to send sourcing price log: {log_err}")
            
    return {"status": "success"}



@app.get("/api/admin/sourcing/user-prices")
async def get_user_prices(user_id: int, init_data: str):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import UserCountryPrice, User
    async with async_session() as session:
        result = await session.execute(
            select(UserCountryPrice, User)
            .join(User, (UserCountryPrice.user_id == User.id) & (User.is_active_sourcing == True))
            .order_by(UserCountryPrice.created_at.desc())
        )
        data = []
        for ucp, user in result.all():
            flag = "🌐"
            name = f"Code {ucp.country_code}"
            
            # Use iso_code if available (not 'XX')
            iso = ucp.iso_code if ucp.iso_code and ucp.iso_code != 'XX' else None
            
            try:
                import pycountry
                if iso:
                    from web_admin import get_flag_emoji
                    flag = get_flag_emoji(iso)
                    country = pycountry.countries.get(alpha_2=iso)
                    if country:
                        name = country.name
                        import re
                        name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', name).strip()
                else:
                    n, f, _ = resolve_country_info(ucp.country_code)
                    if n != "Unknown":
                        name = n
                        flag = f
            except: pass
            
            data.append({
                "id": ucp.id,
                "user_id": user.id,
                "user_name": user.full_name or "N/A",
                "user_handle": f"@{user.username}" if user.username else "N/A",
                "country_code": ucp.country_code,
                "iso_code": ucp.iso_code,
                "country_name": f"{flag} {name}",
                "buy_price": ucp.buy_price,
                "approve_delay": ucp.approve_delay,
                "date": ucp.created_at.isoformat() if ucp.created_at else None
            })
        return {"prices": data}

@app.post("/api/admin/sourcing/user-prices")
async def add_user_price(data: UserPriceCreate):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import UserCountryPrice, User
    async with async_session() as session:
        user = await session.get(User, data.user_id_target)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
            
        if data.id:
            # Explicit Update
            ucp = await session.get(UserCountryPrice, data.id)
            if not ucp:
                raise HTTPException(status_code=404, detail="Price record not found")
            ucp.buy_price = data.buy_price
            ucp.approve_delay = data.approve_delay
            # If they changed the country/iso in the modal (though UI might prevent it)
            ucp.country_code = data.country_code
            ucp.iso_code = data.iso_code
        else:
            # Check for Duplicate before adding new
            stmt = select(UserCountryPrice).where(
                UserCountryPrice.user_id == data.user_id_target,
                UserCountryPrice.country_code == data.country_code,
                UserCountryPrice.iso_code == data.iso_code
            )
            existing = (await session.execute(stmt)).scalar()
            if existing:
                raise HTTPException(status_code=400, detail="This country is already added for this user. Please edit the existing entry instead.")
                
            new_ucp = UserCountryPrice(
                user_id=data.user_id_target,
                country_code=data.country_code,
                iso_code=data.iso_code,
                buy_price=data.buy_price,
                approve_delay=data.approve_delay
            )
            session.add(new_ucp)
            
        await session.commit()
        return {"status": "success"}

@app.delete("/api/admin/sourcing/user-prices/{id}")
async def delete_user_price(id: int, user_id: int, init_data: str):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from database.models import UserCountryPrice
    async with async_session() as session:
        ucp = await session.get(UserCountryPrice, id)
        if ucp:
            await session.delete(ucp)
            await session.commit()

        return {"status": "success"}

@app.delete("/api/admin/prices/delete")
async def delete_price_entry(code: str, iso: str, user_id: int, init_data: str, bot: str = "store"):
    from config import BOT_TOKEN, SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        stmt = select(CountryPrice).where(
            CountryPrice.country_code == code,
            CountryPrice.iso_code == iso
        )
        cp = (await session.execute(stmt)).scalar()
        if cp:
            if bot == "sourcing":
                cp.buy_price = 0
            else:
                cp.price = 0
            
            # If both prices are 0, we can fully delete the entry
            if cp.price == 0 and cp.buy_price == 0:
                await session.delete(cp)
        
        await session.commit()
    return {"status": "success"}

@app.post("/api/admin/prices/update")
async def update_price(data: PriceUpdate):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    """General update (mostly used by Store admin now)"""
    async with async_session() as session:
        # Identify by code and ISO
        stmt = select(CountryPrice).where(
            CountryPrice.country_code == data.country_code,
            CountryPrice.iso_code == data.iso_code
        )
        cp = (await session.execute(stmt)).scalar()
        
        if cp:
            # PARTIAL UPDATE: Only touch store price and name
            cp.price = data.price
            if data.country_name and data.country_name != "Unknown":
                cp.country_name = data.country_name
            elif not cp.country_name or cp.country_name == "Unknown":
                name, _, _ = resolve_country_info(data.country_code)
                cp.country_name = name
            
            # CRITICAL: Do NOT overwrite buy_price or approve_delay if update is from store dashboard
            # We keep whatever is currently there.
            cp.updated_at = datetime.utcnow()
        else:
            name = data.country_name
            iso = data.iso_code
            if not name or name == "Unknown" or iso == "XX":
                name_det, _, iso_det = resolve_country_info(data.country_code)
                if not name or name == "Unknown": name = name_det
                if iso == "XX": iso = iso_det
                
            cp = CountryPrice(
                country_code=data.country_code,
                iso_code=iso,
                country_name=name, 
                price=data.price,
                buy_price=0, # Initial sourcing buy price is 0
                approve_delay=0
            )
            session.add(cp)
        await session.commit()
    return {"status": "success"}

@app.delete("/api/admin/stock/delete/{acc_id}")
async def delete_stock(acc_id: int, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        acc = await session.get(Account, acc_id)
        if acc:
            await session.delete(acc)
            await session.commit()
    return {"status": "success"}

@app.post("/api/admin/user/balance")
async def update_balance(data: BalanceUpdate):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        user = await session.get(User, data.user_id_target)
        if user:
            if data.type == "sourcing":
                user.balance_sourcing = data.amount
            else:
                user.balance_store = data.amount
            await session.commit()
            return {"status": "success"}
    raise HTTPException(status_code=404, detail="User not found")

@app.post("/api/admin/user/toggle-ban")
async def toggle_ban(data: BanToggle):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        user = await session.get(User, data.user_id_target)
        if user:
            if data.bot_type == "sourcing":
                user.is_banned_sourcing = data.banned
            else:
                user.is_banned_store = data.banned
            await session.commit()
            return {"status": "success"}
    raise HTTPException(status_code=404, detail="User not found")
# --- Seller Panel APIs ---

@app.get("/api/seller/data")
async def get_seller_data(user_id: int, init_data: str, lang: str = "en"):
    try:
        from config import SELLER_BOT_TOKEN
        if not verify_user_auth_multi(init_data, user_id):
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        async with async_session() as session:
            # CHECK MAINTENANCE MODE FIRST (Admins bypass)
            mnt_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "SOURCING_UNDER_MAINTENANCE"))).scalar_one_or_none()
            maintenance_mode = (mnt_obj.value.lower() == "true") if mnt_obj else False
            
            from config import SOURCING_ADMIN_IDS
            # Support & Channel settings
            support_username = (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none()
            updates_channel = (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none()
            extra_admin_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "sourcing_extra_admin_ids"))).scalar_one_or_none()

            if maintenance_mode and user_id not in SOURCING_ADMIN_IDS:
                return {
                    "maintenance_sourcing": True,
                    "support_username": support_username.value if support_username else "",
                    "updates_channel": updates_channel.value if updates_channel else ""
                }

            if user_id:
                user = await session.get(User, user_id)
                if user and user.is_banned_sourcing and user_id not in SOURCING_ADMIN_IDS:
                    return {
                        "is_banned": True,
                        "support_username": support_username.value if support_username else "",
                        "updates_channel": updates_channel.value if updates_channel else ""
                    }

            user = await session.get(User, user_id)
            if not user:
                # Add new user without active flags
                user = User(id=user_id, balance_sourcing=0.0, balance_store=0.0)
                session.add(user)
                await session.commit()
                await session.refresh(user)
            
            # Fetch Bot Name dynamically
            bot_name = "Bot"
            try:
                from config import SELLER_BOT_TOKEN
                with urllib.request.urlopen(f"https://api.telegram.org/bot{SELLER_BOT_TOKEN}/getMe", timeout=5) as r:
                    res_data = json.loads(r.read().decode())
                    if res_data.get("ok"):
                        bot_name = res_data["result"].get("first_name", "Bot")
            except Exception as b_err:
                logger.error(f"Error fetching bot name: {b_err}")

            # Get stats
            sold_count = (await session.execute(select(func.count(Account.id)).where(Account.seller_id == user_id, Account.status == AccountStatus.SOLD))).scalar() or 0
            pending_count = (await session.execute(select(func.count(Account.id)).where(Account.seller_id == user_id, Account.status == AccountStatus.PENDING))).scalar() or 0
            accepted_count = (await session.execute(select(func.count(Account.id)).where(Account.seller_id == user_id, Account.status == AccountStatus.AVAILABLE))).scalar() or 0
            rejected_count = (await session.execute(select(func.count(Account.id)).where(Account.seller_id == user_id, Account.status == AccountStatus.REJECTED))).scalar() or 0
            
            # Calculate Pending Withdrawn — sum PENDING withdrawal requests
            pending_balance = (await session.execute(
                select(func.sum(WithdrawalRequest.amount)).where(
                    WithdrawalRequest.user_id == user_id,
                    WithdrawalRequest.status == WithdrawalStatus.PENDING
                )
            )).scalar() or 0.0
            
            # Calculate Total Withdrawn — sum only APPROVED withdrawal requests
            total_withdrawn = (await session.execute(
                select(func.sum(WithdrawalRequest.amount)).where(
                    WithdrawalRequest.user_id == user_id,
                    WithdrawalRequest.status == WithdrawalStatus.APPROVED
                )
            )).scalar() or 0.0
            
            # Get prices
            prices_result = await session.execute(select(CountryPrice).where(CountryPrice.buy_price > 0).order_by(CountryPrice.updated_at.desc()))
            prices = prices_result.scalars().all()
            
            # Get custom user prices (organized by code and ISO)
            from database.models import UserCountryPrice
            custom_prices_result = await session.execute(select(UserCountryPrice).where(UserCountryPrice.user_id == user_id))
            custom_rows = custom_prices_result.scalars().all()
            
            # Key: (country_code, iso_code)
            custom_prices = {(cp.country_code, cp.iso_code): cp.buy_price for cp in custom_rows}
            
            formatted_prices = []
            seen_codes = set()
            
            # First, add global prices, applying custom overrides if they exist
            for p in prices:
                try:
                    iso = getattr(p, 'iso_code', None) or 'XX'
                    default_name, default_flag, resolved_iso = resolve_country_info(p.country_code)
                    
                    if iso == 'XX' and resolved_iso != 'XX':
                        iso = resolved_iso
                        
                    flag = get_flag_emoji(iso)
                    name = p.country_name if p.country_name and p.country_name != "Unknown" else default_name
                    
                    # Resolve price: Specific ISO override > Generic XX override > Global price
                    price_val = custom_prices.get((p.country_code, iso), 
                                                custom_prices.get((p.country_code, 'XX'), p.buy_price))
                    
                    if price_val > 0:
                        localized_names = {lc: ln for lc in _LANG_TO_BABEL if (ln := get_localized_country_name(iso, lc))}
                        
                        # Add a debug key to check what iso we passed
                        localized_names["_debug_iso"] = iso
                        
                        # Fallback for cached frontends
                        single_localized = localized_names.get(lang) or name
                        
                        formatted_prices.append({
                            "name": name,
                            "iso": iso,
                            "localized_name": single_localized,
                            "localized_names": localized_names,
                            "flag": flag,
                            "code": p.country_code,
                            "price": price_val
                        })
                        seen_codes.add(p.country_code)
                except Exception as inner_e:
                    logger.error(f"Error processing price for code {p.country_code}: {inner_e}")
                    
            # Next, add any custom prices that are NOT in the global active list
            for (cc, c_iso), cp_buy_price in custom_prices.items():
                # Avoid duplicates if already added via the global prices loop
                if cc not in seen_codes and cp_buy_price > 0:
                    try:
                        # Use the specific ISO if available, else XX
                        n, f, resolved_iso = resolve_country_info(cc)
                        name = n if n != "Unknown" else f"Code {cc}"
                        
                        # If a custom ISO was specified, try to get a better name/flag
                        if c_iso != 'XX':
                            flag = get_flag_emoji(c_iso)
                            target_iso = c_iso
                        else:
                            flag = f
                            target_iso = c_iso if c_iso != 'XX' else None
                        localized_names = {lc: ln for lc in _LANG_TO_BABEL if (ln := get_localized_country_name(target_iso, lc))}
                        localized_names["_debug_iso"] = target_iso
                        
                        # Fallback for cached frontends
                        single_localized = localized_names.get(lang) or name
                        
                        formatted_prices.append({
                            "name": name,
                            "iso": target_iso or cc,
                            "localized_name": single_localized,
                            "localized_names": localized_names,
                            "flag": flag,
                            "code": cc,
                            "price": cp_buy_price
                        })
                    except: pass
                
            
            return {
                "maintenance_mode": False,
                "user": {
                    "id": user.id,
                    "balance": user.balance_sourcing,
                    "pending_balance": pending_balance,
                    "total_withdrawn": total_withdrawn,
                    "is_banned": user.is_banned_sourcing
                },
                "bot_name": bot_name,
                "stats": {
                    "sold": sold_count,
                    "pending": pending_count,
                    "accepted": accepted_count,
                    "rejected": rejected_count
                },
                "prices": formatted_prices,
                "settings": {
                    "min_withdraw_trx": float((await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_trx"))).scalar_one_or_none().value or 4.0) if (await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_trx"))).scalar_one_or_none() else 4.0,
                    "min_withdraw_usdt": float((await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_usdt"))).scalar_one_or_none().value or 10.0) if (await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_usdt"))).scalar_one_or_none() else 10.0,
                    "fee_withdraw_trx": float((await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_trx"))).scalar_one_or_none().value or 0.2) if (await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_trx"))).scalar_one_or_none() else 0.2,
                    "fee_withdraw_usdt": float((await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_usdt"))).scalar_one_or_none().value or 0.2) if (await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_usdt"))).scalar_one_or_none() else 0.2
                },
                "support_username": support_username.value if support_username else "",
                "updates_channel": updates_channel.value if updates_channel else "",
                "extra_admin_ids": extra_admin_obj.value if extra_admin_obj else ""
            }
    except Exception as e:
        logger.error(f"Seller Data API Error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"خطأ برمي: {str(e)}")

@app.post("/api/seller/request-otp")
async def seller_request_otp(data: SellerOTPRequest):
    from services.session_manager import request_app_code
    from config import SELLER_BOT_TOKEN
    if not verify_user_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=401, detail="Unauthorized identity")

    async with async_session() as session:
        user = await session.get(User, data.user_id)
        if user and user.is_banned_sourcing:
            raise HTTPException(status_code=403, detail="عذراً، أنت محظور من التوريد.")
        
        # 0. OTP Flood Protection (Cooldown)
        now = time.time()
        phone_key = f"p_{data.phone.strip()}"
        user_key = f"u_{data.user_id}"
        
        last_phone_req = otp_cooldowns.get(phone_key, 0)
        last_user_req = otp_cooldowns.get(user_key, 0)
        
        if now - last_phone_req < OTP_COOLDOWN_SECONDS:
            wait_time = int(OTP_COOLDOWN_SECONDS - (now - last_phone_req))
            lang = user.language if user else "ar"
            raise HTTPException(status_code=429, detail=get_text("wait_code", lang, wait_time=wait_time))
            
        if now - last_user_req < OTP_COOLDOWN_SECONDS:
            wait_time = int(OTP_COOLDOWN_SECONDS - (now - last_user_req))
            lang = user.language if user else "ar"
            raise HTTPException(status_code=429, detail=get_text("wait_another", lang, wait_time=wait_time))

        # Update cooldowns
        otp_cooldowns[phone_key] = now
        otp_cooldowns[user_key] = now
            
    try:
        phone = data.phone.strip()
        if not phone.startswith("+"): phone = "+" + phone
        
        # Pre-check 1: Duplicity
        async with async_session() as session:
            dup_stmt = select(Account).where(Account.phone_number == phone)
            existing = (await session.execute(dup_stmt)).scalar()
            if existing:
                lang = user.language if user else "ar"
                raise HTTPException(status_code=400, detail=get_text("acc_exists", lang))

        # 2. Country & Pricing Check
        try:
            # Clean phone and parse
            phone_p = phone if phone.startswith("+") else "+" + phone
            parsed = phonenumbers.parse(phone_p)
            cc = str(parsed.country_code)
            target_iso = phonenumbers.region_code_for_number(parsed) or 'XX'
            
            async with async_session() as session:
                from sqlalchemy import or_
                # Fetch all possible price candidates for this country code
                # This covers both specific ISO and global 'XX' entries
                
                # Resilient code matching
                cc_clean = cc.lstrip("+")
                cc_plus = "+" + cc_clean
                
                logger.info(f"OTP Request: User={data.user_id}, CC={cc}, ISO={target_iso}")
                
                # Check User Specific Prices first
                ucp_stmt = select(UserCountryPrice).where(
                    UserCountryPrice.user_id == data.user_id,
                    or_(UserCountryPrice.country_code == cc_clean, UserCountryPrice.country_code == cc_plus)
                )
                ucp_list = (await session.execute(ucp_stmt)).scalars().all()
                logger.info(f"OTP User Candidates: {[f'{u.country_code}/{u.iso_code}' for u in ucp_list]}")
                
                # Filter for best match
                # Priority: Exact ISO > Global XX > First available for this code
                ucp = next((u for u in ucp_list if u.iso_code == target_iso), 
                           next((u for u in ucp_list if u.iso_code == 'XX'), 
                                (ucp_list[0] if ucp_list else None)))
                
                # Check Global Prices
                cp_stmt = select(CountryPrice).where(
                    or_(CountryPrice.country_code == cc_clean, CountryPrice.country_code == cc_plus)
                )
                cp_list = (await session.execute(cp_stmt)).scalars().all()
                logger.info(f"OTP Global Candidates: {[f'{c.country_code}/{c.iso_code}' for c in cp_list]}")
                
                cp = next((c for c in cp_list if c.iso_code == target_iso), 
                          next((c for c in cp_list if c.iso_code == 'XX'), 
                               (cp_list[0] if cp_list else None)))
                
            # 3. Final Resolution
            final_buy_price = ucp.buy_price if ucp else (cp.buy_price if cp else 0)
            
            if final_buy_price <= 0:
                raise HTTPException(status_code=400, detail="Sorry, this country is not requested at the moment.")
                
            # Clean number for Telegram (E164)
            phone_clean = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
                    
        except HTTPException as he: raise he
        except Exception as e:
            logger.error(f"Sourcing Price Error: {e}")
            raise HTTPException(status_code=400, detail="Error detecting country price. Please check number format.")

        phone_code_hash = await request_app_code(data.user_id, phone_clean)
        return {"hash": phone_code_hash, "phone": phone_clean}
    except Exception as e:
        logger.error(f"Seller OTP Request Error: {e}")
        if isinstance(e, HTTPException): raise e
        err_msg = str(e)
        err_lower = err_msg.lower()
        # Handle Telegram FLOOD_WAIT
        if "flood_wait" in err_lower or "flood" in err_lower:
            import re as _re
            wait_match = _re.search(r'wait of (\d+) seconds', err_msg, _re.IGNORECASE)
            wait_secs = int(wait_match.group(1)) if wait_match else 3600
            if wait_secs >= 3600:
                wait_str = f"{wait_secs // 3600}h {(wait_secs % 3600) // 60}m"
            else:
                wait_str = f"{wait_secs // 60}m {wait_secs % 60}s"
            raise HTTPException(status_code=429, detail=f"FLOOD|{wait_str}")
        if any(x in err_lower for x in ["banned", "frozen", "security"]):
            raise HTTPException(status_code=400, detail=err_msg)
        # Handle common Telegram number errors cleanly
        if "phone_number_invalid" in err_lower:
            raise HTTPException(status_code=400, detail="INVALID_PHONE|This phone number is not valid or not registered on Telegram")
        if "phone_number_banned" in err_lower:
            raise HTTPException(status_code=400, detail="BANNED_PHONE|This number is permanently banned by Telegram")
        if "phone_number_unoccupied" in err_lower:
            raise HTTPException(status_code=400, detail="INVALID_PHONE|This phone number has no Telegram account")
        raise HTTPException(status_code=500, detail=f"Request error: {str(e)}")

@app.post("/api/seller/submit-otp")
async def seller_submit_otp(data: SellerOTPSubmit):
    from services.session_manager import submit_app_code
    from config import SELLER_BOT_TOKEN
    if not verify_user_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=401, detail="Unauthorized identity")
    try:
        submit_result = await submit_app_code(data.user_id, data.phone, data.hash, data.code)
        
        if not submit_result:
            raise HTTPException(status_code=400, detail="The verification code you entered is incorrect")
            
        session_string = submit_result["session_string"]
        two_fa_password = submit_result["two_fa_password"]
        has_other_sessions = submit_result["has_other_sessions"]
            
        async with async_session() as session:
            # Automatic price detection
            price = 0
            try:
                phone_p = data.phone if data.phone.startswith("+") else "+" + data.phone
                parsed = phonenumbers.parse(phone_p)
                cc = str(parsed.country_code)
                target_iso = phonenumbers.region_code_for_number(parsed) or 'XX'
                
                # Resilient code matching
                cc_clean = cc.lstrip("+")
                cc_plus = "+" + cc_clean
                
                # 1. User Price
                ucp_stmt = select(UserCountryPrice).where(
                    UserCountryPrice.user_id == data.user_id,
                    or_(UserCountryPrice.country_code == cc_clean, UserCountryPrice.country_code == cc_plus)
                )
                ucp_list = (await session.execute(ucp_stmt)).scalars().all()
                ucp = next((u for u in ucp_list if u.iso_code == target_iso), 
                           next((u for u in ucp_list if u.iso_code == 'XX'), 
                                (ucp_list[0] if ucp_list else None)))
                
                # 2. Global Price
                cp_stmt = select(CountryPrice).where(
                    or_(CountryPrice.country_code == cc_clean, CountryPrice.country_code == cc_plus)
                )
                cp_list = (await session.execute(cp_stmt)).scalars().all()
                cp = next((c for c in cp_list if c.iso_code == target_iso), 
                          next((c for c in cp_list if c.iso_code == 'XX'), 
                               (cp_list[0] if cp_list else None)))
                
                price = ucp.buy_price if ucp else (cp.buy_price if cp else 0)
                locked_approve_delay = ucp.approve_delay if ucp else (cp.approve_delay if cp else 0)
            except Exception as e:
                logger.error(f"Submit Price Detection Error: {e}")

            new_acc = Account(
                phone_number=data.phone,
                country=data.country,
                price=price,
                session_string=session_string,
                two_fa_password=two_fa_password,
                status=AccountStatus.PENDING,
                seller_id=data.user_id,
                created_at=datetime.now(),
                locked_buy_price=price,
                locked_approve_delay=locked_approve_delay
            )
            session.add(new_acc)
            await session.commit()
            
            await check_and_alert_missing_price(data.country, data.phone, session)
            
        return {"status": "success", "price": price}
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        logger.error(f"Seller OTP Submit Error: {e}")
        err_msg = str(e)
        err_msg_lower = err_msg.lower()
        
        # Custom 2FA Handling
        if "password" in err_msg_lower or "two-step" in err_msg_lower:
            raise HTTPException(status_code=400, detail="AUTH_ERROR|Please disable Two-Step Verification (2FA) and try again.")
            
        if "phone_code_invalid" in err_msg_lower:
            raise HTTPException(status_code=400, detail="WRONG_CODE|The verification code you entered is incorrect.")
            
        if "phone_code_expired" in err_msg_lower:
            raise HTTPException(status_code=400, detail="EXPIRED_CODE|This code has expired. Please request a new one.")

        if any(msg in err_msg_lower for msg in ["restricted", "frozen", "security check"]):
            raise HTTPException(status_code=400, detail=f"ACCOUNT_ERROR|{err_msg}")
            
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/seller/withdraw")
async def seller_withdraw(req: WithdrawSubmit):
    async with async_session() as session:
        # 1. AUTH VERIFICATION
        from config import SELLER_BOT_TOKEN
        if not verify_user_auth_multi(req.init_data, req.user_id):
            raise HTTPException(status_code=401, detail="Unauthorized: Telegram identity verification failed.")

        # Secure User Fetch with Row Locking
        user = await session.get(User, req.user_id, with_for_update=True)
        if not user:
            raise HTTPException(status_code=403, detail="User not verified for sourcing bot.")
        
        # Validation: Use FULL balance for withdrawal
        withdraw_amount = user.balance_sourcing
        
        # Get dynamic withdrawal minimums from AppSetting
        trx_setting = (await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_trx"))).scalar()
        usdt_setting = (await session.execute(select(AppSetting).where(AppSetting.key == "min_withdraw_usdt"))).scalar()
        
        try:
            min_trx = float(trx_setting.value) if trx_setting and trx_setting.value else 4.0
        except ValueError:
            min_trx = 4.0
            
        try:
            min_usdt = float(usdt_setting.value) if usdt_setting and usdt_setting.value else 10.0
        except ValueError:
            min_usdt = 10.0
        
        # Get dynamic withdrawal fees from AppSetting
        trx_fee_setting = (await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_trx"))).scalar()
        usdt_fee_setting = (await session.execute(select(AppSetting).where(AppSetting.key == "fee_withdraw_usdt"))).scalar()

        try:
            fee_trx = float(trx_fee_setting.value) if trx_fee_setting and trx_fee_setting.value else 0.2
        except ValueError:
            fee_trx = 0.2
            
        try:
            fee_usdt = float(usdt_fee_setting.value) if usdt_fee_setting and usdt_fee_setting.value else 0.2
        except ValueError:
            fee_usdt = 0.2
            
        min_amount = min_trx if "TRX" in req.method else min_usdt
        fee = fee_trx if "TRX" in req.method else fee_usdt
        
        if withdraw_amount < min_amount:
            raise HTTPException(status_code=400, detail=f"Minimum withdrawal is ${min_amount}")
        
        if withdraw_amount <= fee:
            raise HTTPException(status_code=400, detail="Amount too low to cover network fees")

        net_amount = withdraw_amount - fee

        # Create Request
        tid = generate_transaction_id()
        withdraw = WithdrawalRequest(
            user_id=req.user_id,
            amount=withdraw_amount,
            method=req.method,
            address=req.address,
            fee=fee,
            net_amount=net_amount,
            transaction_id=tid
        )
        
        # Deduct balance immediately
        user.balance_sourcing = 0
        
        session.add(withdraw)
        await session.flush() # Secure the ID
        
        # Link accounts to this withdrawal
        await session.execute(
            update(Account)
            .where(
                Account.seller_id == req.user_id, 
                or_(Account.status == AccountStatus.AVAILABLE, Account.status == AccountStatus.SOLD),
                Account.withdrawal_id == None
            )
            .values(withdrawal_id=withdraw.id)
        )
        
        await session.commit()
        await session.refresh(withdraw)
        return {"ok": True, "id": tid}

@app.get("/api/seller/withdrawals")
async def get_withdrawals(user_id: int, init_data: str, page: int = 1, status: str = "all"):
    from config import SELLER_BOT_TOKEN
    if not verify_user_auth_multi(init_data, user_id):
        return {"history": [], "total_pages": 0, "current_page": 1, "total_count": 0}

    page_size = 10
    offset = (page - 1) * page_size
    async with async_session() as session:
        # Build base filter
        base_filters = [WithdrawalRequest.user_id == user_id]
        if status != "all":
            try:
                # Convert string status to enum
                enum_status = WithdrawalStatus(status.lower())
                base_filters.append(WithdrawalRequest.status == enum_status)
            except: pass

        # Get total count for pagination
        count_stmt = select(func.count(WithdrawalRequest.id)).where(*base_filters)
        total_count = (await session.execute(count_stmt)).scalar() or 0
        total_pages = (total_count + page_size - 1) // page_size

        # Get page data
        stmt = select(WithdrawalRequest).where(*base_filters).order_by(WithdrawalRequest.created_at.desc()).offset(offset).limit(page_size)
        results = (await session.execute(stmt)).scalars().all()
        
        history = []
        for r in results:
            history.append({
                "id": r.id,
                "transaction_id": r.transaction_id,
                "amount": r.amount,
                "fee": r.fee,
                "net_amount": r.net_amount,
                "method": r.method,
                "address": r.address,
                "status": r.status.value,
                "date": r.created_at.isoformat() if r.created_at else None
            })
        return {
            "history": history,
            "total_pages": total_pages,
            "current_page": page,
            "total_count": total_count
        }

@app.get("/api/admin/withdrawals/all")
async def admin_get_all_withdrawals(user_id: int, init_data: str, page: int = 1, status: str = "all"):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    page_size = 10
    offset = (page - 1) * page_size
    async with async_session() as session:
        # Build base filter
        filters = []
        if status != "all":
            # Map string to Enum member safely
            s_map = {
                "pending": WithdrawalStatus.PENDING, 
                "approved": WithdrawalStatus.APPROVED, 
                "rejected": WithdrawalStatus.REJECTED
            }
            if status.lower() in s_map:
                filters.append(WithdrawalRequest.status == s_map[status.lower()])
            
        # Count total
        count_stmt = select(func.count(WithdrawalRequest.id)).where(*filters)
        total_count = (await session.execute(count_stmt)).scalar() or 0
        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
        
        # Build results query
        stmt = select(WithdrawalRequest).where(*filters).order_by(WithdrawalRequest.created_at.desc()).offset(offset).limit(page_size)
        results = (await session.execute(stmt)).scalars().all()
        
        history = []
        for r in results:
            # Fetch user info for display
            u = await session.get(User, r.user_id)
            history.append({
                "id": r.id,
                "user_id": r.user_id,
                "user_name": u.full_name if u else "N/A",
                "user_handle": f"@{u.username}" if u and u.username else "N/A",
                "transaction_id": r.transaction_id,
                "amount": r.amount,
                "fee": r.fee,
                "net_amount": r.net_amount,
                "method": r.method,
                "address": r.address,
                "status": r.status.value,
                "date": r.created_at.isoformat() if r.created_at else None
            })
            
        return {
            "history": history,
            "total_pages": total_pages,
            "current_page": page,
            "total_count": total_count
        }

@app.post("/api/admin/withdrawals/action")
async def admin_withdrawal_action(data: WithdrawAction):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        req = await session.get(WithdrawalRequest, data.request_id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")
            
        if req.status != WithdrawalStatus.PENDING:
            raise HTTPException(status_code=400, detail=f"Request is already {req.status.value}")
            
        # 1. Update Status
        if data.action == "approve":
            req.status = WithdrawalStatus.APPROVED
            btn_text = "✅ Approved"
            msg_theme = "🟢"
        elif data.action == "reject":
            # NO REFUND as per user request
            req.status = WithdrawalStatus.REJECTED
            btn_text = "❌ Rejected (No Refund)"
            msg_theme = "🔴"
        else:
            raise HTTPException(status_code=400, detail="Invalid action")
            
        await session.commit()
        
        # 2. Notify User via Bot
        bot = getattr(app.state, 'bot_seller', None)
        if bot:
            try:
                # Localized message based on user preference
                user = await session.get(User, req.user_id)
                lang = user.language if user else "ar"
                
                if data.action == 'approve':
                    msg = get_text("withdraw_approved", lang, tx_id=req.transaction_id, amount=req.amount)
                else:
                    msg = get_text("withdraw_rejected", lang, tx_id=req.transaction_id, amount=req.amount)
                
                await bot.send_message(req.user_id, msg, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to send withdrawal notification: {e}")
                
        return {"ok": True, "status": "success", "message": f"Withdrawal {data.action}ed successfully"}

@app.get("/api/admin/withdrawal/{request_id}/audit")
async def get_withdrawal_audit(request_id: int, user_id: int, init_data: str):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        # 1. Get current withdrawal request
        req = await session.get(WithdrawalRequest, request_id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")
            
        # 2. Fetch accounts linked to this withdrawal
        acc_stmt = select(Account).where(
            Account.withdrawal_id == request_id
        ).order_by(Account.created_at.desc())
        
        accounts = (await session.execute(acc_stmt)).scalars().all()
        
        return {
            "accounts": [
                {
                    "id": a.id,
                    "phone": a.phone_number,
                    "country": a.country,
                    "price": a.price,
                    "status": a.status.value,
                    "date": a.created_at.isoformat()
                } for a in accounts
            ],
            "start_date": req.created_at.isoformat(), # Use request date as ref
            "total_count": len(accounts),
            "total_audit_value": sum(a.price for a in accounts)
        }

@app.post("/api/admin/accounts/check-alive")
async def admin_check_account_alive(data: dict):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from services.session_manager import is_session_alive
    acc_id = data.get("account_id")
    async with async_session() as session:
        acc = await session.get(Account, acc_id)
        if not acc: return {"status": "error", "message": "Not found"}
        
        if acc.status == AccountStatus.SOLD:
            return {"status": "sold"}
            
        try:
            is_alive, reason = await is_session_alive(acc.session_string)
            if is_alive:
                return {"status": "alive"}
            else:
                # If is_session_alive returns False, return the specific reason
                return {"status": "dead", "error": reason}
        except Exception as e:
            return {"status": "dead", "error": str(e)}







@app.get("/api/admin/countries-for-code/{code}")
async def get_countries_for_code(code: str, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    """Returns a list of matching countries for a given numeric code."""
    try:
        clean_code = code.strip().lstrip('+').lstrip('0')
        numeric_code = int(clean_code)
        regions = phonenumbers.COUNTRY_CODE_TO_REGION_CODE.get(numeric_code, [])
        
        results = []
        for r in regions:
            try:
                country = pycountry.countries.get(alpha_2=r)
                if country:
                    name = country.name
                    name = re.sub(r'\s*\(?[A-Z]{2,3}\)?\s*$', '', name).strip()
                    results.append({"iso": r, "name": name, "flag": get_flag_emoji(r)})
            except: pass
        return results
    except:
        return []

@app.get("/api/seller/detect-country")
async def detect_country(phone: str, user_id: int, init_data: str):
    from config import SELLER_BOT_TOKEN
    if not verify_user_auth_multi(init_data, user_id):
        return {"found": False}
    try:
        # Clean input
        raw = phone.strip().lstrip('+')
        if not raw: return {"found": False}
        
        # Immediate CC detection (best for short input like +20)
        detected_cc = None
        for i in range(4, 0, -1):
            prefix = raw[:i]
            if prefix.isdigit() and int(prefix) in phonenumbers.COUNTRY_CODE_TO_REGION_CODE:
                detected_cc = prefix
                break
        
        # Fallback to full parsing if it's a long number
        target_iso = 'XX'
        try:
            phone_p = phone if phone.startswith('+') else f"+{phone}"
            parsed = phonenumbers.parse(phone_p)
            detected_cc = str(parsed.country_code)
            target_iso = phonenumbers.region_code_for_number(parsed) or 'XX'
        except: pass

        if not detected_cc:
            return {"found": False}
            
        async with async_session() as session:
            from sqlalchemy import or_
            cc_clean = detected_cc.lstrip("+")
            cc_plus = "+" + cc_clean
            
            # 1. Custom User Price
            ucp = None
            if user_id > 0:
                ucp_stmt = select(UserCountryPrice).where(
                    UserCountryPrice.user_id == user_id,
                    or_(UserCountryPrice.country_code == cc_clean, UserCountryPrice.country_code == cc_plus)
                )
                ucp_res = await session.execute(ucp_stmt)
                ucp_list = ucp_res.scalars().all()
                ucp = next((u for u in ucp_list if u.iso_code == target_iso), 
                           next((u for u in ucp_list if u.iso_code == 'XX'), 
                                (ucp_list[0] if ucp_list else None)))
            
            # 2. Global Price
            cp_stmt = select(CountryPrice).where(
                or_(CountryPrice.country_code == cc_clean, CountryPrice.country_code == cc_plus)
            )
            cp_res = await session.execute(cp_stmt)
            cp_list = cp_res.scalars().all()
            cp = next((c for c in cp_list if c.iso_code == target_iso), 
                      next((c for c in cp_list if c.iso_code == 'XX'), 
                           (cp_list[0] if cp_list else None)))
            
            # Resolution
            price_val = ucp.buy_price if ucp else (cp.buy_price if cp else 0)
            
            if price_val > 0:
                # Resolve Name & Flag
                display_iso = target_iso
                if display_iso == 'XX':
                    display_iso = phonenumbers.region_code_for_country_code(int(detected_cc))
                
                name = cp.country_name if cp else (ucp.country_name if hasattr(ucp, 'country_name') else "Requested Country")
                if not cp and ucp:
                    n, _, _ = resolve_country_info(detected_cc)
                    name = n if n != "Unknown" else f"Code {detected_cc}"

                return {
                    "found": True,
                    "name": name,
                    "iso": display_iso,
                    "flag": get_flag_emoji(display_iso) if display_iso != 'XX' else "🌐",
                    "price": price_val
                }
    except Exception as e:
        logger.error(f"Detection Error: {e}")
    return {"found": False}

@app.get("/api/seller/accounts")
async def get_seller_accounts(user_id: int, init_data: str, page: int = 1, limit: int = 10, status: str = "all"):
    from config import SELLER_BOT_TOKEN
    if not verify_user_auth_multi(init_data, user_id):
        return {"accounts": [], "total_pages": 0, "current_page": 1, "total_count": 0}

    async with async_session() as session:
        offset = (page - 1) * limit
        
        # Build base filter
        base_filters = [Account.seller_id == user_id]
        if status != "all":
            if status == "pending":
                base_filters.append(Account.status == AccountStatus.PENDING)
            elif status == "accepted":
                # Sellers see SOLD as AVAILABLE/ACCEPTED
                base_filters.append(or_(Account.status == AccountStatus.AVAILABLE, Account.status == AccountStatus.SOLD))
            elif status == "rejected":
                base_filters.append(Account.status == AccountStatus.REJECTED)

        # Get total count for pagination
        count_stmt = select(func.count(Account.id)).where(*base_filters)
        total_count = (await session.execute(count_stmt)).scalar() or 0
        total_pages = math.ceil(total_count / limit) if total_count > 0 else 1
        
        stmt = select(Account).where(*base_filters).order_by(Account.id.desc()).offset(offset).limit(limit)
        results = (await session.execute(stmt)).scalars().all()
        accounts_data = []
        for a in results:
            # Detect reward price for seller
            actual_buy_price = 0
            approve_delay = 0
            flag = "🌐"
            try:
                parsed = phonenumbers.parse(a.phone_number)
                cc = str(parsed.country_code)
                region = phonenumbers.region_code_for_number(parsed)
                flag = get_flag_emoji(region)
                cp_row = (await session.execute(select(CountryPrice).where(CountryPrice.country_code == cc))).scalar()
                if cp_row:
                    actual_buy_price = cp_row.buy_price
                    approve_delay = cp_row.approve_delay
            except: pass

            # Prefer locked values snapshotted at submission (immune to admin changes)
            actual_buy_price = a.locked_buy_price if a.locked_buy_price is not None else actual_buy_price
            approve_delay = a.locked_approve_delay if a.locked_approve_delay is not None else approve_delay

            ready_at = (a.created_at + timedelta(seconds=approve_delay)) if a.created_at else None

            # Mask SOLD status for seller to show as AVAILABLE (ACCEPTED)
            status_name = a.status.name
            if a.status == AccountStatus.SOLD:
                status_name = "AVAILABLE"

            accounts_data.append({
                "phone": a.phone_number,
                "status": status_name,
                "country": a.country,
                "flag": flag,
                "iso": region,
                "buy_price": actual_buy_price,
                "ready_at": int(ready_at.timestamp() * 1000) if ready_at else None,
                "date": a.created_at.isoformat() if a.created_at else None,
                "reject_reason": a.reject_reason
            })

        return {
            "accounts": accounts_data,
            "total_pages": total_pages,
            "current_page": page,
            "server_now": int(datetime.utcnow().timestamp() * 1000)
        }

@app.get("/api/admin/sourcing/history")
async def get_admin_sourcing_history(
    user_id: int,
    init_data: str,
    page: int = 1, 
    limit: int = 10, 
    filter: str = "PENDING",
    search: str = None
):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        offset = (page - 1) * limit
        base_stmt = select(Account)
        
        # 1. Status Filter (Bypassed if searching)
        is_searching = bool(search and search.strip())
        
        # 1. Status Filter
        if filter == "PENDING":
            base_stmt = base_stmt.where(Account.status == AccountStatus.PENDING)
        elif filter == "ACCEPTED":
            base_stmt = base_stmt.where(Account.status == AccountStatus.AVAILABLE)
        elif filter == "SOLD":
            base_stmt = base_stmt.where(Account.status == AccountStatus.SOLD)
        elif filter == "REJECTED":
            # REJECTED = all rejected except REVOKED (which has its own filter)
            base_stmt = base_stmt.where(Account.status == AccountStatus.REJECTED, Account.reject_reason != "REVOKED")
        elif filter == "FROZEN":
            # FROZEN: banned/frozen accounts — explicitly exclude REVOKED
            base_stmt = base_stmt.where(Account.status == AccountStatus.REJECTED, or_(Account.reject_reason.ilike("%frozen%"), Account.reject_reason.ilike("%banned%"), Account.reject_reason.ilike("%company%")), Account.reject_reason != "REVOKED")
        elif filter == "SPAM":
            base_stmt = base_stmt.where(Account.status == AccountStatus.REJECTED, Account.reject_reason.ilike("%spam%"))
        elif filter == "REVOKED":
            base_stmt = base_stmt.where(Account.status == AccountStatus.REJECTED, Account.reject_reason == "REVOKED")
        
        # 2. Search Filter (Phone or ID)
        if is_searching:
            s = f"%{search.strip()}%"
            base_stmt = base_stmt.where(
                or_(
                    Account.phone_number.ilike(s),
                    cast(Account.seller_id, String).ilike(s),
                    Account.country.ilike(s)
                )
            )

        total_count = (await session.execute(
            select(func.count()).select_from(base_stmt.subquery())
        )).scalar() or 0
        total_pages = math.ceil(total_count / limit) if total_count > 0 else 1
        
        stmt = base_stmt.order_by(Account.id.desc()).offset(offset).limit(limit)
        results = (await session.execute(stmt)).scalars().all()
        
        history = []
        import phonenumbers
        for a in results:
            flag = "🌐"
            approve_delay = 0
            price = 0
            try:
                parsed = phonenumbers.parse(a.phone_number)
                cc = str(parsed.country_code)
                region = phonenumbers.region_code_for_number(parsed)
                # Helper to get flag emoji (if not available, we use Globe)
                try:
                    flag = "".join(chr(127397 + ord(c)) for c in region)
                except: pass

                cp_row = (await session.execute(select(CountryPrice).where(CountryPrice.country_code == cc))).scalar()
                if cp_row:
                    price = cp_row.buy_price
                    approve_delay = cp_row.approve_delay
            except: pass

            # Prefer locked values snapshotted at submission (immune to admin changes)
            price = a.locked_buy_price if a.locked_buy_price is not None else price
            approve_delay = a.locked_approve_delay if a.locked_approve_delay is not None else approve_delay

            ready_at = (a.created_at + timedelta(seconds=approve_delay)) if a.created_at else None

            history.append({
                "id": a.id,
                "phone": a.phone_number,
                "country": f"{flag} {a.country}",
                "buy_price": price,
                "status": a.status.name,
                "seller_id": a.seller_id,
                "two_fa_password": a.two_fa_password,
                "ready_at": int(ready_at.timestamp() * 1000) if ready_at else None,
                "date": a.created_at.isoformat() if a.created_at else None,
                "reject_reason": a.reject_reason,
                "is_available": a.status == AccountStatus.AVAILABLE
            })
            
        return {
            "history": history,
            "total_pages": total_pages,
            "current_page": page,
            "server_now": int(datetime.utcnow().timestamp() * 1000)
        }

@app.get("/api/admin/sourcing/account/{phone}/code")
async def get_account_otp(phone: str, user_id: int, init_data: str):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        account = (await session.execute(select(Account).where(Account.phone_number == phone))).scalar()
        if not account:
            return {"success": False, "error": "ACCOUNT_NOT_FOUND"}
        if not account.session_string:
            return {"success": False, "error": "SESSION_NOT_FOUND"}
            
        try:
            from services.session_manager import get_telegram_login_code
            code = await get_telegram_login_code(account.session_string)
            if code:
                return {"success": True, "code": code}
            else:
                return {"success": False, "error": "NO_CODE_RECEIVED"}
        except Exception as e:
            err_str = str(e)
            if "SESSION_REVOKED" in err_str:
                return {"success": False, "error": "SESSION_NOT_FOUND"}
            return {"success": False, "error": err_str}

@app.delete("/api/admin/sourcing/account/{phone}")
async def revoke_sourcing_account(phone: str, user_id: int, init_data: str):
    from config import SELLER_BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        account = (await session.execute(select(Account).where(Account.phone_number == phone))).scalar()
        if not account:
            return {"success": False, "error": "Account not found"}
            
        # 1. Terminate Bot Session from the Telegram Account
        if account.session_string:
            try:
                from services.session_manager import create_client
                client = await create_client(account.session_string)
                await client.connect()
                await client.log_out() # Permanently kills the bot's session
                await client.disconnect()
            except Exception as e:
                logger.warning(f"Failed to log out session for {phone} during revocation: {e}")

        # 2. Update Status to REJECTED (REVOKED) instead of deleting
        account.status = AccountStatus.REJECTED
        account.reject_reason = "REVOKED"
        await session.commit()
        
        return {"success": True, "message": "Account revoked and session terminated."}

@app.post("/api/admin/user/sync")
async def sync_user_identity(data: UserSync):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(data.init_data, data.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:    
        # 1. Select the correct bot based on bot_type
        bot = app.state.bot_buyer if data.bot_type == "store" else app.state.bot_seller
        
        if not bot:
            raise HTTPException(status_code=500, detail="Bot instance not found for sync")
            
        # 2. Fetch latest data from Telegram
        chat = await bot.get_chat(data.user_id_target)
        
        # 3. Format name and username
        new_full_name = f"{chat.first_name or ''} {chat.last_name or ''}".strip() or "N/A"
        new_username = chat.username or None
        
        # 4. Update Database
        async with async_session() as session:
            user = await session.get(User, data.user_id_target)
            if user:
                user.full_name = new_full_name
                user.username = new_username
                await session.commit()
                
                return {
                    "status": "success",
                    "full_name": new_full_name,
                    "username": f"@{new_username}" if new_username else "N/A"
                }
        
        raise HTTPException(status_code=404, detail="User not found in database")
        
    except Exception as e:
        logger.error(f"Identity Sync Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- Settings Management ---
@app.post("/api/admin/system/settings")
async def save_system_settings(data: dict):
    from config import BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        for key, value in data.items():
            if key in ["user_id", "init_data"]:
                continue
            stmt = select(AppSetting).where(AppSetting.key == key)
            res = await session.execute(stmt)
            obj = res.scalar_one_or_none()
            
            if obj:
                obj.value = str(value)
            else:
                session.add(AppSetting(key=key, value=str(value)))
        
        await session.commit()
        
        # Immediately apply extra_admin_ids to memory
        if "extra_admin_ids" in data:
            import config, os
            base_admins = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
            config.STORE_ADMIN_IDS.clear()
            config.STORE_ADMIN_IDS.extend(base_admins)
            value = data.get("extra_admin_ids")
            if value and isinstance(value, str):
                for eid in value.split(","):
                    if eid.strip().isdigit():
                        parsed_id = int(eid.strip())
                        if parsed_id not in config.STORE_ADMIN_IDS:
                            config.STORE_ADMIN_IDS.append(parsed_id)
            logger.info(f"Updated STORE_ADMIN_IDS: {config.STORE_ADMIN_IDS}")

        return {"status": "success"}

@app.post("/api/admin/store/referral-settings")
async def save_referral_settings(data: dict):
    from config import BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        keys = {
            "referral_join_bonus": str(data.get("join_bonus", "0")),
            "referral_commission_percent": str(data.get("commission_percent", "0"))
        }
        for key, value in keys.items():
            stmt = select(AppSetting).where(AppSetting.key == key)
            res = await session.execute(stmt)
            obj = res.scalar_one_or_none()
            if obj:
                obj.value = value
            else:
                session.add(AppSetting(key=key, value=value))
        await session.commit()
        return {"status": "success"}

@app.post("/api/admin/store/general-settings-legacy")
async def save_store_general_settings_legacy(data: dict):
    from config import BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        keys = {
            "bot_name": data.get("bot_name"),
            "purchase_log_channel_id": data.get("purchase_log_channel_id")
        }
        for key, value in keys.items():
            if value is None: continue
            stmt = select(AppSetting).where(AppSetting.key == key)
            res = await session.execute(stmt)
            obj = res.scalar_one_or_none()
            if obj:
                obj.value = str(value)
            else:
                session.add(AppSetting(key=key, value=str(value)))
        await session.commit()
        return {"status": "success"}

# --- End of Web Admin SOURCINGPRO ---
@app.get("/api/admin/subscription-channels")
async def get_subscription_channels(user_id: int, init_data: str, bot_type: str = "store"):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        result = await session.execute(select(SubscriptionChannel).where(SubscriptionChannel.bot_type == bot_type))
        channels = result.scalars().all()
        return [{"id": c.id, "bot_type": c.bot_type, "username": c.username, "link": c.link} for c in channels]

@app.post("/api/user/settings")
async def update_user_settings(data: dict):
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    lang = data.get("language")
    
    if not verify_user_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    async with async_session() as session:
        user = await session.get(User, u_id)
        if user:
            user.language = lang
            await session.commit()
            return {"ok": True}
        raise HTTPException(status_code=404, detail="User not found")

@app.post("/api/admin/subscription-channels")
async def add_subscription_channel(data: dict):
    from config import BOT_TOKEN, ADMIN_IDS
    u_id = data.get("user_id")
    i_data = data.get("init_data")
    if not verify_admin_auth_multi(i_data, u_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    bot_type = data.get("bot_type", "store")
    username = data.get("username")
    link = data.get("link")
    if not username or not link:
        return {"ok": False, "error": "Username and Link are required"}
    
    async with async_session() as session:
        new_channel = SubscriptionChannel(bot_type=bot_type, username=username, link=link)
        session.add(new_channel)
        await session.commit()
        await session.refresh(new_channel)
        return {"ok": True, "id": new_channel.id}

@app.delete("/api/admin/subscription-channels/{channel_id}")
async def delete_subscription_channel(channel_id: int, user_id: int, init_data: str):
    from config import BOT_TOKEN, ADMIN_IDS
    if not verify_admin_auth_multi(init_data, user_id):
        raise HTTPException(status_code=403, detail="Unauthorized")
    async with async_session() as session:
        channel = await session.get(SubscriptionChannel, channel_id)
        if channel:
            await session.delete(channel)
            await session.commit()
        return {"ok": True}

# ─── TESTING / RESET ENDPOINTS ───────────────────────────────────────────────

@app.get("/api/admin/test/clear-deposits")
async def test_clear_deposits():
    """[TESTING] Clear all deposits + DEPOSIT transactions + reset balance_store."""
    async with async_session() as session:
        deposit_count = (await session.execute(
            select(func.count(Deposit.id))
        )).scalar() or 0

        txn_count = (await session.execute(
            select(func.count(Transaction.id)).where(
                Transaction.type == TransactionType.DEPOSIT
            )
        )).scalar() or 0

        await session.execute(delete(Deposit))
        await session.execute(
            delete(Transaction).where(Transaction.type == TransactionType.DEPOSIT)
        )
        await session.execute(update(User).values(balance_store=0.0))
        await session.commit()

    return {
        "status": "success",
        "deposits_cleared": deposit_count,
        "transactions_cleared": txn_count,
        "message": f"Cleared {deposit_count} deposits and reset all store balances."
    }


@app.get("/api/admin/test/clear-sold-accounts")
async def test_clear_sold_accounts():
    """[TESTING] Permanently DELETE all SOLD accounts from DB + clear BUY transactions."""
    async with async_session() as session:
        sold_count = (await session.execute(
            select(func.count(Account.id)).where(Account.status == AccountStatus.SOLD)
        )).scalar() or 0

        buy_txn_count = (await session.execute(
            select(func.count(Transaction.id)).where(
                Transaction.type == TransactionType.BUY
            )
        )).scalar() or 0

        await session.execute(
            delete(Account).where(Account.status == AccountStatus.SOLD)
        )
        await session.execute(
            delete(Transaction).where(Transaction.type == TransactionType.BUY)
        )
        await session.commit()

    return {
        "status": "success",
        "accounts_deleted": sold_count,
        "buy_transactions_cleared": buy_txn_count,
        "message": f"Permanently deleted {sold_count} SOLD accounts from the database."
    }


@app.get("/api/admin/test/delete-account")
async def test_delete_account(phone: str):
    """[TESTING] Permanently delete a single account by phone number."""
    async with async_session() as session:
        result = await session.execute(
            select(Account).where(Account.phone_number == phone)
        )
        account = result.scalar_one_or_none()

        if not account:
            raise HTTPException(status_code=404, detail=f"No account found with phone: {phone}")

        account_id   = account.id
        phone_stored = account.phone_number
        status_val   = account.status.value

        await session.delete(account)
        await session.commit()

    return {
        "status": "success",
        "deleted_account_id": account_id,
        "phone_number": phone_stored,
        "was_status": status_val,
        "message": f"Account {phone_stored} permanently deleted."
    }

# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/check-subscription")
async def check_subscription(user_id: int, bot_type: str = "store"):
    from config import BOT_TOKEN, SELLER_BOT_TOKEN, ADMIN_IDS
    
    # Admins bypass check
    if user_id in ADMIN_IDS:
        return {"ok": True}
        
    token = BOT_TOKEN if bot_type == "store" else SELLER_BOT_TOKEN
    
    async with async_session() as session:
        result = await session.execute(select(SubscriptionChannel).where(SubscriptionChannel.bot_type == bot_type))
        channels = result.scalars().all()
        
    if not channels:
        return {"ok": True}
        
    not_subscribed = []
    for ch in channels:
        try:
            # Telegram API check
            chat_id = ch.username
            api_url = f"https://api.telegram.org/bot{token}/getChatMember?chat_id={chat_id}&user_id={user_id}"
            
            def do_check():
                try:
                    r = requests.get(api_url, timeout=5)
                    return r.json()
                except: return None
                
            data = await asyncio.to_thread(do_check)
            
            if not data or not data.get("ok"):
                # If bot is not admin or channel not found, we might want to skip or block. 
                # To be safe and avoid locking out everyone on misconfig, we skip errors for now.
                # But if data.ok is false, it usually means the bot can't see the member.
                continue
                
            status = data["result"]["status"]
            if status in ["left", "kicked"]:
                not_subscribed.append({"username": ch.username, "link": ch.link})
        except Exception as e:
            logger.error(f"Error checking sub for {ch.username}: {e}")
            continue
            
    if not_subscribed:
        return {"ok": False, "channels": not_subscribed}
        
    return {"ok": True}
