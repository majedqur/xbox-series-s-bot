# Xbox Series S alerts: OpenSooq -> Telegram (runs on GitHub Actions)
import argparse
import getpass
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

ENGLISH_LISTINGS = "https://jo.opensooq.com/en/gaming-consoles/consoles/o37361-xbox/xbox-series-s"
OPENSOOQ_URLS = [
    ENGLISH_LISTINGS + "?search=true&sort_code=recent&Post_type=7511",
    ENGLISH_LISTINGS,
    "https://jo.opensooq.com/ar/العاب-فيديو-وملحقاتها/اجهزة-العاب/o37361-اكس-بوكس/اكس-بوكس-سيريس-اس",
]
LATEST_COUNT = 5

STATE_FILE = Path(__file__).resolve().parent / "state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

SERIES_S = re.compile(
    r"(series\s*s(?!\w))|(س[ي]{0,2}ر[ي]{0,2}[سز]\s*(?:اس|s)(?!\w))|((?<!\w)xss(?!\w))"
)
SERIES_X = re.compile(
    r"(?<!\w)(?:series\s*x|series\s*s[\s/]*x|س[ي]?ر[ي]?[سز]\s*(?:اكس|x))(?!\w)"
)
OS_LINK = re.compile(r"/(?:ar|en)/search/(\d{5,})")
WANTED = re.compile(
    r"(?<!\w)(?:مطلوب(?:ة|ين)?|للشراء|بدور على|بدي اشتري|ابحث عن|ببحث عن|wtb)(?!\w)"
    r"|^(?:مين (?:بلتقي )?عنده|(?:اللي|الي|لي) عنده|(?:للتبديل|للبدل) على|"
    r"wanted|looking (?:to buy|for)|buying)(?!\w)"
)


def normalize(text):
    text = (text or "").lower()
    text = re.sub("[أإآ]", "ا", text).replace("ـ", "")
    text = re.sub(r"[\u064b-\u065f\u0670\u200e\u200f]", "", text)
    text = re.sub(r"[-_/|،,.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_series_s(text):
    title = normalize(text)
    return bool(SERIES_S.search(title) and not SERIES_X.search(title))


def is_sale(text):
    return is_series_s(text) and not WANTED.search(normalize(text))


def tg(method, **params):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/{method}", data=params, timeout=40
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError(f"Telegram {method} failed to connect or returned an invalid response") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data.get('description')}")
    return data["result"]


def send(chat_id, text):
    tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


class AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = {}
        self.current = None
        self.in_title = False
        self.in_price = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            href = attrs.get("href") or ""
            match = OS_LINK.fullmatch(href)
            self.current = match.group(1) if match and match.group(1) not in self.items else None
            if self.current:
                self.items[self.current] = {"href": href, "title": "", "price": ""}
        elif self.current:
            if tag == "h2":
                self.in_title = True
            elif tag == "span" and "text-lg" in (attrs.get("class") or "").split():
                self.in_price = True

    def handle_endtag(self, tag):
        if tag == "a":
            self.current = None
            self.in_title = self.in_price = False
        elif tag == "h2":
            self.in_title = False
        elif tag == "span":
            self.in_price = False

    def handle_data(self, data):
        if self.current and self.in_title:
            self.items[self.current]["title"] += data
        if self.current and self.in_price:
            self.items[self.current]["price"] += data


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
        title = " ".join(a["title"].split())
        if title:
            ads[pid] = {
                "id": "os:" + pid, "title": title, "price": " ".join(a["price"].split()),
                "url": urljoin(base, a["href"]),
            }
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
    for index, url in enumerate(OPENSOOQ_URLS):
        if index:
            time.sleep(3)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            found = parse(r.text, url)
            print("opensooq:", r.status_code, len(found), "ads, html size", len(r.text))
            if r.status_code == 200 and found:
                return found
            errors.append(f"HTTP {r.status_code}: ما لقيت إعلانات قابلة للقراءة")
        except Exception as e:
            print("opensooq error:", e)
            errors.append(str(e)[:150])
    return []


