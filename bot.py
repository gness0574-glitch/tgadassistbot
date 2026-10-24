"""
Telegram Authenticity Analyzer
Send the bot a link to a channel, group or bot and it replies with an
ESTIMATE of how real the audience is.

Two clients run together:
  * bot  -> the BotFather bot people talk to
  * user -> a normal Telegram account (MTProto) that does the actual scanning
"""
import asyncio
import html
import logging
import os
import re
import statistics
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telethon import TelegramClient, errors, events, functions, types
from telethon.sessions import StringSession

import ads

load_dotenv()
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_SCAN_LIMIT = int(os.getenv("GROUP_SCAN_LIMIT", "5000"))  # members sampled per group
CHANNEL_POST_LIMIT = int(os.getenv("CHANNEL_POST_LIMIT", "100"))  # posts analysed per channel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("analyzer")

USER_SESSION = os.getenv("USER_SESSION", "")  # generated once with generate_session.py (needed on Railway)

bot = TelegramClient(StringSession(), API_ID, API_HASH)
# Scanner account: use a spare account, not your main one
user = TelegramClient(StringSession(USER_SESSION) if USER_SESSION else "user_session", API_ID, API_HASH)

LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(\+[\w-]+|joinchat/[\w-]+|[A-Za-z][\w]{3,})"
    r"|(?<![\w/])@([A-Za-z][\w]{3,})"
)


def fmt(n: float) -> str:
    return f"{int(round(n)):,}"


def pct(part: float, whole: float) -> str:
    return f"{(part / whole * 100):.1f}%" if whole else "0%"


def parse_link(text: str):
    m = LINK_RE.search(text or "")
    if not m:
        return None, None
    token = m.group(1) or m.group(2)
    if token.startswith("+"):
        return "invite", token[1:]
    if token.startswith("joinchat/"):
        return "invite", token.split("/", 1)[1]
    return "public", token


# --------------------------------------------------------------------------- #
# Channels: engagement analysis (member list of a channel is admin-only)
# --------------------------------------------------------------------------- #
async def analyze_channel(entity) -> str:
    full = await user(functions.channels.GetFullChannelRequest(entity))
    subs = full.full_chat.participants_count or 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)  # skip posts still gaining views
    posts = []
    async for m in user.iter_messages(entity, limit=CHANNEL_POST_LIMIT * 2):
        if m.views is None or m.date > cutoff:
            continue
        reactions = sum(r.count for r in m.reactions.results) if m.reactions else 0
        posts.append((m.views, reactions, m.forwards or 0))
        if len(posts) >= CHANNEL_POST_LIMIT:
            break

    title = html.escape(entity.title)
    if not posts or not subs:
        return f"<b>{title}</b>\nNot enough post data to analyse."

    views = [p[0] for p in posts]
    avg_views = statistics.mean(views)
    med_views = statistics.median(views)
    view_rate = med_views / subs
    react_rate = sum(p[1] for p in posts) / max(sum(views), 1)
    cv = statistics.pstdev(views) / avg_views if avg_views else 0  # variation between posts

    # Heuristic score (0-100): view rate 60%, reactions 25%, natural variation 15%
    score = round(
        100
        * (
            0.60 * min(view_rate / 0.15, 1)
            + 0.25 * min(react_rate / 0.005, 1)
            + 0.15 * min(cv / 0.15, 1)
        )
    )
    not_reached = max(subs - med_views, 0)

    flags = []
    if view_rate < 0.03:
        flags.append("Very low view rate: most subscribers never see posts (inactive, muted, bought or bots).")
    if react_rate < 0.0005:
        flags.append("Extremely low reaction rate for this many views.")
    if len(posts) >= 20 and cv < 0.08:
        flags.append("Views are unusually uniform from post to post (can mean paid or automated views).")
    flags_txt = "\n".join(f"- {f}" for f in flags) or "- No strong warning signs found."

    return (
        f"<b>📢 Channel: {title}</b>\n"
        f"Subscribers: <b>{fmt(subs)}</b>\n"
        f"Posts analysed: {len(posts)}\n\n"
        f"<b>Views</b>\n"
        f"Average: {fmt(avg_views)} | Typical (median): {fmt(med_views)}\n"
        f"View rate: <b>{view_rate * 100:.2f}%</b> of subscribers\n"
        f"Reaction rate: {react_rate * 100:.3f}% of views\n\n"
        f"<b>Estimate</b>\n"
        f"Likely active audience: ~{fmt(med_views)} ({pct(med_views, subs)})\n"
        f"Not reaching posts (inactive / bought / bots): ~{fmt(not_reached)} ({pct(not_reached, subs)})\n"
        f"Authenticity score: <b>{score}/100</b>\n\n"
        f"<b>Signals</b>\n{html.escape(flags_txt)}\n\n"
        f"<i>Telegram does not reveal channel subscribers or label views as fake, "
        f"so this is an estimate from public engagement, not proof.</i>"
    )


