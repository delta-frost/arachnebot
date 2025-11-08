import discord
from discord.ext import commands
import logging
from logging.handlers import RotatingFileHandler
import random
from collections import deque, defaultdict
from dotenv import load_dotenv
import os
import asyncio
import re
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None  # Fallback: specific-time daily reminders will be unavailable without zoneinfo
    ZoneInfoNotFoundError = Exception

# Optional async SQLite for persistence
try:
    import aiosqlite
    from aiosqlite import Error as AioSqliteError
except ImportError:
    aiosqlite = None
    class AioSqliteError(Exception):
        pass

load_dotenv()
DB_PATH = os.getenv('BOT_DB_PATH', 'botdata.sqlite3')
TOKEN = os.getenv('DISCORD_TOKEN')
LOG_LEVEL = getattr(logging, os.getenv('BOT_LOG_LEVEL', 'INFO').upper(), logging.INFO)

handler = RotatingFileHandler(filename='discord.log', encoding='utf-8', maxBytes=5_000_000, backupCount=3)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Dynamic command prefix support
DEFAULT_PREFIX = '!'
# Maps guild_id -> prefix (persisted in DB); DMs use DEFAULT_PREFIX
GUILD_PREFIXES = {}


def get_prefix(bot_obj, message):
    try:
        guild = getattr(message, 'guild', None)
        if guild and guild.id in GUILD_PREFIXES:
            return GUILD_PREFIXES[guild.id]
    except AttributeError:
        # No guild on this message; fall back to default
        pass
    return DEFAULT_PREFIX


async def load_guild_prefixes():
    if not aiosqlite:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT guild_id, prefix FROM guild_prefs") as cur:
            async for gid, pref in cur:
                if gid and pref:
                    GUILD_PREFIXES[int(gid)] = str(pref)


async def upsert_guild_prefix(guild_id: int, prefix: str):
    if not aiosqlite:
        GUILD_PREFIXES[guild_id] = prefix
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("REPLACE INTO guild_prefs(guild_id, prefix) VALUES(?,?)", (guild_id, prefix))
        await db.commit()
        GUILD_PREFIXES[guild_id] = prefix


async def clear_guild_prefix(guild_id: int):
    if not aiosqlite:
        GUILD_PREFIXES.pop(guild_id, None)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM guild_prefs WHERE guild_id = ?", (guild_id,))
        await db.commit()
        GUILD_PREFIXES.pop(guild_id, None)


bot = commands.Bot(command_prefix=get_prefix, intents=intents)
bot.remove_command('help')

# --- Persistence helpers ---
async def init_db():
    if not aiosqlite:
        logging.warning("aiosqlite not installed; running in in-memory mode only.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                tz TEXT,
                channel_id INTEGER
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                message TEXT NOT NULL,
                tz TEXT,
                fixed_time INTEGER,
                hour INTEGER,
                minute INTEGER,
                weekday INTEGER,
                next_run TEXT,
                created_at TEXT,
                cancelled INTEGER DEFAULT 0,
                paused INTEGER DEFAULT 0,
                tags TEXT NULL
            )
            """
        )
        # migrations: ensure columns paused and tags exist (idempotent)
        try:
            cur = await db.execute("PRAGMA table_info(reminders)")
            cols = [row[1] for row in await cur.fetchall()]
            if 'paused' not in cols:
                await db.execute("ALTER TABLE reminders ADD COLUMN paused INTEGER DEFAULT 0")
            if 'tags' not in cols:
                await db.execute("ALTER TABLE reminders ADD COLUMN tags TEXT NULL")
        except AioSqliteError as e:
            logging.debug("DB migration check failed (non-fatal): %s", e)
        # Helpful indices for common queries
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user_cancelled ON reminders(user_id, cancelled)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_cancelled ON reminders(cancelled)")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_prefs (
                guild_id INTEGER PRIMARY KEY,
                prefix TEXT
            )
            """
        )
        await db.commit()

