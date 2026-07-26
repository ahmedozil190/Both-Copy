import os
import shutil
import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Enum, Boolean, BigInteger, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from config import DATABASE_URL

Base = declarative_base()

# Engine creation
# Auto-migrate local DB to persistent volume if needed
if os.path.exists("/data") and not os.path.exists("/data/app.db"):
    if os.path.exists("app.db"):
        try:
            shutil.copy2("app.db", "/data/app.db")
            print("Successfully migrated app.db to /data/app.db")
        except Exception as e:
            print(f"Migration failed: {e}")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)

class AccountStatus(enum.Enum):
    AVAILABLE = "available"
    PENDING = "pending"
    SOLD = "sold"
    REJECTED = "rejected"
    
class WithdrawalStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class TransactionType(enum.Enum):
    DEPOSIT = "deposit"
    BUY = "buy"
    SELL = "sell"
    WITHDRAW = "withdraw"
    REFERRAL = "referral"

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True) # Telegram User ID
    balance_store = Column(Float, default=0.0)
    balance_sourcing = Column(Float, default=0.0)
    language = Column(String, default="ar")
    join_date = Column(DateTime, default=datetime.utcnow)
    full_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
     
    # Isolation flags
    is_active_store = Column(Boolean, default=False)
    is_active_sourcing = Column(Boolean, default=False)
    is_banned_store = Column(Boolean, default=False)
    is_banned_sourcing = Column(Boolean, default=False)
    
    # Mode: "store" (buyer) or "seller"
    current_mode = Column(String, default="store")

    # Referral System
    referred_by = Column(BigInteger, ForeignKey('users.id'), nullable=True)
    refer_count = Column(Integer, default=0) # Total number of people referred
    referral_earnings = Column(Float, default=0.0)
    referral_bonus_awarded = Column(Boolean, default=False)

class Account(Base):
    __tablename__ = 'accounts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String, unique=True, nullable=False)
    country = Column(String, nullable=False)
    session_string = Column(String, nullable=True)
    status = Column(Enum(AccountStatus), default=AccountStatus.AVAILABLE)
    price = Column(Float, nullable=False)
    seller_id = Column(BigInteger, ForeignKey('users.id'), nullable=True)
    buyer_id = Column(BigInteger, ForeignKey('users.id'), nullable=True)
    otp_code = Column(String, nullable=True)
    two_fa_password = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    purchased_at = Column(DateTime, nullable=True)
    # Locked values at submission time — immune to admin price/delay changes
    locked_buy_price = Column(Float, nullable=True)
    locked_approve_delay = Column(Integer, nullable=True)
    # Rejection reason for display in dashboards
    reject_reason = Column(String, nullable=True)
    
    # New fields for external servers
    server_id = Column(Integer, ForeignKey('api_servers.id'), nullable=True)
    hash_code = Column(String, nullable=True)
    
    # Withdrawal linking
    withdrawal_id = Column(Integer, ForeignKey('withdrawal_requests.id'), nullable=True)

class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    type = Column(Enum(TransactionType), nullable=False)
    amount = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

class CountryPrice(Base):
    __tablename__ = 'country_prices'
    id = Column(Integer, primary_key=True, autoincrement=True)
    country_code = Column(String, nullable=False) # e.g. "1" (Not unique anymore)
    iso_code = Column(String, default="XX") # e.g. "US", "CA"
    country_name = Column(String, nullable=False) # e.g. "Egypt"
    price = Column(Float, nullable=False, default=1.0) # Selling Price
    buy_price = Column(Float, nullable=False, default=0.5) # Buying Price from people
    approve_delay = Column(Integer, nullable=False, default=0) # Auto-approval delay in seconds
    log_quantity = Column(Integer, nullable=False, default=1000) # Quantity shown in channel log
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class UserCountryPrice(Base):
    __tablename__ = 'user_country_prices'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    country_code = Column(String, nullable=False) # e.g. "1"
    iso_code = Column(String, default="XX") # e.g. "US"
    buy_price = Column(Float, nullable=False)
    approve_delay = Column(Integer, nullable=False, default=0) # Custom auto-approval delay
    created_at = Column(DateTime, default=datetime.utcnow)

class UserStorePrice(Base):
    __tablename__ = 'user_store_prices'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    country_code = Column(String, nullable=False) # e.g. "1"
    iso_code = Column(String, default="XX") # e.g. "US"
    sell_price = Column(Float, nullable=False) # Custom discount selling price for buyers
    created_at = Column(DateTime, default=datetime.utcnow)

class WithdrawalRequest(Base):
    __tablename__ = 'withdrawal_requests'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    amount = Column(Float, nullable=False)
    method = Column(String, nullable=False) # e.g. "TRX - TRC20"
    address = Column(String, nullable=False) # Wallet Address
    fee = Column(Float, nullable=False, default=0.0) # Network fee deducted
    net_amount = Column(Float, nullable=False, default=0.0) # Amount to be sent after fee
    transaction_id = Column(String(12), unique=True, nullable=True) # e.g. "TC782794467F"
    status = Column(Enum(WithdrawalStatus), default=WithdrawalStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)

