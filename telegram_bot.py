"""
telegram_bot.py — Telethon + FastAPI сервис (под Render/n8n)

Функции:
- POST /dialogs         — список диалогов (группы/чаты/каналы/юзеры)
- POST /export_members  — экспорт участников по username ИЛИ по chat_id (если username = null)

ВАЖНО:
Ты попросил “сразу вставить все данные” (API_ID/API_HASH). Это будет работать,
но лучше хранить их в переменных окружения (Render Environment) — безопаснее.
"""

import os
import json
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, model_validator
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User  # type: ignore


# ===================== ВСТАВЛЕННЫЕ ДАННЫЕ (как просил) =====================
API_ID = 35934203
API_HASH = "bee9dcdda52b88bfb22d2db54d142445"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Хранилище: имя → клиент
ACTIVE_CLIENTS: Dict[str, TelegramClient] = {}

# Изменяем формат: добавляем флаг needs_2fa
PENDING_AUTH: Dict[str, Dict[str, Any]] = {}
# Формат: {phone: {"session_str": "...", "phone_code_hash": "...", "needs_2fa": False}}
# ===========================================================================


# -------------------- helpers: sessions --------------------
def _load_sessions_from_env() -> Dict[str, str]:
    """
    Поддерживаем:
      - TG_SESSIONS_JSON='{"test5":"<STRING_SESSION_1>","acc2":"<STRING_SESSION_2>"}'
      - или TG_SESSION_STRING + TG_ACCOUNT_NAME
    """
    sessions: Dict[str, str] = {}

    raw_json = (os.getenv("TG_SESSIONS_JSON") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        sessions[k.strip()] = v.strip()
        except Exception:
            pass

    if not sessions:
        single = (os.getenv("TG_SESSION_STRING") or "").strip()
        name = (os.getenv("TG_ACCOUNT_NAME") or "default").strip() or "default"
        if single:
            sessions[name] = single

    return sessions


async def _ensure_clients_started() -> None:
    sessions = _load_sessions_from_env()
    if not sessions:
        raise RuntimeError("No sessions provided. Set TG_SESSIONS_JSON or TG_SESSION_STRING.")

    for account, session_str in sessions.items():
        if account in ACTIVE_CLIENTS:
            continue

        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                f"Session for account '{account}' is not authorized. "
                f"Re-create StringSession for this account."
            )

        ACTIVE_CLIENTS[account] = client


async def _shutdown_clients() -> None:
    for _, client in list(ACTIVE_CLIENTS.items()):
        try:
            await client.disconnect()
        except Exception:
            pass
    ACTIVE_CLIENTS.clear()


async def _get_client(account: str) -> TelegramClient:
    await _ensure_clients_started()
    client = ACTIVE_CLIENTS.get(account)
    if not client:
        raise HTTPException(status_code=400, detail=f"Account not found: {account}")
    return client


# -------------------- Pydantic models --------------------
class DialogsReq(BaseModel):
    account: str
    limit: int = 100


class DialogInfo(BaseModel):
    id: int
    title: Optional[str] = None
    username: Optional[str] = None
    is_group: bool = False
    is_channel: bool = False
    is_user: bool = False
    unread_count: int = 0
    last_message_date: Optional[str] = None


class ExportMembersReq(BaseModel):
    account: str
    group: Optional[Union[str, int]] = None  # username/ссылка/или число строкой
    chat_id: Optional[int] = None            # числовой id из /dialogs
    limit: int = 0                           # 0 = без лимита
    offset: int = 0

    @model_validator(mode="after")
    def require_target(self) -> "ExportMembersReq":
        if (self.group is None or self.group == "") and self.chat_id is None:
            raise ValueError("Provide either 'group' or 'chat_id'")
        return self


class MemberInfo(BaseModel):
    id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_bot: Optional[bool] = None


def _normalize_target(req: ExportMembersReq) -> Union[str, int]:
    """
    Поддержка:
      - chat_id: 1450445959
      - group: "kvartirikrasnodare123"
      - group: "1450445959" (строка-число)
      - group: "-1001450445959" (bot api формат) -> 1450445959 (best effort)
    """
    target: Union[str, int] = req.chat_id if req.chat_id is not None else (req.group or "")

    if isinstance(target, str):
        t = target.strip()

        # Bot API style -100...
        if t.startswith("-100") and t[4:].lstrip("-").isdigit():
            return int(t[4:])

        # numeric string
        if t.lstrip("-").isdigit():
            n = int(t)
            if n < 0 and str(n).startswith("-100"):
                return int(str(n)[4:])
            return n

        return t

    # int bot-api style
    if isinstance(target, int) and target < 0 and str(target).startswith("-100"):
        return int(str(target)[4:])

    return target


# -------------------- FastAPI app --------------------
app = FastAPI(title="Telethon Multi-Account Service")


@app.on_event("startup")
async def startup_event() -> None:
    try:
        await _ensure_clients_started()
    except Exception:
        # не валим процесс: в облаке может не быть сессии на старте
        pass


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await _shutdown_clients()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "accounts_loaded": list(ACTIVE_CLIENTS.keys()),
        "webhook_url_set": bool(WEBHOOK_URL),
        "api_id_set": bool(API_ID),
        "api_hash_set": bool(API_HASH),
    }


@app.post("/dialogs", response_model=List[DialogInfo])
async def dialogs(req: DialogsReq) -> List[DialogInfo]:
    try:
        client = await _get_client(req.account)
        limit = max(1, min(int(req.limit), 5000))

        result: List[DialogInfo] = []
        async for d in client.iter_dialogs(limit=limit):
            entity = d.entity
            is_user = isinstance(entity, User)
            is_group = isinstance(entity, Chat) or (
                isinstance(entity, Channel) and getattr(entity, "megagroup", False)
            )
            is_channel = isinstance(entity, Channel) and getattr(entity, "broadcast", False)

            last_dt = d.date.isoformat() if getattr(d, "date", None) else None

            result.append(
                DialogInfo(
                    id=int(d.id),
                    title=getattr(d, "name", None),
                    username=getattr(entity, "username", None),
                    is_group=bool(is_group),
                    is_channel=bool(is_channel),
                    is_user=bool(is_user),
                    unread_count=int(getattr(d, "unread_count", 0) or 0),
                    last_message_date=last_dt,
                )
            )

        return result

    except FloodWaitError as e:
        raise HTTPException(status_code=429, detail=f"FloodWait: wait {e.seconds} seconds")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"dialogs error: {e}")


@app.post("/export_members", response_model=List[MemberInfo])
async def export_members(req: ExportMembersReq) -> List[MemberInfo]:
    """
    Экспорт участников:
      - обычные группы
      - супергруппы/мегагруппы
    """
    try:
        client = await _get_client(req.account)
        target = _normalize_target(req)

        entity = await client.get_entity(target)

        members: List[MemberInfo] = []
        i = 0

        async for u in client.iter_participants(entity):
            if req.offset and i < req.offset:
                i += 1
                continue
            i += 1

            members.append(
                MemberInfo(
                    id=int(u.id),
                    username=getattr(u, "username", None),
                    first_name=getattr(u, "first_name", None),
                    last_name=getattr(u, "last_name", None),
                    is_bot=bool(getattr(u, "bot", False)),
                )
            )

            if req.limit and len(members) >= req.limit:
                break

        return members

    except FloodWaitError as e:
        raise HTTPException(status_code=429, detail=f"FloodWait: wait {e.seconds} seconds")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"export_members error: {e}")


# -------------------- Run (Render-friendly) --------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, log_level="info")
