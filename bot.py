"""Posts objectives labelled "New" on fut.gg/objectives to a Discord channel via webhook.

An objective is posted when it shows the "New" label now but did not show it at
the previous run. So an objective that keeps its label for several days is posted
once, and one that comes back with the label again (e.g. a weekly one) is posted
again. posted.json only remembers which objectives were labelled New last time.

Env vars (same names as the SBC bot, so the same workflow file works):
  DISCORD_WEBHOOK_URL  webhook to post to (GitHub secret)
  PING_ROLE_ID         optional role ID to ping after the post (GitHub secret)
  DRY_RUN=1            print what would be posted instead of sending it
  TEST_MODE=1          post the objectives currently labelled New (or the first few on the page if none), even if already posted
  TEST_URL=<link>      post just this one objective page, skipping the site scan
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE = "https://www.fut.gg"
LIST_URL = f"{BASE}/objectives/"
STATE_FILE = Path("posted.json")  # remembers what's already been posted
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
TEST_MODE = os.environ.get("TEST_MODE") == "1"
TEST_URL = os.environ.get("TEST_URL", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Links to individual objectives, e.g.
# /objectives/campaigns/113-squad-foundations-shunsuke-mito/
# (the numeric id stops category pages like /objectives/expiring-soon/ matching)
OBJ_HREF = re.compile(
    r"^(?:https://www\.fut\.gg)?/objectives/[a-z0-9-]+/\d+-[^/]+/?$"
)
# The "New" label must be exactly "New" (so "Newcastle Special" won't match). It
# can be its own text piece ("New") or stuck onto the end of the title ("PointsNew").
NEW_BADGE = re.compile(r"^\s*new\s*$", re.I)
NEW_SUFFIX = re.compile(r"[a-z0-9)!?.]New$")

# How long to keep re-checking the site for new content (scheduled runs set
# POLL_MINUTES; 0 = check once), and how many seconds between checks.
POLL_MINUTES = float(os.environ.get("POLL_MINUTES") or 0)
POLL_EVERY = 20

# In test mode, how many objectives from the top of the page to post
TEST_LIMIT = 3

# Title line shown above the objective cards - edit the text/emojis however you like
HEADER = "# 🚨 🎯 **NEW OBJECTIVE** 🎯 🚨"

# Message posted at the very bottom, after all the cards. "" = no footer.
FOOTER = ""

# Role to ping after the post. Set as a GitHub secret named PING_ROLE_ID
# (numbers only) or paste the ID here. "" = no ping.
PING_ROLE_ID = os.environ.get("PING_ROLE_ID", "").strip()

# Show the task name in bold before each requirement, e.g.
# "**Winners Circle:** Win 7 matches in any FUT game mode."
# Set to False to show just the requirement text.
SHOW_TASK_NAMES = True


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


IMAGE_ATTRS = (
    "src",
    "data-src",
    "data-original",
    "data-lazy-src",
    "data-lazy",
    "data-image",
    "data-url",
)


def img_source(img):
    """Best image URL an <img> tag carries (src, lazy-load attrs, srcset)."""
    candidates = [img.get(a) for a in IMAGE_ATTRS]
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        for item in srcset.split(","):
            parts = item.strip().split()
            if parts:
                candidates.append(parts[0])
    for src in candidates:
        if not src or src.startswith("data:"):
            continue
        src = urljoin(BASE, src.strip())
        low = src.lower()
        if any(g in low for g in ("fut-social", "favicon", "logo", "placeholder", "default-image")):
            continue
        return src
    return None


def bigger(src):
    """fut.gg serves resized images (…/width=300/…); ask for a larger one."""
    return re.sub(r"width=\d+", "width=600", src) if src else src


NOISE = {"rewards", "reward", "award", "item", "pack", "total"}


def is_noise(text):
    low = text.lower()
    return low in NOISE or bool(re.fullmatch(r"[\d,]+\s*total", low))


def clean_reward(text):
    # "1x 4x 83+ Gold Players Pack" -> "4x 83+ Gold Players Pack"
    return re.sub(r"^\d+x\s+(?=\d+x\b)", "", text, flags=re.I).strip()


def merge_reward_tokens(tokens):
    """The page splits some rewards into separate text pieces ("200" + "SP",
    "1" + "x"). Stick the real ones back together and drop the stray "1x"."""
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if re.fullmatch(r"\d+x", t, re.I) or t.lower() == "x":
            i += 1  # stray multiplier
            continue
        if re.fullmatch(r"[\d,]+", t) and nxt is not None:
            if nxt.lower() == "x":
                after = tokens[i + 2] if i + 2 < len(tokens) else ""
                if not after or re.match(r"\d+x\b", after, re.I):
                    i += 2  # redundant "1 x" before "4x 83+ Gold Players Pack"
                else:
                    out.append(f"{t}x {after}")
                    i += 3
                continue
            out.append(f"{t} {nxt}")  # "200" + "SP" -> "200 SP"
            i += 2
            continue
        out.append(t)
        i += 1
    return [clean_reward(t) for t in out]


def page_text(page):
    """Raw page HTML with escaped JSON quotes un-escaped, for regex searching."""
    return str(page).replace('\\"', '"')


def humanize(seconds):
    if seconds <= 0:
        return ""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours and days < 3:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins and not days:
        parts.append(f"{mins} min{'s' if mins != 1 else ''}")
    return " ".join(parts) or "less than a minute"


def find_expires_in(page):
    """How long the objective is still available, e.g. '6 days', or '' if the
    page doesn't say. Tries fut.gg's own text first, then an end date."""
    html = page_text(page)

    # 1) fut.gg's own "expires in" text (same field the SBC pages use)
    m = re.search(r'"?expiresIn"?:\s*"([^"]+)"', html)
    if m and m.group(1).strip():
        return m.group(1).strip()

    # 2) An end date/time - ISO string or epoch - turned into "time left"
    now = datetime.now(timezone.utc)
    m = re.search(
        r'"?(?:expiresAt|endsAt|endDate|expirationDate|endTime|expires)"?:\s*"(\d{4}-\d{2}-\d{2}T[^"]+)"',
        html,
    )
    if m:
        try:
            end = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            text = humanize((end - now).total_seconds())
            if text:
                return text
        except ValueError:
            pass
    m = re.search(r'"?(?:expiresAt|endsAt|endDate|expirationDate|endTime)"?:\s*(\d{10,13})\b', html)
    if m:
        ts = int(m.group(1))
        ts = ts / 1000 if ts > 10**11 else ts
        text = humanize(ts - now.timestamp())
        if text:
            return text

    # 3) Visible text on the page, e.g. "Expires in 6 days"
    visible = page.get_text(" ", strip=True)
    m = re.search(
        r"(?:expires?|ends?|ending|available for)\s*(?:in)?\s*:?\s*"
        r"(\d+\s*(?:days?|hours?|hrs?|minutes?|mins?|weeks?|months?)"
        r"(?:\s*\d+\s*(?:hours?|hrs?|minutes?|mins?))?)",
        visible,
        re.I,
    )
    if m:
        return m.group(1).strip()

    # Nothing found - print what the page does say about expiry so it can be fixed
    hints = re.findall(r".{0,40}(?:expire|endsAt|endDate|endTime).{0,60}", html, re.I)[:5]
    print("DEBUG no expiry found; nearby text:", hints)
    return ""


def is_player_link(tag):
    return tag is not None and "/players/" in (tag.get("href") or "")


def parse_objective(page):
    """Walk the objective page top to bottom and pull out the tasks, the
    rewards list and the reward image(s).

    Page order: hero (overall reward) -> "Rewards" list -> one <h4> per task
    followed by its requirement text.
    """
    root = page.find("main") or page.body or page
    state = "hero"  # hero -> rewards -> tasks
    hero_player = rewards_player = None
    hero_texts, reward_lines, reward_imgs = [], [], []
    tasks, current, desc_open = [], None, False

    for el in root.descendants:
        if el.find_parent(["script", "style", "nav", "header", "footer"]):
            continue

        if isinstance(el, Tag):
            if el.name == "h4":
                name = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                if name.lower() == "rewards":  # in case the label is a heading
                    state = "rewards"
                    continue
                state = "tasks"
                current = {"name": name, "desc": []}
                tasks.append(current)
                desc_open = True
            elif el.name == "img":
                src = img_source(el)
                if state == "tasks":
                    desc_open = False  # the requirement text ends at the first icon
                    continue
                if is_player_link(el.find_parent("a")):
                    player = {"name": (el.get("alt") or "").strip(), "image": src}
                    if state == "rewards" and not rewards_player:
                        rewards_player = player
                    elif state == "hero" and not hero_player:
                        hero_player = player
                elif state == "rewards" and src:
                    reward_imgs.append(src)
            continue

        if not isinstance(el, NavigableString) or el.find_parent("h4"):
            continue
        text = re.sub(r"\s+", " ", str(el)).strip()
        if not text:
            continue
        if text.startswith("©"):
            break

        if state == "hero":
            if text.lower() == "rewards":
                state = "rewards"
            else:
                hero_texts.append(text)
        elif state == "rewards":
            if is_noise(text) or is_player_link(el.find_parent("a")):
                continue
            reward_lines.append(text)
        elif current is not None and desc_open:
            if is_noise(text) or re.match(r"^\d+x\b", text, re.I) or re.match(r"^[\d,]+\s*(sp|coins)\b", text, re.I):
                desc_open = False
            else:
                current["desc"].append(text)

    # Fallback if the page has no "Rewards" label: short reward-looking lines
    # from the top of the page.
    if not reward_lines:
        for t in hero_texts:
            if (
                len(t) <= 60
                and not t.endswith((".", "!"))
                and not is_noise(t)
                and re.search(r"\bSP\b|coins|pack|pick|token|boost|evo", t, re.I)
            ):
                reward_lines.append(clean_reward(t))

    player = rewards_player or hero_player
    rewards = list(dict.fromkeys(merge_reward_tokens(reward_lines)))  # same line can appear twice

    if player and player["name"]:
        rewards = [r for r in rewards if r.lower() != player["name"].lower()]
        rewards.insert(0, f"⭐ {player['name']}")  # overall reward always first

    if player and player["image"]:
        image, is_player = bigger(player["image"]), True
    else:
        non_sp = [i for i in reward_imgs if "sp.webp" not in i]
        image = (non_sp or reward_imgs or [None])[0]
        is_player = False

    task_lines = []
    for t in tasks:
        desc = " ".join(t["desc"]).strip()
        if desc and SHOW_TASK_NAMES:
            task_lines.append(f"**{t['name']}**\n{desc}")
        else:
            task_lines.append(desc or t["name"])

    return {"tasks": task_lines, "rewards": rewards, "image": image, "is_player_reward": is_player}


def find_title(page, url):
    tag = page.find("h1") or page.find("title")
    title = tag.get_text(" ", strip=True) if tag else url.rstrip("/").split("/")[-1]
    return re.sub(r"\s*-\s*EA SPORTS FC.*$", "", title).strip()


def build_objective(url, page):
    data = parse_objective(page)
    data.update({"url": url, "title": find_title(page, url), "expires": find_expires_in(page)})
    return data


def to_embed(obj):
    head = f"## 🆕 {obj['title']}\n[More info]({obj['url']})"

    rewards = ""
    if obj["rewards"]:
        rewards = "\n## 🎁 Rewards\n" + "\n".join(f"- {r}" for r in obj["rewards"])

    expires = f"\n## ⏰ Expires In\n{obj['expires']}" if obj.get("expires") else ""

    tasks = ""
    if obj["tasks"]:
        budget = 4000 - len(head) - len(rewards) - len(expires) - 40  # Discord caps the description
        lines, used = [], 0
        for i, line in enumerate(obj["tasks"]):
            used += len(line) + 2
            if used > budget:
                lines.append(f"…and {len(obj['tasks']) - i} more")
                break
            lines.append(line)
        tasks = "\n## 📋 Tasks\n" + "\n\n".join(lines)

    embed = {"description": (head + tasks + rewards + expires).strip()[:4000], "color": 0x3498DB}

    if obj["image"]:
        # Player rewards get the big full-width image at the bottom; anything
        # else gets the smaller thumbnail slot.
        key = "image" if obj["is_player_reward"] else "thumbnail"
        embed[key] = {"url": obj["image"]}

    print("DEBUG EMBED:")
    print(json.dumps(embed, indent=2, ensure_ascii=False))
    return embed


def card_container(anchor):
    """Walk up from a link until the parent holds more than one objective (= the card)."""
    node = anchor
    while node.parent is not None and node.parent.name not in ("body", "html"):
        hrefs = {a["href"] for a in node.parent.find_all("a", href=OBJ_HREF)}
        if len(hrefs) > 1:
            break
        node = node.parent
    return node


EXPIRES_RE = re.compile(
    r"expires?\s*in\s*(\d+\s*(?:days?|hours?|hrs?|minutes?|mins?|weeks?|months?)"
    r"(?:\s*\d+\s*(?:hours?|hrs?|minutes?|mins?))?)",
    re.I,
)


def card_info(anchor):
    """Is the whole card labelled New (the badge can sit outside the link), and
    what does the card say about expiry ("Expires in 7 days")?"""
    card = card_container(anchor)
    is_new = any(
        NEW_BADGE.match(str(t))
        for t in card.find_all(string=True)
        if not t.find_parent(["script", "style"])
    ) or any(NEW_SUFFIX.search(a.get_text("", strip=True)) for a in card.find_all("a"))
    m = EXPIRES_RE.search(card.get_text(" ", strip=True))
    return {"new": is_new, "expires": m.group(1).strip() if m else "", "text": card.get_text(" ", strip=True)}


def find_objective_links(html):
    """Every objective on the listing page, in page order, as
    {url: {"new": bool, "expires": str}}."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=OBJ_HREF):
        url = urljoin(BASE, a["href"])
        info = card_info(a)
        old = found.get(url)
        if old:
            info["new"] = info["new"] or old["new"]
            info["expires"] = info["expires"] or old["expires"]
        found[url] = info
    if not found:
        sys.exit("No objective cards found - the page layout may have changed.")
    new_count = sum(i["new"] for i in found.values())
    print(f"Found {len(found)} objectives on the page, {new_count} labelled New")
    for url, i in list(found.items())[:3]:
        print(f"DEBUG card {url}: new={i['new']} text={i['text'][:150]!r}")
    return found


