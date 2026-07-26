import os
import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from database import async_session, Account, AccountStatus, User, Transaction, TransactionType
from sqlalchemy.future import select
from sqlalchemy import func
from keyboards import admin_main_keyboard, admin_user_keyboard, admin_back_keyboard

logger = logging.getLogger(__name__)
router = Router()

class AdminState(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_add_balance = State()
    waiting_for_sub_balance = State()
    waiting_for_broadcast = State()

def is_admin(user_id: int) -> bool:
    from config import STORE_ADMIN_IDS
    return user_id in STORE_ADMIN_IDS

@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.reply(f"هذا هو الايدي الحقيقي الخاص بحسابك: <code>{message.from_user.id}</code>\nقم بنسخه ووضعه في ADMIN_IDS في ملف .env", parse_mode="HTML")

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
        
    await state.clear()
        
    web_url = os.getenv("WEBAPP_URL", os.getenv("WEB_URL", "http://127.0.0.1:8000")).rstrip("/")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Store Panel 🛒", web_app=WebAppInfo(url=f"{web_url}/admin/store"))],
        [InlineKeyboardButton(text="Sourcing Panel 🚀", web_app=WebAppInfo(url=f"{web_url}/admin/sourcing"))]
    ])
    
    text = "👋 Welcome To The Admin Panel 📊\n\nClick the button below to open."
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

@router.callback_query(F.data == "admin_main")
async def cq_admin_main(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.clear()
    await call.message.edit_text("👨‍💻 **لوحة تحكم الإدارة الرئيسية:**\nاختر من القائمة أدناه:", reply_markup=admin_main_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data == "admin_stats")
async def cq_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    
    async with async_session() as session:
        users_count = (await session.execute(select(func.count(User.id)))).scalar()
        total_balance_store = (await session.execute(select(func.sum(User.balance_store)))).scalar() or 0.0
        total_balance_sourcing = (await session.execute(select(func.sum(User.balance_sourcing)))).scalar() or 0.0
        
        avail_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.AVAILABLE))).scalar()
        sold_count = (await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.SOLD))).scalar()
        
    text = (
        "📊 <b>الإحصائيات العامة للمتجر:</b>\n\n"
        f"👥 <b>إجمالي المستخدمين:</b> {users_count}\n"
        f"💰 <b>إجمالي أرصدة المتجر:</b> ${total_balance_store:.3f if f'{total_balance_store:.3f}'[-1] != '0' else total_balance_store:.2f}\n"
        f"💸 <b>إجمالي أرصدة التوريد:</b> ${total_balance_sourcing:.3f if f'{total_balance_sourcing:.3f}'[-1] != '0' else total_balance_sourcing:.2f}\n\n"
        f"📱 <b>الأرقام المتاحة للبيع:</b> {avail_count}\n"
        f"✅ <b>الأرقام المباعة:</b> {sold_count}"
    )
    await call.message.edit_text(text, reply_markup=admin_back_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "admin_users")