class Deposit(Base):
    __tablename__ = 'deposits'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    amount = Column(Float, nullable=False)
    txid = Column(String, unique=True, nullable=False) # Binance TxID
    method = Column(String, nullable=True) # Payment Method
    created_at = Column(DateTime, default=datetime.utcnow)

class AppSetting(Base):
    __tablename__ = 'app_settings'
    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)

class ApiServer(Base):
    __tablename__ = 'api_servers'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    api_key = Column(String, nullable=False)
    server_type = Column(String, default="standard") # 'standard' (Spider/Max) or 'lion' (TG-Lion)
    extra_id = Column(String, nullable=True) # For YourID in TG-Lion
    profit_margin = Column(Float, default=20.0)
    min_profit = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class SubscriptionChannel(Base):
    __tablename__ = 'subscription_channels'
    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_type = Column(String, default="store") # 'store' or 'sourcing'
    username = Column(String, nullable=False) # e.g. "@OzZoOSMS"
    link = Column(String, nullable=False) # e.g. "https://t.me/OzZoOSMS"
    created_at = Column(DateTime, default=datetime.utcnow)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
        # Auto-migration: Check columns for various tables
        try:
            # 1. withdrawal_requests.transaction_id
            def check_withdraw_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(withdrawal_requests)"))
                return [row[1] for row in cursor]
            
            w_cols = await conn.run_sync(check_withdraw_cols)
            if 'transaction_id' not in w_cols:
                await conn.execute(text("ALTER TABLE withdrawal_requests ADD COLUMN transaction_id VARCHAR(12)"))
                print("Successfully added transaction_id column to withdrawal_requests")
            
            # 2. deposits.method
            def check_deposit_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(deposits)"))
                return [row[1] for row in cursor]
            
            d_cols = await conn.run_sync(check_deposit_cols)
            if 'method' not in d_cols:
                await conn.execute(text("ALTER TABLE deposits ADD COLUMN method VARCHAR(50)"))
                print("Successfully added method column to deposits")

            # 3. accounts.server_id & hash_code
            def check_account_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(accounts)"))
                return [row[1] for row in cursor]
            
            a_cols = await conn.run_sync(check_account_cols)
            if 'server_id' not in a_cols:
                await conn.execute(text("ALTER TABLE accounts ADD COLUMN server_id INTEGER"))
                print("Successfully added server_id column to accounts")
            if 'hash_code' not in a_cols:
                await conn.execute(text("ALTER TABLE accounts ADD COLUMN hash_code TEXT"))
                print("Successfully added hash_code column to accounts")

            # 4. api_servers.server_type & extra_id
            def check_srv_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(api_servers)"))
                return [row[1] for row in cursor]
            
            s_cols = await conn.run_sync(check_srv_cols)
            if 'server_type' not in s_cols:
                await conn.execute(text("ALTER TABLE api_servers ADD COLUMN server_type VARCHAR(20) DEFAULT 'standard'"))
                print("Successfully added server_type column to api_servers")
            if 'extra_id' not in s_cols:
                await conn.execute(text("ALTER TABLE api_servers ADD COLUMN extra_id VARCHAR(100)"))
                print("Successfully added extra_id column to api_servers")

            # 5. country_prices.log_quantity
            def check_cp_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(country_prices)"))
                return [row[1] for row in cursor]
            
            cp_cols = await conn.run_sync(check_cp_cols)
            if 'log_quantity' not in cp_cols:
                await conn.execute(text("ALTER TABLE country_prices ADD COLUMN log_quantity INTEGER DEFAULT 1000"))
                print("Successfully added log_quantity column to country_prices")

            # 6. subscription_channels.bot_type
            def check_sub_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(subscription_channels)"))
                return [row[1] for row in cursor]
            
            try:
                sub_cols = await conn.run_sync(check_sub_cols)
                if 'bot_type' not in sub_cols:
                    await conn.execute(text("ALTER TABLE subscription_channels ADD COLUMN bot_type VARCHAR DEFAULT 'store'"))
                    print("Successfully added bot_type column to subscription_channels")
            except Exception:
                pass

            # 7. users.refer_count & referral_bonus_awarded
            def check_user_cols(connection):
                cursor = connection.execute(text("PRAGMA table_info(users)"))
                return [row[1] for row in cursor]
            
            try:
                u_cols = await conn.run_sync(check_user_cols)
                if 'refer_count' not in u_cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN refer_count INTEGER DEFAULT 0"))
                    print("Successfully added refer_count column to users")
                if 'referral_bonus_awarded' not in u_cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN referral_bonus_awarded BOOLEAN DEFAULT 0"))
                    print("Successfully added referral_bonus_awarded column to users")
            except Exception:
                pass
                
        except Exception as e:
            print(f"Migration check failed: {e}")
