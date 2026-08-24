import discord
import sqlite3
import os
import shutil
import asyncio
import difflib
import json
import time
import re
from datetime import datetime, timedelta

TOKEN = os.environ["TOKEN"]

ALLOWED_CHANNEL_ID = 1538465015135346768
UNANSWERED_CHANNEL_ID = 1540997587740532746

# Subscription settings
# Create a Discord role with this exact name and give it permission to view
# the private channel(s) you want subscribers to access.
SUBSCRIBER_ROLE_NAME = "Subscriber"
SUBSCRIPTION_CHECK_INTERVAL = 60  # seconds
SUBSCRIPTION_EXPIRY_WARNING_HOURS = 24
SUBSCRIPTION_ADMIN_CHANNEL_ID = 0  # Set to your private admin channel ID; 0 = any admin command channel

COOLDOWN_SECONDS = 2
FUZZY_THRESHOLD = 0.90
FUZZY_MARGIN = 0.08
MIN_WORD_OVERLAP = 0.50

# Persist the SQLite database on the Railway Volume mounted at /data.
# On the first run, preserve an existing local faq.db if one exists.
DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
DATABASE_FILE = os.path.join(DATA_DIR, "faq.db")
LOCAL_DATABASE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faq.db")
if DATABASE_FILE != LOCAL_DATABASE_FILE and not os.path.exists(DATABASE_FILE) and os.path.exists(LOCAL_DATABASE_FILE):
    shutil.copy2(LOCAL_DATABASE_FILE, DATABASE_FILE)

BACKUP_FOLDER = "backups"

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.reactions = True

client = discord.Client(intents=intents)