def load_state():
    """URLs that were labelled New at the previous run, or None on a first run
    (also None for the old posted-list format, so it starts fresh)."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("new"), list):
        return set(data["new"])
    return None


def save_state(new_urls):
    STATE_FILE.write_text(json.dumps({"new": sorted(new_urls)}, indent=2))


def load_objective(url, expires_hint=""):
    try:
        page = BeautifulSoup(get(url), "html.parser")
    except requests.RequestException as e:
        print(f"Could not fetch objective page {url}: {e}")
        return None
    obj = build_objective(url, page)
    obj["expires"] = obj["expires"] or expires_hint  # fall back to the card's "Expires in 7 days"
    return obj


def post(embeds):
    for i in range(0, len(embeds), 10):  # Discord allows 10 embeds per message
        payload = {"embeds": embeds[i : i + 10]}
        if i == 0:
            payload["content"] = HEADER
        r = requests.post(WEBHOOK, json=payload, timeout=30)
        r.raise_for_status()
        time.sleep(1)

    if FOOTER or PING_ROLE_ID:
        content = f"<@&{PING_ROLE_ID}> {FOOTER}".strip() if PING_ROLE_ID else FOOTER
        # flags=4 stops Discord adding a big link preview under the footer
        payload = {"content": content, "flags": 4}
        if PING_ROLE_ID:  # only this role can be pinged, nothing else
            payload["allowed_mentions"] = {"roles": [PING_ROLE_ID]}
        r = requests.post(WEBHOOK, json=payload, timeout=30)
        r.raise_for_status()


def main():
    if not WEBHOOK and not DRY_RUN:
        sys.exit("DISCORD_WEBHOOK_URL is not set")

    if TEST_URL:
        print(f"TEST_URL set - posting just this one objective: {TEST_URL}")
        page = BeautifulSoup(get(TEST_URL), "html.parser")
        embed = to_embed(build_objective(TEST_URL, page))
        if DRY_RUN:
            print(json.dumps([embed], indent=2, ensure_ascii=False))
        else:
            post([embed])
        return

    if TEST_MODE:
        found = find_objective_links(get(LIST_URL))
        # Prefer objectives labelled New; if none are, use the first few on the page
        new_urls = [u for u, i in found.items() if i["new"]][:TEST_LIMIT] or list(found)[:TEST_LIMIT]
        print(f"Test mode - posting {len(new_urls)} objective(s), ignoring what was posted before")
        new = [o for o in (load_objective(u, found[u]["expires"]) for u in new_urls) if o]
        embeds = [to_embed(o) for o in new]
        if DRY_RUN:
            print(json.dumps(embeds, indent=2, ensure_ascii=False))
        elif embeds:
            post(embeds)
        return

    previous = load_state()
    if previous is None:
        print("First run - posting everything currently labelled New")
        previous = set()

    # New content only appears on the site at release time, so keep checking
    # for up to POLL_MINUTES and post the moment something new turns up.
    deadline = time.time() + POLL_MINUTES * 60
    while True:
        found = find_objective_links(get(LIST_URL))
        labelled = {u for u, i in found.items() if i["new"]}
        new_urls = [u for u in found if u in labelled and u not in previous]
        print(f"{len(new_urls)} objective(s) to post")

        new = [o for o in (load_objective(u, found[u]["expires"]) for u in new_urls) if o]
        if new or time.time() >= deadline:
            break
        print(f"Nothing new yet - checking again in {POLL_EVERY}s")
        time.sleep(POLL_EVERY)

    if new:
        embeds = [to_embed(o) for o in new]
        if DRY_RUN:
            print(json.dumps(embeds, indent=2, ensure_ascii=False))
        else:
            post(embeds)

    if not DRY_RUN:
        save_state(labelled)  # only reached if posting didn't fail


if __name__ == "__main__":
    main()
