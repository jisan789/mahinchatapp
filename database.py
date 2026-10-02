"""
Database module for LiveChat
Supports:
1. External PHP JSON Receiver (via PHP_STORAGE_URL environment variable)
2. Local SQLite fallback (livechat.db)
"""

import sqlite3
import os
import json
import urllib.request
import urllib.parse
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("livechat.db")

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "livechat.db")
PHP_STORAGE_URL = os.environ.get("PHP_STORAGE_URL", "http://cdn.jisanfx.top/mahinapp/reciever.php").strip()


def get_sqlite_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database tables and indexes (for SQLite)."""
    if PHP_STORAGE_URL:
        logger.info(f"[DB] Using external PHP JSON storage at {PHP_STORAGE_URL}")

    try:
        with get_sqlite_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    msg_type TEXT NOT NULL,
                    text_content TEXT,
                    media_duration REAL,
                    client_time TEXT,
                    seen INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN seen INTEGER DEFAULT 0;")
            except Exception:
                pass

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_pair 
                ON messages(sender, recipient);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_created 
                ON messages(created_at);
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS seen_records (
                    msg_id TEXT PRIMARY KEY,
                    marked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
        logger.info("[DB] SQLite database initialized.")
    except Exception as e:
        logger.warning(f"[DB] SQLite init skipped or failed: {e}")


def save_message(
    sender: str,
    recipient: str,
    msg_type: str,
    text_content: Optional[str] = None,
    media_duration: Optional[float] = None,
    client_time: Optional[str] = None,
    seen: bool = False,
) -> Dict[str, Any]:
    """Persist a message to PHP endpoint or SQLite."""
    payload = {
        "sender": sender,
        "recipient": recipient,
        "msg_type": msg_type,
        "text_content": text_content,
        "audio": text_content if msg_type == "voice" else None,
        "image": text_content if msg_type == "image" else None,
        "media_duration": media_duration,
        "client_time": client_time,
        "seen": seen,
    }

    # If PHP_STORAGE_URL is configured, send HTTP POST to PHP receiver
    if PHP_STORAGE_URL:
        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                PHP_STORAGE_URL,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "LiveChat-Server/1.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("status") == "ok" and "data" in result:
                    return result["data"]
        except Exception as e:
            logger.error(f"[DB] Failed to save to PHP storage ({e}). Falling back to SQLite.")

    # SQLite fallback
    with get_sqlite_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO messages (sender, recipient, msg_type, text_content, media_duration, client_time, seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sender, recipient, msg_type, text_content, media_duration, client_time, 1 if seen else 0),
        )
        msg_id = cursor.lastrowid
        conn.commit()

        cursor.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
        row = cursor.fetchone()
        if row:
            d = dict(row)
            d["seen"] = bool(d.get("seen", 0))
            return d
        return {
            "id": msg_id,
            "sender": sender,
            "recipient": recipient,
            "msg_type": msg_type,
            "text_content": text_content,
            "media_duration": media_duration,
            "client_time": client_time,
            "seen": seen,
        }


def mark_messages_seen(sender: str, recipient: str, msg_ids: Optional[List[Any]] = None) -> bool:
    """Mark messages sent by sender to recipient as seen on external PHP storage & local SQLite."""
    php_success = False
    if PHP_STORAGE_URL:
        try:
            payload = {
                "action": "seen",
                "sender": sender,
                "recipient": recipient,
                "ids": msg_ids or [],
            }
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                PHP_STORAGE_URL,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "LiveChat-Server/1.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("status") == "ok":
                    php_success = True
                    logger.info(f"[DB] Marked messages as seen in PHP storage (updated: {result.get('updated')})")
        except Exception as e:
            logger.error(f"[DB] Failed to mark messages seen in PHP storage: {e}")

    # SQLite fallback update
    try:
        with get_sqlite_connection() as conn:
            cursor = conn.cursor()
            if msg_ids:
                for mid in msg_ids:
                    cursor.execute("INSERT OR IGNORE INTO seen_records (msg_id) VALUES (?)", (str(mid),))
                placeholders = ",".join("?" for _ in msg_ids)
                cursor.execute(
                    f"UPDATE messages SET seen = 1 WHERE id IN ({placeholders})",
                    tuple(msg_ids),
                )
            elif sender and recipient:
                cursor.execute(
                    "UPDATE messages SET seen = 1 WHERE sender = ? AND recipient = ?",
                    (sender, recipient),
                )
            elif recipient:
                cursor.execute(
                    "UPDATE messages SET seen = 1 WHERE recipient = ?",
                    (recipient,),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"[DB] SQLite mark seen error: {e}")

    return php_success


def get_conversation(user1: str, user2: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Fetch messages exchanged between user1 and user2."""
    if PHP_STORAGE_URL:
        try:
            params = urllib.parse.urlencode({
                "action": "get",
                "user1": user1,
                "user2": user2
            })
            url = f"{PHP_STORAGE_URL}?{params}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "LiveChat-Server/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("status") == "ok" and "messages" in result:
                    msgs = result["messages"]
                    seen_id_set = set()
                    try:
                        with get_sqlite_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute("SELECT msg_id FROM seen_records")
                            seen_id_set = {str(row[0]) for row in cursor.fetchall()}
                    except Exception:
                        pass

                    total = len(msgs)
                    for i, m in enumerate(msgs):
                        m_id = str(m.get("id", ""))
                        if m.get("seen") is True or m_id in seen_id_set:
                            m["seen"] = True
                        else:
                            # If recipient replied after this message, it was seen!
                            recipient = m.get("recipient")
                            has_reply = any(msgs[j].get("sender") == recipient for j in range(i + 1, total))
                            m["seen"] = has_reply
                    return msgs
        except Exception as e:
            logger.error(f"[DB] Failed to get messages from PHP storage ({e}). Falling back to SQLite.")

    # SQLite fallback
    with get_sqlite_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE (sender = ? AND recipient = ?)
                   OR (sender = ? AND recipient = ?)
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC;
            """,
            (user1, user2, user2, user1, limit),
        )
        rows = cursor.fetchall()
        result_list = []
        for row in rows:
            d = dict(row)
            d["seen"] = bool(d.get("seen", 0))
            result_list.append(d)
        return result_list


def clear_messages() -> bool:
    """Clear all messages from PHP storage and local SQLite."""
    success = False
    if PHP_STORAGE_URL:
        try:
            sep = "&" if "?" in PHP_STORAGE_URL else "?"
            url = f"{PHP_STORAGE_URL}{sep}action=clear"
            req = urllib.request.Request(url, headers={"User-Agent": "LiveChat-Server/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("status") == "ok":
                    success = True
                    logger.info("[DB] Cleared PHP storage messages.json.")
        except Exception as e:
            logger.error(f"[DB] Error clearing PHP storage ({e})")

    try:
        with get_sqlite_connection() as conn:
            conn.execute("DELETE FROM messages;")
            conn.commit()
        success = True
        logger.info("[DB] Cleared SQLite messages.")
    except Exception as e:
        logger.error(f"[DB] Error clearing SQLite messages: {e}")

    return success
