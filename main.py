import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
import re
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # Fallback: specific-time daily reminders will be unavailable without zoneinfo

# Optional async SQLite for persistence
try:
    import aiosqlite
except Exception:
    aiosqlite = None

load_dotenv()
DB_PATH = os.getenv('BOT_DB_PATH', 'botdata.sqlite3')
TOKEN = os.getenv('DISCORD_TOKEN')

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)
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
                cancelled INTEGER DEFAULT 0
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
            INSERT INTO reminders (user_id, type, message, tz, fixed_time, hour, minute, weekday, next_run, created_at, cancelled)
            VALUES (?,?,?,?,?,?,?,?,?,?,0)
            """,
            (
                entry.get('user_id'), entry.get('type'), entry.get('message'), entry.get('tz'),
                1 if entry.get('fixed_time') else 0, entry.get('hour'), entry.get('minute'), entry.get('weekday'),
                entry.get('next_run').isoformat() if entry.get('next_run') else None,
                entry.get('created_at').isoformat() if entry.get('created_at') else None,
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
        async with db.execute("SELECT id, user_id, type, message, tz, fixed_time, hour, minute, weekday, next_run, created_at FROM reminders WHERE cancelled = 0") as cur:
            async for rid, user_id, rtype, message, tz, fixed_time, hour, minute, weekday, next_run, created_at in cur:
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
                    }
                    out.append(entry)
                except Exception as e:
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
                except Exception:
                    ch = None
            if ch is not None:
                return ch
        # Fallback DM
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if user:
            dm = user.dm_channel or await user.create_dm(
            return dm
    except Exception as e:
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
        if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
            return
        ch = await get_user_delivery_channel(entry.get('user_id'))
        if ch:
            await ch.send(f"{entry.get('message','')}\n<@{entry.get('user_id')}>")
    except Exception as e:
        logging.exception("Error delivering loaded one-time reminder: %s", e)
    finally:
        try:
            if entry in CURRENT_REMINDERS:
                CURRENT_REMINDERS.remove(entry)
            await cancel_reminder(entry)
        except Exception:
            pass

async def _schedule_daily_noctx(entry: dict):
    try:
        while True:
            next_run = entry.get('next_run') or (datetime.now() + timedelta(days=1))
            delay = max(0, (next_run - datetime.now()).total_seconds())
            await asyncio.sleep(delay)
            if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                break
            ch = await get_user_delivery_channel(entry.get('user_id'))
            if ch:
                try:
                    await ch.send(f"{entry.get('message','')}\n<@{entry.get('user_id')}>")
                except Exception as e:
                    logging.exception("Daily reminder send failed: %s", e)
            # Compute next
            try:
                if entry.get('fixed_time') and entry.get('tz'):
                    entry['next_run'] = _compute_next_daily_fixed_run(entry['hour'], entry['minute'], entry['tz'])
                else:
                    entry['next_run'] = datetime.now() + timedelta(days=1)
                await update_reminder_next_run(entry)
            except Exception:
                pass
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
            ch = await get_user_delivery_channel(entry.get('user_id'))
            if ch:
                try:
                    await ch.send(f"{entry.get('message','')}\n<@{entry.get('user_id')}>")
                except Exception as e:
                    logging.exception("Weekly reminder send failed: %s", e)
            try:
                if entry.get('fixed_time') and entry.get('tz') is not None:
                    entry['next_run'] = _compute_next_weekly_fixed_run(entry['hour'], entry['minute'], entry['tz'], entry['weekday'])
                else:
                    entry['next_run'] = datetime.now() + timedelta(days=7)
                await update_reminder_next_run(entry)
            except Exception:
                pass
    except Exception as e:
        logging.exception("Weekly scheduler error: %s", e)

@bot.event
async def on_ready():
    print(f"{bot.user.name} is ready to go")
    try:
        await init_db()
        await load_users_into_memory()
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

# Preferred channel names to deliver reminders
REMINDERS_CHANNEL_NAMES = ["🗓️reminders", "reminders"]

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

# In-memory user timezone settings (lost on restart)
# Maps user_id -> IANA timezone string (e.g., "America/New_York")
USER_TIMEZONES = {}

# In-memory per-user reminder delivery channel/thread (lost on restart)
# Maps user_id -> channel_or_thread_id (int)
USER_REMINDER_CHANNELS = {}


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
                except Exception:
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
                except Exception:
                    pass
                if can_send:
                    return ch
        # Fallback to invoking channel
        return ctx.channel
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

    # Try HH:MM first
    fixed = _parse_hhmm(when)
    if fixed:
        hour, minute = fixed
        tz_name = USER_TIMEZONES.get(ctx.author.id)
        if not tz_name:
            return await ctx.send("Please set your time zone first with !settimezone <IANA_tz> (e.g., America/New_York).")
        due = _compute_next_one_time_fixed_run(hour, minute, tz_name)
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
        }
        CURRENT_REMINDERS.append(entry)
        try:
            rid = await insert_reminder(entry)
            if rid and rid > 0:
                entry['id'] = rid
        except Exception as e:
            logging.exception("Failed to persist one-time fixed reminder: %s", e)
        pretty_when = f"at {hour:02d}:{minute:02d} ({tz_name}) on {due.strftime('%Y-%m-%d')}"
        await ctx.send(f"{ctx.author.mention} I will remind you {pretty_when}: {message}")

        async def deliver():
            try:
                delay = max(0, (due - datetime.now()).total_seconds())
                await asyncio.sleep(delay)
                # Skip if cancelled or removed
                if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                    return
                target_channel = await get_reminders_channel(ctx)
                await target_channel.send(f"{message}\n{ctx.author.mention}")
            except Exception as e:
                logging.exception("Error delivering reminder: %s", e)
            finally:
                try:
                    if entry in CURRENT_REMINDERS:
                        CURRENT_REMINDERS.remove(entry)
                    await cancel_reminder(entry)
                except Exception:
                    pass
        asyncio.create_task(deliver())
        return

    # Fallback to duration-based parsing
    seconds, result = parse_duration(when)
    if seconds is None:
        return await ctx.send(result)

    due = datetime.now() + timedelta(seconds=seconds)
    # Register in-memory
    entry = {
        'user_id': ctx.author.id,
        'type': 'once',
        'message': message,
        'next_run': due,
        'created_at': datetime.now(),
    }
    CURRENT_REMINDERS.append(entry)
    try:
        rid = await insert_reminder(entry)
        if rid and rid > 0:
            entry['id'] = rid
    except Exception as e:
        logging.exception("Failed to persist one-time reminder: %s", e)

    confirmation = f"{ctx.author.mention} I will remind you in {result}: {message}"
    await ctx.send(confirmation)

    async def deliver():
        try:
            await asyncio.sleep(seconds)
            # Skip if cancelled or removed
            if entry.get('cancelled') or entry not in CURRENT_REMINDERS:
                return
            # If the channel still exists and the bot can send messages, deliver it to the reminders channel.
            target_channel = await get_reminders_channel(ctx)
            await target_channel.send(f"{message}\n{ctx.author.mention}")
        except Exception as e:
            # Swallow exceptions to avoid crashing the task; optional: log to file.
            logging.exception("Error delivering reminder: %s", e)
        finally:
            # Remove from registry when done
            try:
                if entry in CURRENT_REMINDERS:
                    CURRENT_REMINDERS.remove(entry)
                await cancel_reminder(entry)
            except Exception:
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
        when_str = f"{datetime.now().strftime('%A')} weekly at {fixed_hour:02d}:{fixed_min:02d} ({tz_name}) starting {next_run.strftime('%Y-%m-%d')}"
    else:
        now = datetime.now()
        next_run = compute_next_weekday_run(now, target_idx)
        when_str = f"{next_run.strftime('%A')} at around {next_run.strftime('%H:%M')} (starting {next_run.strftime('%Y-%m-%d')})"
        tz_name = None

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
    }
    CURRENT_REMINDERS.append(entry)
    try:
        rid = await insert_reminder(entry)
        if rid and rid > 0:
            entry['id'] = rid
    except Exception as e:
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
                try:
                    await target_channel.send(f"{actual_message}\n{ctx.author.mention}")
                except Exception as e:
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
    except Exception:
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
    except Exception:
        tz = None
    now_server = datetime.now()
    if not tz:
        # Approximate using server local time if tz not available
        now = now_server
        days_ahead = (target_weekday - now.weekday()) % 7
        if days_ahead == 0 and (now.hour, now.minute) >= (hour, minute):
            days_ahead = 7
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
        return candidate
    now_user = datetime.now(tz)
    candidate = now_user.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (target_weekday - now_user.weekday()) % 7
    if days_ahead == 0 and candidate <= now_user:
        days_ahead = 7
    elif days_ahead > 0:
        candidate = candidate + timedelta(days=days_ahead)
        # Rebuild time after adding days to avoid day overflow issues
        candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Convert to server-local naive
    return candidate.astimezone().replace(tzinfo=None)


def _compute_next_one_time_fixed_run(hour: int, minute: int, tz_name: str) -> datetime:
    """Next occurrence today or tomorrow at HH:MM in user's tz, returned as server-local naive."""
    try:
        tz = ZoneInfo(tz_name) if ZoneInfo else None
    except Exception:
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


