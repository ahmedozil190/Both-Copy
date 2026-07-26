import asyncio
import logging
import sys
import os
from datetime import datetime, timedelta

import phonenumbers
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from uvicorn import Config, Server

from config import BOT_TOKEN
from database import init_db, async_session, Account, AccountStatus, CountryPrice, User, Transaction, TransactionType
from sqlalchemy import select
from services.i18n import get_text
from handlers import main_router
from web_admin import app
from middlewares import UnifiedMiddleware

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def auto_approve_task(bot: Bot):
    """Background task to automatically approve pending accounts after delay."""
    from services.session_manager import is_session_alive
    logger.info("Starting Auto-Approve background task...")
    while True:
        try:
            async with async_session() as session:
                stmt = select(Account).where(Account.status == AccountStatus.PENDING)
                pending_accs = (await session.execute(stmt)).scalars().all()
                
                for acc in pending_accs:
                    try:
                        approve_delay = acc.locked_approve_delay
                        buy_price = acc.locked_buy_price

                        if approve_delay is None or buy_price is None:
                            # Legacy account fallback
                            p = phonenumbers.parse(acc.phone_number)
                            country_code = str(p.country_code)
                            target_iso = phonenumbers.region_code_for_number(p) or 'XX'
                            cp_stmt = select(CountryPrice).where(
                                CountryPrice.country_code == country_code,
                                CountryPrice.iso_code == target_iso
                            )
                            cp = (await session.execute(cp_stmt)).scalar()
                            approve_delay = approve_delay if approve_delay is not None else (cp.approve_delay if cp else 0)
                            buy_price = buy_price if buy_price is not None else (cp.buy_price if cp else 0)

                        delay_delta = timedelta(seconds=approve_delay)
                        if datetime.utcnow() >= (acc.created_at + delay_delta):
                            # Pre-Approval Verification Check
                            is_alive, reject_reason = await is_session_alive(acc.session_string)
                            
                            # Get seller with row locking to prevent race conditions
                            seller = await session.get(User, acc.seller_id, with_for_update=True)
                            
                            if is_alive:
                                sessions_count = 1
                                try:
                                    from services.session_manager import create_client
                                    from pyrogram.raw.functions.auth import ResetAuthorizations
                                    client = await create_client(acc.session_string)
                                    await client.connect()
                                    
                                    from pyrogram.raw.functions.account import GetAuthorizations
                                    auth_result = await client.invoke(GetAuthorizations())
                                    sessions_count = len(auth_result.authorizations)

                                    if sessions_count > 1:
                                        try:
                                            await client.invoke(ResetAuthorizations())
                                            logger.info(f"[SessionManager] Terminated other sessions for {acc.phone_number} successfully.")
                                            sessions_count = 1
                                        except Exception as e:
                                            err_str = str(e).lower()
                                            if "fresh_reset_authorisation_forbidden" in err_str:
                                                logger.info(f"[SessionManager] Cannot terminate sessions for {acc.phone_number} yet (24h restriction).")
                                            else:
                                                logger.warning(f"[SessionManager] Failed to reset auths for {acc.phone_number}: {e}")
                                    
                                    from services.session_manager import perform_full_wipe
                                    await perform_full_wipe(client)
                                    await client.disconnect()
                                except Exception as e:
                                    logger.warning(f"[SessionManager] Could not verify/terminate sessions for {acc.phone_number}: {e}")

                            # Get seller again (with lock)
                            seller = await session.get(User, acc.seller_id, with_for_update=True)

                            if is_alive:
                                if sessions_count > 1:
                                    # Delay approval by exactly 24 hours from NOW
                                    acc.created_at = datetime.utcnow() + timedelta(hours=24)
                                    logger.info(f"Delayed approval for {acc.phone_number} by 24h due to active sessions.")
                                    
                                    if seller:
                                        try:
                                            lang = seller.language if seller else "ar"
                                            msg = get_text("pending_sessions", lang, phone=acc.phone_number)
                                            await bot.send_message(
                                                seller.id,
                                                msg,
                                                parse_mode="HTML"
                                            )
                                        except Exception as n_err:
                                            logger.warning(f"Failed to send delay notification to seller: {n_err}")
                                    
                                    await session.commit()
                                    continue

                                # Auto-Approve!
                                acc.status = AccountStatus.AVAILABLE
                                logger.info(f"[AutoApprove] Approving {acc.phone_number} | seller_id={acc.seller_id} | buy_price={buy_price}")
                                
                                if seller:
                                    seller.balance_sourcing += buy_price
                                    tx = Transaction(user_id=seller.id, type=TransactionType.SELL, amount=buy_price)
                                    session.add(tx)
                                    
                                    try:
                                        lang = seller.language if seller else "ar"
                                        msg = get_text("number_approved", lang, phone=acc.phone_number, amount=buy_price)
                                        await bot.send_message(
                                            seller.id,
                                            msg,
                                            parse_mode="HTML"
                                        )
                                        logger.info(f"[AutoApprove] Notified seller {seller.id}")
                                    except Exception as n_err:
                                        logger.error(f"[AutoApprove] Failed to notify seller {seller.id}: {n_err}")
                                else:
                                    logger.warning(f"[AutoApprove] No seller found for seller_id={acc.seller_id}")
                            else:
                                # Reject due to ban/freeze
                                acc.status = AccountStatus.REJECTED
                                acc.reject_reason = reject_reason
                                logger.info(f"[AutoApprove] Rejecting {acc.phone_number} | reason={reject_reason} | seller_id={acc.seller_id}")
                                if seller:
                                    try:
                                        lang = seller.language if seller else "ar"
                                        reason_text = get_text(reject_reason, lang)
                                        msg = get_text("number_rejected", lang, phone=acc.phone_number, reason=reason_text)
                                        await bot.send_message(
                                            seller.id,
                                            msg,
                                            parse_mode="HTML"
                                        )
                                        logger.info(f"[AutoApprove] Rejection notification sent to seller {seller.id}")
                                    except Exception as n_err:
                                        logger.error(f"[AutoApprove] Failed to send rejection to seller {seller.id}: {n_err}")
                                else:
                                    logger.warning(f"[AutoApprove] No seller found to notify rejection for seller_id={acc.seller_id}")
                    except Exception as item_err:
                        logger.error(f"Error processing pending account {acc.id}: {item_err}")
                
                await session.commit()
        except Exception as e:
            logger.error(f"Auto-approve loop error: {e}")
        
        await asyncio.sleep(60)

