from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import STORE_URL, WEBAPP_URL

# WebApp URLs
STORE_WEBAPP_URL = STORE_URL
SELLER_WEBAPP_URL = f"{WEBAPP_URL}/seller?v=3"

def store_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Open Store 🛒", web_app=WebAppInfo(url=STORE_WEBAPP_URL))]
    ])

def seller_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Open Sourcing Panel 🚀", web_app=WebAppInfo(url=SELLER_WEBAPP_URL))]
    ])

def admin_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 الإحصائيات (Stats)", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 إدارة المستخدمين (Users)", callback_data="admin_users")],
        [InlineKeyboardButton(text="📦 إدارة المخزون (Stock)", callback_data="admin_stock")],
        [InlineKeyboardButton(text="📢 إذاعة رسالة (Broadcast)", callback_data="admin_broadcast")]
    ])

def admin_user_keyboard(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ إضافة رصيد", callback_data=f"usr_add_{user_id}"),
         InlineKeyboardButton(text="➖ خصم رصيد", callback_data=f"usr_sub_{user_id}")],
         [InlineKeyboardButton(text="الرجوع 🔙", callback_data="admin_main")]
    ])

def admin_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="الرجوع 🔙", callback_data="admin_main")]
    ])