# --------------------------------------------------------------------------- #
# Groups: scan the visible member list
# --------------------------------------------------------------------------- #
def classify(u: types.User) -> str:
    if u.bot:
        return "bot"
    if u.deleted:
        return "deleted"
    if getattr(u, "scam", False) or getattr(u, "fake", False):
        return "flagged"
    if getattr(u, "premium", False):
        return "real"

    points = 0
    if not u.photo:
        points += 1
    if not u.username:
        points += 1
    st = u.status
    if st is None or isinstance(st, types.UserStatusEmpty):
        points += 2  # "last seen a long time ago" / never
    elif isinstance(st, types.UserStatusLastMonth):
        points += 1
    return "suspicious" if points >= 3 else "real"


async def analyze_group(entity) -> str:
    total = (await user.get_participants(entity, limit=0)).total
    counts = {"bot": 0, "deleted": 0, "flagged": 0, "suspicious": 0, "real": 0}
    scanned = 0
    note = ""
    try:
        async for u in user.iter_participants(entity, limit=GROUP_SCAN_LIMIT):
            counts[classify(u)] += 1
            scanned += 1
    except errors.ChatAdminRequiredError:
        note = "The member list of this group is hidden, so no scan was possible."
    except errors.FloodWaitError as e:
        note = f"Telegram rate limit hit after {scanned} members (wait {e.seconds}s). Showing partial results."

    title = html.escape(getattr(entity, "title", "group"))
    if not scanned:
        return f"<b>👥 Group: {title}</b>\nMembers: <b>{fmt(total)}</b>\n{html.escape(note)}"

    def est(k):  # extrapolate the sample to the full member count
        return counts[k] / scanned * total

    fake_like = est("bot") + est("deleted") + est("flagged") + est("suspicious")
    return (
        f"<b>👥 Group: {title}</b>\n"
        f"Total members: <b>{fmt(total)}</b>\n"
        f"Sampled: {fmt(scanned)} members\n\n"
        f"<b>Estimated breakdown</b>\n"
        f"✅ Likely real: ~{fmt(est('real'))} ({pct(counts['real'], scanned)})\n"
        f"🤖 Bots: ~{fmt(est('bot'))} ({pct(counts['bot'], scanned)})\n"
        f"🗑 Deleted accounts: ~{fmt(est('deleted'))} ({pct(counts['deleted'], scanned)})\n"
        f"🚩 Flagged scam/fake: ~{fmt(est('flagged'))} ({pct(counts['flagged'], scanned)})\n"
        f"⚠️ Suspicious (no photo, no username, long inactive): ~{fmt(est('suspicious'))} "
        f"({pct(counts['suspicious'], scanned)})\n\n"
        f"Total fake / non-real estimate: <b>~{fmt(fake_like)} ({pct(fake_like, total)})</b>\n"
        f"{html.escape(note)}\n"
        f"<i>'Suspicious' is a heuristic. Real people can have no photo or username. "
        f"Groups over ~10k members only expose part of the list to Telegram clients.</i>"
    )


# --------------------------------------------------------------------------- #
# Bots: profile info only (users of a bot are not visible)
# --------------------------------------------------------------------------- #
async def analyze_bot(entity) -> str:
    full = await user(functions.users.GetFullUserRequest(entity))
    fu = full.full_user
    monthly = getattr(fu, "bot_active_users", None)
    about = fu.about or "No description."
    name = html.escape(" ".join(filter(None, [entity.first_name, entity.last_name])))
    return (
        f"<b>🤖 Bot: {name}</b> (@{entity.username})\n"
        f"Monthly users shown by Telegram: <b>{fmt(monthly) if monthly else 'not available'}</b>\n"
        f"Verified: {'yes' if entity.verified else 'no'} | Scam flag: {'YES' if entity.scam else 'no'}\n\n"
        f"About: {html.escape(about[:300])}\n\n"
        f"<i>Telegram does not expose who uses a bot, so real vs fake users cannot be split.</i>"
    )


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
async def analyze_entity(entity) -> str:
    if isinstance(entity, types.User):
        return await analyze_bot(entity) if entity.bot else "That link is a personal account, not a channel, group or bot."
    if isinstance(entity, types.Channel):
        return await analyze_channel(entity) if entity.broadcast else await analyze_group(entity)
    if isinstance(entity, types.Chat):
        return await analyze_group(entity)
    return "Unsupported link type."


