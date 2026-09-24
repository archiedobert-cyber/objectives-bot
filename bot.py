"""Posts objectives labelled "New" on fut.gg/objectives to a Discord channel via webhook.

Env vars:
  DISCORD_WEBHOOK_URL  webhook to post to (GitHub secret)
  DRY_RUN=1            print what would be posted instead of sending it
  TEST_MODE=1          post every current "New" objective, even if already posted
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.fut.gg"
LIST_URL = f"{BASE}/objectives/"
STATE_FILE = Path("posted.json")  # remembers what's already been posted
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
TEST_MODE = os.environ.get("TEST_MODE") == "1"  # post every current "New" objective, even if already posted

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Links to individual objectives, e.g. /objectives/seasonal/72-weekly-rush-points/
# (the numeric id stops tab pages like /objectives/expiring-soon/ from matching)
OBJECTIVE_HREF = re.compile(
    r"^(?:https://www\.fut\.gg)?/objectives/[a-z0-9-]+/\d+-[a-z0-9-]+/?$", re.I
)
# The badge must be exactly "New" (so a title like "Newcastle Special" won't match)
NEW_BADGE = re.compile(r"^\s*new\s*$", re.I)
GENERIC_REWARD_LABELS = {"item"}  # placeholder label fut.gg shows for some rewards

# Title line shown above the objective cards - edit the text/emojis however you like
HEADER = "# 🎯🆕 **NEW OBJECTIVES ALERT** 🆕🎯\n-# 

# Message posted at the very bottom, after all the objective cards.
# Edit the text/emojis/link however you like, or set it to "" for no footer.
FOOTER = ""

# Role to ping in the footer (pings once per post). Paste the role's ID - numbers
# only, e.g. "123456789012345678" - or leave as "" for no ping.
PING_ROLE_ID = "1551541765327167599"


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def is_generic_image(src):
    src = src.lower()
    return any(
        bit in src
        for bit in ("fut-social", "favicon", "logo", "placeholder", "default-image", "sp.webp")
    )


def clean_image(src):
    if not src or src.startswith("data:"):
        return None
    src = urljoin(BASE, src.strip())
    return None if is_generic_image(src) else src


def image_from_tag(img):
    for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-lazy"):
        image = clean_image(img.get(attr))
        if image:
            return image
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        for item in srcset.split(","):
            image = clean_image(item.strip().split()[0])
            if image:
                return image
    return None


def find_image(card, url):
    """Objective image: first reward image on the card, else the page's social image."""
    for img in card.find_all("img"):
        image = image_from_tag(img)
        if image:
            return image

    try:
        page = BeautifulSoup(get(url), "html.parser")
        for attrs in (
            {"property": "og:image"},
            {"name": "twitter:image"},
        ):
            meta = page.find("meta", attrs=attrs)
            if meta:
                image = clean_image(meta.get("content"))
                if image:
                    return image
    except requests.RequestException as e:
        print(f"Could not fetch objective page for image: {e}")
    return None


def split_card_text(anchor):
    """Card text -> (title, description, rewards). Card order: title, description, rewards."""
    parts = [p for p in anchor.stripped_strings if not NEW_BADGE.match(p)]
    if not parts:
        return None, "", []

    title = parts[0]
    rest = parts[1:]

    description = ""
    # Some objectives have no description - then the next line is already a reward
    if rest and (re.search(r"[.!?]$", rest[0]) or len(rest[0]) > 60):
        description = rest.pop(0)

    rewards, seen = [], set()
    for line in rest:
        key = line.lower()
        if key in seen or key in GENERIC_REWARD_LABELS:
            continue  # rewards show up twice (image label + text), keep one
        seen.add(key)
        rewards.append(line)
    return title, description, rewards


def find_new_objectives(html):
    soup = BeautifulSoup(html, "html.parser")
    anchors = {}
    for a in soup.find_all("a", href=OBJECTIVE_HREF):
        anchors.setdefault(urljoin(BASE, urlparse(a["href"]).path), a)

    if not anchors:
        sys.exit("No objective cards found - the page layout may have changed.")
    print(f"Found {len(anchors)} objectives on the page")

    new = []
    for url, a in anchors.items():
        if not a.find(string=NEW_BADGE):
            continue

        title, description, rewards = split_card_text(a)
        if not title:
            title = url.rstrip("/").split("/")[-1].split("-", 1)[-1].replace("-", " ").title()

        new.append(
            {
                "url": url,
                "title": title,
                "description": description,
                "rewards": rewards,
                "image": find_image(a, url),
            }
        )
    return new


def to_embed(obj):
    description = f"## {obj['title']}"

    if obj["description"]:
        description += "\n\n" + obj["description"]

    if obj["rewards"]:
        description += "\n\n## 🎁 Rewards\n" + "\n".join(obj["rewards"])

    embed = {
        "title": obj["title"][:256],
        "url": obj["url"],
        "description": description.strip()[:4000],
        "color": 0x2ECC71,
    }
    if obj["image"]:
        embed["thumbnail"] = {"url": obj["image"]}
    return embed


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

    posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
    new = [o for o in find_new_objectives(get(LIST_URL)) if TEST_MODE or o["url"] not in posted]
    print(f"{len(new)} new objective(s) to post" + (" (test mode)" if TEST_MODE else ""))
    if not new:
        return

    embeds = [to_embed(o) for o in new]
    if DRY_RUN:
        print(json.dumps(embeds, indent=2, ensure_ascii=False))
        return

    post(embeds)
    STATE_FILE.write_text(json.dumps(sorted(posted | {o["url"] for o in new}), indent=2))


if __name__ == "__main__":
    main()
