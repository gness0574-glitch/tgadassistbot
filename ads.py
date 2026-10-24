"""
Ad planner: given a channel / group / bot, produce a ready-to-paste Telegram Ads plan
(ad texts, language, search keywords, shortlist of vetted channels to target).
It does NOT create the campaign: that is done manually at ads.telegram.org.
"""
import asyncio
import html
import json
import os
import re
import statistics
from collections import Counter

from telethon import errors, functions, types

MAX_AD = 160
STOP = set(
    "about their there which would could these those other where while after before "
    "being thing really never every again still might first great today https telegram "
    "channel group join subscribe".split()
)


def clip(text: str, n: int = MAX_AD) -> str:
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    return text[: n - 1].rsplit(" ", 1)[0].rstrip(".,;:- ") + "…"


# ------------------------------------------------------------------ context
async def gather_context(user, entity) -> dict:
    about, kind = "", "channel"
    if isinstance(entity, types.User):
        full = await user(functions.users.GetFullUserRequest(entity))
        about = full.full_user.about or ""
        title = " ".join(filter(None, [entity.first_name, entity.last_name])) or entity.username
        kind = "bot"
    elif isinstance(entity, types.Channel):
        full = await user(functions.channels.GetFullChannelRequest(entity))
        about = full.full_chat.about or ""
        title = entity.title
        kind = "channel" if entity.broadcast else "group"
    else:
        title, kind = getattr(entity, "title", "this group"), "group"

    posts = []
    if kind in ("channel", "group"):
        async for m in user.iter_messages(entity, limit=40):
            if m.message:
                posts.append(m.message[:300])
    return {
        "title": title,
        "kind": kind,
        "about": about,
        "sample": "\n".join(posts)[:3000],
        "username": getattr(entity, "username", None),
    }


def detect_language(text: str) -> str:
    try:
        from langdetect import detect

        return detect(text) if text.strip() else "unknown"
    except Exception:
        return "unknown"


# ------------------------------------------------------------------ plan: Claude or fallback
async def ai_plan(ctx: dict):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=key)
        prompt = (
            "You are helping prepare a Telegram Ads campaign (sponsored messages).\n"
            f"Advertised {ctx['kind']}: {ctx['title']}\n"
            f"Description: {ctx['about']}\n"
            f"Recent content sample:\n{ctx['sample']}\n\n"
            "Return ONLY JSON with keys:\n"
            '"language" (language name of the audience), "topic" (short topic label), '
            '"ad_texts" (3 different ads, each max 160 characters, plain text, no line breaks, '
            "no profanity, no guaranteed-profit or income claims, no clickbait, written in the audience language), "
            '"search_keywords" (3 short keywords or names to search for similar channels).'
        )
        resp = await client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "text", None))
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        return {
            "language": str(data["language"]),
            "topic": str(data["topic"]),
            "ad_texts": [clip(str(t)) for t in data["ad_texts"]][:3],
            "search_keywords": [str(k) for k in data["search_keywords"]][:3],
            "source": "Claude API",
        }
    except Exception:
        return None


def fallback_plan(ctx: dict) -> dict:
    text = f"{ctx['title']} {ctx['about']} {ctx['sample']}"
    words = [w for w in re.findall(r"[^\W\d_]{5,}", text.lower()) if w not in STOP]
    keywords = [w for w, _ in Counter(words).most_common(3)] or [ctx["title"]]
    name = ctx["title"]
    noun = {"bot": "bot", "channel": "channel", "group": "group"}[ctx["kind"]]
    ads = [
        clip(f"{name}: a {noun} about {keywords[0]}. Tap to see what's inside."),
        clip(f"Looking for {keywords[0]}? Check out {name} and join the community."),
        clip(f"New on Telegram: {name}. Useful updates on {', '.join(keywords[:2])}. Try it today."),
    ]
    return {
        "language": detect_language(f"{ctx['about']} {ctx['sample']}"),
        "topic": ", ".join(keywords[:2]),
        "ad_texts": ads,
        "search_keywords": keywords,
        "source": "templates (add ANTHROPIC_API_KEY for better ad copy)",
    }


