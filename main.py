import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
import re
from datetime import datetime, timedelta

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
    print(f"We are ready to go, {bot.user.name}")

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


@bot.command(name="remindme", help="Set a reminder: !remindme <time> <message>. Example: !remindme 10m stretch")
async def remindme(ctx, when: str = None, *, message: str = None):
    """
    Schedule a reminder that pings the user after the given duration with the provided message.
    Usage: !remindme 10m drink water
    """
    if when is None or message is None:
        return await ctx.send("Usage: !remindme <time> <message> (e.g., !remindme 15m check the oven)")

    seconds, result = parse_duration(when)
    if seconds is None:
        return await ctx.send(result)

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


@bot.command(name="remindevery", help="Set a recurring weekly reminder: !remindevery <weekday> <message>. Example: !remindevery friday drink water")
async def remindevery(ctx, weekday: str = None, *, message: str = None):
    """
    Schedule a recurring weekly reminder on the specified weekday, at around the time this command is used.
    Usage: !remindevery friday drink water
    """
    if weekday is None or message is None:
        return await ctx.send("Usage: !remindevery <weekday> <message> (e.g., !remindevery friday drink water)")

    target_idx = parse_weekday(weekday)
    if target_idx is None:
        return await ctx.send("Invalid weekday. Use names like Monday, Tue, Wednesday, Thu, Friday, Sat, Sun.")

    now = datetime.now()
    next_run = compute_next_weekday_run(now, target_idx)
    when_str = f"{next_run.strftime('%A')} at around {next_run.strftime('%H:%M')} (starting {next_run.strftime('%Y-%m-%d')})"

    await ctx.send(f"{ctx.author.mention} I will remind you every {when_str}. Note: reminders are in-memory and stop if the bot restarts. Message: {message}")

    async def loop():
        try:
            target_channel = await get_reminders_channel(ctx)
            initial_delay = max(0, (next_run - datetime.now()).total_seconds())
            await asyncio.sleep(initial_delay)
            while True:
                try:
                    await target_channel.send(f"{message}\n{ctx.author.mention}")
                except Exception as e:
                    logging.exception("Error delivering recurring reminder: %s", e)
                # Sleep approximately one week. For simplicity we ignore DST shifts.
                await asyncio.sleep(7 * 24 * 3600)
        except Exception as e:
            logging.exception("Recurring reminder loop ended: %s", e)

    asyncio.create_task(loop())


@bot.command(name="remindeveryday", help="Set a recurring daily reminder: !remindeveryday <message>. Sends a reminder every day at around the time you run the command.")
async def remindeveryday(ctx, *, message: str = None):
    """
    Schedule a recurring daily reminder at around the time this command is used.
    Usage: !remindeveryday drink water
    """
    if message is None:
        return await ctx.send("Usage: !remindeveryday <message> (e.g., !remindeveryday drink water)")

    now = datetime.now()
    next_run = now + timedelta(days=1)
    when_str = f"every day at around {next_run.strftime('%H:%M')} (starting {next_run.strftime('%Y-%m-%d')})"

    await ctx.send(f"{ctx.author.mention} I will remind you {when_str}. Note: reminders are in-memory and stop if the bot restarts. Message: {message}")

    async def loop():
        try:
            target_channel = await get_reminders_channel(ctx)
            initial_delay = max(0, (next_run - datetime.now()).total_seconds())
            await asyncio.sleep(initial_delay)
            while True:
                try:
                    await target_channel.send(f"{message}\n{ctx.author.mention}")
                except Exception as e:
                    logging.exception("Error delivering daily recurring reminder: %s", e)
                # Sleep approximately one day. For simplicity we ignore DST shifts.
                await asyncio.sleep(24 * 3600)
        except Exception as e:
            logging.exception("Daily recurring reminder loop ended: %s", e)

    asyncio.create_task(loop())


@bot.command(name="help", help="Show information about all commands")
async def help_command(ctx):
    """
    Display help for all available commands with usage examples and notes.
    """
    prefix = '!'
    help_text = (
        "Here are the available commands:\n\n"
        f"{prefix}remindme <time> <message>\n"
        "  - Set a one-time reminder after a delay.\n"
        "  - Time supports: s (seconds), m (minutes), h (hours), d (days).\n"
        "  - Examples: !remindme 30s stretch | !remindme 10m drink water | !remindme 2h check oven\n\n"
        f"{prefix}remindevery <weekday> <message>\n"
        "  - Set a weekly recurring reminder on a given weekday (Mon/Tue/Wed/Thu/Fri/Sat/Sun).\n"
        "  - Example: !remindevery friday drink water\n\n"
        f"{prefix}remindeveryday <message>\n"
        "  - Set a daily recurring reminder at about the same time you run the command.\n"
        "  - Example: !remindeveryday drink water\n\n"
        "Notes:\n"
        "- Reminders are posted in the 🗓️reminders channel (or #reminders) if available;\n"
        "  otherwise they are posted in the command's channel.\n"
        "- Reminders are in-memory only and will be lost if the bot restarts.\n"
        "- Times are approximate; across DST changes there may be slight shifts.\n"
        "- Reminder messages are delivered as your custom text with your mention on a new line."
    )
    await ctx.send(help_text)

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