async def upsert_user_settings(user_id: int, tz: str = None, channel_id: int = None):
    if not aiosqlite:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        # Merge existing
        cur = await db.execute("SELECT tz, channel_id FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        old_tz, old_ch = (row[0], row[1]) if row else (None, None)
        new_tz = tz if tz is not None else old_tz
        new_ch = channel_id if channel_id is not None else old_ch
        await db.execute("REPLACE INTO users(user_id, tz, channel_id) VALUES(?,?,?)", (user_id, new_tz, new_ch))
        await db.commit()

async def load_users_into_memory():
    if not aiosqlite:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, tz, channel_id FROM users") as cur:
            async for user_id, tz, channel_id in cur:
                if tz:
                    USER_TIMEZONES[user_id] = tz
                if channel_id:
                    USER_REMINDER_CHANNELS[user_id] = channel_id

async def insert_reminder(entry: dict) -> int:
    if not aiosqlite:
        return -1
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO reminders (user_id, type, message, tz, fixed_time, hour, minute, weekday, next_run, created_at, cancelled, paused, tags)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)
            """,
            (
                entry.get('user_id'), entry.get('type'), entry.get('message'), entry.get('tz'),
                1 if entry.get('fixed_time') else 0, entry.get('hour'), entry.get('minute'), entry.get('weekday'),
                entry.get('next_run').isoformat() if entry.get('next_run') else None,
                entry.get('created_at').isoformat() if entry.get('created_at') else None,
                1 if entry.get('paused') else 0,
                entry.get('tags'),
            )
        )
        await db.commit()
        return cur.lastrowid or -1

async def update_reminder_next_run(entry: dict):
    if not aiosqlite:
        return
    rid = entry.get('id')
    if not rid:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET next_run = ? WHERE id = ?", (entry.get('next_run').isoformat() if entry.get('next_run') else None, rid))
        await db.commit()

async def update_reminder_paused(entry: dict, paused: bool):
    if not aiosqlite:
        return
    rid = entry.get('id')
    if not rid:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET paused = ? WHERE id = ?", (1 if paused else 0, rid))
        await db.commit()

async def cancel_reminder(entry: dict):
    if not aiosqlite:
        return
    rid = entry.get('id')
    if not rid:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET cancelled = 1 WHERE id = ?", (rid,))
        await db.commit()

async def load_active_reminders() -> list:
    if not aiosqlite:
        return []
    out = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, user_id, type, message, tz, fixed_time, hour, minute, weekday, next_run, created_at, paused, tags FROM reminders WHERE cancelled = 0") as cur:
            async for rid, user_id, rtype, message, tz, fixed_time, hour, minute, weekday, next_run, created_at, paused, tags in cur:
                try:
                    entry = {
                        'id': rid,
                        'user_id': user_id,
                        'type': rtype,
                        'message': message,
                        'tz': tz,
                        'fixed_time': bool(fixed_time),
                        'hour': hour,
                        'minute': minute,
                        'weekday': weekday,
                        'next_run': datetime.fromisoformat(next_run) if next_run else None,
                        'created_at': datetime.fromisoformat(created_at) if created_at else datetime.now(),
                        'paused': bool(paused),
                        'tags': tags,
                    }
                    out.append(entry)
                except (ValueError, TypeError) as e:
                    logging.exception("Failed to parse reminder row: %s", e)
    return out

async def get_user_delivery_channel(user_id: int):
    # Prefer configured channel/thread, else DM
    try:
        target_id = USER_REMINDER_CHANNELS.get(user_id)
        if target_id:
            ch = bot.get_channel(target_id)
            if ch is None:
                try:
                    ch = await bot.fetch_channel(target_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    ch = None
            if ch is not None:
                return ch
        # Fallback DM
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if user:
            dm = user.dm_channel or await user.create_dm()
            return dm
    except (discord.HTTPException, discord.Forbidden, discord.NotFound) as e:
        logging.exception("Failed to resolve delivery channel for user %s: %s", user_id, e)
    return None

async def schedule_loaded_entries(entries: list):
    for entry in entries:
        CURRENT_REMINDERS.append(entry)
        if entry.get('type') == 'once':
            asyncio.create_task(_schedule_once_noctx(entry))
        elif entry.get('type') == 'daily':
            asyncio.create_task(_schedule_daily_noctx(entry))
        elif entry.get('type') == 'weekly':
            asyncio.create_task(_schedule_weekly_noctx(entry))

async def _schedule_once_noctx(entry: dict):
    try:
        due = entry.get('next_run') or (datetime.now() + timedelta(seconds=10))
        delay = max(0, (due - datetime.now()).total_seconds())
        await asyncio.sleep(delay)
        # If paused, wait until unpaused without advancing
        while entry.get('paused') and not entry.get('cancelled') and entry in CURRENT_REMINDERS:
            await asyncio.sleep(30)
        if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
            return
        await asyncio.sleep(random.uniform(0, 2))
        ch = await get_user_delivery_channel(entry.get('user_id'))
        if ch and _should_deliver(entry):
            await ch.send(f"{entry.get('message','')}\n<@{entry.get('user_id')}>")
    except asyncio.CancelledError:
        raise
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
        logging.exception("Error delivering loaded one-time reminder: %s", e)
    finally:
        try:
            if entry in CURRENT_REMINDERS:
                CURRENT_REMINDERS.remove(entry)
            await cancel_reminder(entry)
        except AioSqliteError:
            pass

async def _schedule_daily_noctx(entry: dict):
    try:
        while True:
            next_run = entry.get('next_run') or (datetime.now() + timedelta(days=1))
            delay = max(0, (next_run - datetime.now()).total_seconds())
            await asyncio.sleep(delay)
            if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                break
            # If paused, wait without advancing next_run
            while entry.get('paused') and not entry.get('cancelled') and entry in CURRENT_REMINDERS:
                await asyncio.sleep(30)
            if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                break
            await asyncio.sleep(random.uniform(0, 2))
            ch = await get_user_delivery_channel(entry.get('user_id'))
            if ch and _should_deliver(entry):
                try:
                    await ch.send(f"{entry.get('message','')}\n<@{entry.get('user_id')}>")
                except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                    logging.exception("Daily reminder send failed: %s", e)
            # Compute next
            try:
                if entry.get('fixed_time') and entry.get('tz'):
                    entry['next_run'] = _compute_next_daily_fixed_run(entry['hour'], entry['minute'], entry['tz'])
                else:
                    entry['next_run'] = datetime.now() + timedelta(days=1)
            except Exception:
                pass
            try:
                await update_reminder_next_run(entry)
            except AioSqliteError:
                pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.exception("Daily scheduler error: %s", e)

async def _schedule_weekly_noctx(entry: dict):
    try:
        while True:
            next_run = entry.get('next_run') or compute_next_weekday_run(datetime.now(), entry.get('weekday') or datetime.now().weekday())
            delay = max(0, (next_run - datetime.now()).total_seconds())
            await asyncio.sleep(delay)
            if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                break
            # If paused, wait without advancing next_run
            while entry.get('paused') and not entry.get('cancelled') and entry in CURRENT_REMINDERS:
                await asyncio.sleep(30)
            if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                break
            await asyncio.sleep(random.uniform(0, 2))
            ch = await get_user_delivery_channel(entry.get('user_id'))
            if ch and _should_deliver(entry):
                try:
                    await ch.send(f"{entry.get('message','')}\n<@{entry.get('user_id')}>")
                except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                    logging.exception("Weekly reminder send failed: %s", e)
            try:
                if entry.get('fixed_time') and entry.get('tz') is not None:
                    entry['next_run'] = _compute_next_weekly_fixed_run(entry['hour'], entry['minute'], entry['tz'], entry['weekday'])
                else:
                    entry['next_run'] = datetime.now() + timedelta(days=7)
            except Exception:
                pass
            try:
                await update_reminder_next_run(entry)
            except AioSqliteError:
                pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.exception("Weekly scheduler error: %s", e)

@bot.event
async def on_ready():
    print(f"{bot.user.name} is ready to go")
    try:
        await init_db()
        await load_users_into_memory()
        await load_guild_prefixes()
        entries = await load_active_reminders()
        await schedule_loaded_entries(entries)
        print(f"Loaded {len(entries)} reminders from DB")
    except Exception as e:
        logging.exception("Startup DB init/load failed: %s", e)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Ignore messages sent by webhooks
    if message.webhook_id:
        return

    await bot.process_commands(message)

# --- Reminder utilities ---
UNIT_SECONDS = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
}

MIN_DELAY = 5          # seconds
MAX_DELAY = 30 * 24 * 3600  # 30 days in seconds


# In-memory registry of scheduled reminders (lost on restart)
# Each entry: {
#   'user_id': int,
#   'type': 'once'|'daily'|'weekly',
#   'message': str,
#   'next_run': datetime,
#   'created_at': datetime,
#   'tz': str | None,           # optional: user's IANA timezone if needed
#   'fixed_time': bool | None,  # for reminders with specific HH:MM
#   'hour': int | None,
#   'minute': int | None,
#   'weekday': int | None,      # for weekly (0=Mon..6=Sun)
# }
CURRENT_REMINDERS = []

# De-duplication guard: prevent duplicate sends within a short time window (e.g., around DST shifts)
DEDUP_WINDOW_SECONDS = 600  # 10 minutes
_LAST_FIRED = {}  # key -> datetime of last successful delivery

def _reminder_key(entry: dict):
    rid = entry.get('id')
    if rid:
        return ('id', rid)
    # Fallback composite key for in-memory, not-yet-persisted entries
    return (
        'anon',
        entry.get('user_id'),
        entry.get('type'),
        entry.get('message'),
        entry.get('hour'),
        entry.get('minute'),
        entry.get('weekday'),
    )


def _should_deliver(entry: dict) -> bool:
    try:
        key = _reminder_key(entry)
        now = datetime.now()
        last = _LAST_FIRED.get(key)
        if last and (now - last).total_seconds() < DEDUP_WINDOW_SECONDS:
            return False
        _LAST_FIRED[key] = now
        return True
    except Exception:
        # On any unexpected error, default to delivering once
        return True

# In-memory user timezone settings (lost on restart)
# Maps user_id -> IANA timezone string (e.g., "America/New_York")
USER_TIMEZONES = {}

# In-memory per-user reminder delivery channel/thread (lost on restart)
# Maps user_id -> channel_or_thread_id (int)
USER_REMINDER_CHANNELS = {}

# Limits and rate limiting
DEFAULT_LIMITS = {
    'per_user_active': 50,
    'per_user_per_guild': 30,
    'per_guild_total': 2000,
    'message_len': 500,
    'max_one_time_horizon_days': 365,
}

# Simple in-memory counters for per-guild usage (creation time only; not persisted)
GUILD_ACTIVE_COUNTS = defaultdict(int)  # guild_id -> active reminders count
GUILD_USER_ACTIVE_COUNTS = defaultdict(lambda: defaultdict(int))  # guild_id -> (user_id -> count)

# Token bucket: 10 actions per minute per user for create/edit/delete
USER_ACTION_BUCKETS = defaultdict(deque)  # user_id -> deque[timestamps]
ACTIONS_PER_MINUTE = 10


def _rate_limit_check(user_id: int) -> bool:
    now = datetime.now().timestamp()
    dq = USER_ACTION_BUCKETS[user_id]
    # drop entries older than 60s
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= ACTIONS_PER_MINUTE:
        return False
    dq.append(now)
    return True


def _count_user_active(user_id: int) -> int:
    # Prefer DB if available
    if aiosqlite:
        # This is async normally, but for quick precheck we rely on in-memory; DB counts are used in diagnostics/limits display.
        pass
    return sum(1 for e in CURRENT_REMINDERS if e.get('user_id') == user_id and not e.get('cancelled'))


def _enforce_limits_precreate(ctx, message_text: str, due: datetime | None, rtype: str) -> str | None:
    """Return error string if a limit is violated; None if ok."""
    # Message length
    if message_text is None or len(message_text.strip()) == 0:
        return "Please include a message."
    if len(message_text) > DEFAULT_LIMITS['message_len']:
        return f"Message too long. Please keep it under {DEFAULT_LIMITS['message_len']} characters."
    # One-time horizon
    if rtype == 'once' and due is not None:
        horizon = (due - datetime.now()).total_seconds()
        if horizon < MIN_DELAY:
            return f"The time is too soon. Minimum delay is {MIN_DELAY}s."
        max_horizon = DEFAULT_LIMITS['max_one_time_horizon_days'] * 86400
        if horizon > max_horizon:
            return f"The date/time is too far in the future. Max horizon is {DEFAULT_LIMITS['max_one_time_horizon_days']} days."
    # Per-user active
    user_active = _count_user_active(ctx.author.id)
    if user_active >= DEFAULT_LIMITS['per_user_active']:
        return (f"Limit reached: You have {user_active} active reminders (max {DEFAULT_LIMITS['per_user_active']}). "
                f"Use `{getattr(ctx,'prefix','!')}reminders` and `{getattr(ctx,'prefix','!')}deletereminder <number>` to free some up.")
    # Per-guild counts (in-memory only)
    if getattr(ctx, 'guild', None) is not None:
        gid = ctx.guild.id
        per_user_guild = GUILD_USER_ACTIVE_COUNTS[gid][ctx.author.id]
        if per_user_guild >= DEFAULT_LIMITS['per_user_per_guild']:
            return (f"Server limit reached: You have {per_user_guild} active reminders in this server (max {DEFAULT_LIMITS['per_user_per_guild']}).")
        guild_total = GUILD_ACTIVE_COUNTS[gid]
        if guild_total >= DEFAULT_LIMITS['per_guild_total']:
            return (f"Server is at capacity for reminders (max {DEFAULT_LIMITS['per_guild_total']}). Please try later or delete some.")
    return None


def _bump_guild_counters_on_create(ctx):
    if getattr(ctx, 'guild', None) is not None:
        gid = ctx.guild.id
        GUILD_ACTIVE_COUNTS[gid] += 1
        GUILD_USER_ACTIVE_COUNTS[gid][ctx.author.id] += 1


def _dec_guild_counters_on_delete(ctx):
    if getattr(ctx, 'guild', None) is not None:
        gid = ctx.guild.id
        if GUILD_ACTIVE_COUNTS[gid] > 0:
            GUILD_ACTIVE_COUNTS[gid] -= 1
        if GUILD_USER_ACTIVE_COUNTS[gid][ctx.author.id] > 0:
            GUILD_USER_ACTIVE_COUNTS[gid][ctx.author.id] -= 1


async def get_reminders_channel(ctx):
    """
    Resolve the target channel/thread to post reminders for the invoking user.
    Priority:
    1) User-configured channel/thread set via !setremindchannel.
    2) Fallback to the invoking channel (works for text channels, threads, and DMs).
    """
    try:
        user_id = getattr(getattr(ctx, 'author', None), 'id', None)
        # Use user-configured target if available
        if user_id and user_id in USER_REMINDER_CHANNELS:
            target_id = USER_REMINDER_CHANNELS.get(user_id)
            ch = bot.get_channel(target_id)
            if ch is None:
                try:
                    ch = await bot.fetch_channel(target_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    ch = None
            if ch is not None:
                # Check send permissions where applicable (guild channels/threads)
                can_send = True
                try:
                    guild = getattr(ch, 'guild', None)
                    if guild is not None:
                        me = guild.me or ctx.me
                        perms = ch.permissions_for(me)
                        can_send = bool(getattr(perms, 'send_messages', False))
                except AttributeError:
                    pass
                if can_send:
                    return ch
        # Fallback to invoking channel; ensure we can send, else DM
        ch = ctx.channel
        try:
            guild = getattr(ch, 'guild', None)
            if guild is not None:
                me = guild.me or ctx.me
                perms = ch.permissions_for(me)
                if not bool(getattr(perms, 'send_messages', False)):
                    dm = await get_user_delivery_channel(user_id) if user_id else None
                    return dm or ch
        except AttributeError:
            pass
        return ch
    except Exception as e:
        logging.exception("Failed to resolve delivery channel: %s", e)
        return ctx.channel


def parse_duration(text: str):
    """
    Parse a compact duration string like '30s', '10m', '2h', '1d'.
    Returns (seconds:int, pretty:str) or (None, error:str) when invalid.
    """
    text = text.strip().lower()
    m = re.fullmatch(r"(\d+)([smhd])", text)
    if not m:
        return None, "Please provide time like 30s, 10m, 2h, or 1d."
    value = int(m.group(1))
    unit = m.group(2)
    seconds = value * UNIT_SECONDS[unit]
    if seconds < MIN_DELAY:
        return None, f"Duration too short. Minimum is {MIN_DELAY}s."
    if seconds > MAX_DELAY:
        return None, "Duration too long. Maximum is 30d."
    # Pretty string
    unit_names = {'s': 'second', 'm': 'minute', 'h': 'hour', 'd': 'day'}
    pretty = f"{value} {unit_names[unit]}{'s' if value != 1 else ''}"

    return seconds, pretty


@bot.command(name="remindme", help="Set a reminder: !remindme <time|HH:MM> <message>. Examples: !remindme 10m stretch | !remindme 08:15 stand up")
async def remindme(ctx, when: str = None, *, message: str = None):
    """
    Schedule a reminder that pings the user either after a duration or at a specific time today/tomorrow in your time zone.
    Usage: !remindme 10m drink water | !remindme 08:30 drink water (requires !settimezone)
    """
    if when is None or message is None:
        return await ctx.send("Usage: !remindme <time|HH:MM> <message> (e.g., !remindme 15m check the oven | !remindme 08:30 drink water)")
    # Rate limit
    if not _rate_limit_check(ctx.author.id):
        return await ctx.send("You're doing that too often. Please wait a bit before creating more reminders (10 actions/min).")

    # Try HH:MM first
    fixed = _parse_hhmm(when)
    if fixed:
        hour, minute = fixed
        tz_name = USER_TIMEZONES.get(ctx.author.id)
        if not tz_name:
            return await ctx.send("Please set your time zone first with !settimezone <IANA_tz> (e.g., America/New_York).")
        due = _compute_next_one_time_fixed_run(hour, minute, tz_name)
        # Limits check
        err = _enforce_limits_precreate(ctx, message, due, 'once')
        if err:
            return await ctx.send(err)
        # Register in-memory
        entry = {
            'user_id': ctx.author.id,
            'type': 'once',
            'message': message,
            'next_run': due,
            'created_at': datetime.now(),
            'tz': tz_name,
            'fixed_time': True,
            'hour': hour,
            'minute': minute,
            'paused': False,
        }
        CURRENT_REMINDERS.append(entry)
        _bump_guild_counters_on_create(ctx)
        try:
            rid = await insert_reminder(entry)
            if rid and rid > 0:
                entry['id'] = rid
        except AioSqliteError as e:
            logging.exception("Failed to persist one-time fixed reminder: %s", e)
        pretty_when = f"at {hour:02d}:{minute:02d} ({tz_name}) on {due.strftime('%Y-%m-%d')}"
        await ctx.send(f"{ctx.author.mention} I will remind you {pretty_when}: {message}")

        async def deliver():
            try:
                delay = max(0, (due - datetime.now()).total_seconds())
                await asyncio.sleep(delay)
                # If paused, wait
                while entry.get('paused') and not entry.get('cancelled') and entry in CURRENT_REMINDERS:
                    await asyncio.sleep(30)
                # Skip if cancelled or removed
                if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                    return
                await asyncio.sleep(random.uniform(0, 2))
                target_channel = await get_reminders_channel(ctx)
                if _should_deliver(entry):
                    await target_channel.send(f"{message}\n{ctx.author.mention}")
            except asyncio.CancelledError:
                raise
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                logging.exception("Error delivering reminder: %s", e)
            finally:
                try:
                    if entry in CURRENT_REMINDERS:
                        CURRENT_REMINDERS.remove(entry)
                    await cancel_reminder(entry)
                    _dec_guild_counters_on_delete(ctx)
                except Exception:
                    pass
        asyncio.create_task(deliver())
        return

    # Fallback to duration-based parsing
    seconds, result = parse_duration(when)
    if seconds is None:
        return await ctx.send(result)

    due = datetime.now() + timedelta(seconds=seconds)
    # Limits check
    err = _enforce_limits_precreate(ctx, message, due, 'once')
    if err:
        return await ctx.send(err)
    # Register in-memory
    entry = {
        'user_id': ctx.author.id,
        'type': 'once',
        'message': message,
        'next_run': due,
        'created_at': datetime.now(),
        'paused': False,
    }
    CURRENT_REMINDERS.append(entry)
    _bump_guild_counters_on_create(ctx)
    try:
        rid = await insert_reminder(entry)
        if rid and rid > 0:
            entry['id'] = rid
    except AioSqliteError as e:
        logging.exception("Failed to persist one-time reminder: %s", e)

    confirmation = f"{ctx.author.mention} I will remind you in {result}: {message}"
    await ctx.send(confirmation)

    async def deliver():
        try:
            await asyncio.sleep(seconds)
            # If paused, wait
            while entry.get('paused') and not entry.get('cancelled') and entry in CURRENT_REMINDERS:
                await asyncio.sleep(30)
            # Skip if cancelled or removed
            if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                return
            # If the channel still exists and the bot can send messages, deliver it to the reminders channel.
            await asyncio.sleep(random.uniform(0, 2))
            target_channel = await get_reminders_channel(ctx)
            if _should_deliver(entry):
                await target_channel.send(f"{message}\n{ctx.author.mention}")
        except asyncio.CancelledError:
            raise
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
            # Swallow exceptions to avoid crashing the task; optional: log to file.
            logging.exception("Error delivering reminder: %s", e)
        finally:
            # Remove from registry when done
            try:
                if entry in CURRENT_REMINDERS:
                    CURRENT_REMINDERS.remove(entry)
                await cancel_reminder(entry)
                _dec_guild_counters_on_delete(ctx)
            except AioSqliteError:
                pass

    # Fire-and-forget task
    asyncio.create_task(deliver())

# --- Weekly recurring reminders ---
WEEKDAY_ALIASES = {
    'monday': 0, 'mon': 0,
    'tuesday': 1, 'tue': 1, 'tues': 1,
    'wednesday': 2, 'wed': 2,
    'thursday': 3, 'thu': 3, 'thur': 3, 'thurs': 3,
    'friday': 4, 'fri': 4,
    'saturday': 5, 'sat': 5,
    'sunday': 6, 'sun': 6,
}


def parse_weekday(name: str):
    if not name:
        return None
    key = name.strip().lower()
    return WEEKDAY_ALIASES.get(key)


def compute_next_weekday_run(now: datetime, target_weekday: int) -> datetime:
    """
    Compute the next datetime occurrence for the given weekday at the same time of day as 'now'.
    If today is the target weekday, schedule for the same time next week.
    """
    days_ahead = (target_weekday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return now + timedelta(days=days_ahead)


def _humanize_time_until(dt: datetime) -> str:
    try:
        now = datetime.now()
        delta = dt - now
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            return f"due now ({dt.strftime('%Y-%m-%d %H:%M')})"
        days, rem = divmod(total_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes and not days:
            parts.append(f"{minutes}m")
        pretty = ' '.join(parts) if parts else f"{total_seconds}s"
        return f"in {pretty} ({dt.strftime('%Y-%m-%d %H:%M')})"
    except Exception:
        return dt.strftime('%Y-%m-%d %H:%M')


@bot.command(name="remindevery", help="Set a recurring weekly reminder: !remindevery <weekday> [HH:MM] <message>. Uses your time zone for specific times.")
async def remindevery(ctx, weekday: str = None, *, message: str = None):
    """
    Schedule a recurring weekly reminder on the specified weekday.
    Usage:
    - !remindevery friday drink water  -> every Friday around this time
    - !remindevery fri 09:00 drink water  -> every Friday at 09:00 in your time zone (requires !settimezone)
    """
    if weekday is None or message is None:
        return await ctx.send("Usage: !remindevery <weekday> [HH:MM] <message> (e.g., !remindevery friday 09:00 drink water)")
    # Rate limit
    if not _rate_limit_check(ctx.author.id):
        return await ctx.send("You're doing that too often. Please wait a bit before creating more reminders (10 actions/min).")

    target_idx = parse_weekday(weekday)
    if target_idx is None:
        return await ctx.send("Invalid weekday. Use names like Monday, Tue, Wednesday, Thu, Friday, Sat, Sun.")

    fixed_hour = fixed_min = None
    actual_message = message.strip()

    m = re.match(r"^(\d{1,2}:\d{2})\s+(.+)$", actual_message)
    if m:
        hhmm = m.group(1)
        actual_message = m.group(2)
        parsed = _parse_hhmm(hhmm)
        if not parsed:
            return await ctx.send("Invalid time format. Use 24h HH:MM, e.g., 07:45 or 19:05.")
        fixed_hour, fixed_min = parsed
        tz_name = USER_TIMEZONES.get(ctx.author.id)
        if not tz_name:
            return await ctx.send("Please set your time zone first with !settimezone <IANA_tz> (e.g., America/New_York).")
        next_run = _compute_next_weekly_fixed_run(fixed_hour, fixed_min, tz_name, target_idx)
        when_str = f"{next_run.strftime('%A')} weekly at {fixed_hour:02d}:{fixed_min:02d} ({tz_name}) starting {next_run.strftime('%Y-%m-%d')}"
    else:
        now = datetime.now()
        next_run = compute_next_weekday_run(now, target_idx)
        when_str = f"{next_run.strftime('%A')} at around {next_run.strftime('%H:%M')} (starting {next_run.strftime('%Y-%m-%d')})"
        tz_name = None

    # Limits check
    err = _enforce_limits_precreate(ctx, actual_message, None, 'weekly')
    if err:
        return await ctx.send(err)

    # Register in-memory
    entry = {
        'user_id': ctx.author.id,
        'type': 'weekly',
        'message': actual_message,
        'next_run': next_run,
        'created_at': datetime.now(),
        'tz': tz_name,
        'fixed_time': fixed_hour is not None,
        'hour': fixed_hour,
        'minute': fixed_min,
        'weekday': target_idx,
        'paused': False,
    }
    CURRENT_REMINDERS.append(entry)
    _bump_guild_counters_on_create(ctx)
    try:
        rid = await insert_reminder(entry)
        if rid and rid > 0:
            entry['id'] = rid
    except AioSqliteError as e:
        logging.exception("Failed to persist weekly reminder: %s", e)

    await ctx.send(f"{ctx.author.mention} I will remind you every {when_str}. Message: {actual_message}")

    async def loop():
        try:
            target_channel = await get_reminders_channel(ctx)
            initial_delay = max(0, (next_run - datetime.now()).total_seconds())
            await asyncio.sleep(initial_delay)
            while True:
                # Stop if cancelled/removed
                if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                    break
                # If paused, wait
                while entry.get('paused') and not entry.get('cancelled') and entry in CURRENT_REMINDERS:
                    await asyncio.sleep(30)
                if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                    break
                try:
                    await asyncio.sleep(random.uniform(0, 2))
                    if _should_deliver(entry):
                        await target_channel.send(f"{actual_message}\n{ctx.author.mention}")
                except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                    logging.exception("Error delivering recurring reminder: %s", e)
                # Update next occurrence and then sleep until next_run
                try:
                    if entry.get('fixed_time') and entry.get('tz') is not None:
                        entry['next_run'] = _compute_next_weekly_fixed_run(entry['hour'], entry['minute'], entry['tz'], entry['weekday'])
                        next_sleep = max(0, (entry['next_run'] - datetime.now()).total_seconds())
                    else:
                        entry['next_run'] = datetime.now() + timedelta(days=7)
                        next_sleep = 7 * 24 * 3600
                    await update_reminder_next_run(entry)
                except Exception:
                    next_sleep = 7 * 24 * 3600
                # Sleep, but wake if cancelled by re-check after sleep
                await asyncio.sleep(next_sleep)
            # Cleanup: ensure removed from registry
            try:
                if entry in CURRENT_REMINDERS:
                    CURRENT_REMINDERS.remove(entry)
            except Exception:
                pass
        except Exception as e:
            logging.exception("Recurring reminder loop ended: %s", e)

    asyncio.create_task(loop())


def _parse_hhmm(text: str):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2))
    if 0 <= h <= 23 and 0 <= mnt <= 59:
        return h, mnt
    return None


def _compute_next_daily_fixed_run(hour: int, minute: int, tz_name: str) -> datetime:
    """Compute next occurrence in server-local naive time for a user's tz fixed time."""
    try:
        tz = ZoneInfo(tz_name) if ZoneInfo else None
    except ZoneInfoNotFoundError:
        tz = None
    now_server = datetime.now()
    if not tz:
        # Fallback: schedule next day at server time (approximation)
        candidate = now_server.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_server:
            candidate = candidate + timedelta(days=1)
        return candidate
    now_user = datetime.now(tz)
    candidate = now_user.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_user:
        candidate = candidate + timedelta(days=1)
    # Convert to server local naive
    candidate_server = candidate.astimezone().replace(tzinfo=None)
    return candidate_server