async def analyze_link(kind: str, token: str) -> str:
    if kind == "invite":
        res = await user(functions.messages.CheckChatInviteRequest(token))
        if isinstance(res, types.ChatInviteAlready):
            return await analyze_entity(res.chat)
        label = "Channel" if getattr(res, "broadcast", False) else "Group"
        return (
            f"<b>{label}: {html.escape(res.title)}</b>\n"
            f"Members: <b>{fmt(res.participants_count or 0)}</b>\n\n"
            f"This is a private invite link. The scanner account has not joined it, "
            f"so a deeper analysis is not possible. Join with the scanner account and send the link again."
        )
    entity = await user.get_entity(token)
    return await analyze_entity(entity)


HELP = (
    "👋 Send me a link to a <b>channel</b>, <b>group</b> or <b>bot</b> "
    "(e.g. https://t.me/example or @example) and I'll estimate how real its audience is.\n\n"
    "📣 To plan a Telegram Ads campaign, send <code>/ads link</code> and I'll suggest ad text, "
    "language, and vetted channels to target."
)


async def resolve_entity(kind: str, token: str):
    if kind == "invite":
        res = await user(functions.messages.CheckChatInviteRequest(token))
        return res.chat if isinstance(res, types.ChatInviteAlready) else None
    return await user.get_entity(token)


async def handle_ads(event, kind, token):
    status = await event.reply("🧠 Building your ad plan, this can take a minute...")
    try:
        entity = await resolve_entity(kind, token)
        if entity is None:
            result = "That is a private invite link the scanner account hasn't joined, so I can't read it."
        elif isinstance(entity, types.User) and not entity.bot:
            result = "That link is a personal account. Send a channel, group or bot link."
        else:
            result = await ads.build_plan(user, entity)
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
        result = "I couldn't find that username. Check the link and try again."
    except errors.FloodWaitError as e:
        result = f"Telegram rate limit reached. Try again in {e.seconds} seconds."
    except Exception:
        log.exception("ad plan failed")
        result = "Something went wrong while building the ad plan."
    await status.edit(result, parse_mode="html", link_preview=False)


@bot.on(events.NewMessage(incoming=True))
async def handler(event):
    text = event.raw_text or ""
    if text.startswith(("/start", "/help")):
        await event.reply(HELP, parse_mode="html")
        return
    if text.startswith("/ads"):
        kind, token = parse_link(text)
        if not kind:
            await event.reply("Send it like this: <code>/ads https://t.me/yourchannel</code>", parse_mode="html")
            return
        await handle_ads(event, kind, token)
        return
    kind, token = parse_link(text)
    if not kind:
        await event.reply("I couldn't find a Telegram link in that message.\n\n" + HELP, parse_mode="html")
        return

    status = await event.reply("🔎 Analysing, this can take a minute for large groups...")
    try:
        result = await analyze_link(kind, token)
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
        result = "I couldn't find that username. Check the link and try again."
    except errors.InviteHashExpiredError:
        result = "That invite link has expired."
    except errors.InviteHashInvalidError:
        result = "That invite link is invalid."
    except errors.FloodWaitError as e:
        result = f"Telegram rate limit reached. Try again in {e.seconds} seconds."
    except Exception:
        log.exception("analysis failed")
        result = "Something went wrong while analysing that link."
    await status.edit(result, parse_mode="html", link_preview=False)


async def main():
    if USER_SESSION:
        await user.connect()
        if not await user.is_user_authorized():
            raise SystemExit("USER_SESSION is invalid or expired. Run generate_session.py again.")
    else:
        await user.start()  # local use: first run asks for phone number and login code
    await bot.start(bot_token=BOT_TOKEN)
    log.info("Bot is running. Press Ctrl+C to stop.")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