# ------------------------------------------------------------------ channel shortlist
async def score_channel(user, ch):
    try:
        full = await user(functions.channels.GetFullChannelRequest(ch))
        subs = full.full_chat.participants_count or 0
        if subs < 1000:  # Telegram only shows ads in channels with 1,000+ subscribers
            return None
        views, reacts = [], 0
        async for m in user.iter_messages(ch, limit=30):
            if m.views is None:
                continue
            views.append(m.views)
            reacts += sum(r.count for r in m.reactions.results) if m.reactions else 0
        if len(views) < 5:
            return None
        med = statistics.median(views)
        view_rate = med / subs
        react_rate = reacts / max(sum(views), 1)
        cv = statistics.pstdev(views) / statistics.mean(views)
        score = round(
            100 * (0.60 * min(view_rate / 0.15, 1) + 0.25 * min(react_rate / 0.005, 1) + 0.15 * min(cv / 0.15, 1))
        )
        return {
            "title": ch.title,
            "username": ch.username,
            "subs": subs,
            "view_rate": view_rate,
            "score": score,
        }
    except (errors.FloodWaitError, errors.ChannelPrivateError, errors.RPCError):
        return None


async def find_channels(user, keywords, exclude_id, want: int = 6):
    seen = {}
    for kw in keywords:
        try:
            res = await user(functions.contacts.SearchRequest(q=kw, limit=15))
        except errors.FloodWaitError:
            break
        for c in res.chats:
            if isinstance(c, types.Channel) and c.broadcast and c.username and c.id != exclude_id:
                seen.setdefault(c.id, c)
        await asyncio.sleep(0.5)

    scored = []
    for c in list(seen.values())[:12]:
        s = await score_channel(user, c)
        if s:
            scored.append(s)
        await asyncio.sleep(0.5)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:want]


# ------------------------------------------------------------------ output
async def build_plan(user, entity) -> str:
    ctx = await gather_context(user, entity)
    plan = await ai_plan(ctx) or fallback_plan(ctx)
    channels = await find_channels(user, plan["search_keywords"], entity.id)

    ads_txt = "\n".join(f"{i}. <code>{html.escape(t)}</code> ({len(t)}/160)" for i, t in enumerate(plan["ad_texts"], 1))
    if channels:
        ch_lines = []
        for c in channels:
            tag = "✅ healthy" if c["score"] >= 60 else ("⚠️ doubtful" if c["score"] >= 35 else "🚫 likely inflated")
            ch_lines.append(
                f"• @{c['username']}: {c['subs']:,} subs, view rate {c['view_rate'] * 100:.1f}%, "
                f"score {c['score']}/100 {tag}"
            )
        ch_txt = "\n".join(ch_lines)
    else:
        ch_txt = "No suitable channels (1,000+ subscribers) were found for those keywords. Try /ads with a different link."

    return (
        f"<b>📣 Ad plan for {html.escape(ctx['title'])}</b> ({ctx['kind']})\n"
        f"Copy written by: {html.escape(plan['source'])}\n\n"
        f"<b>Targeting to select on ads.telegram.org</b>\n"
        f"Audience language: <b>{html.escape(plan['language'])}</b>\n"
        f"Topic: <b>{html.escape(plan['topic'])}</b>\n"
        f"Search keywords used: {html.escape(', '.join(plan['search_keywords']))}\n\n"
        f"<b>Ad text options (max 160 characters)</b>\n{ads_txt}\n\n"
        f"<b>Channel shortlist (vetted for real engagement)</b>\n{ch_txt}\n\n"
        f"<b>How to launch (manual)</b>\n"
        f"1. Open ads.telegram.org and log in with your Telegram account.\n"
        f"2. Create the ad: paste one text above and your t.me link in the URL field.\n"
        f"3. Choose the language, topic and the healthy channels above. "
        f"Targeting can't be edited after creation, so double-check it.\n"
        f"4. Set your CPM and daily budget, start small, submit for review.\n\n"
        f"<i>Channel scores are estimates from public engagement. Check Telegram's current ad policies, "
        f"minimum budget and link rules on the ads site before paying.</i>"
    )