def _compute_next_weekly_fixed_run(hour: int, minute: int, tz_name: str, target_weekday: int) -> datetime:
    """Next occurrence of target_weekday at HH:MM in user's tz, returned as server-local naive."""
    try:
        tz = ZoneInfo(tz_name) if ZoneInfo else None
    except ZoneInfoNotFoundError:
        tz = None
    now_server = datetime.now()
    if not tz:
        # Approximate using server local time if tz not available
        now = now_server
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_weekday - now.weekday()) % 7
        if days_ahead == 0 and base <= now:
            days_ahead = 7
        candidate = base + timedelta(days=days_ahead)
        return candidate
    now_user = datetime.now(tz)
    base = now_user.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (target_weekday - now_user.weekday()) % 7
    if days_ahead == 0 and base <= now_user:
        days_ahead = 7
    candidate = base + timedelta(days=days_ahead)
    # Rebuild time after adding days to avoid any overflow issues (safety)
    candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Convert to server-local naive
    return candidate.astimezone().replace(tzinfo=None)


def _compute_next_one_time_fixed_run(hour: int, minute: int, tz_name: str) -> datetime:
    """Next occurrence today or tomorrow at HH:MM in user's tz, returned as server-local naive."""
    try:
        tz = ZoneInfo(tz_name) if ZoneInfo else None
    except ZoneInfoNotFoundError:
        tz = None
    now_server = datetime.now()
    if not tz:
        candidate = now_server.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_server:
            candidate = candidate + timedelta(days=1)
        return candidate
    now_user = datetime.now(tz)
    candidate = now_user.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_user:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone().replace(tzinfo=None)


