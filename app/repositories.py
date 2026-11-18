import hashlib
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db import mongo

DEFAULT_FILTERS = {"keywords": [], "location": "Ташкент", "employment_type": "полная"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def resume_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


# ---------- users ----------
async def get_or_create_user(telegram_id: int) -> dict:
    return await mongo.users.find_one_and_update(
        {"telegram_id": telegram_id},
        {"$setOnInsert": {"created_at": _now(), "filters": dict(DEFAULT_FILTERS), "resume_text": ""}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def set_resume(telegram_id: int, text: str) -> None:
    await mongo.users.update_one({"telegram_id": telegram_id}, {"$set": {"resume_text": text}})


async def set_resume_if_empty(telegram_id: int, text: str, name: str = "") -> bool:
    result = await mongo.users.update_one(
        {
            "telegram_id": telegram_id,
            "$or": [{"resume_text": ""}, {"resume_text": {"$exists": False}}],
        },
        {"$set": {"resume_text": text, "resume_name": name}},
    )
    return result.modified_count == 1


async def clear_resume(telegram_id: int) -> None:
    await mongo.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"resume_text": ""}, "$unset": {"resume_name": "", "resume_profile": "", "resume_profile_hash": ""}},
    )


async def update_filters(telegram_id: int, **fields) -> None:
    await mongo.users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {f"filters.{k}": v for k, v in fields.items()}},
    )


async def merge_legacy_user(legacy_key: int, account_key: int) -> dict:
    """Присоединяет данные, которые человек завёл в боте до аккаунтов, к пользователю аккаунта.

    «Старый» пользователь: запись users с telegram_id = Telegram id, не принадлежащая никакому аккаунту.
    Данные аккаунта не перезаписываются: резюме переносится, только если у аккаунта его нет, фильтры только
    если у аккаунта нет должностей. Результаты анализа переносятся вместе с резюме, к которому они относятся.
    Возвращает {"moved": [...], "resume_conflict": bool}.
    """
    result = {"moved": [], "resume_conflict": False}
    if legacy_key == account_key:
        return result
    if await mongo.web_accounts.find_one({"user_key": legacy_key}):
        return result  # это данные другого аккаунта, чужие данные не трогаем
    legacy = await mongo.users.find_one({"telegram_id": legacy_key})
    if not legacy:
        return result
    target = await get_or_create_user(account_key)

    legacy_resume = (legacy.get("resume_text") or "").strip()
    target_resume = (target.get("resume_text") or "").strip()
    resume_settled = True  # резюме перенесено, совпадает с имеющимся или его не было
    if legacy_resume and not target_resume:
        if await set_resume_if_empty(account_key, legacy_resume, legacy.get("resume_name") or ""):
            result["moved"].append("resume")
        else:
            resume_settled = False
    elif legacy_resume and legacy_resume != target_resume:
        resume_settled = False
        result["resume_conflict"] = True

    legacy_filters = legacy.get("filters") or {}
    if legacy_filters.get("keywords") and not (target.get("filters") or {}).get("keywords"):
        await mongo.users.update_one(
            {"telegram_id": account_key}, {"$set": {"filters": {**DEFAULT_FILTERS, **legacy_filters}}}
        )
        result["moved"].append("filters")

    if legacy_resume and resume_settled:
        moved_logs = 0
        for log in await mongo.analysis_logs.find({"user_id": legacy["_id"]}).to_list(None):
            twin = await mongo.analysis_logs.find_one({"user_id": target["_id"], "vacancy_id": log["vacancy_id"]})
            if twin:
                await mongo.analysis_logs.delete_one({"_id": log["_id"]})  # у аккаунта свой анализ этой вакансии
            else:
                await mongo.analysis_logs.update_one({"_id": log["_id"]}, {"$set": {"user_id": target["_id"]}})
                moved_logs += 1
        if moved_logs:
            result["moved"].append("analyses")

    if resume_settled:  # всё, что можно, перенесено: старая запись больше не нужна
        await mongo.users.delete_one({"_id": legacy["_id"]})
    return result


async def save_resume_profile(user_id: ObjectId, rhash: str, profile: dict) -> None:
    """Результат «чтения» резюме ИИ. Привязан к хэшу текста: новое резюме читается заново."""
    await mongo.users.update_one(
        {"_id": user_id}, {"$set": {"resume_profile": profile, "resume_profile_hash": rhash}}
    )


# ---------- chat_messages ----------
async def add_chat_messages(user_id: ObjectId, messages: list[dict]) -> None:
    now = _now()
    # у каждого сообщения своё время (Mongo хранит миллисекунды): порядок «вопрос, ответ» не должен зависеть от случая
    docs = [
        {"user_id": user_id, "created_at": now + timedelta(milliseconds=i), **m} for i, m in enumerate(messages)
    ]
    if docs:
        await mongo.chat_messages.insert_many(docs)


async def get_chat_history(user_id: ObjectId, limit: int = 30) -> list[dict]:
    docs = await mongo.chat_messages.find({"user_id": user_id}).sort([("created_at", -1), ("_id", -1)]).limit(limit).to_list(None)
    return list(reversed(docs))


async def clear_chat(user_id: ObjectId) -> None:
    await mongo.chat_messages.delete_many({"user_id": user_id})


# ---------- vacancies ----------
async def get_vacancies_by_source_ids(source_ids: list[str]) -> dict[str, dict]:
    if not source_ids:
        return {}
    docs = await mongo.vacancies.find({"source_id": {"$in": source_ids}}).to_list(None)
    return {d["source_id"]: d for d in docs}


async def save_vacancy(vacancy: dict) -> dict:
    try:
        return await mongo.vacancies.find_one_and_update(
            {"source_id": vacancy["source_id"]},
            {"$set": vacancy},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:  # параллельный upsert той же вакансии
        return await mongo.vacancies.find_one({"source_id": vacancy["source_id"]})


async def get_vacancy(vacancy_id: ObjectId) -> dict | None:
    return await mongo.vacancies.find_one({"_id": vacancy_id})


# ---------- analysis_logs ----------
async def get_analyses_map(user_id: ObjectId, vacancy_ids: list[ObjectId], rhash: str) -> dict[ObjectId, dict]:
    """Уже готовые анализы для текущей версии резюме: {vacancy_id: analysis}."""
    if not vacancy_ids:
        return {}
    docs = await mongo.analysis_logs.find(
        {"user_id": user_id, "vacancy_id": {"$in": vacancy_ids}, "resume_hash": rhash}
    ).to_list(None)
    return {d["vacancy_id"]: d for d in docs}


async def get_analysis(user_id: ObjectId, vacancy_id: ObjectId, rhash: str) -> dict | None:
    return await mongo.analysis_logs.find_one({"user_id": user_id, "vacancy_id": vacancy_id, "resume_hash": rhash})


async def save_analysis(user_id: ObjectId, vacancy_id: ObjectId, data: dict) -> dict:
    return await mongo.analysis_logs.find_one_and_update(
        {"user_id": user_id, "vacancy_id": vacancy_id},
        {"$set": {**data, "processed_at": _now()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