db = sqlite3.connect(DATABASE_FILE)
db.row_factory = sqlite3.Row
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS faqs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT UNIQUE NOT NULL,
    answer TEXT NOT NULL,
    uses INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    dislikes INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS unanswered (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_warning_sent TEXT
)
""")
# Upgrade an older subscriptions table if this bot had one before.
try:
    cursor.execute("ALTER TABLE subscriptions ADD COLUMN created_at TEXT")
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE subscriptions ADD COLUMN last_warning_sent TEXT")
except sqlite3.OperationalError:
    pass

cursor.execute(
    "UPDATE subscriptions SET created_at = COALESCE(created_at, expires_at) "
    "WHERE created_at IS NULL"
)

db.commit()

DEFAULT_FAQS = {
    "Scroll to the section Everything About Sports Betting!, locate the second and third words.": "Expert tips",
    "Scroll to the section called Changing Regulations, locate the first two words.": "Regulations can",
    "Scroll to the section called Our Mission, locate the first 2 words": "To help",
    "Scroll to the section called \"Our Mission\", locate the third and fourth words from the end of the second sentence.": "professional online"
}

def load_default_faqs():
    for trigger, answer in DEFAULT_FAQS.items():
        cursor.execute("SELECT id FROM faqs WHERE trigger = ?", (trigger,))
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT INTO faqs (trigger, answer, created_at) VALUES (?, ?, ?)",
                (trigger, answer, datetime.now().isoformat())
            )
    db.commit()

load_default_faqs()

# One-time FAQ restoration helper.
# Put faq_backup_20260824_022617.json beside bot.py before deployment.
# This restores any FAQ entries from the backup that are missing from faq.db.
RESTORE_BACKUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faq_backup_20260824_022617.json")
RESTORE_BACKUP_ON_START = True

def restore_faq_backup():
    if not RESTORE_BACKUP_ON_START or not os.path.exists(RESTORE_BACKUP_FILE):
        return
    try:
        with open(RESTORE_BACKUP_FILE, "r", encoding="utf-8") as f:
            backup = json.load(f)

        if not isinstance(backup, dict):
            print("FAQ backup restore skipped: backup is not a dictionary.")
            return

        restored = 0
        skipped = 0
        for trigger, answer in backup.items():
            if not isinstance(trigger, str) or not isinstance(answer, str):
                skipped += 1
                continue
            trigger = trigger.strip()
            answer = answer.strip()
            if not trigger or not answer:
                skipped += 1
                continue

            cursor.execute("SELECT id FROM faqs WHERE trigger = ?", (trigger,))
            if cursor.fetchone() is None:
                cursor.execute(
                    "INSERT INTO faqs (trigger, answer, created_at) VALUES (?, ?, ?)",
                    (trigger, answer, datetime.now().isoformat())
                )
                restored += 1

        db.commit()
        cursor.execute("SELECT COUNT(*) AS count FROM faqs")
        total = cursor.fetchone()["count"]
        print(f"FAQ backup restore: added {restored}, skipped {skipped}, total FAQs: {total}")
    except Exception as e:
        print(f"FAQ backup restore failed: {e}")

restore_faq_backup()

def normalize_text(text):
    text = str(text).lower().strip().replace("```", "").replace("`", "")
    replacements = {"\u2018":"'", "\u2019":"'", "\u201c":'"', "\u201d":'"',
                    "\u2013":"-", "\u2014":"-", "\u00a0":" "}
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def word_set(text):
    return set(normalize_text(text).split())


def fuzzy_score(a, b):
    na, nb = normalize_text(a), normalize_text(b)
    sequence = difflib.SequenceMatcher(None, na, nb).ratio()
    wa, wb = word_set(a), word_set(b)
    overlap = len(wa & wb) / max(len(wa), len(wb)) if wa and wb else 0.0
    return sequence * 0.70 + overlap * 0.30, sequence, overlap


def find_faq(question):
    cursor.execute("SELECT * FROM faqs ORDER BY id")
    faqs = cursor.fetchall()
    nq = normalize_text(question)

    for faq in faqs:
        if nq == normalize_text(faq["trigger"]):
            return faq, 1.0

    candidates = []
    for faq in faqs:
        score, sequence, overlap = fuzzy_score(question, faq["trigger"])
        candidates.append((score, sequence, overlap, faq))

    if not candidates:
        return None, 0.0

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_sequence, best_overlap, best_faq = candidates[0]
    second_score = candidates[1][0] if len(candidates) > 1 else 0.0

    if (best_score >= FUZZY_THRESHOLD
        and best_sequence >= 0.86
        and best_overlap >= MIN_WORD_OVERLAP
        and best_score - second_score >= FUZZY_MARGIN):
        return best_faq, best_score

    return None, best_score


def is_admin(member):
    if not isinstance(member, discord.Member):
        return False
    return (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    )

def subscription_command_allowed(message):
    if not is_admin(message.author):
        return False
    if SUBSCRIPTION_ADMIN_CHANNEL_ID and message.channel.id != SUBSCRIPTION_ADMIN_CHANNEL_ID:
        return False
    return True


async def delete_after(message, seconds=10):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except:
        pass

user_cooldowns = {}

def is_on_cooldown(user_id):
    now = time.time()
    last_time = user_cooldowns.get(user_id, 0)
    if now - last_time < COOLDOWN_SECONDS:
        return True
    user_cooldowns[user_id] = now
    return False

def add_faq(trigger, answer):
    trigger = trigger.strip()
    answer = answer.strip()
    if not trigger or not answer:
        return False
    try:
        cursor.execute(
            "INSERT INTO faqs (trigger, answer, created_at) VALUES (?, ?, ?)",
            (trigger, answer, datetime.now().isoformat())
        )
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def delete_faq(trigger):
    cursor.execute("DELETE FROM faqs WHERE trigger = ?", (trigger,))
    deleted = cursor.rowcount > 0
    db.commit()
    return deleted

def edit_faq(trigger, new_answer):
    cursor.execute(
        "UPDATE faqs SET answer = ? WHERE trigger = ?",
        (new_answer.strip(), trigger.strip())
    )
    edited = cursor.rowcount > 0
    db.commit()
    return edited

def increment_usage(faq_id):
    cursor.execute("UPDATE faqs SET uses = uses + 1 WHERE id = ?", (faq_id,))
    db.commit()

def add_feedback(faq_id, positive):
    if positive:
        cursor.execute("UPDATE faqs SET likes = likes + 1 WHERE id = ?", (faq_id,))
    else:
        cursor.execute("UPDATE faqs SET dislikes = dislikes + 1 WHERE id = ?", (faq_id,))
    db.commit()

def log_unanswered(message):
    cursor.execute(
        """
        INSERT INTO unanswered
        (question, user_id, username, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            message.content,
            message.author.id,
            str(message.author),
            datetime.now().isoformat()
        )
    )
    db.commit()