def _compute_specific_date_run(year: int, month: int, day: int, hour: int, minute: int, tz_name: str = None) -> datetime:
    """Compute a server-local naive datetime for a specific calendar date/time in a user's tz (if available).
    If tz_name is None or invalid, uses server local time. Raises ValueError if the provided date is invalid.
    """
    # Validate date by constructing a date first
    try:
        _ = datetime(year, month, day)  # will raise if invalid
    except Exception as e:
        raise ValueError("Invalid date") from e
    # Try to use timezone if available
    tz = None
    if tz_name and ZoneInfo:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    if tz is None:
        # Server local naive datetime (construct directly instead of replacing 'now')
        return datetime(year, month, day, hour, minute, 0, 0)
    # Build in user's tz, then convert to server local naive
    dt_user = datetime(year, month, day, hour, minute, 0, 0, tzinfo=tz)
    return dt_user.astimezone().replace(tzinfo=None)


@bot.command(name="settimezone", help="Set your time zone: !settimezone <IANA_tz> (e.g., America/New_York)")
async def settimezone(ctx, tz: str = None):
    if tz is None:
        return await ctx.send("Usage: !settimezone <IANA_tz> (e.g., America/Los_Angeles). Find yours at https://en.wikipedia.org/wiki/List_of_tz_database_time_zones")
    if ZoneInfo is None:
        return await ctx.send("Sorry, this bot requires Python 3.9+ with zoneinfo to set time zones.")
    try:
        _ = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        return await ctx.send("Invalid time zone. Please provide a valid IANA tz (e.g., Europe/London, Asia/Kolkata).")
    USER_TIMEZONES[ctx.author.id] = tz
    try:
        await upsert_user_settings(ctx.author.id, tz=tz)
    except AioSqliteError as e:
        logging.exception("Failed to persist timezone: %s", e)
    await ctx.send(f"{ctx.author.mention} Your time zone has been set to {tz}.")


@bot.command(name="setremindchannel", aliases=["setchannel"], help="Set the channel or thread where your reminders will be sent: !setremindchannel [here|#channel|channel_id|thread_id]")
async def setchannel(ctx, *, target: str = None):
    """
    Set your reminders delivery channel/thread.
    Usage: !setremindchannel [here|#channel|channel_id|thread_id]
    If omitted, defaults to the current channel/thread.
    """
    try:
        ch_obj = None
        if target is None or target.strip().lower() in ("here", "this"):
            ch_obj = ctx.channel
        else:
            t = target.strip()
            id_str = None
            m = re.fullmatch(r"<#(\d+)>", t)
            if m:
                id_str = m.group(1)
            elif re.fullmatch(r"\d{5,}", t):
                id_str = t
            else:
                # Try resolve by name within the guild
                if ctx.guild is not None:
                    name = t.lstrip('#').lower()
                    ch_obj = discord.utils.get(ctx.guild.text_channels, name=name)
                    # Try active threads in the current channel
                    if ch_obj is None:
                        try:
                            threads = []
                            if hasattr(ctx.channel, 'threads'):
                                threads = list(ctx.channel.threads)
                            # Also include archived threads if available via API (skip HTTP for simplicity)
                            for th in threads:
                                if getattr(th, 'name', '').lower() == name:
                                    ch_obj = th
                                    break
                        except AttributeError:
                            pass
            if ch_obj is None and id_str:
                try:
                    cid = int(id_str)
                except ValueError:
                    cid = None
                if cid is not None:
                    ch_obj = bot.get_channel(cid)
                    if ch_obj is None:
                        try:
                            ch_obj = await bot.fetch_channel(cid)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            ch_obj = None
        if ch_obj is None:
            return await ctx.send("I couldn't find that channel/thread. Provide a channel mention like #general, a numeric ID, or use !setremindchannel here.")
        # Validate bot can send messages
        can_send = True
        try:
            guild = getattr(ch_obj, 'guild', None)
            if guild is not None:
                me = guild.me or ctx.me
                perms = ch_obj.permissions_for(me)
                can_send = bool(getattr(perms, 'send_messages', False))
        except AttributeError:
            pass
        if not can_send:
            return await ctx.send("I don't have permission to send messages in that channel/thread. Please choose another.")
        USER_REMINDER_CHANNELS[ctx.author.id] = ch_obj.id
        try:
            await upsert_user_settings(ctx.author.id, channel_id=ch_obj.id)
        except AioSqliteError as e:
            logging.exception("Failed to persist reminder channel: %s", e)
        # Build friendly destination text
        dest = "that channel"
        try:
            if isinstance(ch_obj, discord.Thread):
                dest = f"thread '{ch_obj.name}'"
            elif hasattr(ch_obj, 'name'):
                dest = f"#{ch_obj.name}"
        except Exception:
            pass
        await ctx.send(f"{ctx.author.mention} Your reminders will now be sent to {dest}.")
    except Exception as ex:
        logging.exception("Failed to set reminders channel: %s", ex)
        await ctx.send("Sorry, I couldn't set your reminders channel right now.")


