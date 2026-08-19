import discord
import json
import os
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

client = discord.Client(intents=intents)

FAQ_FILE = 'faq.json'

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

AUTO_REPLIES = load_faq()

def save_faq():
    with open(FAQ_FILE, 'w', encoding='utf-8') as f:
        json.dump(AUTO_REPLIES, f, indent=2, ensure_ascii=False) # FIX 1: was saving wrong variable

def is_admin(member):
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild

async def delete_after(msg, seconds=10):
    await asyncio.sleep(seconds)
    try:
        await msg.delete()
    except:
        pass

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')
    print(f'Bot is online and ready with {len(AUTO_REPLIES)} triggers!')

@client.event
async def on_message(message):
    global AUTO_REPLIES
    if message.author == client.user:
        return

    msg = message.content

    # ADD COMMAND - SILENT
    if msg.startswith('!add '):
        if not is_admin(message.author): return
        try:
            _, rest = msg.split('!add ', 1)
            trigger, reply = rest.split('|', 1)
            AUTO_REPLIES[trigger.strip()] = reply.strip()
            save_faq()
            await message.delete() # delete command
        except: pass
        return

    # DELETE COMMAND
    elif msg.startswith('!del '):
        if not is_admin(message.author): return
        trigger = msg[5:].strip()
        if trigger in AUTO_REPLIES:
            del AUTO_REPLIES[trigger]
            save_faq()
        await message.delete()
        return

    # IMPORT COMMAND - SILENT
    elif msg.startswith('!import'):
        if not is_admin(message.author): return
        lines = msg.split('\n')[1:]
        for line in lines:
            if '|' in line:
                try:
                    trigger, reply = line.split('|', 1)
                    AUTO_REPLIES[trigger.strip()] = reply.strip()
                except: pass
        save_faq()
        await message.delete()
        return

    # LIST COMMAND - FIX 2
    elif msg == '!help':
        if not AUTO_REPLIES:
            reply_msg = await message.channel.send("No triggers set yet.")
        else:
            faq_list = "\n".join([f"`{k}`" for k in list(AUTO_REPLIES.keys())[:20]])
            reply_msg = await message.channel.send(f"**{len(AUTO_REPLIES)} Triggers**\n{faq_list}")
        asyncio.create_task(delete_after(reply_msg, 20))
        return

    # AUTO REPLY - WORDS ONLY
    else:
        for trigger, reply in AUTO_REPLIES.items():
            if trigger.lower() in msg.lower():
                reply_msg = await message.channel.send(reply) # FIX 3: ONLY raw words
                asyncio.create_task(delete_after(message, 10))
                asyncio.create_task(delete_after(reply_msg, 10))
                return

client.run(os.environ['TOKEN'])