def get_subscriber_role(guild):
    return discord.utils.get(guild.roles, name=SUBSCRIBER_ROLE_NAME)


def get_subscription(user_id, guild_id):
    cursor.execute(
        "SELECT * FROM subscriptions WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id)
    )
    return cursor.fetchone()


def set_subscription(user_id, guild_id, expires_at, created_at=None):
    if created_at is None:
        created_at = datetime.now()

    cursor.execute(
        """
        INSERT INTO subscriptions
            (user_id, guild_id, expires_at, created_at, last_warning_sent)
        VALUES (?, ?, ?, ?, NULL)
        ON CONFLICT(user_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            expires_at = excluded.expires_at,
            created_at = COALESCE(subscriptions.created_at, excluded.created_at),
            last_warning_sent = NULL
        """,
        (
            user_id,
            guild_id,
            expires_at.isoformat(),
            created_at.isoformat()
        )
    )
    db.commit()

def remove_subscription(user_id, guild_id):
    cursor.execute(
        "DELETE FROM subscriptions WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id)
    )
    db.commit()


def subscription_expired(subscription):
    if not subscription:
        return True
    try:
        return datetime.fromisoformat(subscription["expires_at"]) <= datetime.now()
    except (ValueError, TypeError):
        return True


async def send_subscription_warning(member, expires_at):
    """Send a private warning once when about 24 hours remain."""
    subscription = get_subscription(member.id, member.guild.id)
    if not subscription:
        return

    remaining = expires_at - datetime.now()
    if remaining.total_seconds() <= 0:
        return

    if remaining.total_seconds() > SUBSCRIPTION_EXPIRY_WARNING_HOURS * 3600:
        return

    last_warning = subscription["last_warning_sent"]
    if last_warning:
        return

    try:
        embed = discord.Embed(
            title="⏰ Subscription Expiring Soon",
            description=(
                f"Your **{SUBSCRIBER_ROLE_NAME}** access expires in approximately "
                f"**{max(1, int(remaining.total_seconds() // 3600))} hour(s)**."
            ),
            color=discord.Color.orange()
        )
        embed.add_field(
            name="Expires",
            value=expires_at.strftime("%d %b %Y, %I:%M %p"),
            inline=False
        )
        embed.set_footer(text="Contact an administrator if you need a renewal.")

        await member.send(embed=embed)

        cursor.execute(
            "UPDATE subscriptions SET last_warning_sent = ? WHERE user_id = ?",
            (datetime.now().isoformat(), member.id)
        )
        db.commit()
    except discord.Forbidden:
        # DMs disabled; don't repeatedly try every minute.
        cursor.execute(
            "UPDATE subscriptions SET last_warning_sent = ? WHERE user_id = ?",
            (datetime.now().isoformat(), member.id)
        )
        db.commit()
    except Exception as e:
        print("SUBSCRIPTION WARNING ERROR:", e)


async def notify_subscription_expired(member):
    try:
        embed = discord.Embed(
            title="🔒 Subscription Expired",
            description=(
                f"Your **{SUBSCRIBER_ROLE_NAME}** access has expired. "
                "Your private channel access has been removed."
            ),
            color=discord.Color.red()
        )
        await member.send(embed=embed)
    except discord.Forbidden:
        pass
    except Exception as e:
        print("SUBSCRIPTION EXPIRY DM ERROR:", e)


