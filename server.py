"""
LiveChat WebSocket Server
FastAPI backend for real-time 2-user chat.
Deploy on Render — reads PORT env variable automatically.
"""

import os
import json
import asyncio
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from contextlib import asynccontextmanager
from starlette.requests import Request
from database import init_db, save_message, get_conversation, clear_messages, mark_messages_seen

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("livechat")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("[DB] SQLite database initialized.")
    yield


app = FastAPI(title="LiveChat Server", version="1.0.0", lifespan=lifespan)

# ─────────────────────────────────────────────────────────────
#  Connection Registry
# ─────────────────────────────────────────────────────────────
VALID_USERS = {"cipher", "echo"}
OPPONENTS: dict[str, str] = {
    "cipher": "echo",
    "echo": "cipher",
}

# {user_key: WebSocket}
connections: dict[str, WebSocket] = {}


async def safe_send(ws: WebSocket, payload: dict) -> bool:
    """Send JSON safely, returns False if the connection is broken."""
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


async def notify_opponent(user_key: str, payload: dict):
    """Forward a payload to the opponent if they are connected."""
    opponent = OPPONENTS.get(user_key)
    if opponent and opponent in connections:
        ok = await safe_send(connections[opponent], payload)
        if not ok:
            connections.pop(opponent, None)


async def background_save_message(user_key: str, opponent: str, msg_type: str, data: dict):
    """Save message in the background without blocking real-time WebSocket communication."""
    try:
        if msg_type == "chat":
            content = data.get("text")
        elif msg_type == "image":
            content = data.get("image")
        else:
            content = data.get("audio")
        saved = await asyncio.to_thread(
            save_message,
            sender=user_key,
            recipient=opponent,
            msg_type=msg_type,
            text_content=content,
            media_duration=data.get("duration"),
            client_time=data.get("time"),
            seen=False,
        )
        logger.info(f"[DB] Async saved {msg_type} #{saved.get('id')} from {user_key} to {opponent}")
    except Exception as e:
        logger.error(f"[DB] Error saving message in background: {e}")


async def background_mark_seen(sender: str, recipient: str, ids: list | None = None):
    """Persist seen status in background task without blocking."""
    try:
        await asyncio.to_thread(
            mark_messages_seen,
            sender=sender,
            recipient=recipient,
            msg_ids=ids,
        )
        logger.info(f"[DB] Async marked messages seen from {sender} to {recipient}")
    except Exception as e:
        logger.error(f"[DB] Error marking seen in background: {e}")


# ─────────────────────────────────────────────────────────────
#  WebSocket Endpoint
# ─────────────────────────────────────────────────────────────
@app.websocket("/ws/{user_key}")
async def websocket_endpoint(websocket: WebSocket, user_key: str):
    if user_key not in VALID_USERS:
        await websocket.close(code=4001, reason="Unknown user")
        return

    await websocket.accept()

    # If user already has an active connection (e.g. page refresh),
    # close the old one gracefully before replacing
    old_ws = connections.get(user_key)
    if old_ws is not None:
        try:
            await old_ws.close(code=1000, reason="Replaced by new connection")
        except Exception:
            pass

    connections[user_key] = websocket
    logger.info(f"[+] {user_key} connected  (total: {len(connections)})")

    opponent = OPPONENTS[user_key]

    # Tell opponent this user is now online
    await notify_opponent(user_key, {"type": "presence", "status": "online", "user": user_key})

    # Tell this user whether opponent is already online
    await safe_send(websocket, {
        "type": "presence",
        "status": "online" if opponent in connections else "offline",
        "user": opponent
    })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type", "")

            # 1. Forward INSTANTLY over direct WebSocket (Zero Latency!)
            data["from"] = user_key
            await notify_opponent(user_key, data)

            # 2. Persist chat, voice & image messages to PHP/DB in background task without blocking live chat.
            if msg_type in ("chat", "voice", "image"):
                asyncio.create_task(background_save_message(user_key, opponent, msg_type, data))

            # 3. Persist seen status to PHP/DB in background task
            if msg_type in ("seen", "mark_seen"):
                ids = data.get("ids") or ([data.get("msg_id")] if data.get("msg_id") else None)
                is_all = data.get("all", False)
                asyncio.create_task(background_mark_seen(sender=opponent, recipient=user_key, ids=None if is_all else ids))

            logger.debug(f"  {user_key} → {opponent}: {msg_type}")

    except WebSocketDisconnect:
        # CRITICAL: Only remove from dict if THIS websocket is still the current one.
        # If a new connection already replaced us, don't touch it.
        if connections.get(user_key) is websocket:
            connections.pop(user_key, None)
            logger.info(f"[-] {user_key} disconnected (total: {len(connections)})")
            await notify_opponent(user_key, {"type": "presence", "status": "offline", "user": user_key})
        else:
            logger.info(f"[-] {user_key} stale connection closed (replaced by newer)")



# ─────────────────────────────────────────────────────────────
#  Message History Endpoint
# ─────────────────────────────────────────────────────────────
@app.get("/api/messages/{user_key}")
async def get_messages(user_key: str):
    if user_key not in VALID_USERS:
        return JSONResponse({"status": "error", "message": "Unknown user"}, status_code=404)
    opponent = OPPONENTS[user_key]
    messages = get_conversation(user_key, opponent)
    return JSONResponse({"status": "ok", "user": user_key, "opponent": opponent, "messages": messages})


@app.post("/api/messages/seen")
async def api_mark_seen(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_key = body.get("user")
    opponent = body.get("opponent") or (OPPONENTS.get(user_key) if user_key else None)
    ids = body.get("ids")
    is_all = body.get("all", False)
    if not user_key or user_key not in VALID_USERS or not opponent:
        return JSONResponse({"status": "error", "message": "Invalid user"}, status_code=400)

    ok = await asyncio.to_thread(
        mark_messages_seen,
        sender=opponent,
        recipient=user_key,
        msg_ids=None if is_all else ids,
    )

    if opponent in connections:
        await safe_send(connections[opponent], {
            "type": "seen",
            "from": user_key,
            "ids": ids,
            "all": is_all,
        })

    return JSONResponse({"status": "ok" if ok else "error"})


@app.post("/api/messages/clear")
async def api_clear_messages():
    ok = clear_messages()
    # Broadcast to all connected users that chat was cleared
    for ukey, ws in list(connections.items()):
        await safe_send(ws, {"type": "chat_cleared"})
    return JSONResponse({"status": "ok" if ok else "error"})


# ─────────────────────────────────────────────────────────────
#  Health Check & Keep-Alive Ping
# ─────────────────────────────────────────────────────────────
@app.get("/ping")
@app.get("/api/ping")
async def ping():
    return PlainTextResponse("alive")


@app.get("/health")
async def health():
    return JSONResponse({"status": "alive", "connected": list(connections.keys())})


# ─────────────────────────────────────────────────────────────
#  Static Files + SPA Catch-all
# ─────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/app.js")
async def serve_app_js():
    return FileResponse("static/app.js", media_type="application/javascript", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/style.css")
async def serve_style_css():
    return FileResponse("static/style.css", media_type="text/css", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/")
async def serve_root():
    return FileResponse("index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ─────────────────────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
