from pathlib import Path
from fastapi.staticfiles import StaticFiles
from app.web.api import router as web_router

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from app import container
from app.bot.handlers import router
from app.config import settings
from app.db import mongo
from app.web.api import router as web_router
from pathlib import Path

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


async def _run_long_polling(
    bot: Bot,
    dp: Dispatcher,
    mongo_ready: asyncio.Event | None = None,
) -> None:
    if mongo_ready:
        await mongo_ready.wait()

    retry_delay = 5
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            log.info("Запущен режим long polling")
            await dp.start_polling(
                bot,
                handle_signals=False,
                allowed_updates=dp.resolve_used_update_types(),
            )
            return
        except asyncio.CancelledError:
            raise
        except TelegramNetworkError as e:
            log.warning(
                "Нет соединения с Telegram API: %s. Повторная попытка через %s сек.",
                e,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


async def _connect_mongo_with_retry(ready: asyncio.Event) -> None:
    retry_delay = 5
    while True:
        try:
            if await mongo.connect():
                ready.set()
                return
        except Exception as e:  # noqa: BLE001
            log.error("Ошибка подключения к MongoDB: %s", e)

        log.warning("MongoDB недоступна. Повторная попытка через %s сек.", retry_delay)
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)


async def _configure_webhook(bot: Bot, dp: Dispatcher, mongo_ready: asyncio.Event) -> None:
    await mongo_ready.wait()
    retry_delay = 5
    while True:
        try:
            await bot.set_webhook(
                url=f"{settings.webhook_base_url.rstrip('/')}/webhook",
                secret_token=settings.webhook_secret,
                drop_pending_updates=False,
                allowed_updates=dp.resolve_used_update_types(),
            )
            log.info("Запущен режим webhook")
            return
        except TelegramNetworkError as e:
            log.warning(
                "Нет соединения с Telegram API: %s. Повторная попытка через %s сек.",
                e,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.webhook_base_url and not settings.webhook_secret:
        raise RuntimeError("Для webhook-режима задайте WEBHOOK_SECRET")

    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    app.state.bot, app.state.dp = bot, dp

    mongo_ready = asyncio.Event()
    mongo_task = asyncio.create_task(_connect_mongo_with_retry(mongo_ready))
    polling_task = None
    webhook_task = None
    if settings.webhook_base_url:
        webhook_task = asyncio.create_task(_configure_webhook(bot, dp, mongo_ready))
        log.info("Задача настройки webhook запущена")
    else:
        polling_task = asyncio.create_task(_run_long_polling(bot, dp, mongo_ready))
        log.info("Задача long polling запущена, ожидается подключение MongoDB")

    try:
        yield
    finally:
        for task in (polling_task, webhook_task, mongo_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        for t in list(_tasks):
            t.cancel()
        with suppress(Exception):
            await bot.session.close()
        await container.ai.aclose()
        await container.scraper.aclose()
        await mongo.close()


app = FastAPI(title="Job Assistant", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok" if await mongo.ping() else "degraded"}


@app.post("/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if x_telegram_bot_api_secret_token != settings.webhook_secret or not settings.webhook_secret:
        raise HTTPException(status_code=403, detail="forbidden")
    bot, dp = request.app.state.bot, request.app.state.dp
    update = Update.model_validate(await request.json(), context={"bot": bot})
    # отвечаем Telegram сразу, обработка идёт в фоне, не блокируя других пользователей
    task = asyncio.create_task(dp.feed_update(bot, update))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"ok": True}

app.include_router(web_router)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "web" / "static", html=True), name="web")

# ---------- веб-интерфейс (дополнительно к Telegram-боту) ----------
if settings.web_enabled:
    app.include_router(web_router)
    # монтируется последним: /health, /webhook и /api имеют приоритет
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "web" / "static", html=True), name="web")