async def cq_admin_users(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.edit_text("ارسل آي دي (ID) المستخدم للبحث عنه:", reply_markup=admin_back_keyboard())
    await state.set_state(AdminState.waiting_for_user_id)

@router.message(AdminState.waiting_for_user_id)
async def process_user_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("يرجى إرسال ID صحيح (أرقام فقط).")
        return
        
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        
    if not user:
        await message.answer("المستخدم غير موجود في قاعدة البيانات.", reply_markup=admin_back_keyboard())
        return
        
    text = (
        f"👤 <b>بيانات المستخدم:</b>\n"
        f"<b>ID:</b> <code>{user.id}</code>\n"
        f"<b>رصيد المتجر:</b> <code>${user.balance_store:.3f if f'{user.balance_store:.3f}'[-1] != '0' else user.balance_store:.2f}</code>\n"
        f"<b>رصيد التوريد:</b> <code>${user.balance_sourcing:.3f if f'{user.balance_sourcing:.3f}'[-1] != '0' else user.balance_sourcing:.2f}</code>\n"
        f"<b>تاريخ الانضمام:</b> {user.join_date.strftime('%Y-%m-%d')}"
    )
    await message.answer(text, reply_markup=admin_user_keyboard(user.id), parse_mode="HTML")
    await state.clear()

# Add / Sub Balance callbacks
@router.callback_query(F.data.startswith("usr_add_"))
async def cq_add_balance(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    user_id = call.data.split("_")[2]
    await state.update_data(target_user_id=int(user_id))
    await call.message.edit_text(f"أرسل المبلغ الذي تود إضافته للمستخدم {user_id}:", reply_markup=admin_back_keyboard())
    await state.set_state(AdminState.waiting_for_add_balance)

@router.message(AdminState.waiting_for_add_balance)
async def process_add_balance(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    target_id = data.get("target_user_id")
    
    try:
        amount = float(message.text.strip())
        if amount <= 0: raise ValueError
    except ValueError:
        await message.answer("الرجاء إدخال مبلغ صحيح أكبر من 0.")
        return
        
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.id == target_id))).scalar_one_or_none()
        if user:
            user.balance_store += amount
            txn = Transaction(user_id=target_id, type=TransactionType.DEPOSIT, amount=amount)
            session.add(txn)
            
            # Referral Dynamic Bonus
            comm_percent = 1.0
            if user.referred_by:
                referrer = (await session.execute(select(User).where(User.id == user.referred_by))).scalar_one_or_none()
                if referrer:
                    from database import AppSetting
                    comm_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "referral_commission_percent"))).scalar_one_or_none()
                    comm_percent = float(comm_obj.value) if comm_obj and comm_obj.value else 1.0
                    
                    bonus = amount * (comm_percent / 100.0)
                    referrer.balance_store += bonus
                    referrer.referral_earnings = (referrer.referral_earnings or 0.0) + bonus
                    tx_ref = Transaction(user_id=referrer.id, type=TransactionType.REFERRAL, amount=bonus)
                    session.add(tx_ref)
                    
                    try:
                        from config import BOT_TOKEN
                        from aiogram import Bot
                        notify_bot = Bot(token=BOT_TOKEN)
                        await notify_bot.send_message(referrer.id, f"🎉 You received a referral bonus of ${bonus:.3f if f'{bonus:.3f}'[-1] != '0' else bonus:.2f} from a user's deposit!")
                        await notify_bot.session.close()
                    except Exception:
                        pass
            
            await session.commit()
            await message.answer(f"✅ تم إضافة ${amount:.3f if f'{amount:.3f}'[-1] != '0' else amount:.2f} لرصيد المتجر الخاص بالمستخدم بنجاح.\nالرصيد الجديد: ${user.balance_store:.3f if f'{user.balance_store:.3f}'[-1] != '0' else user.balance_store:.2f}\n(تم احتساب عمولة الإحالة بنسبة {comm_percent}%)", reply_markup=admin_back_keyboard())
        else:
            await message.answer("حدث خطأ، المستخدم غير موجود.")
    await state.clear()

