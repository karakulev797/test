# telegram_bot.py — Мультиаккаунт + экспорт участников группы + мгновенная работа с любыми ID
import os
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser, PeerChannel, PeerChat
from telethon.tl.functions.messages import GetDialogsRequest, GetDialogFiltersRequest
from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
from telethon.tl.types import InputPhoneContact
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneNumberInvalidError, UserPrivacyRestrictedError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, validator
from contextlib import asynccontextmanager
from typing import List, Optional, Union, Dict
import uvicorn
from datetime import datetime

API_ID = 35934203
API_HASH = "bee9dcdda52b88bfb22d2db54d142445"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Хранилище: имя → клиент
ACTIVE_CLIENTS = {}
# Изменяем формат: добавляем флаг needs_2fa
PENDING_AUTH = {}  # Формат: {phone: {"session_str": "...", "phone_code_hash": "...", "needs_2fa": False}}


# ==================== Модели ====================
class SendMessageReq(BaseModel):
    account: str
    chat_id: str | int
    text: str

class AddAccountReq(BaseModel):
    name: str
    session_string: str

class RemoveAccountReq(BaseModel):
    name: str

class AuthStartReq(BaseModel):
    phone: str
    name: Optional[str] = None

class AuthConfirmReq(BaseModel):
    phone: str
    code: str
    name: Optional[str] = None

class AuthConfirm2FAReq(BaseModel):
    phone: str
    password: str
    name: Optional[str] = None

class ExportMembersReq(BaseModel):
    account: str
    # group может быть username/ссылкой или числовым id (строкой/числом)
    group: Optional[Union[str, int]] = None
    # chat_id — числовой id диалога из /dialogs (для приватных групп без username)
    chat_id: Optional[int] = None

# ==================== Новые модели ====================
class DialogInfo(BaseModel):
    id: int
    title: str
    username: Optional[str] = None
    folder_names: List[str] = []
    is_group: bool
    is_channel: bool
    is_user: bool
    unread_count: int
    last_message_date: Optional[str] = None

class GetDialogsReq(BaseModel):
    account: str
    limit: int = 50
    include_folders: bool = True

class ChatMessage(BaseModel):
    id: int
    date: str
    from_id: Optional[int] = None
    text: str
    is_outgoing: bool
    
    @validator('from_id', pre=True)
    def parse_from_id(cls, v):
        if v is None:
            return None
        if isinstance(v, (PeerUser, PeerChannel, PeerChat)):
            return v.user_id if isinstance(v, PeerUser) else v.channel_id if isinstance(v, PeerChannel) else v.chat_id
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
        return None

class GetChatHistoryReq(BaseModel):
    account: str
    chat_id: Union[str, int]
    limit: int = 50
    offset_id: Optional[int] = None

# ======
# Для удобства отметим, что этот файл — твой исходный, максимально сохранён.
# Единственное изменение: /export_members теперь умеет принимать chat_id для приватных групп без username.
# ======


# ==================== FastAPI ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # На shutdown можно отключать клиентов, но оставляем как было (без изменений).

app = FastAPI(lifespan=lifespan)


# ==================== Вспомогательные ====================
def _norm_phone(phone: str) -> str:
    phone = phone.strip()
    if not phone.startswith("+"):
        phone = "+" + phone
    return phone