@bot.command(name="setprefix", help="Change the bot's command prefix for this server: !setprefix <new_prefix> | reset")
async def setprefix(ctx, *, new_prefix: str = None):
    """
    Change the bot's command prefix for the current server.
    - Usage: <prefix>setprefix <new_prefix>
    - Use 'reset' to restore the default prefix.
    - Only available in servers, and requires Manage Guild or Administrator permission (or be the server owner).
    """
    try:
        if ctx.guild is None:
            return await ctx.send("This command can only be used in a server (not in DMs).")
        # Permission check
        is_owner = ctx.author.id == getattr(ctx.guild, 'owner_id', 0)
        perms = getattr(ctx.author, 'guild_permissions', None)
        has_perm = bool(perms.administrator or perms.manage_guild) if perms else False
        if not (is_owner or has_perm):
            return await ctx.send("You need Administrator or Manage Server permission to change the prefix.")
        if new_prefix is None or not str(new_prefix).strip():
            current = GUILD_PREFIXES.get(ctx.guild.id, DEFAULT_PREFIX)
            return await ctx.send(f"Usage: {getattr(ctx, 'prefix', DEFAULT_PREFIX)}setprefix <new_prefix> | reset\nCurrent prefix here: `{current}`")
        token = str(new_prefix).strip()
        # Allow reset keywords
        if token.lower() in {"reset", "default", "clear"}:
            await clear_guild_prefix(ctx.guild.id)
            return await ctx.send(f"Prefix reset to default: `{DEFAULT_PREFIX}`")
        # Basic validation: 1-5 visible non-whitespace characters
        if len(token) < 1 or len(token) > 5 or any(ch.isspace() for ch in token):
            return await ctx.send("Please choose a prefix of 1–5 non‑whitespace characters (e.g., !, ?, $, .).")
        await upsert_guild_prefix(ctx.guild.id, token)
        return await ctx.send(f"Prefix updated for this server: `{token}`. Try `{token}help`.")
    except Exception as e:
        logging.exception("Failed to set prefix: %s", e)
        await ctx.send("Sorry, I couldn't change the prefix right now.")


@bot.command(name="remindeveryday", help="Set a recurring daily reminder: !remindeveryday [HH:MM] <message>. Uses your set time zone for specific times.")
async def remindeveryday(ctx, *, message: str = None):
    """
    Schedule a recurring daily reminder.
    Usage:
    - !remindeveryday drink water  -> every day around this time
    - !remindeveryday 08:30 drink water  -> every day at 08:30 in your time zone (requires !settimezone)
    """
    if message is None or not message.strip():
        return await ctx.send("Usage: !remindeveryday [HH:MM] <message> (e.g., !remindeveryday 08:30 drink water)")
    # Rate limit
    if not _rate_limit_check(ctx.author.id):
        return await ctx.send("You're doing that too often. Please wait a bit before creating more reminders (10 actions/min).")

    fixed_hour = fixed_min = None
    actual_message = message.strip()

    # Try to parse a leading HH:MM token
    m = re.match(r"^(\d{1,2}:\d{2})\s+(.+)$", actual_message)
    if m:
        hhmm = m.group(1)
        actual_message = m.group(2)
        parsed = _parse_hhmm(hhmm)
        if not parsed:
            return await ctx.send("Invalid time format. Use 24h HH:MM, e.g., 07:45 or 19:05.")
        fixed_hour, fixed_min = parsed
        tz_name = USER_TIMEZONES.get(ctx.author.id)
        if not tz_name:
            return await ctx.send("Please set your time zone first with !settimezone <IANA_tz> (e.g., America/New_York).")
        next_run = _compute_next_daily_fixed_run(fixed_hour, fixed_min, tz_name)
        when_str = f"every day at {fixed_hour:02d}:{fixed_min:02d} ({tz_name}) starting {next_run.strftime('%Y-%m-%d')}"
    else:
        now = datetime.now()
        next_run = now + timedelta(days=1)
        when_str = f"every day at around {next_run.strftime('%H:%M')} (starting {next_run.strftime('%Y-%m-%d')})"
        tz_name = None

    # Limits check (daily doesn't use horizon)
    err = _enforce_limits_precreate(ctx, actual_message, None, 'daily')
    if err:
        return await ctx.send(err)

    # Register in-memory
    entry = {
        'user_id': ctx.author.id,
        'type': 'daily',
        'message': actual_message,
        'next_run': next_run,
        'created_at': datetime.now(),
        'tz': tz_name,
        'fixed_time': fixed_hour is not None,
        'hour': fixed_hour,
        'minute': fixed_min,
        'paused': False,
    }
    CURRENT_REMINDERS.append(entry)
    _bump_guild_counters_on_create(ctx)
    try:
        rid = await insert_reminder(entry)
        if rid and rid > 0:
            entry['id'] = rid
    except AioSqliteError as e:
        logging.exception("Failed to persist daily reminder: %s", e)

    await ctx.send(f"{ctx.author.mention} I will remind you {when_str}. Message: {actual_message}")

    async def loop():
        try:
            target_channel = await get_reminders_channel(ctx)
            initial_delay = max(0, (next_run - datetime.now()).total_seconds())
            await asyncio.sleep(initial_delay)
            while True:
                # Stop if cancelled/removed
                if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                    break
                # If paused, wait
                while entry.get('paused') and not entry.get('cancelled') and entry in CURRENT_REMINDERS:
                    await asyncio.sleep(30)
                if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                    break
                try:
                    await asyncio.sleep(random.uniform(0, 2))
                    if _should_deliver(entry):
                        await target_channel.send(f"{actual_message}\n{ctx.author.mention}")
                except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                    logging.exception("Error delivering daily recurring reminder: %s", e)
                # Compute next occurrence
                try:
                    if entry.get('fixed_time') and entry.get('tz'):
                        entry['next_run'] = _compute_next_daily_fixed_run(entry['hour'], entry['minute'], entry['tz'])
                    else:
                        entry['next_run'] = datetime.now() + timedelta(days=1)
                    await update_reminder_next_run(entry)
                    next_sleep = max(0, (entry['next_run'] - datetime.now()).total_seconds())
                except Exception:
                    next_sleep = 24 * 3600
                await asyncio.sleep(next_sleep)
            # Cleanup: ensure removed from registry
            try:
                if entry in CURRENT_REMINDERS:
                    CURRENT_REMINDERS.remove(entry)
            except Exception:
                pass
        except Exception as e:
            logging.exception("Daily recurring reminder loop ended: %s", e)

    asyncio.create_task(loop())


@bot.command(name="reminders", help="List your current reminders")
async def list_reminders(ctx):
    """List all in-memory reminders scheduled by the invoking user, with next run times."""
    try:
        items = [e for e in CURRENT_REMINDERS if e.get('user_id') == ctx.author.id]
        if not items:
            return await ctx.send(f"{ctx.author.mention} you have no reminders set.")
        # Sort by next_run
        items.sort(key=lambda e: e.get('next_run') or datetime.max)
        lines = []
        for idx, e in enumerate(items, start=1):
            typ = e.get('type', 'once')
            msg = e.get('message', '')
            nr = e.get('next_run')
            when = _humanize_time_until(nr) if isinstance(nr, datetime) else 'unknown time'
            paused = ' [paused]' if e.get('paused') else ''
            lines.append(f"{idx}. [{typ}{paused}] {when} — {msg}")
        text = "Your current reminders:\n" + "\n".join(lines)
        text += "\n\nTip: delete with !deletereminder <number>, edit with !editreminder <number> <new message>, snooze with !snooze <number> <duration>, or pause/resume with !pausereminder/!resumereminder."
        await ctx.send(text)
    except Exception as ex:
        logging.exception("Failed to list reminders: %s", ex)
        await ctx.send("Sorry, I couldn't retrieve your reminders right now.")


@bot.command(name="deletereminder", aliases=["delreminder", "rmreminder"], help="Delete one of your reminders by its number from !reminders")
async def delete_reminder(ctx, index: int = None):
    """Delete the invoking user's reminder by its 1-based index as shown in !reminders."""
    try:
        # Rate limit
        if not _rate_limit_check(ctx.author.id):
            return await ctx.send("You're doing that too often. Please wait a bit before modifying reminders (10 actions/min).")
        if index is None:
            return await ctx.send("Usage: !deletereminder <number> (use !reminders to see the list)")
        if index <= 0:
            return await ctx.send("Please provide a positive number (1, 2, 3, ...).")
        # Acquire the user's reminders in the same ordering as list_reminders
        items = [e for e in CURRENT_REMINDERS if e.get('user_id') == ctx.author.id]
        if not items:
            return await ctx.send(f"{ctx.author.mention} you have no reminders to delete.")
        items.sort(key=lambda e: e.get('next_run') or datetime.max)
        if index > len(items):
            return await ctx.send(f"Invalid number. You currently have {len(items)} reminder(s).")
        entry = items[index - 1]
        # Mark cancelled and remove from registry
        entry['cancelled'] = True
        try:
            CURRENT_REMINDERS.remove(entry)
        except ValueError:
            pass
        try:
            await cancel_reminder(entry)
        except AioSqliteError as e:
            logging.exception("Failed to cancel reminder in DB: %s", e)
        _dec_guild_counters_on_delete(ctx)
        msg = entry.get('message', '')
        typ = entry.get('type', 'once')
        await ctx.send(f"Deleted reminder #{index} [{typ}]: {msg}")
    except Exception as ex:
        logging.exception("Failed to delete reminder: %s", ex)
        await ctx.send("Sorry, I couldn't delete that reminder right now.")