async def remove_expired_subscriptions():
    """Remove the Subscriber role from users whose subscription has expired."""
    cursor.execute("SELECT * FROM subscriptions")
    subscriptions = cursor.fetchall()

    for subscription in subscriptions:
        guild = client.get_guild(subscription["guild_id"])
        if not guild:
            continue

        role = get_subscriber_role(guild)
        member = guild.get_member(subscription["user_id"])

        if not subscription_expired(subscription):
            if member:
                expires_at = datetime.fromisoformat(subscription["expires_at"])
                await send_subscription_warning(member, expires_at)
            continue

        if role and member and role in member.roles:
            try:
                await member.remove_roles(
                    role,
                    reason="Subscription expired"
                )
            except discord.HTTPException as e:
                print(
                    f"Could not remove expired Subscriber role "
                    f"from {subscription['user_id']}: {e}"
                )

        if member:
            await notify_subscription_expired(member)

        remove_subscription(subscription["user_id"], subscription["guild_id"])


async def subscription_expiry_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await remove_expired_subscriptions()
        except Exception as e:
            print("SUBSCRIPTION CHECK ERROR:", e)

        await asyncio.sleep(SUBSCRIPTION_CHECK_INTERVAL)


def export_faqs():
    cursor.execute("SELECT trigger, answer FROM faqs ORDER BY id")
    return {row["trigger"]: row["answer"] for row in cursor.fetchall()}