@bot.command(name="settimezone", help="Set your time zone: !settimezone <IANA_tz> (e.g., America/New_York)")
async def settimezone(ctx, tz: str = None):
    if tz is None:
        return await ctx.send("Usage: !settimezone <IANA_tz> (e.g., America/Los_Angeles). Find yours at https://en.wikipedia.org/wiki/List_of_tz_database_time_zones")
    if ZoneInfo is None:
        return await ctx.send("Sorry, this bot requires Python 3.9+ with zoneinfo to set time zones.")
    try:
        _ = ZoneInfo(tz)
    except Exception:
        return await ctx.send("Invalid time zone. Please provide a valid IANA tz (e.g., Europe/London, Asia/Kolkata).")
    USER_TIMEZONES[ctx.author.id] = tz
    try:
        await upsert_user_settings(ctx.author.id, tz=tz)
    except Exception as e:
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
                        except Exception:
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
                        except Exception:
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
        except Exception:
            pass
        if not can_send:
            return await ctx.send("I don't have permission to send messages in that channel/thread. Please choose another.")
        USER_REMINDER_CHANNELS[ctx.author.id] = ch_obj.id
        try:
            await upsert_user_settings(ctx.author.id, channel_id=ch_obj.id)
        except Exception as e:
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
    }
    CURRENT_REMINDERS.append(entry)
    try:
        rid = await insert_reminder(entry)
        if rid and rid > 0:
            entry['id'] = rid
    except Exception as e:
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
                try:
                    await target_channel.send(f"{actual_message}\n{ctx.author.mention}")
                except Exception as e:
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
            lines.append(f"{idx}. [{typ}] {when} — {msg}")
        text = "Your current reminders:\n" + "\n".join(lines)
        text += "\n\nTip: delete with !deletereminder <number>, or edit with !editreminder <number> <new message>."
        await ctx.send(text)
    except Exception as ex:
        logging.exception("Failed to list reminders: %s", ex)
        await ctx.send("Sorry, I couldn't retrieve your reminders right now.")


