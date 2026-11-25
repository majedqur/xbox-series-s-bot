# Xbox Series S alerts: OpenSooq -> Telegram (runs on GitHub Actions)
import html
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OPENSOOQ_URLS = [
    "https://jo.opensooq.com/ar/find?term=xbox+series+s",
    "https://jo.opensooq.com/ar/find?term=اكس+بوكس+سيريس+اس",
]

STATE_FILE = Path(__file__).resolve().parent / "state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

SERIES_S = re.compile(
    r"(series\s*s(?!\w))|(س[ي]?ر[ي]?[سز]\s*(?:اس|s)(?!\w))|((?<!\w)xss(?!\w))"
)
OS_LINK = re.compile(r"/(?:ar|en)/search/(\d{5,})")


def normalize(text):
    text = (text or "").lower()
    text = re.sub("[أإآ]", "ا", text).replace("ـ", "")
    text = re.sub(r"[-_/|،,.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_series_s(text):
    return bool(SERIES_S.search(normalize(text)))


def tg(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data.get('description')}")
    return data["result"]


def send(chat_id, text):
    tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


class AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = {}
        self.cur = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            m = OS_LINK.search(href)
            self.cur = m.group(1) if m else None
            if m:
                self.items.setdefault(self.cur, {"href": href, "text": ""})

    def handle_endtag(self, tag):
        if tag == "a":
            self.cur = None

    def handle_data(self, data):
        if self.cur:
            self.items[self.cur]["text"] += " " + data


def walk(node, out):
    if isinstance(node, dict):
        title = node.get("title")
        pid = node.get("post_id") or node.get("id")
        prices = [v for k, v in node.items()
                  if "price" in str(k).lower() and isinstance(v, (str, int, float)) and v]
        if isinstance(title, str) and str(pid).isdigit() and any("price" in str(k).lower() for k in node):
            out[str(pid)] = {"title": title, "price": str(prices[0]) if prices else ""}
        for v in node.values():
            walk(v, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, out)


def parse(page, base):
    ads = {}
    p = AnchorParser()
    p.feed(page)
    for pid, a in p.items.items():
        ads[pid] = {"id": "os:" + pid, "title": re.sub(r"\s+", " ", a["text"]).strip(),
                    "price": "", "url": urljoin(base, a["href"])}
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if m:
        try:
            found = {}
            walk(json.loads(m.group(1)), found)
            for pid, info in found.items():
                ad = ads.setdefault(pid, {"id": "os:" + pid, "title": "", "price": "",
                                          "url": urljoin(base, "/ar/search/" + pid)})
                ad["title"] = info["title"] or ad["title"]
                ad["price"] = info["price"]
        except Exception as e:
            print("NEXT_DATA error:", e)
    return list(ads.values())


def fetch(errors):
    ads = []
    for url in OPENSOOQ_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            found = parse(r.text, url)
            print("opensooq:", len(found), "ads, html size", len(r.text))
            ads += found
        except Exception as e:
            print("opensooq error:", e)
            errors.append(str(e)[:150])
        time.sleep(3)
    return ads


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_TOKEN secret is missing")
    if not CHAT_ID:
        for upd in tg("getUpdates"):
            msg = upd.get("message")
            if msg:
                cid = msg["chat"]["id"]
                send(cid, f"رقم المحادثة: <code>{cid}</code>")
        sys.exit("TELEGRAM_CHAT_ID secret is missing: send /start to the bot and re-run")

    state = {"seen": [], "initialized": False, "warned": False}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))

    errors = []
    ads = fetch(errors)
    if not ads:
        if not state["warned"]:
            send(CHAT_ID, "⚠️ ما قدرت أقرأ إعلانات من السوق المفتوح: "
                 + html.escape("; ".join(errors) or "الصفحة بدون إعلانات"))
        state["warned"] = True
    else:
        state["warned"] = False
        seen = set(state["seen"])
        fresh = []
        for ad in ads:
            if ad["id"] not in seen and is_series_s(ad["title"]):
                seen.add(ad["id"])
                state["seen"].append(ad["id"])
                fresh.append(ad)
        if not state["initialized"]:
            send(CHAT_ID, f"✅ البوت شغال. تجاهلت {len(fresh)} إعلان موجود، وببعتلك الجديد.")
            state["initialized"] = True
        else:
            for ad in fresh:
                text = "🎮 <b>" + html.escape(ad["title"][:200]) + "</b>\n"
                if ad["price"]:
                    text += "💰 " + html.escape(ad["price"]) + "\n"
                send(CHAT_ID, text + ad["url"])
                time.sleep(1)
        print("new ads:", len(fresh))

    state["seen"] = state["seen"][-5000:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