def create_backup():
    os.makedirs(BACKUP_FOLDER, exist_ok=True)
    filename = datetime.now().strftime("faq_backup_%Y%m%d_%H%M%S.json")
    path = os.path.join(BACKUP_FOLDER, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(export_faqs(), f, indent=2, ensure_ascii=False)
    return path

@client.event
async def on_ready():
    cursor.execute("SELECT COUNT(*) AS count FROM faqs")
    faq_count = cursor.fetchone()["count"]
    print("=" * 50)
    print(f"Logged in as: {client.user}")
    print(f"FAQ triggers: {faq_count}")
    print(f"FAQ channel: {ALLOWED_CHANNEL_ID}")
    print(f"Unanswered log channel: {UNANSWERED_CHANNEL_ID}")
    print("Bot is online and ready!")
    print("=" * 50)

    if not hasattr(client, "subscription_task"):
        client.subscription_task = asyncio.create_task(subscription_expiry_loop())

@client.event
async def on_raw_reaction_add(payload):
    if payload.user_id == client.user.id:
        return
    if payload.channel_id != ALLOWED_CHANNEL_ID:
        return

    emoji = str(payload.emoji)
    if emoji not in ["👍", "👎"]:
        return

    channel = client.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except:
        return

    if not message.embeds:
        return

    for embed in message.embeds:
        if not embed.footer:
            continue

        footer_text = embed.footer.text or ""
        if not footer_text.startswith("FAQ_ID:"):
            continue

        try:
            faq_id = int(footer_text.replace("FAQ_ID:", ""))
            add_feedback(faq_id, emoji == "👍")
        except:
            pass

@client.event
async def on_message(message):
    if message.author.bot:
        return

    msg = message.content.strip()

    # FAQ/admin commands stay restricted to the FAQ channel.
    # Subscription commands use their own admin permission/channel rules,
    # while members can use !mysubscription anywhere.
    subscription_message = (
        msg.lower() in ("!mysubscription", "!my-subscription")
        or msg.startswith("!subscribe ")
        or msg.startswith("!unsubscribe ")
        or msg.startswith("!subscription ")
        or msg == "!subscribers"
    )
    if message.channel.id != ALLOWED_CHANNEL_ID and not subscription_message:
        return

    # ---------------- SUBSCRIPTION COMMANDS ----------------
    if msg.startswith("!subscribe "):
        if not subscription_command_allowed(message):
            return

        parts = msg.split()
        if len(parts) != 3 or not message.mentions:
            reply = await message.channel.send(
                "❌ **Usage:** `!subscribe @user DAYS`\n"
                "Example: `!subscribe @Dwip 30`"
            )
            asyncio.create_task(delete_after(reply, 8))
            return

        try:
            days = int(parts[-1])
            if days <= 0 or days > 3650:
                raise ValueError
        except ValueError:
            reply = await message.channel.send(
                "❌ Days must be between **1 and 3650**."
            )
            asyncio.create_task(delete_after(reply, 5))
            return

        member = message.mentions[0]

        role = get_subscriber_role(message.guild)
        if role is None:
            try:
                role = await message.guild.create_role(
                    name=SUBSCRIBER_ROLE_NAME,
                    reason="Subscription access role"
                )
            except discord.Forbidden:
                reply = await message.channel.send(
                    "❌ I can't create the Subscriber role. "
                    "Give me **Manage Roles** permission."
                )
                asyncio.create_task(delete_after(reply, 8))
                return

        existing = get_subscription(member.id, message.guild.id)
        now = datetime.now()

        if existing and not subscription_expired(existing):
            old_expiry = datetime.fromisoformat(existing["expires_at"])
            expires_at = old_expiry + timedelta(days=days)
            action = "renewed"
        else:
            expires_at = now + timedelta(days=days)
            action = "activated"

        set_subscription(
            member.id,
            message.guild.id,
            expires_at,
            created_at=(
                datetime.fromisoformat(existing["created_at"])
                if existing and existing["created_at"] else now
            )
        )

        try:
            await member.add_roles(
                role,
                reason=f"Subscription {action} until {expires_at.isoformat()}"
            )
        except discord.Forbidden:
            remove_subscription(member.id, message.guild.id)
            reply = await message.channel.send(
                "❌ I couldn't assign the Subscriber role.\n"
                "Make sure my bot's role is **above** the Subscriber role."
            )
            asyncio.create_task(delete_after(reply, 8))
            return

        embed = discord.Embed(
            title="✅ Subscription Activated",
            color=discord.Color.green()
        )
        embed.add_field(name="Member", value=member.mention, inline=True)
        embed.add_field(name="Duration Added", value=f"{days} day(s)", inline=True)
        embed.add_field(
            name="Expires",
            value=expires_at.strftime("%d %b %Y, %I:%M %p"),
            inline=False
        )
        embed.set_footer(text="Subscriber access is controlled by the Subscriber role.")

        reply = await message.channel.send(embed=embed)
        await message.delete()
        asyncio.create_task(delete_after(reply, 12))
        return

    if msg.startswith("!unsubscribe "):
        if not subscription_command_allowed(message):
            return

        if not message.mentions:
            reply = await message.channel.send(
                "❌ **Usage:** `!unsubscribe @user`"
            )
            asyncio.create_task(delete_after(reply, 5))
            return

        member = message.mentions[0]
        role = get_subscriber_role(message.guild)

        remove_subscription(member.id, message.guild.id)

        if role and role in member.roles:
            try:
                await member.remove_roles(
                    role,
                    reason="Subscription manually removed"
                )
            except discord.Forbidden:
                reply = await message.channel.send(
                    "⚠️ Database subscription removed, but I couldn't remove "
                    "the Discord role. Check my **Manage Roles** permission."
                )
                asyncio.create_task(delete_after(reply, 8))
                return

        embed = discord.Embed(
            title="🔒 Subscription Removed",
            description=f"Access has been removed from {member.mention}.",
            color=discord.Color.red()
        )
        reply = await message.channel.send(embed=embed)
        await message.delete()
        asyncio.create_task(delete_after(reply, 8))
        return

    # Member self-check: available to everyone.
    if msg.lower() in ("!mysubscription", "!my-subscription"):
        subscription = get_subscription(message.author.id, message.guild.id)

        if not subscription or subscription_expired(subscription):
            embed = discord.Embed(
                title="🔴 No Active Subscription",
                description=(
                    "You currently do not have an active subscription.\n\n"
                    "If you believe this is incorrect, please contact an administrator."
                ),
                color=discord.Color.red()
            )
            embed.add_field(
                name="Access",
                value="🔒 Subscriber access is inactive.",
                inline=False
            )
        else:
            expires_at = datetime.fromisoformat(subscription["expires_at"])
            remaining = expires_at - datetime.now()
            total_seconds = max(0, int(remaining.total_seconds()))
            days_left = total_seconds // 86400
            hours_left = (total_seconds % 86400) // 3600
            minutes_left = (total_seconds % 3600) // 60

            embed = discord.Embed(
                title="📋 Your Subscription",
                description="Your Subscriber access is currently active.",
                color=discord.Color.green()
            )
            embed.add_field(
                name="Status",
                value="🟢 Active",
                inline=True
            )
            embed.add_field(
                name="Time Remaining",
                value=f"**{days_left}d {hours_left}h {minutes_left}m**",
                inline=True
            )
            embed.add_field(
                name="Expires",
                value=expires_at.strftime("%d %b %Y, %I:%M %p"),
                inline=False
            )
            embed.add_field(
                name="Channel Access",
                value="🔓 Subscriber access is active.",
                inline=False
            )
            embed.set_footer(text="Use !mysubscription anytime to check your status.")

        reply = await message.channel.send(embed=embed)
        asyncio.create_task(delete_after(reply, 12))
        return

    if msg.startswith("!subscription "):
        if not subscription_command_allowed(message):
            return

        if not message.mentions:
            reply = await message.channel.send(
                "❌ **Usage:** `!subscription @user`"
            )
            asyncio.create_task(delete_after(reply, 5))
            return

        member = message.mentions[0]
        subscription = get_subscription(member.id, message.guild.id)

        if not subscription or subscription_expired(subscription):
            embed = discord.Embed(
                title="ℹ️ No Active Subscription",
                description=f"{member.mention} has no active subscription.",
                color=discord.Color.orange()
            )
        else:
            expires_at = datetime.fromisoformat(subscription["expires_at"])
            remaining = expires_at - datetime.now()
            total_seconds = max(0, int(remaining.total_seconds()))
            days_left = total_seconds // 86400
            hours_left = (total_seconds % 86400) // 3600
            minutes_left = (total_seconds % 3600) // 60

            embed = discord.Embed(
                title="📋 Subscription Details",
                color=discord.Color.blue()
            )
            embed.add_field(name="Member", value=member.mention, inline=False)
            embed.add_field(
                name="Status",
                value="🟢 Active",
                inline=True
            )
            embed.add_field(
                name="Time Remaining",
                value=f"**{days_left}d {hours_left}h {minutes_left}m**",
                inline=True
            )
            embed.add_field(
                name="Expires",
                value=expires_at.strftime("%d %b %Y, %I:%M %p"),
                inline=False
            )

        reply = await message.channel.send(embed=embed)
        asyncio.create_task(delete_after(reply, 12))
        return

    if msg == "!subscribers":
        if not subscription_command_allowed(message):
            return

        cursor.execute(
            "SELECT * FROM subscriptions WHERE guild_id = ? ORDER BY expires_at",
            (message.guild.id,)
        )
        rows = cursor.fetchall()

        active = []
        for row in rows:
            if not subscription_expired(row):
                member = message.guild.get_member(row["user_id"])
                if member:
                    expires = datetime.fromisoformat(row["expires_at"])
                    active.append(
                        f"{member.mention} — expires **{expires.strftime('%d %b %Y, %I:%M %p')}**"
                    )

        embed = discord.Embed(
            title="👥 Active Subscribers",
            description="\n".join(active[:50]) if active else "No active subscribers.",
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Showing {min(len(active), 50)} active subscriber(s).")
        await message.channel.send(embed=embed)
        return

    # --------------------------------------------------------

    if msg.startswith("!add "):
        if not is_admin(message.author):
            return
        try:
            trigger, answer = msg[5:].split("|", 1)
            success = add_faq(trigger, answer)

            reply = await message.channel.send(
                "✅ FAQ added successfully."
                if success
                else "⚠️ That exact trigger already exists."
            )

            await message.delete()
            asyncio.create_task(delete_after(reply, 5))
        except Exception as e:
            print("ADD ERROR:", e)
        return

    if msg.startswith("!del "):
        if not is_admin(message.author):
            return

        trigger = msg[5:].strip()
        reply = await message.channel.send(
            "🗑️ FAQ deleted."
            if delete_faq(trigger)
            else "⚠️ Trigger not found."
        )

        await message.delete()
        asyncio.create_task(delete_after(reply, 5))
        return

    if msg.startswith("!edit "):
        if not is_admin(message.author):
            return

        try:
            trigger, new_answer = msg[6:].split("|", 1)
            reply = await message.channel.send(
                "✏️ FAQ updated."
                if edit_faq(trigger, new_answer)
                else "⚠️ Trigger not found."
            )
            await message.delete()
            asyncio.create_task(delete_after(reply, 5))
        except:
            pass
        return

    if msg.startswith("!import"):
        if not is_admin(message.author):
            return

        lines = msg.splitlines()[1:]
        imported = duplicates = invalid = 0
        details = []

        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if line.startswith("```text"):
                line = line[7:].strip()
            elif line.startswith("```"):
                line = line[3:].strip()
            if line.endswith("```"):
                line = line[:-3].strip()
            if not line:
                continue

            if "|" not in line:
                invalid += 1
                details.append(f"Line {line_number}: missing |")
                continue

            trigger, answer = (x.strip() for x in line.split("|", 1))
            if not trigger or not answer:
                invalid += 1
                details.append(f"Line {line_number}: empty question/answer")
                continue

            if add_faq(trigger, answer):
                imported += 1
            else:
                duplicates += 1
                details.append(f"Line {line_number}: duplicate trigger")

        report = (f"📥 **Import complete**\n"
                  f"✅ Imported: **{imported}**\n"
                  f"⚠️ Duplicates skipped: **{duplicates}**\n"
                  f"❌ Invalid lines skipped: **{invalid}**")
        if details:
            report += "\n\n**Details:**\n" + "\n".join("• " + x for x in details[:8])
            if len(details) > 8:
                report += f"\n• ...and {len(details)-8} more."

        await message.delete()
        reply = await message.channel.send(report[:1950])
        asyncio.create_task(delete_after(reply, 10))
        return

    if msg == "!export":
        if not is_admin(message.author):
            return

        filename = "faq_export.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(export_faqs(), f, indent=2, ensure_ascii=False)

        await message.channel.send(
            "📤 FAQ export:",
            file=discord.File(filename)
        )

        try:
            os.remove(filename)
        except:
            pass

        await message.delete()
        return

    if msg == "!backup":
        if not is_admin(message.author):
            return

        path = create_backup()
        await message.channel.send(
            "💾 Backup created:",
            file=discord.File(path)
        )
        await message.delete()
        return

    if msg == "!help":
        if not is_admin(message.author):
            return

        cursor.execute("SELECT COUNT(*) AS count FROM faqs")
        count = cursor.fetchone()["count"]

        embed = discord.Embed(
            title="FAQ Bot Commands",
            description=(
                "**!add question | answer**\nAdd a new FAQ.\n\n"
                "**!del question**\nDelete an FAQ.\n\n"
                "**!edit question | new answer**\nEdit an existing FAQ.\n\n"
                "**!import**\nImport multiple FAQs.\n\n"
                "**!export**\nExport FAQs as JSON.\n\n"
                "**!backup**\nCreate a backup.\n\n"
                "**!stats**\nView usage and feedback statistics.\n\n"
                "**!unanswered**\nView unanswered questions.\n\n"
                "**!subscribe @user DAYS**\nActivate or extend channel access.\n\n"
                "**!unsubscribe @user**\nImmediately remove access.\n\n"
                "**!subscription @user**\nView status and expiry.\n\n"
                "**!subscribers**\nList all active subscribers.\n\n"
                "**!mysubscription**\nCheck your own subscription status.\n\n"
                f"Total FAQs: **{count}**"
            ),
            color=discord.Color.blue()
        )

        await message.channel.send(embed=embed)
        return

    if msg == "!stats":
        if not is_admin(message.author):
            return

        cursor.execute("""
            SELECT
                SUM(uses) AS total_uses,
                SUM(likes) AS total_likes,
                SUM(dislikes) AS total_dislikes
            FROM faqs
        """)
        totals = cursor.fetchone()

        cursor.execute("""
            SELECT trigger, uses
            FROM faqs
            ORDER BY uses DESC
            LIMIT 10
        """)
        popular = cursor.fetchall()

        description = (
            f"📊 **Total answers:** {totals['total_uses'] or 0}\n"
            f"👍 **Likes:** {totals['total_likes'] or 0}\n"
            f"👎 **Dislikes:** {totals['total_dislikes'] or 0}\n\n"
            "**Most Asked FAQs:**\n"
        )

        for i, faq in enumerate(popular, start=1):
            description += (
                f"{i}. {faq['trigger'][:80]} — **{faq['uses']}**\n"
            )

        embed = discord.Embed(
            title="FAQ Statistics",
            description=description,
            color=discord.Color.green()
        )

        await message.channel.send(embed=embed)
        return

    if msg == "!unanswered":
        if not is_admin(message.author):
            return

        cursor.execute("""
            SELECT question, username, created_at
            FROM unanswered
            ORDER BY id DESC
            LIMIT 10
        """)
        rows = cursor.fetchall()

        if not rows:
            await message.channel.send("✅ No unanswered questions.")
            return

        description = ""

        for row in rows:
            description += (
                f"**{row['username']}**\n"
                f"{row['question'][:150]}\n"
                f"`{row['created_at'][:19]}`\n\n"
            )

        embed = discord.Embed(
            title="Recent Unanswered Questions",
            description=description,
            color=discord.Color.orange()
        )

        await message.channel.send(embed=embed)
        return

    if is_on_cooldown(message.author.id):
        return

    faq, score = find_faq(msg)

    if faq:
        increment_usage(faq["id"])

        embed = discord.Embed(
            title="❓ " + faq["trigger"][:256],
            description=faq["answer"],
            color=discord.Color.green()
        )

        embed.set_footer(text=f"FAQ_ID:{faq['id']}")

        reply_msg = await message.reply(
            embed=embed,
            mention_author=False
        )

        try:
            await reply_msg.add_reaction("👍")
            await reply_msg.add_reaction("👎")
        except:
            pass

        asyncio.create_task(delete_after(message, 10))
        asyncio.create_task(delete_after(reply_msg, 10))
        return

    # Unanswered question
    log_unanswered(message)

    try:
        log_channel = client.get_channel(UNANSWERED_CHANNEL_ID)

        if log_channel:
            embed = discord.Embed(
                title="❓ Unanswered FAQ",
                description=message.content[:4000],
                color=discord.Color.orange()
            )

            embed.add_field(
                name="User",
                value=message.author.mention,
                inline=True
            )

            embed.add_field(
                name="Channel",
                value=message.channel.mention,
                inline=True
            )

            embed.add_field(
                name="User ID",
                value=str(message.author.id),
                inline=True
            )

            embed.timestamp = datetime.now()

            await log_channel.send(embed=embed)

    except Exception as e:
        print("LOG CHANNEL ERROR:", e)


client.run(TOKEN)
