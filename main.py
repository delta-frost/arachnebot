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

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')

@bot.event
async def on_ready():
    print(f"{bot.user.name} is ready to go")

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


async def get_reminders_channel(ctx):
    """
    Resolve the target channel to post reminders.
    Priority:
    1) A text channel named "🗓️reminders" in the same guild.
    2) Fallback to a channel named "reminders" if the emoji name doesn't exist.
    3) Fallback to the invoking channel if nothing suitable is found or in DMs.
    """
    try:
        # If invoked in a guild, try to find the designated reminders channel.
        if ctx.guild is not None:
            me = ctx.guild.me or ctx.me
            for name in REMINDERS_CHANNEL_NAMES:
                ch = discord.utils.get(ctx.guild.text_channels, name=name)
                if ch and ch.permissions_for(me).send_messages:
                    return ch
            logging.warning("Reminders channel not found or no permission in guild '%s'. Falling back to invoking channel.", getattr(ctx.guild, 'name', 'unknown'))
            return ctx.channel
        # In DMs, there is no guild — send back to the DM channel.
        return ctx.channel
    except Exception as e:
        logging.exception("Failed to resolve reminders channel: %s", e)
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
        pretty_when = f"at {hour:02d}:{minute:02d} ({tz_name}) on {due.strftime('%Y-%m-%d')}"
        await ctx.send(f"{ctx.author.mention} I will remind you {pretty_when}: {message}")

        async def deliver():
            try:
                delay = max(0, (due - datetime.now()).total_seconds())
                await asyncio.sleep(delay)
                target_channel = await get_reminders_channel(ctx)
                await target_channel.send(f"{message}\n{ctx.author.mention}")
            except Exception as e:
                logging.exception("Error delivering reminder: %s", e)
            finally:
                try:
                    if entry in CURRENT_REMINDERS:
                        CURRENT_REMINDERS.remove(entry)
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

    confirmation = f"{ctx.author.mention} I will remind you in {result}: {message}"
    await ctx.send(confirmation)

    async def deliver():
        try:
            await asyncio.sleep(seconds)
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

    await ctx.send(f"{ctx.author.mention} I will remind you every {when_str}. Note: reminders are in-memory and stop if the bot restarts. Message: {actual_message}")

    async def loop():
        try:
            target_channel = await get_reminders_channel(ctx)
            initial_delay = max(0, (next_run - datetime.now()).total_seconds())
            await asyncio.sleep(initial_delay)
            while True:
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
                except Exception:
                    next_sleep = 7 * 24 * 3600
                await asyncio.sleep(next_sleep)
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
    await ctx.send(f"{ctx.author.mention} Your time zone has been set to {tz}.")


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

    await ctx.send(f"{ctx.author.mention} I will remind you {when_str}. Note: reminders are in-memory and stop if the bot restarts. Message: {actual_message}")

    async def loop():
        try:
            target_channel = await get_reminders_channel(ctx)
            initial_delay = max(0, (next_run - datetime.now()).total_seconds())
            await asyncio.sleep(initial_delay)
            while True:
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
                    next_sleep = max(0, (entry['next_run'] - datetime.now()).total_seconds())
                except Exception:
                    next_sleep = 24 * 3600
                await asyncio.sleep(next_sleep)
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
        await ctx.send(text)
    except Exception as ex:
        logging.exception("Failed to list reminders: %s", ex)
        await ctx.send("Sorry, I couldn't retrieve your reminders right now.")


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
        f"{prefix}reminders\n"
        "  - List your current reminders and their next run time.\n\n"
        "Notes:\n"
        "- Reminders are posted in the 🗓️reminders channel (or #reminders) if available;\n"
        "  otherwise they are posted in the command's channel.\n"
        "- Reminders are in-memory only and will be lost if the bot restarts.\n"
        "- Times are approximate; across DST changes there may be slight shifts.\n"
        "- Reminder messages are delivered as your custom text with your mention on a new line.\n"
        "- Fixed-time reminders use your time zone; set it via !settimezone."
    )
    await ctx.send(help_text)

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
