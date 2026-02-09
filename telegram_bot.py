"""
telegram_bot.py — Multi-account Telethon service for:
- listing dialogs (groups/chats/channels/users)
- exporting members from a group by username OR by numeric chat_id
Designed to be called from n8n (HTTP Request node).

Run:
  pip install telethon fastapi uvicorn pydantic

Env:
  export TG_API_ID="35934203"
  export TG_API_HASH="bee9dcdda52b88bfb22d2db54d142445"
  # Option A (recommended): multiple accounts
  export TG_SESSIONS_JSON='{"test5":"<STRING_SESSION_1>","acc2":"<STRING_SESSION_2>"}'
  # Option B: single account
  export TG_SESSION_STRING="<STRING_SESSION>"
  export TG_ACCOUNT_NAME="test5"

Start:
  uvicorn telegram_bot:app --host 0.0.0.0 --port 8000

Endpoints:
  POST /dialogs
    body: {"account":"test5","limit":100}
  POST /export_members
    body: {"account":"test5","chat_id":1450445959}
    OR   {"account":"test5","group":"some_group_username"}
    OR   {"account":"test5","group":"1450445959"}  # numeric as string OK
"""

import os
import json
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, root_validator
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User  # type: ignore


# -------------------- Config --------------------
TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "")

# -------------------- In-memory clients --------------------
ACTIVE_CLIENTS: Dict[str, TelegramClient] = {}


def _load_sessions_from_env() -> Dict[str, str]:
    """
    Supported env:
      - TG_SESSIONS_JSON='{"acc":"<string_session>","acc2":"<string_session2>"}'
      - OR TG_SESSION_STRING + TG_ACCOUNT_NAME
    """
    sessions: Dict[str, str] = {}

    raw_json = os.getenv("TG_SESSIONS_JSON", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        sessions[k.strip()] = v.strip()
        except Exception:
            # ignore, will fall back to single-session
            pass

    if not sessions:
        single = os.getenv("TG_SESSION_STRING", "").strip()
        name = os.getenv("TG_ACCOUNT_NAME", "default").strip() or "default"
        if single:
            sessions[name] = single

    return sessions


async def _ensure_clients_started() -> None:
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID / TG_API_HASH are not set")

    sessions = _load_sessions_from_env()
    if not sessions:
        raise RuntimeError("No sessions provided. Set TG_SESSIONS_JSON or TG_SESSION_STRING.")

    for account, session_str in sessions.items():
        if account in ACTIVE_CLIENTS:
            continue

        client = TelegramClient(StringSession(session_str), TG_API_ID, TG_API_HASH)
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
    group: Optional[Union[str, int]] = None  # username/link/or numeric id as string/int
    chat_id: Optional[int] = None            # numeric dialog id (from /dialogs)
    limit: int = 0                           # 0 = no limit
    offset: int = 0

    @root_validator
    def require_target(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        group, chat_id = values.get("group"), values.get("chat_id")
        if (group is None or group == "") and chat_id is None:
            raise ValueError("Provide either 'group' or 'chat_id'")
        return values


class MemberInfo(BaseModel):
    id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_bot: Optional[bool] = None


# -------------------- Helpers --------------------
def _normalize_target(req: ExportMembersReq) -> Union[str, int]:
    """
    Accept:
      - req.chat_id (int)
      - req.group:
          - "username"
          - "1450445959" (numeric string)
          - 1450445959 (int)
          - "-1001450445959" (BotAPI style) -> 1450445959 (best effort)
    """
    target: Union[str, int] = req.chat_id if req.chat_id is not None else (req.group or "")
    if isinstance(target, str):
        t = target.strip()
        # normalize bot-api style -100...
        if t.startswith("-100") and t[4:].lstrip("-").isdigit():
            return int(t[4:])
        # numeric string
        if t.lstrip("-").isdigit():
            n = int(t)
            if n < 0 and str(n).startswith("-100"):
                return int(str(n)[4:])
            return n
        return t
    # int
    if isinstance(target, int) and target < 0 and str(target).startswith("-100"):
        return int(str(target)[4:])
    return target


async def _get_client(account: str) -> TelegramClient:
    await _ensure_clients_started()
    client = ACTIVE_CLIENTS.get(account)
    if not client:
        raise HTTPException(status_code=400, detail=f"Account not found: {account}")
    return client


# -------------------- FastAPI app --------------------
app = FastAPI(title="Telethon Multi-Account Service")


@app.on_event("startup")
async def startup_event() -> None:
    try:
        await _ensure_clients_started()
    except Exception:
        # do not crash in cloud; endpoints will return clear errors
        pass


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await _shutdown_clients()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "accounts_loaded": list(ACTIVE_CLIENTS.keys()),
        "api_id_set": bool(TG_API_ID),
        "api_hash_set": bool(TG_API_HASH),
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
            is_group = isinstance(entity, Chat) or (isinstance(entity, Channel) and getattr(entity, "megagroup", False))
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
    Exports participants for:
      - basic groups
      - supergroups / megagroups
    Note: Telegram can restrict members list visibility in some groups.
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