@bot.command(name="deletereminder", aliases=["delreminder", "rmreminder"], help="Delete one of your reminders by its number from !reminders")
async def delete_reminder(ctx, index: int = None):
    """Delete the invoking user's reminder by its 1-based index as shown in !reminders."""
    try:
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
        except Exception as e:
            logging.exception("Failed to cancel reminder in DB: %s", e)
        msg = entry.get('message', '')
        typ = entry.get('type', 'once')
        await ctx.send(f"Deleted reminder #{index} [{typ}]: {msg}")
    except Exception as ex:
        logging.exception("Failed to delete reminder: %s", ex)
        await ctx.send("Sorry, I couldn't delete that reminder right now.")


@bot.command(name="help", help="Show information about all commands")
async def help_command(ctx):
    """
    Display help for all available commands with usage examples and notes.
    """
    prefix = '!'
    help_text = (
        "Here are the available commands:\n\n"
        f"{prefix}remindme <time|HH:MM> <message>\n"
        "  - Set a one-time reminder after a delay, or at a specific time today/tomorrow in your time zone.\n"
        "  - Duration supports: s (seconds), m (minutes), h (hours), d (days).\n"
        "  - Examples: !remindme 30s stretch | !remindme 10m drink water | !remindme 08:15 stand up\n\n"
        f"{prefix}remindevery <weekday> [HH:MM] <message>\n"
        "  - Set a weekly recurring reminder on a given weekday (Mon/Tue/Wed/Thu/Fri/Sat/Sun).\n"
        "  - Examples: !remindevery friday drink water | !remindevery fri 09:00 drink water\n\n"
        f"{prefix}remindeveryday [HH:MM] <message>\n"
        "  - Set a daily recurring reminder. Optional HH:MM uses your set time zone.\n"
        "  - Examples: !remindeveryday drink water | !remindeveryday 08:30 drink water\n\n"
        f"{prefix}settimezone <IANA_tz>\n"
        "  - Set your time zone (e.g., America/New_York). Required for specific times.\n\n"
        f"{prefix}setremindchannel [here|#channel|channel_id|thread_id]\n"
        "  - Set the channel or thread where your reminders will be sent. Defaults to the current channel/thread.\n\n"
        f"{prefix}reminders\n"
        "  - List your current reminders and their next run time.\n\n"
        f"{prefix}deletereminder <number>\n"
        "  - Delete one of your reminders by its number as shown in !reminders.\n\n"
        f"{prefix}editreminder <number> <new message>\n"
        "  - Edit one of your reminders by its number as shown in !reminders.\n\n"
        "Notes:\n"
        "- By default, reminders are sent to the channel or thread where you set them up.\n"
        "- You can change the destination with !setremindchannel [here|#channel|channel_id|thread_id].\n"
        "- Reminders are saved to a database and survive bot restarts.\n"
        "- Times are approximate; across DST changes there may be slight shifts.\n"
        "- Reminder messages are delivered as your custom text with your mention on a new line.\n"
        "- Fixed-time reminders use your time zone; set it via !settimezone."
    )
    await ctx.send(help_text)

@bot.command(name="editreminder", aliases=["editrem", "edit"], help="Edit one of your reminders by its number from !reminders: !editreminder <number> <new message>")
async def edit_reminder(ctx, index: int = None, *, new_message: str = None):
    """Edit the invoking user's reminder message by its 1-based index as shown in !reminders."""
    try:
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

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
