import logging

import certifi
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING

from app.config import settings

log = logging.getLogger(__name__)


class Mongo:
    def __init__(self) -> None:
        self.client: AsyncIOMotorClient | None = None
        self.db = None

    @property
    def web_accounts(self):
        return self.db["web_accounts"]

    @property
    def web_sessions(self):
        return self.db["web_sessions"]

    async def connect(self) -> bool:
        uri = settings.mongodb_uri
        log.info("Проверка MongoDB URI: prefix=%r, len=%d", uri[:15], len(uri))
        if self.client is None:
            self.client = AsyncIOMotorClient(
                uri,
                tz_aware=True,
                serverSelectionTimeoutMS=5_000,
                connectTimeoutMS=5_000,
                tlsCAFile=certifi.where(),
            )
            self.db = self.client[settings.mongodb_db]

        try:
            await self.client.admin.command("ping")
            await self.users.create_index("telegram_id", unique=True)
            await self.vacancies.create_index("source_id", unique=True)
            await self.analysis_logs.create_index([("user_id", ASCENDING), ("vacancy_id", ASCENDING)], unique=True)
            await self.ensure_account_indexes()
            await self.chat_messages.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
            await self.web_sessions.create_index("token_hash", unique=True)
            await self.web_sessions.create_index("expires_at", expireAfterSeconds=0)
        except Exception as e:  # noqa: BLE001
            log.error("MongoDB пока недоступна: %s", e)
            return False

        log.info("MongoDB подключена, индексы созданы")
        return True
    
    

    async def ensure_account_indexes(self) -> None:
        await self.web_accounts.create_index("email", unique=True)
        await self.web_accounts.create_index("user_key", unique=True)
        # логин и Telegram есть не у всех старых аккаунтов, поэтому уникальность только для заполненных
        await self.web_accounts.create_index(
            "login", unique=True, partialFilterExpression={"login": {"$type": "string"}}
        )
        await self.web_accounts.create_index(
            "telegram_id", unique=True, partialFilterExpression={"telegram_id": {"$type": "number"}}
        )

    async def ping(self) -> bool:
        if self.client is None:
            return False
        try:
            await self.client.admin.command("ping")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        if self.client is not None:
            self.client.close()

    @property
    def users(self):
        return self.db["users"]

    @property
    def vacancies(self):
        return self.db["vacancies"]

    @property
    def analysis_logs(self):
        return self.db["analysis_logs"]

    @property
    def chat_messages(self):
        return self.db["chat_messages"]

    @property
    def web_accounts(self):
        return self.db["web_accounts"]

    @property
    def web_sessions(self):
        return self.db["web_sessions"]


mongo = Mongo()
