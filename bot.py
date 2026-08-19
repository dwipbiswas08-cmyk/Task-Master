import discord
import json
import os
import asyncio

# 1. SET UP INTENTS
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

client = discord.Client(intents=intents)

FAQ_FILE = 'faq.json'

# Load triggers from file so they don't reset on restart
def load_faq():
    try:
        with open(FAQ_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {
    "Scroll to the section Everything About Sports Betting!, locate the second and third words.": "Expert tips",
    "Scroll to the section called Changing Regulations, locate the first two words.": "Regulations can",
    "Scroll to the section called Our Mission, locate the first 2 words": "To help",
    #... paste all your other 125 triggers here...
    "Scroll to the section called \"Our Mission\", locate the third and fourth words from the end of the second sentence.": "professional online"
}

def save_faq():
    with open(FAQ_FILE, 'w', encoding='utf-8') as f:
        json.dump(AUTO_REPLIES, f, indent=2, ensure_ascii=False)

AUTO_REPLIES = load_faq()

def is_admin(member):
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild

async def delete_after(msg, seconds=10):
    await asyncio.sleep(seconds)
    try:
        await msg.delete()
    except:
        pass

# 3. WHEN BOT STARTS
@client.event
async def on_ready():
    print(f'Logged in as {client.user}')
    print(f'Bot is online and ready with {len(AUTO_REPLIES)} triggers!')

# 4. WHEN SOMEONE SENDS A MESSAGE
@client.event
async def on_message(message):
    global AUTO_REPLIES
    if message.author == client.user:
        return

    msg = message.content

    # ADD COMMAND - ADMIN ONLY
    if msg.startswith('!add '):
        if not is_admin(message.author):
            reply_msg = await message.channel.send(f"{message.author.mention} ❌ Admins only")
            asyncio.create_task(delete_after(reply_msg))
            return
        try:
            _, rest = msg.split('!add ', 1)
            trigger, reply = rest.split('|', 1)
            AUTO_REPLIES[trigger.strip()] = reply.strip()
            save_faq()
            reply_msg = await message.channel.send(f"{message.author.mention} ✅ Added: `{trigger.strip()}`")
            asyncio.create_task(delete_after(reply_msg))
        except:
            reply_msg = await message.channel.send(f"{message.author.mention} Use format: `!add trigger text | reply text`")
            asyncio.create_task(delete_after(reply_msg))

    # DELETE COMMAND - ADMIN ONLY
    elif msg.startswith('!del '):
        if not is_admin(message.author):
            reply_msg = await message.channel.send(f"{message.author.mention} ❌ Admins only")
            asyncio.create_task(delete_after(reply_msg))
            return
        trigger = msg[5:].strip()
        if trigger in AUTO_REPLIES:
            del AUTO_REPLIES[trigger]
            save_faq()
            reply_msg = await message.channel.send(f"{message.author.mention} 🗑️ Deleted: `{trigger}`")
            asyncio.create_task(delete_after(reply_msg))
        else:
            reply_msg = await message.channel.send(f"{message.author.mention} Trigger not found: `{trigger}`")
            asyncio.create_task(delete_after(reply_msg))

    # IMPORT COMMAND - ADMIN ONLY - BULK ADD
    elif msg.startswith('!import'):
        if not is_admin(message.author):
            reply_msg = await message.channel.send(f"{message.author.mention} ❌ Admins only")
            asyncio.create_task(delete_after(reply_msg))
            return

        # Expect format:!import then new lines with trigger | reply
        lines = msg.split('\n')[1:] # skip the!import line
        added = 0
        failed = 0
        for line in lines:
            if '|' in line:
                try:
                    trigger, reply = line.split('|', 1)
                    AUTO_REPLIES[trigger.strip()] = reply.strip()
                    added += 1
                except:
                    failed += 1

        if added > 0:
            save_faq()

        reply_msg = await message.channel.send(
            f"{message.author.mention} 📥 Imported **{added}** triggers. Failed: **{failed}**\nTotal: **{len(AUTO_REPLIES)}**"
        )
        asyncio.create_task(delete_after(reply_msg, 15))

    # LIST COMMAND
    elif msg == '!help':
        if not AUTO_REPLIES:
            reply_msg = await message.channel.send("No triggers set yet.")
        else:
            faq_list = "\n".join([f"`{k}`" for k in list(AUTO_REPLIES.keys())[:20]]) # show first 20
            reply_msg = await message.channel.send(f"**📚 {len(AUTO_REPLIES)} Triggers**\n{faq_list}\n...and more")
        asyncio.create_task(delete_after(reply_msg, 20))

    # AUTO REPLY
    else:
        for trigger, reply in AUTO_REPLIES.items():
            if trigger.lower() in msg.lower(): # case-insensitive partial match
                # Mention the phrase + reply, then auto delete both
                reply_msg = await message.channel.send(
                    f"{message.author.mention} **Trigger:** `{trigger}`\n**Answer:** {reply}"
                )
                asyncio.create_task(delete_after(message, 10)) # delete user's message
                asyncio.create_task(delete_after(reply_msg, 10)) # delete bot reply
                break

# 5. RUN THE BOT
client.run(os.environ['MTUzOTMxNjA0MDcxNzMxMjE3NA.GvTSY5.tMrsJ4AB63X6kKsnJEIfSowq5RzIUIww9hAcuA'])
