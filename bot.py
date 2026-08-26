import discord
import sqlite3
import os
import asyncio
import difflib
import json
import time
import re
from datetime import datetime

TOKEN = os.environ["TOKEN"]

ALLOWED_CHANNEL_ID = 1538465015135346768
UNANSWERED_CHANNEL_ID = 1540997587740532746

COOLDOWN_SECONDS = 2
# Fuzzy matching is intentionally strict so the bot does not guess a wrong FAQ.
FUZZY_THRESHOLD = 0.90
FUZZY_MARGIN = 0.08
MIN_WORD_OVERLAP = 0.50

DATABASE_FILE = "faq.db"
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

def normalize_text(text):
    """Normalize text for reliable exact matching."""
    text = str(text).lower().strip()
    text = text.replace("```", "").replace("`", "")

    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-", "\u00a0": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def word_set(text):
    return set(normalize_text(text).split())


def fuzzy_score(a, b):
    """Return a combined similarity score plus its component scores."""
    na = normalize_text(a)
    nb = normalize_text(b)
    sequence = difflib.SequenceMatcher(None, na, nb).ratio()

    words_a = word_set(a)
    words_b = word_set(b)
    overlap = (len(words_a & words_b) / max(len(words_a), len(words_b))) if words_a and words_b else 0.0

    return (sequence * 0.70) + (overlap * 0.30), sequence, overlap


def find_faq(question):
    """Find an FAQ without returning a risky near-match."""
    cursor.execute("SELECT * FROM faqs ORDER BY id")
    faqs = cursor.fetchall()
    normalized_question = normalize_text(question)

    if not normalized_question:
        return None, 0.0

    # Exact normalized match always wins.
    for faq in faqs:
        if normalized_question == normalize_text(faq["trigger"]):
            return faq, 1.0

    candidates = []
    for faq in faqs:
        score, sequence, overlap = fuzzy_score(question, faq["trigger"])
        candidates.append((score, sequence, overlap, faq))

    if not candidates:
        return None, 0.0

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_sequence, best_overlap, best_faq = candidates[0]
    second_score = candidates[1][0] if len(candidates) > 1 else 0.0
    margin = best_score - second_score

    if (
        best_score >= FUZZY_THRESHOLD
        and best_sequence >= 0.86
        and best_overlap >= MIN_WORD_OVERLAP
        and margin >= FUZZY_MARGIN
    ):
        return best_faq, best_score

    # When confidence is not high enough, do NOT guess.
    return None, best_score

def is_admin(member):
    if not isinstance(member, discord.Member):
        return False
    return (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    )

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

    if message.channel.id != ALLOWED_CHANNEL_ID:
        return

    msg = message.content.strip()

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
        imported = 0
        duplicates = 0
        invalid = 0
        details = []

        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line:
                continue

            # Allow imports pasted inside a Discord code block.
            line = line.replace("```text", "").replace("```", "").strip()

            if "|" not in line:
                invalid += 1
                details.append(f"Line {line_number}: missing `|`")
                continue

            trigger, answer = line.split("|", 1)
            trigger = trigger.strip()
            answer = answer.strip()

            if not trigger or not answer:
                invalid += 1
                details.append(f"Line {line_number}: empty question or answer")
                continue

            if add_faq(trigger, answer):
                imported += 1
            else:
                duplicates += 1
                details.append(f"Line {line_number}: duplicate trigger")

        report = (
            f"📥 **Import complete**\n"
            f"✅ Imported: **{imported}**\n"
            f"⚠️ Duplicates skipped: **{duplicates}**\n"
            f"❌ Invalid lines skipped: **{invalid}**"
        )

        if details:
            report += "\n\n**Details:**\n" + "\n".join(f"• {x}" for x in details[:10])
            if len(details) > 10:
                report += f"\n• ...and {len(details) - 10} more."

        await message.delete()
        reply = await message.channel.send(report)
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