@router.message(Command("test_deposit"))
async def cmd_test_deposit(message: Message):
    """Hidden command to simulate a deposit for testing on hosting."""
    if not is_admin(message.from_user.id): return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /test_deposit <amount>")
        return
        
    try:
        amount = float(args[1])
    except ValueError:
        await message.answer("Invalid amount.")
        return

    async with async_session() as session:
        user = (await session.execute(select(User).where(User.id == message.from_user.id))).scalar_one_or_none()
        if not user:
            await message.answer("User not found in DB.")
            return

        user.balance_store += amount
        session.add(Transaction(user_id=user.id, type=TransactionType.DEPOSIT, amount=amount))
        
        bonus_msg = ""
        if user.referred_by:
            referrer = (await session.execute(select(User).where(User.id == user.referred_by))).scalar_one_or_none()
            if referrer:
                from database import AppSetting
                comm_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "referral_commission_percent"))).scalar_one_or_none()
                comm_percent = float(comm_obj.value) if comm_obj and comm_obj.value else 1.0
                
                bonus = amount * (comm_percent / 100.0)
                referrer.balance_store += bonus
                referrer.referral_earnings = (referrer.referral_earnings or 0.0) + bonus
                session.add(Transaction(user_id=referrer.id, type=TransactionType.REFERRAL, amount=bonus))
                bonus_msg = f"\n✨ Referral Bonus of ${bonus:.3f if f'{bonus:.3f}'[-1] != '0' else bonus:.2f} credited to referrer {referrer.id} ({comm_percent}%)"
                
                try:
                    await message.bot.send_message(referrer.id, f"🎉 Test Referral Bonus: You received ${bonus:.3f if f'{bonus:.3f}'[-1] != '0' else bonus:.2f} from a referral deposit!")
                except: pass

        await session.commit()
        await message.answer(f"✅ Simulated Deposit of ${amount:.3f if f'{amount:.3f}'[-1] != '0' else amount:.2f} successful!{bonus_msg}")

@router.callback_query(F.data.startswith("usr_sub_"))
async def cq_sub_balance(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    user_id = call.data.split("_")[2]
    await state.update_data(target_user_id=int(user_id))
    await call.message.edit_text(f"أرسل المبلغ الذي تود خصمه من المستخدم {user_id}:", reply_markup=admin_back_keyboard())
    await state.set_state(AdminState.waiting_for_sub_balance)

@router.message(AdminState.waiting_for_sub_balance)
async def process_sub_balance(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    target_id = data.get("target_user_id")
    
    try:
        amount = float(message.text.strip())
        if amount <= 0: raise ValueError
    except ValueError:
        await message.answer("الرجاء إدخال مبلغ صحيح أكبر من 0.")
        return
        
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.id == target_id))).scalar_one_or_none()
        if user:
            if user.balance_store < amount:
                await message.answer("المبلغ المدخل أكبر من رصيد المتجر!", reply_markup=admin_back_keyboard())
                return
            user.balance_store -= amount
            await session.commit()
            await message.answer(f"✅ تم خصم ${amount:.3f if f'{amount:.3f}'[-1] != '0' else amount:.2f} من رصيد المتجر بنجاح.\nالرصيد المتبقي: ${user.balance_store:.3f if f'{user.balance_store:.3f}'[-1] != '0' else user.balance_store:.2f}", reply_markup=admin_back_keyboard())
        else:
            await message.answer("حدث خطأ، المستخدم غير موجود.")
    await state.clear()

@router.callback_query(F.data == "admin_broadcast")
async def cq_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.edit_text("الرجاء إرسال الرسالة التي تود تعميمها على جميع المستخدمين (نص، صورة، فيديو... يدعم كل شيء):", reply_markup=admin_back_keyboard())
    await state.set_state(AdminState.waiting_for_broadcast)

@router.message(AdminState.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id): return
    
    await message.answer("جاري الإرسال... 🚀")
    
    async with async_session() as session:
        users = (await session.execute(select(User.id))).scalars().all()
        
    success = 0
    fail = 0
    for uid in users:
        try:
            await message.copy_to(chat_id=uid)
            success += 1
        except Exception:
            fail += 1
            
    await message.answer(f"✅ تمت الإذاعة بنجاح!\n\nاستلمها: {success}\nفشل الإرسال لـ: {fail} (قد يكونوا أوقفوا البوت)", reply_markup=admin_main_keyboard())
    await state.clear()
    
@router.callback_query(F.data == "admin_stock")
async def cq_admin_stock(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await call.answer("إدارة تسعير ومخزون الأرقام سيتم برمجتها قريباً كجزء من اللوحة!", show_alert=True)