@bot.command(name="settings", aliases=["mysettings", "config"], help="Show your current settings: time zone, delivery channel, and prefix")
async def settings_command(ctx):
    """Display the user's current configuration: time zone, delivery channel/thread, and the active prefix."""
    try:
        user_id = getattr(getattr(ctx, 'author', None), 'id', None)
        prefix_eff = getattr(ctx, 'prefix', DEFAULT_PREFIX)
        # Time zone
        tz_name = USER_TIMEZONES.get(user_id)
        tz_display = tz_name or "Not set"
        # Configured delivery channel
        ch_id = USER_REMINDER_CHANNELS.get(user_id)
        ch_obj = None
        ch_configured_display = "Not set (defaults to this channel/thread or DMs)"
        if ch_id:
            try:
                ch_obj = bot.get_channel(ch_id)
                if ch_obj is None:
                    try:
                        ch_obj = await bot.fetch_channel(ch_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        ch_obj = None
            except Exception:
                ch_obj = None
            if ch_obj is not None:
                try:
                    if isinstance(ch_obj, discord.Thread):
                        ch_configured_display = f"Thread: {ch_obj.name} (<#{ch_obj.id}>)"
                    elif hasattr(ch_obj, 'name'):
                        ch_configured_display = f"Channel: #{ch_obj.name} (<#{ch_obj.id}>)"
                    else:
                        ch_configured_display = f"<#{ch_obj.id}>"
                except Exception:
                    ch_configured_display = f"<#{getattr(ch_obj, 'id', ch_id)}>"
            else:
                ch_configured_display = f"Unknown or inaccessible (<#{ch_id}>)"
        # Effective destination for this conversation (best effort)
        try:
            resolved = await get_reminders_channel(ctx)
            if resolved is not None:
                if isinstance(resolved, discord.Thread):
                    resolved_display = f"Thread: {resolved.name} (<#{resolved.id}>)"
                elif hasattr(resolved, 'name'):
                    resolved_display = f"Channel: #{resolved.name} (<#{resolved.id}>)"
                else:
                    resolved_display = "Direct Message"
            else:
                resolved_display = "Unknown"
        except Exception:
            resolved_display = "Unknown"
        # Prefix information
        if ctx.guild is not None:
            server_prefix = GUILD_PREFIXES.get(ctx.guild.id, DEFAULT_PREFIX)
            prefix_text = f"Effective: `{prefix_eff}`\nServer prefix: `{server_prefix}`"
        else:
            prefix_text = f"Effective (DMs): `{prefix_eff}`"
        # Build embed
        try:
            embed = discord.Embed(title="Your Settings", color=discord.Color(0x3B8D6F))
            embed.add_field(name="Time zone", value=tz_display, inline=False)
            embed.add_field(name="Configured delivery", value=ch_configured_display, inline=False)
            embed.add_field(name="Where this convo will deliver", value=resolved_display, inline=False)
            embed.add_field(name="Prefix", value=prefix_text, inline=False)
            embed.set_footer(text="Change with: settimezone, setremindchannel, setprefix • View caps with: limits")
            return await ctx.send(embed=embed)
        except Exception:
            pass
        # Plain text fallback
        lines = [
            "Your current settings:",
            f"- Time zone: {tz_display}",
            f"- Configured delivery: {ch_configured_display}",
            f"- Where this convo will deliver: {resolved_display}",
            f"- Prefix: {prefix_text}",
        ]
        await ctx.send("\n".join(lines))
    except Exception as e:
        logging.exception("Failed to show settings: %s", e)
        await ctx.send("Sorry, I couldn't retrieve your settings right now.")

@bot.command(name="help", help="Show information about all commands")
async def help_command(ctx):
    """
    Display help for all available commands with usage examples and notes.
    """
    prefix = getattr(ctx, 'prefix', DEFAULT_PREFIX)
    try:
        embed = discord.Embed(
            title="Arachne Help",
            description="Arachne is currently in a testing phase. Please review each command carefully. For additional support, contact PerfectSaturday.",
            color=discord.Color(0x3B8D6F)
        )
        embed.add_field(
            name="One‑time reminders",
            value=(
                f"• `{prefix}remindme <time|HH:MM> <message>`\n"
                "  Set a one‑time reminder after a delay, or at a specific time today/tomorrow.\n"
                "  Supports `s`/`m`/`h`/`d` (e.g., `30s`, `10m`, `2h`, `1d`).\n"
                f"  Examples: `{prefix}remindme 30s stretch`, `{prefix}remindme 10m drink water`, `{prefix}remindme 08:15 stand up`\n\n"
                f"• `{prefix}remindon <YYYY-MM-DD> [HH:MM] <message>`\n"
                "  Set a one‑time reminder for a specific calendar date (optionally a time).\n"
                f"  Examples: `{prefix}remindon 2025-12-31 Celebrate!`, `{prefix}remindon 2025-12-31 09:00 Wish happy new year`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Recurring reminders",
            value=(
                f"• `{prefix}remindevery <weekday> [HH:MM] <message>`\n"
                "  Weekly reminder on the given weekday (Mon/Tue/Wed/Thu/Fri/Sat/Sun).\n"
                f"  Examples: `{prefix}remindevery friday drink water`, `{prefix}remindevery fri 09:00 drink water`\n\n"
                f"• `{prefix}remindeveryday [HH:MM] <message>`\n"
                "  Daily reminder. If you specify `HH:MM`, your time zone is used."
            ),
            inline=False,
        )
        embed.add_field(
            name="Managing reminders",
            value=(
                f"• `{prefix}reminders` — List your reminders\n"
                f"• `{prefix}deletereminder <number>` — Delete a reminder by its list number\n"
                f"• `{prefix}editreminder <number> <new message>` — Edit a reminder's message"
            ),
            inline=False,
        )
        embed.add_field(
            name="Settings",
            value=(
                f"• `{prefix}settimezone <IANA_tz>` — Set your time zone (e.g., `America/New_York`)\n"
                f"• `{prefix}setremindchannel [here|#channel|channel_id|thread_id]` — Choose where reminders are sent\n"
                f"• `{prefix}setprefix <new_prefix>` — Change the bot's command prefix for this server\n"
                f"• `{prefix}settings` — Show your current settings (time zone, delivery channel, and current prefix)"
            ),
            inline=False,
        )
        embed.set_footer(text=(
            f"Notes: Reminders are saved and survive restarts. Fixed-time reminders use your time zone (set with {prefix}settimezone). "
            "Across DST changes, times may shift slightly. Messages are delivered with your mention on a new line."
        ))
        # Add Limits and QoL sections
        embed.add_field(
            name="Reminder Adjustments",
            value=(
                f"• `{prefix}snooze <number> <duration>` — Snooze a reminder by a duration (e.g., 10m, 2h).\n"
                f"• `{prefix}pausereminder <number>` / `{prefix}resumereminder <number>` — Pause/resume a reminder.\n"
                f"• `{prefix}skipnext <number>` — Skip the next occurrence for a recurring reminder."
            ),
            inline=False,
        )
        embed.add_field(
            name="Limits",
            value=(
                f"Default caps: per-user active {DEFAULT_LIMITS['per_user_active']}, per-user per-server {DEFAULT_LIMITS['per_user_per_guild']}, "
                f"per-server total {DEFAULT_LIMITS['per_guild_total']}, message length {DEFAULT_LIMITS['message_len']} chars, "
                f"one-time horizon {DEFAULT_LIMITS['max_one_time_horizon_days']} days.\n"
                f"Use `{prefix}limits` to view your usage."
            ),
            inline=False,
        )
        await ctx.send(embed=embed)
    except Exception:
        # Fallback to plain text if embeds fail for any reason
        help_text = (
            "Here are the available commands:\n\n"
            f"{prefix}remindme <time|HH:MM> <message>\n"
            "  - Set a one-time reminder after a delay, or at a specific time today/tomorrow in your time zone.\n"
            "  - Duration supports: s (seconds), m (minutes), h (hours), d (days).\n"
            f"  - Examples: {prefix}remindme 30s stretch | {prefix}remindme 10m drink water | {prefix}remindme 08:15 stand up\n\n"
            f"{prefix}remindon <YYYY-MM-DD> [HH:MM] <message>\n"
            "  - Set a one-time reminder for a specific calendar date (optionally time). Uses your time zone if set.\n"
            f"  - Examples: {prefix}remindon 2025-12-31 Celebrate! | {prefix}remindon 2025-12-31 09:00 Wish happy new year\n\n"
            f"{prefix}remindevery <weekday> [HH:MM] <message>\n"
            "  - Set a weekly recurring reminder on a given weekday (Mon/Tue/Wed/Thu/Fri/Sat/Sun).\n"
            f"  - Examples: {prefix}remindevery friday drink water | {prefix}remindevery fri 09:00 drink water\n\n"
            f"{prefix}remindeveryday [HH:MM] <message>\n"
            "  - Set a daily recurring reminder. Optional HH:MM uses your set time zone.\n\n"
            f"{prefix}settimezone <IANA_tz>\n"
            "  - Set your time zone (e.g., America/New_York). Required for specific times.\n\n"
            f"{prefix}setremindchannel [here|#channel|channel_id|thread_id]\n"
            "  - Set the channel or thread where your reminders will be sent. Defaults to the current channel/thread.\n\n"
            f"{prefix}setprefix <new_prefix>\n"
            "  - Change the bot's command prefix for this server.\n\n"
            f"{prefix}settings\n"
            "  - Show your current settings (time zone, delivery channel, and current prefix).\n\n"
            f"{prefix}reminders\n"
            "  - List your current reminders and their next run time.\n\n"
            f"{prefix}deletereminder <number>\n"
            f"  - Delete one of your reminders by its number as shown in {prefix}reminders.\n\n"
            f"{prefix}editreminder <number> <new message>\n"
            f"  - Edit one of your reminders by its number as shown in {prefix}reminders.\n\n"
            f"{prefix}snooze <number> <duration>\n"
            "  - Snooze a reminder by a short duration (e.g., 10m, 2h).\n\n"
            f"{prefix}pausereminder <number> / {prefix}resumereminder <number>\n"
            "  - Pause or resume a reminder.\n\n"
            f"{prefix}skipnext <number>\n"
            "  - Skip the next occurrence for a recurring reminder.\n\n"
            f"{prefix}limits\n"
            "  - Show default caps and your current usage.\n\n"
            "Notes:\n"
            "- By default, reminders are sent to the channel or thread where you set them up.\n"
            f"- You can change the destination with {prefix}setremindchannel [here|#channel|channel_id|thread_id].\n"
            "- Reminders are saved to a database and survive bot restarts.\n"
            "- Times are approximate; across DST changes there may be slight shifts.\n"
            "- Reminder messages are delivered as your custom text with your mention on a new line.\n"
            f"- Fixed-time reminders use your time zone; set it via {prefix}settimezone."
        )
        await ctx.send(help_text)

@bot.command(name="editreminder", aliases=["editrem", "edit"], help="Edit one of your reminders by its number from !reminders: !editreminder <number> <new message>")
async def edit_reminder(ctx, index: int = None, *, new_message: str = None):
    """Edit the invoking user's reminder message by its 1-based index as shown in !reminders."""
    try:
        # Rate limit
        if not _rate_limit_check(ctx.author.id):
            return await ctx.send("You're doing that too often. Please wait a bit before modifying reminders (10 actions/min).")
        if index is None or new_message is None or not str(new_message).strip():
            return await ctx.send("Usage: !editreminder <number> <new message> (use !reminders to see the list)")
        if index <= 0:
            return await ctx.send("Please provide a positive number (1, 2, 3, ...).")
        # Same ordering as list_reminders
        items = [e for e in CURRENT_REMINDERS if e.get('user_id') == ctx.author.id]
        if not items:
            return await ctx.send(f"{ctx.author.mention} you have no reminders to edit.")
        items.sort(key=lambda e: e.get('next_run') or datetime.max)
        if index > len(items):
            return await ctx.send(f"Invalid number. You currently have {len(items)} reminder(s).")
        old = items[index - 1]
        # Limits check for new message length only (no increase in counts)
        err = _enforce_limits_precreate(ctx, str(new_message).strip(), None, old.get('type','once'))
        if err and not err.startswith("Limit reached"):
            # Ignore per-user count as we are replacing; enforce message/horizon constraints only
            return await ctx.send(err)
        # Prepare new entry: copy schedule; change message
        new_entry = {
            'user_id': old.get('user_id'),
            'type': old.get('type', 'once'),
            'message': str(new_message).strip(),
            'next_run': old.get('next_run'),
            'created_at': datetime.now(),
            'tz': old.get('tz'),
            'fixed_time': old.get('fixed_time'),
            'hour': old.get('hour'),
            'minute': old.get('minute'),
            'weekday': old.get('weekday'),
            'paused': old.get('paused', False),
        }
        # Cancel old entry (memory + DB)
        try:
            old['cancelled'] = True
            try:
                CURRENT_REMINDERS.remove(old)
            except ValueError:
                pass
            try:
                await cancel_reminder(old)
            except Exception as e:
                logging.exception("Failed to cancel old reminder during edit: %s", e)
        except Exception:
            pass
        # Add and persist new entry
        CURRENT_REMINDERS.append(new_entry)
        try:
            rid = await insert_reminder(new_entry)
            if rid and rid > 0:
                new_entry['id'] = rid
        except Exception as e:
            logging.exception("Failed to persist edited reminder: %s", e)
        # Schedule the new entry using no-ctx schedulers so it keeps running independent of this context
        try:
            rtype = new_entry.get('type')
            if rtype == 'once':
                asyncio.create_task(_schedule_once_noctx(new_entry))
            elif rtype == 'daily':
                asyncio.create_task(_schedule_daily_noctx(new_entry))
            elif rtype == 'weekly':
                asyncio.create_task(_schedule_weekly_noctx(new_entry))
        except Exception as e:
            logging.exception("Failed to schedule edited reminder: %s", e)
        # Confirmation
        when = new_entry.get('next_run')
        when_text = _humanize_time_until(when) if isinstance(when, datetime) else 'unknown time'
        await ctx.send(f"Updated reminder #{index} [{new_entry.get('type','once')}] — now: {when_text} — {new_entry.get('message','')}")
    except Exception as ex:
        logging.exception("Failed to edit reminder: %s", ex)
        await ctx.send("Sorry, I couldn't edit that reminder right now.")


@bot.command(name="remindon", help="Set a one-time reminder for a specific date: !remindon <YYYY-MM-DD> [HH:MM] <message>")
async def remind_on(ctx, *, args: str = None):
    """Schedule a one-time reminder for a specific calendar date, optionally with a time.
    Usage:
    - !remindon 2025-12-31 Celebrate!
    - !remindon 2025-12-31 09:00 Wish happy new year
    Also accepts DD/MM/YYYY.
    Default time when omitted: 09:00.
    """
    try:
        if args is None or not args.strip():
            return await ctx.send("Usage: !remindon <YYYY-MM-DD> [HH:MM] <message>")
        # Rate limit
        if not _rate_limit_check(ctx.author.id):
            return await ctx.send("You're doing that too often. Please wait a bit before creating more reminders (10 actions/min).")
        text = args.strip()
        # Parse date at the beginning
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})\s+(.*)$", text)
        day_first = False
        if not m:
            m2 = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(.*)$", text)
            if m2:
                day_first = True
                m = m2
        if not m:
            return await ctx.send("Please provide the date as YYYY-MM-DD (e.g., 2025-12-31) or DD/MM/YYYY.")
        if day_first:
            d = int(m.group(1)); mo = int(m.group(2)); y = int(m.group(3)); rest = m.group(4)
        else:
            y = int(m.group(1)); mo = int(m.group(2)); d = int(m.group(3)); rest = m.group(4)
        # Parse optional HH:MM at the start of the remainder
        hour, minute = 9, 0
        mtime = re.match(r"^(\d{1,2}:\d{2})\s+(.*)$", rest)
        actual_message = rest
        if mtime:
            parsed = _parse_hhmm(mtime.group(1))
            if not parsed:
                return await ctx.send("Invalid time format. Use 24h HH:MM, e.g., 07:45 or 19:05.")
            hour, minute = parsed
            actual_message = mtime.group(2)
        if not actual_message or not actual_message.strip():
            return await ctx.send("Please include a message after the date/time.")
        # Compute the target datetime in server local
        tz_name = USER_TIMEZONES.get(ctx.author.id)
        try:
            due = _compute_specific_date_run(y, mo, d, hour, minute, tz_name)
        except ValueError:
            return await ctx.send("That date doesn't look valid. Please check the day/month/year.")
        # Validate future
        if due <= datetime.now():
            return await ctx.send("That date/time is in the past. Please provide a future date/time.")
        # Limits
        err = _enforce_limits_precreate(ctx, actual_message.strip(), due, 'once')
        if err:
            return await ctx.send(err)
        # Register entry
        entry = {
            'user_id': ctx.author.id,
            'type': 'once',
            'message': actual_message.strip(),
            'next_run': due,
            'created_at': datetime.now(),
            'tz': tz_name,
            'fixed_time': True,
            'hour': hour,
            'minute': minute,
            'paused': False,
        }
        CURRENT_REMINDERS.append(entry)
        _bump_guild_counters_on_create(ctx)
        try:
            rid = await insert_reminder(entry)
            if rid and rid > 0:
                entry['id'] = rid
        except Exception as e:
            logging.exception("Failed to persist date reminder: %s", e)
        # Confirmation text
        tz_note = f" ({tz_name})" if tz_name else ""
        await ctx.send(f"{ctx.author.mention} I will remind you on {y:04d}-{mo:02d}-{d:02d} at {hour:02d}:{minute:02d}{tz_note}: {actual_message.strip()}")

        async def deliver():
            try:
                delay = max(0, (due - datetime.now()).total_seconds())
                await asyncio.sleep(delay)
                # If paused, wait
                while entry.get('paused') and not entry.get('cancelled') and entry in CURRENT_REMINDERS:
                    await asyncio.sleep(30)
                if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                    return
                await asyncio.sleep(random.uniform(0, 2))
                target_channel = await get_reminders_channel(ctx)
                if _should_deliver(entry):
                    await target_channel.send(f"{actual_message.strip()}\n{ctx.author.mention}")
            except asyncio.CancelledError:
                raise
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                logging.exception("Error delivering date reminder: %s", e)
            finally:
                try:
                    if entry in CURRENT_REMINDERS:
                        CURRENT_REMINDERS.remove(entry)
                    await cancel_reminder(entry)
                    _dec_guild_counters_on_delete(ctx)
                except Exception:
                    pass
        asyncio.create_task(deliver())
    except Exception as ex:
        logging.exception("Failed to set date reminder: %s", ex)
        await ctx.send("Sorry, I couldn't set that reminder right now.")

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Please configure it in your environment or .env file as DISCORD_TOKEN.")