def save(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATE_FILE)


def send_latest():
    sales = fetch([])
    newest = [ad for ad in sales if ad["price"] and is_sale(ad["title"])][:LATEST_COUNT]
    if not newest:
        send(CHAT_ID, "ما قدرت ألاقي عروض بيع Xbox Series S بأسعارها على السوق المفتوح هسا.")
        print("latest ads sent: 0")
        return
    rows = ["🎮 <b>أحدث عروض Xbox Series S في الأردن</b>", "المصدر: السوق المفتوح"]
    for number, ad in enumerate(newest, start=1):
        title = html.escape(ad["title"][:180])
        url = html.escape(ad["url"], quote=True)
        price = html.escape(ad["price"].replace("JOD", "د.أ"))
        rows.append(f'{number}. <a href="{url}">{title}</a> — <b>{price}</b>')
    send(CHAT_ID, "\n\n".join(rows))
    print("latest ads sent:", len(newest))


def check_commands(state):
    try:
        updates = tg(
            "getUpdates", offset=state.get("telegram_offset", 0),
            allowed_updates=json.dumps(["message"]), timeout=0,
        )
    except RuntimeError as error:
        print("Could not read Telegram commands:", error)
        return
    if not updates:
        return
    for update in updates:
        message = update.get("message", {})
        if str(message.get("chat", {}).get("id")) != CHAT_ID:
            continue
        words = message.get("text", "").split()
        if words and words[0].split("@", 1)[0].lower() == "/latest":
            send_latest()
            break
    state["telegram_offset"] = max(update["update_id"] for update in updates) + 1
    save(state)


def check():
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
        rejected = 0
        for ad in ads:
            if not is_sale(ad["title"]):
                rejected += int(is_series_s(ad["title"]))
            elif ad["id"] not in seen:
                seen.add(ad["id"])
                fresh.append(ad)
        if not state["initialized"]:
            send(CHAT_ID, f"✅ البوت شغال. تجاهلت {len(fresh)} إعلان موجود، وببعتلك الجديد.")
            state["seen"].extend(ad["id"] for ad in fresh)
            state["initialized"] = True
        else:
            for ad in fresh:
                text = "🎮 <b>" + html.escape(ad["title"][:200]) + "</b>\n"
                if ad["price"]:
                    text += "💰 " + html.escape(ad["price"]) + "\n"
                send(CHAT_ID, text + html.escape(ad["url"]))
                state["seen"].append(ad["id"])
                save(state)
                time.sleep(1)
        print("new ads:", len(fresh))
        print("wanted or exchange ads skipped:", rejected)

    state["seen"] = state["seen"][-5000:]
    save(state)
    check_commands(state)


def main():
    global TOKEN, CHAT_ID
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", type=int, metavar="MINUTES", help="Run locally at a fixed interval")
    mode.add_argument("--latest", action="store_true", help="Send the latest priced sales to Telegram")
    args = parser.parse_args()
    if args.latest:
        if not TOKEN or not CHAT_ID:
            parser.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required")
        send_latest()
        tg("setMyCommands", commands=json.dumps([
            {"command": "latest", "description": "أحدث إعلانات Xbox Series S وأسعارها"}
        ], ensure_ascii=False))
        return
    if args.watch is None:
        check()
        return
    if args.watch < 2:
        parser.error("--watch requires at least 2 minutes")
    TOKEN = TOKEN or getpass.getpass("Telegram token: ")
    CHAT_ID = CHAT_ID or getpass.getpass("Telegram chat ID: ")
    if not re.fullmatch(r"\d+:[\w-]+", TOKEN) or not re.fullmatch(r"-?\d+", CHAT_ID):
        parser.error("Invalid Telegram token or chat ID format.")
    try:
        while True:
            try:
                check()
            except RuntimeError as error:
                print(error, file=sys.stderr, flush=True)
            time.sleep(args.watch * 60)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
