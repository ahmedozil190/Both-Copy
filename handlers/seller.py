import os
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from database.engine import async_session
from database.models import User, CountryPrice
from sqlalchemy.future import select

router = Router()

def is_sourcing_admin(user_id: int) -> bool:
    import config
    return user_id in config.ADMIN_IDS

@router.message(Command("ping"))
async def seller_ping(message: Message):
    await message.answer("Bot is Online & Ready! 🚀")

@router.message(F.text.in_({"عربي", "English"}))
async def seller_change_language(message: Message):
    lang_code = "ar" if message.text == "عربي" else "en"
    async with async_session() as session:
        user = await session.get(User, message.from_user.id)
        if user:
            user.language = lang_code
            await session.commit()
    await message.answer("Language updated! / تم تحديث اللغة" if lang_code == "en" else "تم تحديث اللغة!")

@router.message(Command("addcountry"))
async def admin_add_country(message: Message):
    if not is_sourcing_admin(message.from_user.id):
        return

    # Usage: /addcountry  code  name  buy_price  sell_price [delay]
    args = message.text.split()[1:]
    if len(args) < 4:
        await message.answer(
            "<b>⚠️ Usage:</b>\n"
            "<code>/addcountry 20 Egypt 0.5 1.0 0</code>\n\n"
            "Parameters: Code, Name, BuyPrice, SellPrice, [Delay=0]",
            parse_mode="HTML"
        )
        return

    try:
        code = args[0]
        name = args[1]
        buy_p = float(args[2])
        sell_p = float(args[3])
        delay = int(args[4]) if len(args) > 4 else 0

        async with async_session() as session:
            stmt = select(CountryPrice).where(CountryPrice.country_code == code)
            cp = (await session.execute(stmt)).scalar_one_or_none()
            
            if cp:
                cp.country_name = name
                cp.buy_price = buy_p
                cp.price = sell_p
                cp.approve_delay = delay
                status = "Updated"
            else:
                cp = CountryPrice(
                    country_code=code,
                    country_name=name,
                    buy_price=buy_p,
                    price=sell_p,
                    approve_delay=delay
                )
                session.add(cp)
                status = "Added"
            
            await session.commit()
            await message.answer(
                f"✅ <b>{status} Successfully:</b>\n"
                f"- {name} (+{code})\n"
                f"- Buy: ${buy_p:.3f if f'{buy_p:.3f}'[-1] != '0' else buy_p:.2f}\n"
                f"- Sell: ${sell_p:.3f if f'{sell_p:.3f}'[-1] != '0' else sell_p:.2f}",
                parse_mode="HTML"
            )
            
    except Exception as e:
        await message.answer(f"❌ Error: {str(e)}")

@router.message(Command("manage_countries"))
@router.message(Command("sourcing_stats"))
async def admin_manage_countries(message: Message):
    if not is_sourcing_admin(message.from_user.id):
        return
        
    async with async_session() as session:
        result = await session.execute(select(CountryPrice).order_by(CountryPrice.country_name))
        countries = result.scalars().all()
        
    if not countries:
        await message.answer("⚠️ لا توجد دول مضافة حالياً.")
        return
        
    keyboard_list = []
    text = "<b>⚙️ إدارة قائمة الدول والأسعار:</b>\n\nاضغط على ❌ بجانب الدولة لحذفها:\n"
    
    for c in countries:
        keyboard_list.append([
            InlineKeyboardButton(text=f"❌ {c.country_name} (+{c.country_code})", callback_data=f"del_cp_cf_{c.id}")
        ])
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_list)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

# Confirmation Callback
@router.callback_query(F.data.startswith("del_cp_cf_"))
async def callback_delete_country_confirm(call: CallbackQuery):
    if not is_sourcing_admin(call.from_user.id): return
    cp_id = int(call.data.split("_")[3])
    
    async with async_session() as session:
        cp = await session.get(CountryPrice, cp_id)
        if not cp:
            await call.answer("Country not found!")
            return
            
        text = f"⚠️ <b>تأكيد الحذف:</b>\n\nهل أنت متأكد أنك تريد حذف دولة <b>{cp.country_name}</b>؟\nسيؤدي هذا لإزالتها من قائمة الأسعار فوراً."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="نعم، احذف ✅", callback_data=f"del_cp_ex_{cp_id}"),
                InlineKeyboardButton(text="إلغاء 🔙", callback_data="manage_countries_back")
            ]
        ])
        await call.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

# Execution Callback
@router.callback_query(F.data.startswith("del_cp_ex_"))
async def callback_delete_country_execute(call: CallbackQuery):
    if not is_sourcing_admin(call.from_user.id): return
    cp_id = int(call.data.split("_")[3])
    
    async with async_session() as session:
        cp = await session.get(CountryPrice, cp_id)
        if cp:
            name = cp.country_name
            await session.delete(cp)
            await session.commit()
            await call.answer(f"✅ تم حذف {name} بنجاح", show_alert=True)
            # Send the updated list
            await admin_manage_countries(call.message)
            await call.message.delete()
        else:
            await call.answer("خطأ: الدولة غير موجودة.")

@router.callback_query(F.data == "manage_countries_back")
async def callback_manage_countries_back(call: CallbackQuery):
    if not is_sourcing_admin(call.from_user.id): return
    await admin_manage_countries(call.message)
    await call.message.delete()
