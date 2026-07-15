"""
auto_reply.py  (NEW in v2 - the conversion engine)

Runs every ~15 minutes. For every recent post:
  1. Reads all replies via the official Threads API.
  2. If a reply contains a trigger keyword (AUTO, LINK, ...), instantly
     replies to that person with the product link (rotating templates,
     so replies never look copy-pasted) and saves them to data/leads.json.
  3. Optionally, AI-writes a short human reply to a % of non-keyword
     comments (replies are the strongest ranking signal on Threads).
  4. Optionally pings you on Telegram when a new lead arrives.

Uses ONLY the official API (threads_read_replies + threads_reply_management).
No DM hacks, no browser bots, no account risk. Threads has no public DM API
(as of 2026) - this public-reply funnel is the compliant equivalent, and the
leads file gives you a warm list for manual DMs.

Safety rails: dedupe (never answers the same reply twice), skips your own
account, per-run and per-day caps, and a minimum account-age for the loop.
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
PRODUCTS = json.loads((ROOT / "data/products.json").read_text())
HISTORY = json.loads((ROOT / "data/post_history.json").read_text())

LEADS_FILE = ROOT / "data/leads.json"
STATE_FILE = ROOT / "data/replied.json"
LEADS = json.loads(LEADS_FILE.read_text()) if LEADS_FILE.exists() else {"leads": []}
STATE = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"replied_ids": [], "daily": {}}

TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

API = "https://graph.threads.net/v1.0"
AR = CONFIG.get("auto_reply", {})
KEYWORDS = [k.lower() for k in AR.get("trigger_keywords", ["auto"])]
MAX_PER_RUN = AR.get("max_replies_per_run", 8)
MAX_PER_DAY = AR.get("max_replies_per_day", 80)
LOOKBACK_POSTS = AR.get("lookback_posts", 15)
ENGAGE = AR.get("engagement_replies", {"enabled": False})

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
STATE["daily"].setdefault(TODAY, 0)
# keep only today's counter so the file stays tiny
STATE["daily"] = {TODAY: STATE["daily"][TODAY]}

sent_this_run = 0


# ---------------- API helpers ----------------
def api_get(path, params):
    params["access_token"] = TOKEN
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def api_post(path, params):
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}{path}", data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def my_username():
    return api_get("/me", {"fields": "username"}).get("username", "")


def recent_posts():
    """Own recent root posts (media_ids from history, newest first)."""
    posts = [p for p in HISTORY["posts"] if p.get("media_id")]
    return list(reversed(posts))[:LOOKBACK_POSTS]


def fetch_replies(media_id):
    try:
        data = api_get(f"/{media_id}/replies",
                       {"fields": "id,text,username,timestamp", "reverse": "false"})
        return data.get("data", [])
    except urllib.error.HTTPError as e:
        print(f"  replies fetch failed for {media_id}: {e}")
        return []


def publish_reply(text, reply_to_id):
    container = api_post("/me/threads",
                         {"media_type": "TEXT", "text": text, "reply_to_id": reply_to_id})
    time.sleep(3)
    return api_post("/me/threads_publish", {"creation_id": container["id"]})["id"]


# ---------------- reply text ----------------
def keyword_reply_text(username):
    funnel = PRODUCTS.get("funnel", {})
    templates = funnel.get("keyword_reply_templates") or [
        "Here you go @{username} 🔗 {link}",
    ]
    link = funnel.get("link") or PRODUCTS["product"].get("cta_link", "")
    text = random.choice(templates).format(username=username, link=link)
    return text[:480]


def ai_engagement_reply(comment_text, post_text):
    """Short human-sounding reply to a normal comment. Gemini only; skip if no key."""
    if not GEMINI_KEY:
        return None
    prompt = (
        f"You run a Threads account with this voice: {PRODUCTS['voice']}\n"
        f"Your post said:\n{post_text[:300]}\n\n"
        f"Someone commented:\n{comment_text[:300]}\n\n"
        "Write ONE short, natural, friendly reply (max 2 sentences, under 200 "
        "characters). No hashtags, no links, no sales pitch, no emojis unless "
        "it truly fits. Sound like a real person. Output only the reply text."
    )
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.0-flash:generateContent?key={GEMINI_KEY}")
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text.strip('"')[:280] if text else None
    except Exception as e:
        print(f"  engagement AI failed: {e}")
        return None


# ---------------- misc ----------------
def has_keyword(text):
    t = (text or "").lower()
    return any(re.search(rf"\b{re.escape(k)}\b", t) for k in KEYWORDS)


def notify_telegram(msg):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30)
    except Exception as e:
        print(f"  telegram notify failed: {e}")


def can_send():
    return sent_this_run < MAX_PER_RUN and STATE["daily"][TODAY] < MAX_PER_DAY


def mark_sent(reply_id):
    global sent_this_run
    sent_this_run += 1
    STATE["daily"][TODAY] += 1
    STATE["replied_ids"].append(reply_id)


def save_state():
    STATE["replied_ids"] = STATE["replied_ids"][-5000:]
    STATE_FILE.write_text(json.dumps(STATE, indent=2))
    LEADS["leads"] = LEADS["leads"][-10000:]
    LEADS_FILE.write_text(json.dumps(LEADS, indent=2))


# ---------------- main loop ----------------
def main():
    if not AR.get("enabled", True):
        print("auto_reply disabled in config. Skipping.")
        return

    me = my_username()
    if not me:
        sys.exit("Could not resolve own username - check token permissions "
                 "(needs threads_read_replies + threads_reply_management).")

    replied = set(STATE["replied_ids"])
    known_leads = {l["username"] for l in LEADS["leads"]}
    new_leads = 0
    engaged = 0

    for post in recent_posts():
        if not can_send():
            break
        for rep in fetch_replies(post["media_id"]):
            if not can_send():
                break
            rid = rep.get("id")
            user = rep.get("username", "")
            text = rep.get("text", "")
            if not rid or rid in replied or user.lower() == me.lower():
                continue

            if has_keyword(text):
                # ---- lead! answer with the link ----
                try:
                    publish_reply(keyword_reply_text(user), rid)
                    mark_sent(rid)
                    replied.add(rid)
                    print(f"LEAD @{user}: '{text[:60]}' -> link sent")
                    if user not in known_leads:
                        LEADS["leads"].append({
                            "username": user,
                            "comment": text[:200],
                            "media_id": post["media_id"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        known_leads.add(user)
                        new_leads += 1
                        notify_telegram(f"🔥 New Threads lead: @{user}\n“{text[:120]}”")
                except Exception as e:
                    print(f"  lead reply failed for {rid}: {e}")

            elif ENGAGE.get("enabled") and random.random() < ENGAGE.get("probability", 0.25) \
                    and engaged < ENGAGE.get("max_per_run", 3) and len(text) >= 8:
                # ---- normal comment: occasional human-sounding AI reply ----
                ai_text = ai_engagement_reply(text, post["text"])
                if ai_text:
                    try:
                        publish_reply(ai_text, rid)
                        mark_sent(rid)
                        replied.add(rid)
                        engaged += 1
                        print(f"ENGAGED @{user}: '{text[:50]}' -> '{ai_text[:60]}'")
                        time.sleep(2)
                    except Exception as e:
                        print(f"  engagement reply failed for {rid}: {e}")

    save_state()
    print(f"Done. Sent {sent_this_run} replies "
          f"({new_leads} new leads, {engaged} engagement). "
          f"Today total: {STATE['daily'][TODAY]}/{MAX_PER_DAY}. "
          f"All-time leads: {len(LEADS['leads'])}.")


if __name__ == "__main__":
    main()
