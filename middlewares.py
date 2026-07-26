import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject, Message, CallbackQuery, Update, InlineKeyboardMarkup, InlineKeyboardButton, User as TGUser
from sqlalchemy import select, func
from database import async_session, User, AppSetting, SubscriptionChannel, Transaction, TransactionType

logger = logging.getLogger(__name__)

class UnifiedMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        try:
            # 1. Extract User Info
            tg_user: TGUser = data.get("event_from_user")
            if not tg_user or tg_user.is_bot:
                return await handler(event, data)

            user_id = tg_user.id
            full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip() or None
            username = tg_user.username or None

            # Determine event target for sending messages
            target = None
            if isinstance(event, Message):
                target = event
            elif isinstance(event, CallbackQuery):
                target = event.message
            elif isinstance(event, Update):
                if event.message:
                    target = event.message
                elif event.callback_query:
                    target = event.callback_query.message

            # Admins check
            import config
            is_admin = user_id in config.ADMIN_IDS

            # 2. Database Session
            async with async_session() as session:
                # Get or Create User
                stmt = select(User).where(User.id == user_id)
                result = await session.execute(stmt)
                user = result.scalar_one_or_none()

                # Referral check if new or no referrer
                referral_id = None
                if not user or not user.referred_by:
                    msg = None
                    if isinstance(event, Message):
                        msg = event
                    elif isinstance(event, Update) and event.message:
                        msg = event.message

                    if msg and msg.text and msg.text.startswith('/start') and len(msg.text.split()) > 1:
                        start_param = msg.text.split()[1]
                        if start_param.startswith("REF"):
                            try:
                                referral_id = int(start_param.replace("REF", ""))
                            except:
                                pass
                        else:
                            try:
                                referral_id = int(start_param)
                            except:
                                pass

                is_new_join = False
                if not user:
                    user = User(
                        id=user_id,
                        full_name=full_name,
                        username=username,
                        current_mode="store",
                        is_active_store=True,
                        referred_by=referral_id if (referral_id and referral_id != user_id) else None,
                        referral_bonus_awarded=False
                    )
                    session.add(user)
                    is_new_join = True
                    logger.info(f"UnifiedMiddleware: Created new user {user_id} with referrer_by={user.referred_by}")
                else:
                    changed = False
                    if not user.referred_by and referral_id and referral_id != user_id:
                        user.referred_by = referral_id
                        changed = True
                        logger.info(f"UnifiedMiddleware: Updated referral for user {user_id} to {referral_id}")

                    # Auto activate current mode flags
                    if user.current_mode == "store" and not user.is_active_store:
                        user.is_active_store = True
                        changed = True
                        is_new_join = True
                    elif user.current_mode == "seller" and not user.is_active_sourcing:
                        user.is_active_sourcing = True
                        changed = True
                        is_new_join = True

                    if user.full_name != full_name:
                        user.full_name = full_name
                        changed = True
                    if user.username != username:
                        user.username = username
                        changed = True

                    if changed:
                        logger.info(f"UnifiedMiddleware: Updated info for user {user_id}")

                # Save user state for handlers
                data["db_user"] = user
                current_mode = user.current_mode or "store"

                await session.commit()

                # Get fresh instance to avoid expired state
                stmt = select(User).where(User.id == user_id)
                result = await session.execute(stmt)
                user = result.scalar_one_or_none()
                data["db_user"] = user

                if is_new_join:
                    bot: Bot = data.get("bot")
                    if bot:
                        await self._send_join_log(bot, tg_user, user.referred_by, current_mode)

                # 3. Ban Check
                if current_mode == "store" and user.is_banned_store:
                    markup = None
                    lbl = "🚫 Your account has been suspended."
                    supp_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "store_support_username"))).scalar_one_or_none()
                    if not supp_obj:
                        supp_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none()
                    if supp_obj and supp_obj.value:
                        markup = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="Contact Support 🎧", url=f"https://t.me/{supp_obj.value.strip()}")]
                        ])
                    if isinstance(event, Message):
                        await event.answer(lbl, reply_markup=markup, parse_mode="HTML")
                    elif isinstance(event, CallbackQuery):
                        await event.answer(lbl.replace("<b>", "").replace("</b>", ""), show_alert=True)
                    return

                if current_mode == "seller" and user.is_banned_sourcing:
                    markup = None
                    lbl = "🚫 Your seller account has been suspended."
                    supp_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "sourcing_support_username"))).scalar_one_or_none()
                    if not supp_obj:
                        supp_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "SUPPORT_USERNAME"))).scalar_one_or_none()
                    if supp_obj and supp_obj.value:
                        markup = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="Contact Support 🎧", url=f"https://t.me/{supp_obj.value.strip()}")]
                        ])
                    if isinstance(event, Message):
                        await event.answer(lbl, reply_markup=markup, parse_mode="HTML")
                    elif isinstance(event, CallbackQuery):
                        await event.answer("Seller Account Suspended 🚫", show_alert=True)
                    return

                # 4. Maintenance Check — /start always bypasses maintenance
                is_start_command = False
                if isinstance(event, Message) and event.text and event.text.startswith('/start'):
                    is_start_command = True

                if not is_admin and not is_start_command:
                    m_key = "STORE_UNDER_MAINTENANCE" if current_mode == "store" else "SOURCING_UNDER_MAINTENANCE"
                    m_setting = (await session.execute(select(AppSetting).where(AppSetting.key == m_key))).scalar_one_or_none()
                    if m_setting and str(m_setting.value).lower() == "true":
                        ch_key = "store_updates_channel" if current_mode == "store" else "sourcing_updates_channel"
                        ch_obj = (await session.execute(select(AppSetting).where(AppSetting.key == ch_key))).scalar_one_or_none()
                        if not ch_obj:
                            ch_obj = (await session.execute(select(AppSetting).where(AppSetting.key == "UPDATES_CHANNEL"))).scalar_one_or_none()
                        ch_link = ch_obj.value if ch_obj else None
                        markup = None
                        if ch_link:
                            markup = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="Updates Channel 📢", url=ch_link)]
                            ])

                        lbl = "<b>⚠️ The bot is currently under maintenance.</b>"
                        if isinstance(event, Message):
                            await event.answer(lbl, parse_mode="HTML", reply_markup=markup)
                        elif isinstance(event, CallbackQuery):
                            await event.answer("Maintenance Mode ⚠️", show_alert=True)
                        return

                # 5. Subscription Check
                if not is_admin:
                    ch_bot_type = "store" if current_mode == "store" else "sourcing"
                    sub_channels = (await session.execute(select(SubscriptionChannel).where(SubscriptionChannel.bot_type == ch_bot_type))).scalars().all()
                    if sub_channels:
                        bot: Bot = data.get("bot")
                        not_subscribed = []
                        for ch in sub_channels:
                            try:
                                member = await bot.get_chat_member(chat_id=ch.username, user_id=user_id)
                                if member.status in ["left", "kicked"]:
                                    not_subscribed.append(ch)
                            except Exception as ex:
                                logger.error(f"Error checking sub for {ch.username}: {ex}")
                                continue

                        if not_subscribed:
                            buttons = []
                            for ch in not_subscribed:
                                link = ch.link if ch.link.startswith("http") else f"https://t.me/{ch.username.replace('@','')}"
                                buttons.append([InlineKeyboardButton(text="Join Channel", url=link)])
                            
                            kb = InlineKeyboardMarkup(inline_keyboard=buttons)
                            msg = (
                                "🔒 <b>Subscription Required</b>\n\n"
                                "Sorry, you must join our channel first to use the bot:\n\n"
                                "✅ <b>After joining, send /start</b>"
                            )
                            if isinstance(event, Message):
                                await event.answer(msg, reply_markup=kb, parse_mode="HTML")
                            elif isinstance(event, CallbackQuery):
                                await event.message.answer(msg, reply_markup=kb, parse_mode="HTML")
                                await event.answer()
                            return

            # Proceed to handlers
            return await handler(event, data)

        except Exception as e:
            logger.error(f"UnifiedMiddleware critical failure: {e}")
            return await handler(event, data)

    async def _send_join_log(self, bot: Bot, tg_user: TGUser, referred_by: int | None, current_mode: str):
        try:
            setting_key = "store_join_log_channel_id" if current_mode == "store" else "sourcing_join_log_channel_id"
            async with async_session() as session:
                obj = (await session.execute(select(AppSetting).where(AppSetting.key == setting_key))).scalar_one_or_none()
                if not obj or not obj.value or not obj.value.strip():
                    return
                channel_id_raw = obj.value.strip()

                referrer_line = "—"
                if referred_by:
                    referrer = (await session.execute(select(User).where(User.id == referred_by))).scalar_one_or_none()
                    if referrer:
                        ref_name = referrer.full_name or str(referrer.id)
                        ref_user = f" — @{referrer.username}" if referrer.username else ""
                        referrer_line = f"{ref_name}{ref_user} — <code>{referrer.id}</code>"
                    else:
                        referrer_line = f"<code>{referred_by}</code>"

            if channel_id_raw.lstrip("-").isdigit():
                channel_id = int(channel_id_raw)
            else:
                channel_id = channel_id_raw

            bot_label = "SKELETON TG STORE" if current_mode == "store" else "SKELETON TG SELL"
            full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip() or "—"
            username_line = f"@{tg_user.username}" if tg_user.username else "—"

            text = (
                f"🔔 <b>New Member Joined!</b>\n"
                f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
                f"👤  <b>{full_name}</b>\n\n"
                f"🏷️  <b>{username_line}</b>\n\n"
                f"🆔  <b>{tg_user.id}</b>\n\n"
                f"🤖  <b>{bot_label}</b>\n\n"
                f"🔗  <b>{referrer_line}</b>\n\n"
                f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
            )
            await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"UnifiedMiddleware join log fail: {e}")
