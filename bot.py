"""Posts objectives labelled "New" on fut.gg/objectives to a Discord channel via webhook.

Env vars (same names as the SBC bot, so the same workflow file works):
  DISCORD_WEBHOOK_URL  webhook to post to (GitHub secret)
  PING_ROLE_ID         optional role ID to ping after the post (GitHub secret)
  DRY_RUN=1            print what would be posted instead of sending it
  TEST_MODE=1          post every current "New" objective, even if already posted
  TEST_URL=<link>      post just this one objective page, skipping the site scan
"""
import json
import os
import re
import sys
import time
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
# The badge must be exactly "New" (so a title like "Newcastle Special" won't match)
NEW_BADGE = re.compile(r"^\s*new\s*$", re.I)

# Title line shown above the objective cards - edit the text/emojis however you like
HEADER = "# 🚨🎯 **NEW OBJECTIVE ALERT** 🎯🚨"

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
            reward_lines.append(clean_reward(text))
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
    rewards = list(dict.fromkeys(reward_lines))  # same line can appear twice

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
            task_lines.append(f"**{t['name']}:** {desc}")
        else:
            task_lines.append(desc or t["name"])

    return {"tasks": task_lines, "rewards": rewards, "image": image, "is_player_reward": is_player}


def find_title(page, url):
    tag = page.find("h1") or page.find("title")
    title = tag.get_text(" ", strip=True) if tag else url.rstrip("/").split("/")[-1]
    return re.sub(r"\s*-\s*EA SPORTS FC.*$", "", title).strip()


def build_objective(url, page):
    data = parse_objective(page)
    data.update({"url": url, "title": find_title(page, url)})
    return data


def to_embed(obj):
    head = f"## 🎯 {obj['title']}\n[More info]({obj['url']})"

    rewards = ""
    if obj["rewards"]:
        rewards = "\n## 💰 Rewards\n" + "\n".join(f"- {r}" for r in obj["rewards"])

    tasks = ""
    if obj["tasks"]:
        budget = 4000 - len(head) - len(rewards) - 40  # Discord caps the description
        lines, used = [], 0
        for i, line in enumerate(obj["tasks"]):
            used += len(line) + 3
            if used > budget:
                lines.append(f"…and {len(obj['tasks']) - i} more")
                break
            lines.append(f"- {line}")
        tasks = "\n## 📋 Tasks\n" + "\n".join(lines)

    embed = {"description": (head + tasks + rewards).strip()[:4000], "color": 0x3498DB}

    if obj["image"]:
        # Player rewards get the big full-width image at the bottom; anything
        # else gets the smaller thumbnail slot.
        key = "image" if obj["is_player_reward"] else "thumbnail"
        embed[key] = {"url": obj["image"]}

    print("DEBUG EMBED:")
    print(json.dumps(embed, indent=2, ensure_ascii=False))
    return embed


def find_new_objectives(html):
    soup = BeautifulSoup(html, "html.parser")
    groups = {}
    for a in soup.find_all("a", href=OBJ_HREF):
        groups.setdefault(urljoin(BASE, a["href"]), []).append(a)

    if not groups:
        sys.exit("No objective cards found - the page layout may have changed.")
    print(f"Found {len(groups)} objectives on the page")

    new = []
    for url, anchors in groups.items():
        if not any(a.find(string=NEW_BADGE) for a in anchors):
            continue
        try:
            page = BeautifulSoup(get(url), "html.parser")
        except requests.RequestException as e:
            print(f"Could not fetch objective page {url}: {e}")
            continue
        new.append(build_objective(url, page))
    return new


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