# --- Additional helper and commands: limits, snooze, pause/resume, skipnext, diagnostics ---

def _get_user_reminders_sorted(user_id: int):
    items = [e for e in CURRENT_REMINDERS if e.get('user_id') == user_id and not e.get('cancelled')]
    items.sort(key=lambda e: e.get('next_run') or datetime.max)
    return items


@bot.command(name="limits", help="Show your current reminder limits and usage")
async def limits_command(ctx):
    try:
        user_count = _count_user_active(ctx.author.id)
        if getattr(ctx, 'guild', None) is not None:
            gid = ctx.guild.id
            per_user_guild = GUILD_USER_ACTIVE_COUNTS[gid][ctx.author.id]
            guild_total = GUILD_ACTIVE_COUNTS[gid]
        else:
            per_user_guild = user_count
            guild_total = user_count
        # Build embed
        try:
            embed = discord.Embed(title="Your Limits & Usage", color=discord.Color(0x3B8D6F))
            embed.add_field(name="Per-user active", value=f"{user_count} / {DEFAULT_LIMITS['per_user_active']}", inline=False)
            if getattr(ctx, 'guild', None) is not None:
                embed.add_field(name="Per-user in this server", value=f"{per_user_guild} / {DEFAULT_LIMITS['per_user_per_guild']}", inline=False)
                embed.add_field(name="Server total (in-memory)", value=f"{guild_total} / {DEFAULT_LIMITS['per_guild_total']}", inline=False)
            embed.add_field(name="Message length", value=f"<= {DEFAULT_LIMITS['message_len']} characters", inline=False)
            embed.add_field(name="One-time horizon", value=f"<= {DEFAULT_LIMITS['max_one_time_horizon_days']} days", inline=False)
            embed.set_footer(text="These limits help prevent abuse. Contact the server admin for potential adjustments.")
            return await ctx.send(embed=embed)
        except Exception:
            pass
        # Plain text fallback
        lines = [
            f"Per-user active: {user_count} / {DEFAULT_LIMITS['per_user_active']}",
        ]
        if getattr(ctx, 'guild', None) is not None:
            lines.append(f"Per-user in this server: {per_user_guild} / {DEFAULT_LIMITS['per_user_per_guild']}")
            lines.append(f"Server total (in-memory): {guild_total} / {DEFAULT_LIMITS['per_guild_total']}")
        lines.append(f"Message length: <= {DEFAULT_LIMITS['message_len']} characters")
        lines.append(f"One-time horizon: <= {DEFAULT_LIMITS['max_one_time_horizon_days']} days")
        await ctx.send("Your Limits & Usage:\n" + "\n".join(lines))
    except Exception as e:
        logging.exception("Failed to show limits: %s", e)
        await ctx.send("Sorry, I couldn't retrieve limits right now.")