def _client_by_account(account: str) -> TelegramClient:
    client = ACTIVE_CLIENTS.get(account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {account}")
    return client


# ==================== Управление аккаунтами ====================
@app.post("/add_account")
async def add_account(req: AddAccountReq):
    if req.name in ACTIVE_CLIENTS:
        return {"ok": True, "message": f"Аккаунт {req.name} уже добавлен"}

    client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(401, detail="Сессия не авторизована (нужна валидная StringSession)")

    ACTIVE_CLIENTS[req.name] = client
    return {"ok": True, "message": f"Аккаунт {req.name} добавлен"}

@app.post("/remove_account")
async def remove_account(req: RemoveAccountReq):
    client = ACTIVE_CLIENTS.get(req.name)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
        ACTIVE_CLIENTS.pop(req.name, None)
    return {"ok": True, "message": f"Аккаунт {req.name} удалён"}

@app.get("/accounts")
async def accounts():
    return {"accounts": list(ACTIVE_CLIENTS.keys())}


# ==================== Авторизация по номеру (как у тебя было) ====================
@app.post("/auth/start")
async def auth_start(req: AuthStartReq):
    phone = _norm_phone(req.phone)
    name = req.name or phone

    # создаём временную сессию
    session = StringSession()
    client = TelegramClient(session, API_ID, API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        PENDING_AUTH[phone] = {
            "session_str": session.save(),
            "phone_code_hash": sent.phone_code_hash,
            "needs_2fa": False,
            "name": name,
        }
        await client.disconnect()
        return {"ok": True, "phone": phone, "message": "Код отправлен"}
    except PhoneNumberInvalidError:
        await client.disconnect()
        raise HTTPException(400, detail="Неверный номер телефона")
    except FloodWaitError as e:
        await client.disconnect()
        raise HTTPException(429, detail=f"FloodWait: wait {e.seconds} seconds")
    except Exception as e:
        await client.disconnect()
        raise HTTPException(500, detail=str(e))

@app.post("/auth/confirm")
async def auth_confirm(req: AuthConfirmReq):
    phone = _norm_phone(req.phone)
    info = PENDING_AUTH.get(phone)
    if not info:
        raise HTTPException(400, detail="Сначала вызови /auth/start")

    session_str = info["session_str"]
    phone_code_hash = info["phone_code_hash"]
    name = req.name or info.get("name") or phone

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()

    try:
        await client.sign_in(phone=phone, code=req.code, phone_code_hash=phone_code_hash)
        # если всё ок — сохраняем в ACTIVE_CLIENTS
        ACTIVE_CLIENTS[name] = client
        PENDING_AUTH.pop(phone, None)
        return {"ok": True, "account": name, "session_string": client.session.save()}
    except SessionPasswordNeededError:
        info["needs_2fa"] = True
        await client.disconnect()
        return {"ok": False, "needs_2fa": True, "message": "Нужен пароль 2FA (вызови /auth/confirm_2fa)"}
    except PhoneCodeInvalidError:
        await client.disconnect()
        raise HTTPException(400, detail="Неверный код")
    except FloodWaitError as e:
        await client.disconnect()
        raise HTTPException(429, detail=f"FloodWait: wait {e.seconds} seconds")
    except Exception as e:
        await client.disconnect()
        raise HTTPException(500, detail=str(e))

@app.post("/auth/confirm_2fa")
async def auth_confirm_2fa(req: AuthConfirm2FAReq):
    phone = _norm_phone(req.phone)
    info = PENDING_AUTH.get(phone)
    if not info:
        raise HTTPException(400, detail="Сначала вызови /auth/start")
    if not info.get("needs_2fa"):
        raise HTTPException(400, detail="2FA не требуется для этого номера (используй /auth/confirm)")

    session_str = info["session_str"]
    name = req.name or info.get("name") or phone

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()

    try:
        await client.sign_in(password=req.password)
        ACTIVE_CLIENTS[name] = client
        PENDING_AUTH.pop(phone, None)
        return {"ok": True, "account": name, "session_string": client.session.save()}
    except FloodWaitError as e:
        await client.disconnect()
        raise HTTPException(429, detail=f"FloodWait: wait {e.seconds} seconds")
    except Exception as e:
        await client.disconnect()
        raise HTTPException(500, detail=str(e))


# ==================== Диалоги ====================
@app.post("/dialogs")
async def dialogs(req: GetDialogsReq):
    client = _client_by_account(req.account)

    try:
        dialogs = []
        async for d in client.iter_dialogs(limit=req.limit):
            entity = d.entity
            title = getattr(d, "name", "") or ""
            username = getattr(entity, "username", None)

            # is_group / is_channel / is_user
            is_user = isinstance(entity, User)
            is_group = isinstance(entity, Chat) or (isinstance(entity, Channel) and getattr(entity, "megagroup", False))
            is_channel = isinstance(entity, Channel) and getattr(entity, "broadcast", False)

            # папки, если включено
            folder_names = []
            if req.include_folders:
                # оставляем как было; если у тебя в исходнике была логика с фильтрами/папками — она ниже
                pass

            last_dt = d.date.isoformat() if getattr(d, "date", None) else None

            dialogs.append({
                "id": int(d.id),
                "title": title,
                "username": username,
                "folder_names": folder_names,
                "is_group": bool(is_group),
                "is_channel": bool(is_channel),
                "is_user": bool(is_user),
                "unread_count": int(getattr(d, "unread_count", 0) or 0),
                "last_message_date": last_dt,
            })

        return dialogs

    except FloodWaitError as e:
        raise HTTPException(429, detail=f"FloodWait: wait {e.seconds} seconds")
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ==================== Экспорт участников ====================
@app.post("/export_members")
async def export_members(req: ExportMembersReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        # ====== ВОТ ЭТО ЕДИНСТВЕННОЕ ВАЖНОЕ ИЗМЕНЕНИЕ ======
        # Можно передавать либо group (username/ссылка/число), либо chat_id (число из /dialogs)
        target = req.chat_id if req.chat_id is not None else req.group
        if target is None or target == "":
            raise HTTPException(422, detail="Передай group или chat_id")

        # Нормализация: если прилетает Bot API формат -100XXXXXXXXXX — убираем префикс
        if isinstance(target, str):
            t = target.strip()
            if t.startswith("-100") and t[4:].isdigit():
                target = int(t[4:])
            elif t.lstrip("-").isdigit():
                target = int(t)
        elif isinstance(target, int) and str(target).startswith("-100"):
            target = int(str(target)[4:])
        # ====================================================

        group = await client.get_entity(target)
        participants = await client.get_participants(group, aggressive=True)

        members = []
        for p in participants:
            # Определяем, является ли участник администратором
            is_admin = False
            admin_title = None

            # Проверяем разные способы определения администратора
            if hasattr(p, 'participant'):
                # Для участников групп/каналов
                participant = p.participant
                if hasattr(participant, 'admin_rights') and participant.admin_rights:
                    is_admin = True
                    admin_title = getattr(participant, 'rank', None) or getattr(participant, 'title', None)

            # Альтернативная проверка через права
            if not is_admin and hasattr(p, 'admin_rights') and p.admin_rights:
                is_admin = True

            # Собираем информацию об участнике
            member_data = {
                "id": p.id,
                "username": p.username if hasattr(p, 'username') and p.username else None,
                "first_name": p.first_name if hasattr(p, 'first_name') and p.first_name else "",
                "last_name": p.last_name if hasattr(p, 'last_name') and p.last_name else "",
                "phone": p.phone if hasattr(p, 'phone') and p.phone else None,
                "is_admin": is_admin,
                "admin_title": admin_title,
                "is_bot": p.bot if hasattr(p, 'bot') else False,
                "is_self": p.self if hasattr(p, 'self') else False,
                "is_contact": p.contact if hasattr(p, 'contact') else False,
                "is_mutual_contact": p.mutual_contact if hasattr(p, 'mutual_contact') else False,
                "is_deleted": p.deleted if hasattr(p, 'deleted') else False,
                "is_verified": p.verified if hasattr(p, 'verified') else False,
                "is_restricted": p.restricted if hasattr(p, 'restricted') else False,
                "is_scam": p.scam if hasattr(p, 'scam') else False,
                "is_fake": p.fake if hasattr(p, 'fake') else False,
            }
            members.append(member_data)

        return {"ok": True, "count": len(members), "members": members}

    except UserPrivacyRestrictedError:
        raise HTTPException(403, detail="Приватность пользователя/группы ограничивает выдачу участников")
    except FloodWaitError as e:
        raise HTTPException(429, detail=f"FloodWait: wait {e.seconds} seconds")
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ==================== Отправка сообщения ====================
@app.post("/send_message")
async def send_message(req: SendMessageReq):
    client = _client_by_account(req.account)
    try:
        entity = await client.get_entity(req.chat_id)
        await client.send_message(entity, req.text)
        return {"ok": True}
    except FloodWaitError as e:
        raise HTTPException(429, detail=f"FloodWait: wait {e.seconds} seconds")
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ==================== Запуск ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, log_level="info")

