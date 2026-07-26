from .start import router as start_router
from .admin import router as admin_router
from .seller import router as seller_router
from aiogram import Router

main_router = Router()
main_router.include_router(admin_router)
main_router.include_router(start_router)
main_router.include_router(seller_router)
