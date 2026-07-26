import logging
from aiogram import Router, Bot, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.future import select
from sqlalchemy import func
from database.models import User, AppSetting, Transaction, TransactionType
from database.engine import async_session
from keyboards import store_keyboard, seller_keyboard
from config import STORE_URL, WEBAPP_URL
from services.i18n import get_text

logger = logging.getLogger(__name__)
router = Router()

async def update_user_menu_button(bot: Bot, user_id: int, mode: str):
    """Updates the Telegram Menu Button (web app url) based on mode."""
    try:
        url = STORE_URL if mode == "store" else f"{WEBAPP_URL}/seller?v=3"
        await bot.set_chat_menu_button(
            chat_id=user_id,
            menu_button=MenuButtonWebApp(text="Open", web_app=WebAppInfo(url=url))
        )
    except Exception as e:
        logger.error(f"Error updating menu button for {user_id}: {e}")

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, db_user: User = None):
    user_id = message.from_user.id
    
    # Extract referral ID
    args = message.text.split()
    referral_id = None
    if len(args) > 1:
        start_param = args[1]
        if start_param.startswith("REF"):
            try:
                referral_id = int(start_param.replace("REF", ""))
            except ValueError:
                pass
        else:
            try:
                referral_id = int(start_param)
            except ValueError:
                pass

    async with async_session() as session:
        # Check active referral and award join bonus
        if db_user and db_user.referred_by and not db_user.referral_bonus_awarded:
            referrer_id = db_user.referred_by
            referrer = (await session.execute(select(User).where(User.id == referrer_id))).scalar_one_or_none()
            if referrer:
                bonus_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "referral_join_bonus"))).scalar_one_or_none()
                bonus_val = float(bonus_obj.value) if bonus_obj and bonus_obj.value else 0.005
                
                referrer.balance_store = (referrer.balance_store or 0.0) + bonus_val
                referrer.referral_earnings = (referrer.referral_earnings or 0.0) + bonus_val
                referrer.refer_count = (referrer.refer_count or 0) + 1
                
                # Merge current user to update the flag
                db_user = await session.merge(db_user)
                db_user.referral_bonus_awarded = True
                
                txn = Transaction(user_id=referrer_id, type=TransactionType.REFERRAL, amount=bonus_val)
                session.add(txn)
                await session.commit()
                logger.info(f"Referral Awarded: User {user_id} joined via {referrer_id}, awarded ${bonus_val}")

                # Notify referrer
                try:
                    ref_lang = referrer.language if referrer.language else "ar"
                    formatted_bonus = f"{bonus_val:.3f}" if f"{bonus_val:.3f}"[-1] != '0' else f"{bonus_val:.2f}"
                    msg_text = get_text("referral_earned", ref_lang, amount=formatted_bonus)
                    await bot.send_message(referrer_id, msg_text, parse_mode="HTML")
                except Exception as send_err:
                    logger.error(f"Failed to send referral notification to {referrer_id}: {send_err}")

        # Get fresh mode
        mode = db_user.current_mode if db_user else "store"

    # Set appropriate WebApp Menu Button
    await update_user_menu_button(bot, user_id, mode)

    # Send Welcome Message based on Mode
    if mode == "seller":
        await message.answer(
            "Welcome to the Sourcing Panel! 🚀\nClick the button below to open.",
            reply_markup=seller_keyboard(),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "Welcome to the Store! 🛒\nClick the button below to open.",
            reply_markup=store_keyboard(),
            parse_mode="HTML"
        )


@router.message(Command("switch"))
async def cmd_switch(message: Message, bot: Bot, db_user: User):
    user_id = message.from_user.id
    new_mode = "seller" if db_user.current_mode == "store" else "store"
    
    async with async_session() as session:
        # Get user model inside write transaction
        user = await session.get(User, user_id)
        if user:
            user.current_mode = new_mode
            if new_mode == "store" and not user.is_active_store:
                user.is_active_store = True
            elif new_mode == "seller" and not user.is_active_sourcing:
                user.is_active_sourcing = True
            await session.commit()

    # Update Telegram Menu Button
    await update_user_menu_button(bot, user_id, new_mode)

    if new_mode == "seller":
        await message.answer(
            "🔄 Switched to Seller Mode! 🚀\nCheck the menu button or click Open below.",
            reply_markup=seller_keyboard(),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "🔄 Switched to Buyer Mode! 🛒\nCheck the menu button or click Open below.",
            reply_markup=store_keyboard(),
            parse_mode="HTML"
        )


@router.callback_query(F.data == "switch_mode")
async def cq_switch_mode(call: CallbackQuery, bot: Bot, db_user: User):
    user_id = call.from_user.id
    new_mode = "seller" if db_user.current_mode == "store" else "store"
    
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user:
            user.current_mode = new_mode
            if new_mode == "store" and not user.is_active_store:
                user.is_active_store = True
            elif new_mode == "seller" and not user.is_active_sourcing:
                user.is_active_sourcing = True
            await session.commit()

    # Update Telegram Menu Button
    await update_user_menu_button(bot, user_id, new_mode)
    await call.answer("Switched mode successfully / تم تحويل الوضع", show_alert=False)

    if new_mode == "seller":
        await call.message.edit_text(
            "🔄 Switched to Seller Mode! 🚀\nClick the button below to open.",
            reply_markup=seller_keyboard()
        )
    else:
        await call.message.edit_text(
            "🔄 Switched to Buyer/Store Mode! 🛒\nClick the button below to open.",
            reply_markup=store_keyboard()
        )


@router.callback_query(lambda c: c.data == "my_referral")
async def cq_my_referral(call: CallbackQuery, bot: Bot):
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.id == call.from_user.id))).scalar_one_or_none()
        if not user:
            return
            
        refs_count = (await session.execute(select(func.count(User.id)).where(User.referred_by == user.id))).scalar() or 0
        
        # Fetch dynamic settings
        from database.models import AppSetting
        bonus_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "referral_join_bonus"))).scalar_one_or_none()
        comm_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "referral_commission_percent"))).scalar_one_or_none()
        
        bonus_val = float(bonus_obj.value) if bonus_obj and bonus_obj.value else 0.005
        comm_val = float(comm_obj.value) if comm_obj and comm_obj.value else 1.0
        
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=REF{user.id}"
    
    text = (
        "Share your referral link with your friends or channels and earn rewards:\n"
        f"• <b>${bonus_val:.3f if f'{bonus_val:.3f}'[-1] != '0' else bonus_val:.2f}</b> for each person who joins.\n"
        f"• <b>{comm_val}% commission</b> on all their deposits!\n\n"
        f"🔗 <b>Your Link:</b>\n<code>{ref_link}</code>\n\n"
        f"👥 <b>Total Referrals:</b> {refs_count}\n"
        f"💰 <b>Total Earnings:</b> ${user.referral_earnings or 0.0:.3f if f'{user.referral_earnings or 0.0:.3f}'[-1] != '0' else user.referral_earnings or 0.0:.2f}"
    )
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back 🔙", callback_data="back_main")]
    ])
    
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

@router.callback_query(lambda c: c.data == "back_main")
async def cq_back_main(call: CallbackQuery, db_user: User):
    mode = db_user.current_mode if db_user else "store"
    if mode == "seller":
        await call.message.edit_text(
            "Welcome to the Sourcing Panel! 🚀\nClick the button below to open.",
            reply_markup=seller_keyboard()
        )
    else:
        await call.message.edit_text(
            "Welcome to the Store! 🛒\nClick the button below to open.",
            reply_markup=store_keyboard()
        )
