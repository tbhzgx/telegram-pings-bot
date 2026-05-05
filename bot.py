import discord
import aiohttp
import asyncio
import os
import logging
import asyncpg
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.environ["DISCORD_TOKEN"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
DATABASE_URL     = os.environ["DATABASE_URL"]
ADMIN_CHAT_ID    = int(os.environ["ADMIN_CHAT_ID"])   # Your Telegram chat ID (always receives msgs + admin cmds)

# Optional filters (same as v1)
WATCHED_CHANNELS = [int(c.strip()) for c in os.environ.get("WATCHED_CHANNELS", "").split(",") if c.strip()]
WATCHED_SERVERS  = [int(s.strip()) for s in os.environ.get("WATCHED_SERVERS", "").split(",") if s.strip()]

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ── Database ──────────────────────────────────────────────────────────────────
db: asyncpg.Pool = None

async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id     BIGINT PRIMARY KEY,
            username    TEXT,
            active      BOOLEAN DEFAULT TRUE,
            joined_at   TIMESTAMP DEFAULT NOW()
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS allowlist (
            username    TEXT PRIMARY KEY,
            added_at    TIMESTAMP DEFAULT NOW()
        )
    """)
    # Admin is always in the allowlist and users table
    await db.execute("""
        INSERT INTO users (chat_id, username, active)
        VALUES ($1, 'admin', TRUE)
        ON CONFLICT (chat_id) DO NOTHING
    """, ADMIN_CHAT_ID)
    log.info("Database initialised")

async def get_active_users() -> list[int]:
    rows = await db.fetch("SELECT chat_id FROM users WHERE active = TRUE")
    return [r["chat_id"] for r in rows]

async def is_allowed(username: str) -> bool:
    if not username:
        return False
    row = await db.fetchrow("SELECT 1 FROM allowlist WHERE lower(username) = lower($1)", username)
    return row is not None

async def is_registered(chat_id: int) -> bool:
    row = await db.fetchrow("SELECT 1 FROM users WHERE chat_id = $1", chat_id)
    return row is not None

async def register_user(chat_id: int, username: str):
    await db.execute("""
        INSERT INTO users (chat_id, username, active)
        VALUES ($1, $2, TRUE)
        ON CONFLICT (chat_id) DO UPDATE SET active = TRUE, username = $2
    """, chat_id, username)

async def deactivate_user(chat_id: int):
    await db.execute("UPDATE users SET active = FALSE WHERE chat_id = $1", chat_id)

async def add_to_allowlist(username: str):
    await db.execute("""
        INSERT INTO allowlist (username) VALUES ($1)
        ON CONFLICT DO NOTHING
    """, username)

async def remove_from_allowlist(username: str):
    await db.execute("DELETE FROM allowlist WHERE lower(username) = lower($1)", username)

async def list_users():
    return await db.fetch("SELECT chat_id, username, active, joined_at FROM users ORDER BY joined_at")

async def list_allowlist():
    return await db.fetch("SELECT username, added_at FROM allowlist ORDER BY added_at")

# ── Telegram helpers ──────────────────────────────────────────────────────────
async def tg_send(session: aiohttp.ClientSession, chat_id: int, text: str):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with session.post(f"{TG_API}/sendMessage", json=payload) as resp:
        if resp.status == 403:
            # User blocked the bot — deactivate them
            await deactivate_user(chat_id)
            log.info(f"Deactivated user {chat_id} (blocked bot)")
        elif resp.status != 200:
            log.warning(f"Telegram error {resp.status} for chat {chat_id}: {await resp.text()}")

async def tg_photo(session: aiohttp.ClientSession, chat_id: int, url: str, caption: str):
    payload = {"chat_id": chat_id, "photo": url, "caption": caption[:1024], "parse_mode": "HTML"}
    async with session.post(f"{TG_API}/sendPhoto", json=payload) as resp:
        if resp.status != 200:
            log.warning(f"Photo error {resp.status} for {chat_id}")

async def tg_document(session: aiohttp.ClientSession, chat_id: int, url: str, caption: str):
    payload = {"chat_id": chat_id, "document": url, "caption": caption[:1024], "parse_mode": "HTML"}
    async with session.post(f"{TG_API}/sendDocument", json=payload) as resp:
        if resp.status != 200:
            log.warning(f"Document error {resp.status} for {chat_id}")

async def broadcast(session: aiohttp.ClientSession, text: str):
    users = await get_active_users()
    for chat_id in users:
        await tg_send(session, chat_id, text)

async def broadcast_photo(session: aiohttp.ClientSession, url: str, caption: str):
    users = await get_active_users()
    for chat_id in users:
        await tg_photo(session, chat_id, url, caption)

async def broadcast_document(session: aiohttp.ClientSession, url: str, caption: str):
    users = await get_active_users()
    for chat_id in users:
        await tg_document(session, chat_id, url, caption)

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ── Telegram update polling ───────────────────────────────────────────────────
async def handle_telegram_update(session: aiohttp.ClientSession, update: dict):
    msg = update.get("message", {})
    if not msg:
        return

    chat_id  = msg["chat"]["id"]
    username = msg.get("from", {}).get("username", "")
    text     = msg.get("text", "").strip()

    if not text.startswith("/"):
        return

    cmd = text.split()[0].lower()

    # ── /start ────────────────────────────────────────────────────────────────
    if cmd == "/start":
        if await is_registered(chat_id):
            await tg_send(session, chat_id, "✅ You're already subscribed and receiving messages!")
            return
        if not await is_allowed(username):
            await tg_send(session, chat_id,
                "🔒 This bot is invite-only.\n\nAsk the admin to add your Telegram username to the allowlist.")
            log.info(f"Rejected /start from @{username} ({chat_id})")
            return
        await register_user(chat_id, username)
        await tg_send(session, chat_id,
            f"🎉 <b>Welcome, @{escape_html(username)}!</b>\n\nYou're now subscribed. Discord messages will be forwarded here in real time.\n\nSend /stop at any time to unsubscribe.")
        await tg_send(session, ADMIN_CHAT_ID,
            f"👤 New user subscribed: @{escape_html(username)} ({chat_id})")
        log.info(f"Registered new user @{username} ({chat_id})")

    # ── /stop ─────────────────────────────────────────────────────────────────
    elif cmd == "/stop":
        await deactivate_user(chat_id)
        await tg_send(session, chat_id, "👋 You've been unsubscribed. Send /start to re-subscribe anytime.")

    # ── /status ───────────────────────────────────────────────────────────────
    elif cmd == "/status":
        users = await get_active_users()
        registered = await is_registered(chat_id)
        active = chat_id in users
        status = "✅ Active" if active else ("⏸ Inactive (send /start)" if registered else "❌ Not registered")
        await tg_send(session, chat_id, f"Your status: {status}")

    # ── Admin commands (only work for ADMIN_CHAT_ID) ──────────────────────────
    elif chat_id == ADMIN_CHAT_ID:

        # /allow @username — add to allowlist
        if cmd == "/allow":
            parts = text.split()
            if len(parts) < 2:
                await tg_send(session, chat_id, "Usage: /allow username (no @)")
                return
            uname = parts[1].lstrip("@")
            await add_to_allowlist(uname)
            await tg_send(session, chat_id, f"✅ @{uname} added to allowlist. They can now /start.")

        # /remove @username — remove from allowlist
        elif cmd == "/remove":
            parts = text.split()
            if len(parts) < 2:
                await tg_send(session, chat_id, "Usage: /remove username (no @)")
                return
            uname = parts[1].lstrip("@")
            await remove_from_allowlist(uname)
            await tg_send(session, chat_id, f"🗑 @{uname} removed from allowlist.")

        # /users — list all registered users
        elif cmd == "/users":
            users = await list_users()
            if not users:
                await tg_send(session, chat_id, "No users registered yet.")
                return
            lines = ["<b>Registered users:</b>"]
            for u in users:
                status = "✅" if u["active"] else "⏸"
                lines.append(f"{status} @{escape_html(u['username'] or '?')} ({u['chat_id']})")
            await tg_send(session, chat_id, "\n".join(lines))

        # /allowlist — list allowlist
        elif cmd == "/allowlist":
            rows = await list_allowlist()
            if not rows:
                await tg_send(session, chat_id, "Allowlist is empty. Use /allow username to add someone.")
                return
            lines = ["<b>Allowlist:</b>"] + [f"• @{escape_html(r['username'])}" for r in rows]
            await tg_send(session, chat_id, "\n".join(lines))

        # /kick chat_id — deactivate a user
        elif cmd == "/kick":
            parts = text.split()
            if len(parts) < 2:
                await tg_send(session, chat_id, "Usage: /kick <chat_id>")
                return
            try:
                target = int(parts[1])
                await deactivate_user(target)
                await tg_send(session, chat_id, f"🚫 User {target} has been deactivated.")
            except ValueError:
                await tg_send(session, chat_id, "Invalid chat ID.")

        # /help
        elif cmd == "/help":
            await tg_send(session, chat_id,
                "<b>Admin commands:</b>\n"
                "/allow username — add to allowlist\n"
                "/remove username — remove from allowlist\n"
                "/allowlist — show allowlist\n"
                "/users — show all registered users\n"
                "/kick &lt;chat_id&gt; — deactivate a user\n"
                "/help — show this message"
            )

async def poll_telegram(session: aiohttp.ClientSession):
    offset = 0
    log.info("Telegram polling started")
    while True:
        try:
            async with session.get(f"{TG_API}/getUpdates",
                                   params={"offset": offset, "timeout": 30},
                                   timeout=aiohttp.ClientTimeout(total=40)) as resp:
                data = await resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    await handle_telegram_update(session, update)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"Telegram poll error: {e}")
            await asyncio.sleep(5)

# ── Discord client ────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    log.info(f"Discord logged in as {client.user}")
    async with aiohttp.ClientSession() as session:
        await tg_send(session, ADMIN_CHAT_ID, "🤖 <b>Discord→Telegram bot is online!</b>")

@client.event
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return
    if WATCHED_SERVERS and message.guild and message.guild.id not in WATCHED_SERVERS:
        return
    if WATCHED_CHANNELS and message.channel.id not in WATCHED_CHANNELS:
        return

    server  = escape_html(message.guild.name if message.guild else "DM")
    channel = escape_html(f"#{message.channel.name}" if hasattr(message.channel, "name") else "DM")
    author  = escape_html(str(message.author.display_name))
    content = escape_html(message.content)
    header  = f"<b>[{server} / {channel}]</b>\n<b>{author}:</b>"

    async with aiohttp.ClientSession() as session:
        if content:
            await broadcast(session, f"{header} {content}")
        for attachment in message.attachments:
            caption = f"{header} sent: {escape_html(attachment.filename)}"
            if attachment.content_type and attachment.content_type.startswith("image/"):
                await broadcast_photo(session, attachment.url, caption)
            else:
                await broadcast_document(session, attachment.url, caption)
        for embed in message.embeds:
            if embed.url:
                embed_text = f"{header} shared a link:\n{embed.url}"
                if embed.title:
                    embed_text += f"\n<i>{escape_html(embed.title)}</i>"
                await broadcast(session, embed_text)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    async with aiohttp.ClientSession() as session:
        poll_task = asyncio.create_task(poll_telegram(session))
        try:
            await client.start(DISCORD_TOKEN)
        finally:
            poll_task.cancel()

asyncio.run(main())