async def start_bot_service(dp: Dispatcher, bot: Bot, name: str):
    """Safely starts a bot service."""
    try:
        me = await bot.get_me()
        logger.info(f"✅ SUCCESS: Unified {name} Bot (@{me.username}) is connected and starting!")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"❌ FATAL ERROR in {name} Bot connection: {e}")

async def main():
    logger.info("Initializing Unified Bot Ecosystem...")
    
    # 1. Database
    try:
        await init_db()
        logger.info("Database initialized.")
    except Exception as e:
        logger.error(f"Database init failed: {e}")
        return

    # 2. Setup Bot
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing!")
        return
        
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(main_router)
    
    # Register Unified Middleware
    dp.update.outer_middleware(UnifiedMiddleware())

    # 3. Attach bot to app state for Web Admin panel access
    app.state.bot_buyer = bot
    app.state.bot_seller = bot  # WebAdmin might reference bot_seller; we map both to the same instance!
    
    # 4. Web Server Task
    port = int(os.environ.get("PORT", 8000))
    config = Config(app=app, host="0.0.0.0", port=port, log_level="info")
    server = Server(config)
    web_task = asyncio.create_task(server.serve())
    logger.info(f"Web Admin Panel task created on port {port}.")

    # 5. Background Helper Tasks
    tasks = [web_task]
    
    # 6. Set Bot Commands (Side Menu)
    from aiogram.types import BotCommand
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Start")
        ])
        logger.info("Bot commands set successfully.")
    except Exception as e:
        logger.error(f"Failed to set bot commands: {e}")

    # 7. Start Polling Tasks
    tasks.append(asyncio.create_task(start_bot_service(dp, bot, "Store/Seller")))
    tasks.append(asyncio.create_task(auto_approve_task(bot)))

    # Wait for completion
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