@bot.command(name="snooze", help="Snooze a reminder by a short duration: snooze <number> <duration>")
async def snooze_command(ctx, index: int = None, duration: str = None):
    try:
        if not _rate_limit_check(ctx.author.id):
            return await ctx.send("You're doing that too often. Please wait a bit before modifying reminders (10 actions/min).")
        if index is None or duration is None:
            return await ctx.send("Usage: !snooze <number> <duration> (e.g., !snooze 1 10m)")
        if index <= 0:
            return await ctx.send("Please provide a positive number (1, 2, 3, ...).")
        seconds, pretty = parse_duration(duration)
        if seconds is None:
            return await ctx.send(pretty)
        items = _get_user_reminders_sorted(ctx.author.id)
        if not items:
            return await ctx.send("You have no reminders to snooze.")
        if index > len(items):
            return await ctx.send(f"Invalid number. You currently have {len(items)} reminder(s).")
        entry = items[index - 1]
        # Adjust next_run
        base = entry.get('next_run') or datetime.now()
        new_due = base + timedelta(seconds=seconds)
        # Clamp to min delay from now
        if (new_due - datetime.now()).total_seconds() < MIN_DELAY:
            new_due = datetime.now() + timedelta(seconds=MIN_DELAY)
        entry['next_run'] = new_due
        try:
            await update_reminder_next_run(entry)
        except AioSqliteError:
            pass
        await ctx.send(f"Snoozed reminder #{index} by {pretty}. New time: {_humanize_time_until(new_due)}")
    except Exception as e:
        logging.exception("Failed to snooze reminder: %s", e)
        await ctx.send("Sorry, I couldn't snooze that reminder right now.")


@bot.command(name="pausereminder", aliases=["pause"], help="Pause a reminder: pausereminder <number>")
async def pause_reminder_cmd(ctx, index: int = None):
    try:
        if not _rate_limit_check(ctx.author.id):
            return await ctx.send("You're doing that too often. Please wait a bit before modifying reminders (10 actions/min).")
        if index is None:
            return await ctx.send("Usage: !pausereminder <number>")
        if index <= 0:
            return await ctx.send("Please provide a positive number (1, 2, 3, ...).")
        items = _get_user_reminders_sorted(ctx.author.id)
        if not items:
            return await ctx.send("You have no reminders to pause.")
        if index > len(items):
            return await ctx.send(f"Invalid number. You currently have {len(items)} reminder(s).")
        entry = items[index - 1]
        entry['paused'] = True
        try:
            await update_reminder_paused(entry, True)
        except AioSqliteError:
            pass
        await ctx.send(f"Paused reminder #{index}.")
    except Exception as e:
        logging.exception("Failed to pause reminder: %s", e)
        await ctx.send("Sorry, I couldn't pause that reminder right now.")


@bot.command(name="resumereminder", aliases=["resume"], help="Resume a paused reminder: resumereminder <number>")
async def resume_reminder_cmd(ctx, index: int = None):
    try:
        if not _rate_limit_check(ctx.author.id):
            return await ctx.send("You're doing that too often. Please wait a bit before modifying reminders (10 actions/min).")
        if index is None:
            return await ctx.send("Usage: !resumereminder <number>")
        if index <= 0:
            return await ctx.send("Please provide a positive number (1, 2, 3, ...).")
        items = _get_user_reminders_sorted(ctx.author.id)
        if not items:
            return await ctx.send("You have no reminders to resume.")
        if index > len(items):
            return await ctx.send(f"Invalid number. You currently have {len(items)} reminder(s).")
        entry = items[index - 1]
        entry['paused'] = False
        try:
            await update_reminder_paused(entry, False)
        except AioSqliteError:
            pass
        await ctx.send(f"Resumed reminder #{index}.")
    except Exception as e:
        logging.exception("Failed to resume reminder: %s", e)
        await ctx.send("Sorry, I couldn't resume that reminder right now.")


@bot.command(name="skipnext", help="Skip the next occurrence for a recurring reminder: skipnext <number>")
async def skipnext_command(ctx, index: int = None):
    try:
        if not _rate_limit_check(ctx.author.id):
            return await ctx.send("You're doing that too often. Please wait a bit before modifying reminders (10 actions/min).")
        if index is None:
            return await ctx.send("Usage: !skipnext <number>")
        if index <= 0:
            return await ctx.send("Please provide a positive number (1, 2, 3, ...).")
        items = _get_user_reminders_sorted(ctx.author.id)
        if not items:
            return await ctx.send("You have no reminders to modify.")
        if index > len(items):
            return await ctx.send(f"Invalid number. You currently have {len(items)} reminder(s).")
        entry = items[index - 1]
        rtype = entry.get('type')
        if rtype not in ('daily', 'weekly'):
            return await ctx.send("Skip-next only applies to recurring reminders (daily/weekly).")
        if rtype == 'daily':
            entry['next_run'] = (entry.get('next_run') or datetime.now()) + timedelta(days=1)
        else:
            entry['next_run'] = (entry.get('next_run') or datetime.now()) + timedelta(days=7)
        try:
            await update_reminder_next_run(entry)
        except AioSqliteError:
            pass
        when_text = _humanize_time_until(entry['next_run']) if isinstance(entry['next_run'], datetime) else 'unknown time'
        await ctx.send(f"Skipped next occurrence. New next time: {when_text}")
    except Exception as e:
        logging.exception("Failed to skip next: %s", e)
        await ctx.send("Sorry, I couldn't skip the next occurrence right now.")


@bot.command(name="diagnostics", help="Owner-only diagnostics of DB schema and limits")
async def diagnostics_command(ctx):
    try:
        try:
            is_owner = await bot.is_owner(ctx.author)
        except Exception:
            is_owner = False
        if not is_owner:
            return await ctx.send("This command is owner-only.")
        has_paused = has_tags = False
        if aiosqlite:
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    cur = await db.execute("PRAGMA table_info(reminders)")
                    cols = [row[1] for row in await cur.fetchall()]
                    has_paused = 'paused' in cols
                    has_tags = 'tags' in cols
            except Exception as e:
                logging.exception("Diagnostics schema check failed: %s", e)
        user_count = _count_user_active(ctx.author.id)
        msg = (
            f"Schema: paused={'yes' if has_paused else 'no'}, tags={'yes' if has_tags else 'no'}\n"
            f"Limits: per_user_active={DEFAULT_LIMITS['per_user_active']}, per_user_per_guild={DEFAULT_LIMITS['per_user_per_guild']}, per_guild_total={DEFAULT_LIMITS['per_guild_total']}, msg_len={DEFAULT_LIMITS['message_len']}, horizon_days={DEFAULT_LIMITS['max_one_time_horizon_days']}\n"
            f"Your active reminders: {user_count}"
        )
        await ctx.send(msg)
    except Exception as e:
        logging.exception("Diagnostics failed: %s", e)
        await ctx.send("Diagnostics failed.")

bot.run(TOKEN, log_handler=handler, log_level=LOG_LEVEL)