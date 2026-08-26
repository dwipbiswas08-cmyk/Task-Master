import discord
import sqlite3
import os
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

# ---------------------------------------------------------------------------
# FAQ JSON MASTER IMPORT
# ---------------------------------------------------------------------------
# Keep faq_export.json beside bot.py in GitHub.
# It is imported once. SQLite remains authoritative afterwards, so !edit and
# !del changes are not overwritten or resurrected on every Railway restart.
FAQ_JSON_FILE = "faq_export.json"
FAQ_JSON_SEED_KEY = "faq_export_json_seed_v1"

cursor.execute("""
CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")
db.commit()


def import_faq_json_once():
    cursor.execute(
        "SELECT value FROM bot_meta WHERE key = ?",
        (FAQ_JSON_SEED_KEY,)
    )
    if cursor.fetchone() is not None:
        return

    if not os.path.exists(FAQ_JSON_FILE):
        print(f"FAQ JSON not found: {FAQ_JSON_FILE}")
        return

    try:
        with open(FAQ_JSON_FILE, "r", encoding="utf-8") as f:
            master = json.load(f)

        if not isinstance(master, dict):
            raise ValueError("faq_export.json must contain a JSON object.")

        added = 0
        skipped = 0

        for trigger, answer in master.items():
            if not isinstance(trigger, str) or not isinstance(answer, str):
                skipped += 1
                continue

            trigger = trigger.strip()
            answer = answer.strip()
            if not trigger or not answer:
                skipped += 1
                continue

            # Do not overwrite existing DB rows.
            cursor.execute(
                "SELECT id FROM faqs WHERE trigger = ?",
                (trigger,)
            )
            if cursor.fetchone() is not None:
                continue

            try:
                cursor.execute(
                    "INSERT INTO faqs (trigger, answer, created_at) "
                    "VALUES (?, ?, ?)",
                    (trigger, answer, datetime.now().isoformat())
                )
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1

        db.commit()

        cursor.execute(
            "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
            (FAQ_JSON_SEED_KEY, datetime.now().isoformat())
        )
        db.commit()

        cursor.execute("SELECT COUNT(*) AS count FROM faqs")
        total = cursor.fetchone()["count"]
        print(
            f"FAQ JSON import completed: added {added}, "
            f"skipped {skipped}, total FAQs: {total}"
        )

    except Exception as e:
        db.rollback()
        print(f"FAQ JSON import failed: {e}")


import_faq_json_once()



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

# Safe one-time FAQ seed.
# The JSON is used ONLY to initialize a brand-new FAQ database.
# After initialization, edits/deletions/additions in faq.db are authoritative.
# This prevents deleted or edited FAQs from being resurrected on restart.
RESTORE_BACKUP_FILE = "faq_backup_20260824_022617.json"

# The 198-FAQ master is embedded so deployment does NOT depend on the JSON
# file being uploaded separately to Railway.
MASTER_FAQ_COUNT = 198
MASTER_FAQS = {'Scroll to the section Everything About Sports Betting!, locate the second and third words.': 'Expert tips', 'Scroll to the section called Changing Regulations, locate the first two words.': 'Regulations can', 'Scroll to the section called Our Mission, locate the first 2 words': 'To help', 'Scroll to the section called "Our Mission", locate the third and fourth words from the end of the second sentence.': 'professional online', 'Scroll to the section called "Useful Links", locate the last 2 words.': 'Complaints Policy', 'Scroll to the section called "Strategies & Bankroll Management When Betting with Crypto", then to the sub-section "Understand Volatility", locate the sixth, seventh, eighth, and ninth words of the first sentence.': 'move in price quickly', 'Scroll to the section called "Strategies & Bankroll Management When Betting with Crypto", then to the sub-section "Understand Volatility", locate the last three words of the second sentence.': 'one volatile coin', 'Scroll to the section called "Years of Research", locate the last 2 words.': 'and experience', 'Scroll to the section called "We Offer Knowledge & Education", locate the second and third words of the second sentence.': 'clear guides', 'Scroll to the section called "Written Articles", locate the sixth and seventh words of the description.': 'diverse topics', 'Scroll to the section called "Reload Bonuses", locate the first two words.': 'Once the', 'Scroll to the section called "Avoid Chasing Losses", locate the first two words of the third sentence.': 'if your', 'Scroll to the section "About SmartBettingGuide", locate the words between "find" and "betting" in the last sentence.': 'perfect sports', 'Scroll to the section "About SmartBettingGuide", locate the fourth and fifth words of the third sentence.': 'niche betting', 'Scroll to the section called "Betting Ratings", locate the first 2 words.': 'Crypto Betting', 'Scroll to the section called "Hours of Hands-on Testing", locate the first 2 words.': 'We spend', 'Scroll to the section called "Meet Our Expert Team", locate the first 2 words.': 'Zigmas Pekarskas', 'Scroll to the section called "Written Articles", locate the last 2 words.': 'and engaged', 'Scroll to the section called "About SmartBettingGuide", locate the last 2 words.': 'your needs', 'Scroll to the section called Professional Experts, locate the ninth and tenth words of the description.': 'specialized knowledge', 'Scroll to the section called Betting Bonuses, locate the last 2 words.': 'Reload bonuses', 'Scroll to the section called Our Mission, locate the last two words of the description.': 'smarter betting.', 'Scroll to the section called How Do We Rate Top Asian Betting Sites?, then to the sub-section Odds Comparison, and locate the last two words.': 'Live betting', 'Scroll to the section called Our Mission, locate the last two words of the first sentence.': 'better choices', 'Scroll to the section called Stay Updated About Sports Betting and locate the fourth and fifth words.': 'sports predictions', 'Scroll to the section called Data Research, locate the last 2 words': 'withdrawal speed', 'Scroll to the section called Live & In-Play Betting, locate the last two words of the first sentence.': 'crypto payments', 'Scroll to the section called Markets at the Crypto and Bitcoin Sports Betting Websites, locate the fourth and fifth words of the first sentence.': 'sports markets', 'Scroll to the section called Security & Safety in Top Crypto Betting Sites, locate the words between "use" and "and" in the Use a VPN carefully bullet.': 'trusted providers', 'Scroll to the section called Growth of Crypto Sports Betting: What Will We See in the Future?, locate the last two words of the Metaverse Betting row.': 'social features', 'Scroll to the section called Bonuses Betting Requirements in the Best Cryptocurrency Betting Sites, locate the words between "your" and "and" in the first bullet point.': 'balance steady', 'Scroll to the section called Bonuses and Promotions, locate the last two words of the first sentence.': 'careful look.', 'Scroll to the section called Bonuses Betting Requirements in the Best Cryptocurrency Betting Sites, locate the last two words of the Avoid switching coins bullet point.': 'reset progress', 'Scroll to the section called Odds Boosts, locate the first two words.': 'Alongside cashback,', 'Scroll to the section called Football Betting Options, locate the third and fourth words of the third sentence.': 'same time,', 'Find the account description and locate the first 2 words in it.': 'SmartbettingGuide is', 'Scroll to the section called Traditional Sports Markets, locate the words between "like" and "usually".': 'player props', 'Scroll to the section called Plan for Fees and Network Speeds and locate the first two words.': 'Different networks', 'Scroll to the section called Supported Cryptocurrencies & Payment Methods, locate the first two words.': 'Best crypto', 'Scroll to the section called Odds Fairness, locate the last three words.': 'keeps margins reasonable.', 'Scroll to the section called What is Betwinner minimum deposit?, locate the first 2 words.': 'Betwinner mininum', 'Scroll to the section called Cryptocurrency Options, locate the last 2 words.': 'usually irreversible.', 'Scroll to the section called Security Features at Asian Betting Sites and locate the first two words.': 'Security features', 'Scroll to the section called Keep Stakes Consistent, locate the last 2 words.': 'long-term results.', 'Scroll to the section called Live Betting Tools, locate the words between "the" and "how".': 'odds refresh,', 'Scroll to the section called FAQ about Tennis Handicap Betting, and locate the first two words of the answer to "How does tennis handicap betting work?".': 'Tennis handicap', 'Scroll to the section called "We Offer Knowledge & Education", locate the words between "and" and "help" in the second sentence.': 'Expert recommendations', 'Scroll to the section called "Our Achievements", locate the words between "of" and "to" under "Written Articles".': 'Online betting', 'Scroll to the section called "Bonuses & Promotions Guide at the Best Crypto Betting Sites", locate the last two words.': 'USDT deposits', 'Find "Welcome to http://Pokeriomokykla.com" section and locate the first 2 words.': 'Pokeriomokykla.com is', 'Scroll to the section called "How We Test & Review the Top Crypto Betting Sites", then to the sub-section "Security Practices", locate the second, third, and fourth words of the last sentence.': 'security practices are', 'Scroll to the section called "About Us", locate the first 2 words.': 'our vision', 'Scroll to the section called "Plan for Fees and Network Speeds", locate the last two words of the first paragraph.': 'frequent bettors.', 'Scroll to the section called "Esports Betting Markets", locate the words between "FIFA" and "constantly" in the first paragraph.': 'update odds', 'Scroll to the section called "Major Cryptocurrencies (BTC, ETH, LTC)", locate the last two words of the second sentence.': 'confirmation times.', 'Scroll to the section called "Honest Rating", locate the last 2 words.': 'individual needs', 'Scroll to the section called "Native Platform Tokens", locate the last two words.': 'faster withdrawals.', 'Scroll to the section called "Blockchain Payments", locate the last two words of the description.': 'clearly stated.', 'Scroll to the section called How do I contact 22bet support?, locate the last 2 words.': 'international support', 'Scroll to the section called "Security Practices", locate the first two words.': 'strong security', 'Scroll to the section called "Major Cryptocurrencies (BTC, ETH, LTC)", locate the words between "of" and "or" in the last sentence.': 'small bets', 'Scroll to the section called "Proportional Betting", locate the first two words.': 'Proportional Betting', 'Scroll to the section called "Our Vision", locate the fourth and fifth words of the description.': 'most trusted', 'Scroll to the section called "Most Popular Sports Betting Bonuses for Crypto Bettors", locate the second and third words of the Welcome Bonus description.': 'matched crypto', 'Scroll to the section called "Regulation", locate the last two words.': 'Deposit limits', 'Scroll to the section called "How Do We Rate Top Asian Betting Sites?", then to the sub-section "Mobile Site & App", and locate the last two words.': 'Payment features', 'Scroll to the section called "Hedge With Stablecoins", locate the sixth, seventh, and eighth words.': 'Safest tools for', 'Scroll to the section called "Legal Status of Bitcoin and Crypto Sports Betting", locate the last two words of the first sentence in the second paragraph.': 'Grey area', 'Scroll to the section called "Special & Prediction Markets", locate the words between "results " and "major".': 'stock movements', 'Scroll to the section called "Odds Fairness", locate the two words following "overall" in the last sentence.': 'margin levels', 'Scroll to the section called "Markets at the Crypto and Bitcoin Sports Betting Websites", then to the sub-section "Esports Betting Markets", and locate the first four words of the first sentence.': 'eSports suits crypto bettors', 'Scroll to the section called "Security Features at Asian Betting Sites" and locate the last two words.': 'Bookie betting', 'Scroll to the section called "Traditional & Regional Sports Betting Options", locate the first 2 words.': 'Traditional sports', 'Scroll to the section called "Cricket Betting Options", locate the last two words of the final paragraph.': 'Team strategies', 'Scroll to the section called "Are Asian bookies safe?" and locate the third and fourth words.': 'Asian sports', 'Scroll to the section called "Local Payment Systems", locate the first 2 words.': 'Local payment', 'Scroll to the section called "Mobile Betting: Apps and Mobile Sites in Asia", locate the first 2 words.': 'Best online', 'Scroll to the section called "Event-Based Promotions", locate the first two words.': 'Many asian', 'Scroll to the section called "18+ Only. Play Responsibly." and locate the first two words of the second sentence.': 'If gambling', 'Find the section called "Betting Ratings", locate the third and the fourth words.': 'Best betting', 'Scroll to the section called "Security & Safety in Top Crypto Betting Sites", locate the fourth and fifth words of the instruction.': 'Gives you', 'Scroll to the section called "Is Rolletto available in the UK?", locate the fifth and sixth words.': 'available for', 'Scroll to the section called "Most Popular Sports Betting Bonuses for Crypto Bettors", locate the first two words.': 'This overview', 'Scroll to the section called "Our Story", locate the words between "a" and "among" in the description.': 'small project.', 'Scroll to the section called "Popular Sports and Events at Best Asian Betting Sites", locate the last two words.': 'bookies online', 'Scroll to the section called "Asian Bookmakers: What to Expect?", locate the words between "and" and "accounts" in the Mobile compatibility point.': 'manage their', 'Scroll to the section called "Bonuses & Promotions at the Best Sports Betting Sites Reviewe", locate the last two words.': 'betting sites', 'Scroll to the section called "Stay Updated About Sports Betting" and locate the first two words.': 'Free expert', 'Find the section called "Let`s keep in touch", locate the first field and copy the name of it (1 word).': 'Name', 'Scroll to the section called "How do I create an account on Vave?" and locate the third and fourth words.': 'Create an', 'Find the section called "About SmartBettingGuide", locate the first 2 words in the third sentence.': 'We also', 'Scroll to the section called "Customer Support", locate the last two words of the section.': 'handles them.', 'Scroll to the section called "Bonuses Betting Requirements in the Best Cryptocurrency Betting Sites", locate the last two words of the first sentence.': 'bonus abuse.', 'Scroll to the section called "Responsible Gambling at the Top Crypto Betting Sites", locate the last two words of the description in the Crypto-specific support bullet.': 'these issues.', 'Scroll to the section called "Security & Safety in Top Crypto Betting Sites", locate the first two words of the description in the Move larger winnings bullet.': 'Hardware wallets', 'Scroll to the section called "Pros and Cons of Crypto Betting Sites", then to the sub-section "Pros", locate the last three words of the second point.': 'or card charges.', 'Scroll to the section called "Supported Cryptocurrencies & Payment Methods", then to the sub-section "Fiat On-Ramps (Card, Bank Transfer)", locate the third and fourth words of the last sentence.': 'often have', 'Scroll to the section called "Markets at the Crypto and Bitcoin Sports Betting Websites", locate the words between "of" and "as".': 'sports markets,', 'Scroll to the section called "Are crypto betting sites legal?", and locate the third and fourth words.': 'sites legality', 'Scroll to the section called "How We Test & Review the Top Crypto Betting Sites", then to the sub-section "Range of Markets", and locate the first two words of the last sentence.': 'We evaluate', 'Scroll to the section called "Understanding Betting Odds at Top Sites for Sports Betting", locate the first two words.': 'Betting Odds', 'Scroll to the section called "Fixed Betting", locate the first two words.': 'Fixed betting', 'Scroll to the section called "Bankroll Growth Strategy", locate the first two words.': 'Consider employing', 'Scroll to the section called "Interface Quality & Mobile Betting Platforms", locate the last two words.': 'betting markets.', 'Scroll to the section called "Top 10 Best Crypto Betting Sites by Categories", locate the last two words of the http://BC.Game description.': 'Loyalty Program', 'Scroll to the section called "Asian Betting Features", locate the words between "a" and "with" in the Localized Emphasis point.': 'deep connection', 'Scroll to the section called "Range of Markets", locate the first two words.': 'We first', 'Scroll to the section called "Odds Fairness", locate the words between "the" and "margins" in the last sentence.': 'operator keeps', 'Scroll to the section called "Bonuses Betting Requirements in the Best Cryptocurrency Betting Sites", locate the last two words of the bolded phrase in the Watch the expiry timer bullet point.': 'expiry timer', 'Scroll to the section called "Special & Prediction Markets", locate the words between "traditional" and "making" in the second sentence.': 'novelty bets,', 'Scroll to the section called "Security & Safety in Top Crypto Betting Sites", locate the second and third words.': 'betting sites', 'Scroll to the section called "Understanding Asian Betting Odds & Markets", locate the first two words.': 'Asia sports', 'Scroll to the section called "How Do We Rate Top Asian Betting Sites?", then to the sub-section "Betting Markets", and locate the last two words.': 'major competitions.', 'Scroll to the section called "Regulatory Compliance, Protection & Reliability", locate the first two words.': 'We support', 'Scroll to the section called "Fiat On-Ramps (Card, Bank Transfer)", locate the second and third words of the last sentence.': 'card purchases', 'Scroll to the section called Strategies & Bankroll Management When Betting with Crypto, then to the sub-section Hedge With Stablecoins, and locate the first word of the third sentence.': 'most', 'Scroll to the section called Live Betting and Streaming Capabilities, locate the first two words.': 'Live betting', 'Scroll to the section called Data Research, locate the first 2 words.': 'We collect', 'Scroll to the section called Range of Markets, locate the words between "smaller " and "plus" in the description.': 'niche events,', 'Scroll to the section called Your Trusted Guide to Online Sports Betting, locate the first 2 words.': 'expert reviews', 'Scroll to the section called How do I create an account on Vave? and locate the third and fourth words.': 'Create an', 'Scroll to the section called Monthly Readers, locate the words between "and" and "about" in the description.': 'relevant content', 'Scroll to the section called Our Vision, locate the first 2 words.': 'to be', 'Scroll to the section called Legal Status of Bitcoin and Crypto Sports Betting, locate the last two words of the first sentence.': 'you live.', 'Scroll to the section called Most Popular Sports Betting Bonuses for Crypto Bettors, locate the words between "During" and "or" in the Price Boost Multipliers row.': 'special events', 'Scroll to the section called Popular Sports and Events at Best Asian Betting Sites, locate the fifth and sixth words.': 'influenced by', 'Scroll to the section called Understanding Legal Aspects, locate and copy the first 2 words.': 'While many', 'Scroll to the section called Honest Rating, locate the first 2 words.': 'We deliver', 'Scroll to the section called Free spins, locate the last 2 words.': 'svenska licensen', 'Scroll to the section called Migliori Casino non AAMS App, locate the first 2 words.': 'Abbiamo già', 'Scroll to the section called Popular betting markets for Asian football bookies, locate the sub section called Both Teams to Score (BTTS) and find the first 2 words in it (after a colon).': 'You bet', 'Scroll to the section called Conclusiones, locate the first 2 words.': 'A lo', 'Scroll to the section called Written Articles and find the last 2 words.': 'and engaged', 'Scroll to the section called Payment Methods at Bitcoin and Crypto Casinos, locate the first 2 words in this section.': 'Bitcoin is', 'Find the section called Top Tipsters and locate the first tipster.': 'pariskk25', 'Find the section called Everything About Sports Betting! and locate the last 2 words.': 'your inbox.', 'Find the section called Our Experts and locate the first 2 words in it.': 'Our team', 'Scroll to the section called Pros and Cons of Crypto Betting Sites, then to the sub-section Pros, locate the first three words of the last point.': 'Crypto bonuses tend', 'Scroll to the section called Traditional Sports Markets, locate the words between "like" and "usually" in the second paragraph.': 'player props', 'Scroll to the section called Major Cryptocurrencies (BTC, ETH, LTC), locate the last two words of the second sentence.': 'confirmation times', 'Scroll to the section called Blockchain Payments, locate the fifth and sixth words of the first sentence.': 'the most', 'Scroll to the section called Cashback Offers, locate the last two words of the first paragraph.': 'over time', 'Scroll to the section called Best Poker Rooms in 2026, locate the last 2 words.': 'good rakeback', 'Scroll to the section called Security Practices, locate the last word.': 'List', 'Scroll to the section called Hours of Hands-on Testing, locate the first 2 words.': 'We spend', 'Scroll to the section called Transaction Methods & Withdrawal Efficiency, locate the first two words.': 'Funding your', 'Scroll to the section called Follow predictions and locate the last two words of the first sentence.': 'picks instantly.', 'Scroll to the section called Discover tipsters and locate the last two words.': 'your interests.', 'Scroll to the section called Welcome Packages, locate the last two words.': 'the bonus.', 'Scroll to the section called Bet Early or Very Late, Not in Between, locate the last 2 words.': 'information appears', 'Scroll to the section called Pros and Cons of Crypto Betting Sites, locate the last two words of the third bullet point under Pros.': 'strict limits.', 'Scroll to the section called Crypto-Based Markets, locate the words between "exact" and "at" in the last sentence.': 'market price', 'Scroll to the section called Phone Screening step, locate the first two words of the description.': 'After reviewing', 'Scroll to the section called Are cryptocurrencies available at the Megapari bookmaker?, locate the last two words.': 'and USDC.', 'Scroll to the section called How to Choose Good Crypto Sports Betting Bookmakers, then to the subheading Betting Offers, and locate the first three words.': 'Just like when', 'Scroll to the section called Martingale Strategy, locate the first two words.': 'The Martingale', 'Scroll to the section called Best Bookmaker Selection, locate the first two words.': 'Choose online', 'Scroll to the section called How fast are withdrawals with 22bet online betting?, locate the first 2 words.': '22bet processes', 'In the account description find the last two words of the first sentence.': 'crypto betting.', 'Scroll to the section called Types of Top Telegram Groups Offering Sports Betting Tips and locate the first two words.': 'Different best', 'Scroll to the section called Cons, locate the last two words.': 'Anjouan license', 'Find Welcome to http://Pokeriomokykla.com section and locate the first 2 words.': 'Pokeriomokykla.com is', 'Scroll to the section called Scam Offers & Responsible Betting Among Telegram Tipsters, and locate the fourth and fifth words of the first sentence.': 'can be', 'Scroll to the section called Asian Bookmakers: What to Expect?, locate the words between "and" and "accounts" in the Mobile compatibility point.': 'manage their', 'Scroll to the section called Markets at the Crypto and Bitcoin Sports Betting Websites, then to the sub-section Esports Betting Markets, and locate the first four words of the first sentence.': 'Esports suits crypto bettors', 'Scroll to the section called Track Your Bets, locate the first two words.': 'Maintain a', 'Scroll to the section called Bonuses & Promotions Guide at the Best Crypto Betting Sites, locate the first two words.': 'top crypto', 'Scroll to the section called How We Test & Review the Top Crypto Betting Sites, locate the ninth and tenth words of the first sentence.': 'real player', 'Scroll to the section called Use bonuses, locate the first two words.': 'Taking advantage', 'Scroll to the section called Most Popular Sports Betting Bonuses for Crypto Bettors, locate the words between "after" and "threshold" in the fifth row of the When You Receive It column.': 'hitting a', 'Scroll to the section called Best Crypto Sports Betting Sites: Expert In-Depth Analysis, locate the first word.': 'here', 'Scroll to the section called Popular betting markets for Asian football bookies, locate the third and fourth words of the Half-Time/ Full-Time Result point.': 'both the', 'Scroll to the section called Plan for Fees and Network Speeds, locate the third and fourth words of the second paragraph.': 'deposit or', 'Scroll to the section called Types of Top Telegram Groups Offering Sports Betting Tips, and locate the fifth and sixth words of the first sentence.': 'on Telegram', 'Scroll to the section called Top 10 Best Crypto Betting Sites by Categories, and locate the fourth word.': 'Crypto', 'Scroll to the section called Traditional & Regional Sports Betting Options, locate the first two words.': 'Traditional sports', 'Scroll to the section called Security Practices, locate the last two words of the section.': 'our list.', 'Scroll to the section called Our Story, locate the words between "a" and "among" in the description.': 'small project.', 'Scroll to the section called Security & Safety in Top Crypto Betting Sites, locate the words between "audited" and "so" in the Use only verified DeFi platforms bullet.': 'smart contracts', 'Scroll to the section called Legal Status of Bitcoin and Crypto Sports Betting and locate the first two words.': 'The legal', 'Scroll to the section called Monthly Readers, locate the first 2 words.': 'Each month', 'Scroll to the section called Everything About Sports Betting!, locate the last 2 words.': 'your inbox', 'Scroll to the section called Monthly Readers, locate the last 2 words.': 'betting sites.', 'Scroll to the section called Popular Sports and Events at Best Asian Betting Sites, locate the last two words of the introductory paragraph.': 'live betting', 'Scroll to the section called Bonuses and Betting Requirements in the Best Cryptocurrency Betting Sites, locate the words between "your" and "and" in the Use smaller bets bullet point.': 'balance steady', 'Scroll to the section called Range of Markets, locate the last two words.': 'match times', 'Scroll to the section called Years of Research, locate the first 2 words.': 'With more', 'Scroll to the section called Top 10 Best Crypto Betting Sites by Categories, locate the last two words of the 22Bet description.': 'Best Odds', 'Scroll to the section called Our Mission, locate the words between "clear " and "of" in the second sentence.': 'realistic view', 'Scroll to the section called Hours of Hands-on Testing, locate the first two words of the description.': 'We spend', 'Scroll to the section called What is a No KYC Casino?, locate the first 2 words.': 'A no', 'Scroll to the section called Kan ik met iDEAL betalen bij een casino zonder Cruks?, locate the last 2 words.': 'onze toplijst.', 'Scroll to the section called Snellere registratie zonder DigiD, locate the first 2 words.': 'Bij buitenlandse', 'Scroll to the Phone Screening step, locate the first two words of the description.': 'After reviewing', 'Scroll to the section called Assess Playing Styles & Matchups and locate the last two words.': 'set handicaps', 'Scroll to the section called Esports Betting Markets, locate the words between "FIFA" and "constantly" in the first paragraph.': 'update odds', 'Scroll to the section called Plan for Fees and Network Speeds, locate the last two words of the first paragraph.': 'frequent bettors', 'Scroll to the section called Native Platform Tokens, locate the last two words.': 'faster withdrawals.'}

cursor.execute("""
CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")
db.commit()

def _load_master_faqs():
    # Prefer the external JSON when it exists and is valid; otherwise use the
    # embedded 198-FAQ master. This makes the deployment self-contained.
    if os.path.exists(RESTORE_BACKUP_FILE):
        try:
            with open(RESTORE_BACKUP_FILE, "r", encoding="utf-8") as f:
                backup = json.load(f)
            if isinstance(backup, dict) and backup:
                return backup, "external"
        except Exception as e:
            print(f"External FAQ master could not be loaded: {e}")

    return MASTER_FAQS, "embedded"

def repair_initial_faq_seed_once():
    # This migration repairs the earlier bad deployment where the JSON was
    # missing on Railway and only the 4 DEFAULT_FAQS were inserted.
    #
    # It runs ONLY until the initial 198-master migration succeeds. After that,
    # normal !edit / !del / !add changes remain authoritative and are never
    # resurrected on restart.
    cursor.execute(
        "SELECT value FROM bot_meta WHERE key = 'faq_master_v2_repaired'"
    )
    if cursor.fetchone():
        return

    cursor.execute("SELECT COUNT(*) AS count FROM faqs")
    current_count = cursor.fetchone()["count"]

    # If the database already contains at least the complete 198 master set,
    # don't touch it.
    if current_count >= MASTER_FAQ_COUNT:
        cursor.execute(
            "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
            ("faq_master_v2_repaired", datetime.now().isoformat())
        )
        db.commit()
        print(f"FAQ master migration not needed: database has {current_count} FAQs.")
        return

    backup = _load_master_faqs()
    if not isinstance(backup[0], dict):
        print("FAQ master migration aborted: master data is not a dictionary.")
        return

    master, source = backup
    restored = 0
    skipped = 0

    # Match the master by the exact stored trigger. This preserves all 198
    # source entries even when two source questions normalize to the same text.
    cursor.execute("SELECT trigger FROM faqs")
    existing_exact = {row["trigger"] for row in cursor.fetchall()}

    for trigger, answer in master.items():
        if not isinstance(trigger, str) or not isinstance(answer, str):
            skipped += 1
            continue

        trigger = trigger.strip()
        answer = answer.strip()
        if not trigger or not answer:
            skipped += 1
            continue

        if trigger in existing_exact:
            continue

        try:
            cursor.execute(
                "INSERT INTO faqs (trigger, answer, created_at) VALUES (?, ?, ?)",
                (trigger, answer, datetime.now().isoformat())
            )
            existing_exact.add(trigger)
            restored += 1
        except sqlite3.IntegrityError:
            skipped += 1

    db.commit()

    cursor.execute("SELECT COUNT(*) AS count FROM faqs")
    total = cursor.fetchone()["count"]

    if total >= MASTER_FAQ_COUNT:
        cursor.execute(
            "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
            ("faq_master_v2_repaired", datetime.now().isoformat())
        )
        db.commit()
        print(
            f"FAQ master migration completed from {source}: "
            f"added {restored}, skipped {skipped}, total FAQs: {total}"
        )
    else:
        # Do NOT mark migration complete if something prevented the full
        # master from being restored. It can safely retry on the next restart.
        print(
            f"FAQ master migration incomplete from {source}: "
            f"added {restored}, skipped {skipped}, total FAQs: {total}/"
            f"{MASTER_FAQ_COUNT}"
        )

# Defined before the migration call because the migration uses normalize_text.
def normalize_text(text):
    text = str(text).lower().strip().replace("```", "").replace("`", "")
    replacements = {"\\u2018":"'", "\\u2019":"'", "\\u201c":'"', "\\u201d":'"',
                    "\\u2013":"-", "\\u2014":"-", "\\u00a0":" "}
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"[^\\w\\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())

repair_initial_faq_seed_once()

def word_set(text):
    return set(normalize_text(text).split())


def fuzzy_score(a, b):
    na, nb = normalize_text(a), normalize_text(b)
    sequence = difflib.SequenceMatcher(None, na, nb).ratio()
    wa, wb = word_set(a), word_set(b)
    overlap = len(wa & wb) / max(len(wa), len(wb)) if wa and wb else 0.0
    return sequence * 0.70 + overlap * 0.30, sequence, overlap


def find_faq(question):
    """Return an FAQ only for an exact match after harmless normalization.

    No fuzzy/approximate fallback is used for user questions, preventing an
    unrelated FAQ answer from being returned.
    """
    cursor.execute("SELECT * FROM faqs ORDER BY id")
    faqs = cursor.fetchall()
    nq = normalize_text(question)

    if not nq:
        return None, 0.0

    for faq in faqs:
        if nq == normalize_text(faq["trigger"]):
            return faq, 1.0

    return None, 0.0


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

    normalized = normalize_text(trigger)
    if not normalized:
        return False

    cursor.execute("SELECT trigger FROM faqs")
    if any(normalize_text(row["trigger"]) == normalized for row in cursor.fetchall()):
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

def _matching_faq_rows(trigger):
    normalized = normalize_text(trigger)
    if not normalized:
        return []
    cursor.execute("SELECT * FROM faqs ORDER BY id")
    return [row for row in cursor.fetchall()
            if normalize_text(row["trigger"]) == normalized]

def delete_faq(trigger):
    rows = _matching_faq_rows(trigger)
    if not rows:
        return False
    ids = [row["id"] for row in rows]
    cursor.executemany("DELETE FROM faqs WHERE id = ?", [(faq_id,) for faq_id in ids])
    db.commit()
    return True

def edit_faq(trigger, new_answer):
    new_answer = new_answer.strip()
    if not new_answer:
        return False
    rows = _matching_faq_rows(trigger)
    if not rows:
        return False

    # Keep the newest matching row as the canonical FAQ and remove stale duplicates.
    keep = rows[-1]
    cursor.execute("UPDATE faqs SET answer = ? WHERE id = ?", (new_answer, keep["id"]))
    stale_ids = [(row["id"],) for row in rows[:-1]]
    if stale_ids:
        cursor.executemany("DELETE FROM faqs WHERE id = ?", stale_ids)
    db.commit()
    return True

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

    if message.channel.id != ALLOWED_CHANNEL_ID:
        return

    msg = message.content.strip()

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
