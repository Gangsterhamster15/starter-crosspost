#!/usr/bin/env python3
"""Кросспостинг: новые посты из Telegram-канала -> VK и MAX.

Запускается по расписанию (GitHub Actions). Состояние хранить не нужно:
Telegram держит неподтверждённые обновления до 24 часов, а мы подтверждаем
их только после обработки.
"""
import glob
import html
import mimetypes
import os
import re
import secrets
import ssl
import sys
import tempfile
import time

import requests

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID", "").strip()  # необязательно, например -1001234567890
VK_TOKEN = os.environ.get("VK_TOKEN", "")
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
# VK ID (токен пользователя для фото и видео)
VK_APP_ID = os.environ.get("VK_APP_ID", "").strip()
VK_REDIRECT_URI = os.environ.get("VK_REDIRECT_URI", "").strip()
VK_REFRESH_TOKEN = os.environ.get("VK_REFRESH_TOKEN", "").strip()
VK_DEVICE_ID = os.environ.get("VK_DEVICE_ID", "").strip()
VK_CODE_VERIFIER = os.environ.get("VK_CODE_VERIFIER", "").strip()
GH_PAT = os.environ.get("GH_PAT", "").strip()
MAX_TOKEN = os.environ.get("MAX_TOKEN", "")
MAX_CHAT_ID = os.environ.get("MAX_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# Telegram-id людей, чьи пересылки боту в личку публикуются в VK и MAX (через запятую)
ADMIN_IDS = {
    int(x)
    for x in os.environ.get("TG_ADMIN_IDS", "").replace(";", ",").split(",")
    if x.strip().lstrip("-").isdigit()
}
ADMIN_HINTS = set()
# Слова-команды тестового режима: пересланный следом пост бот только покажет в личке
PREVIEW_WORDS = {"тест", "test", "проверка", "preview", "превью"}
# Добавлять ли ссылку «Полная версия в Telegram», если какой-то файл не перенёсся
ADD_TG_LINK = os.environ.get("ADD_TG_LINK") == "1"  # чаты админов, которым нужно ответить подсказкой

SETTLE_SECONDS = 90  # ждём, пока долетят все части альбома
TG_LIMIT_BYTES = 20 * 1024 * 1024  # лимит скачивания файлов через Bot API
MAX_TEXT_LIMIT = 4000
VK_API_VERSION = "5.199"
VK_MAX_ATTACHMENTS = 10

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TG_TOKEN}"
MAX_API = os.environ.get("MAX_API_URL", "https://platform-api2.max.ru").rstrip("/")


# ───────────────────────── Telegram ─────────────────────────

def tg(method, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def post_link(chat, message_id):
    if not chat or chat.get("type") not in ("channel", "supergroup"):
        return None
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    cid = str(chat["id"])
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None


PENDING_PREVIEW = []  # команды «тест», после которых пересылка ещё не пришла


def collect_posts(updates):
    """Склеивает обновления в посты (альбом = один пост)."""
    posts, groups = [], {}
    preview_cmd = {}  # чат админа -> update_id команды «тест», ждущей следующий пост
    for u in updates:
        m = u.get("channel_post")
        reply_to = None
        preview_from = None
        if m:
            if TG_CHANNEL_ID and str(m["chat"]["id"]) != TG_CHANNEL_ID:
                print("Пост другого канала, пропускаю (не совпадает TG_CHANNEL_ID)")
                continue
            print(f"Получен пост канала {m['message_id']}")
            src_chat, src_msg_id = m["chat"], m["message_id"]
        else:
            # личное сообщение боту: берём только пересылки от админов
            m = u.get("message")
            if not m or m["chat"].get("type") != "private":
                kinds = [k for k in u if k != "update_id"]
                print(f"Обновление типа {kinds} пропущено: это не пост канала и не личное сообщение")
                continue
            if m.get("from", {}).get("id") not in ADMIN_IDS:
                print("Личное сообщение от отправителя, которого нет в TG_ADMIN_IDS, пропускаю"
                      + ("" if ADMIN_IDS else " (список TG_ADMIN_IDS пуст)"))
                continue
            origin = m.get("forward_origin")
            if not origin:
                if (m.get("text") or "").strip().lower().strip("/!.") in PREVIEW_WORDS:
                    print("Команда «тест»: следующий пересланный пост только покажу, без публикации")
                    preview_cmd[m["chat"]["id"]] = (u["update_id"], m.get("date", 0))
                    continue
                print("Сообщение от админа пришло не как пересылка поста, пропускаю")
                ADMIN_HINTS.add(m["chat"]["id"])
                continue
            print(f"Получена пересылка от админа (сообщение {m['message_id']})")
            reply_to = m["chat"]["id"]
            preview_from = (preview_cmd.get(reply_to) or (None,))[0]
            if origin.get("type") == "channel":
                src_chat, src_msg_id = origin["chat"], origin["message_id"]
            else:
                src_chat, src_msg_id = None, m["message_id"]
        gid = m.get("media_group_id")
        key = (m["chat"]["id"], gid) if gid else None
        if key and key in groups:
            p = groups[key]
        else:
            if preview_from is not None:
                preview_cmd.pop(reply_to, None)  # «тест» действует на один пост
            p = {
                "group": bool(gid),
                "first_update": u["update_id"],
                "last_date": 0,
                "message_id": src_msg_id,
                "chat": src_chat,
                "reply_to": reply_to,
                "preview_cmd": preview_from,
                "text": "",
                "entities": [],
                "media": [],
            }
            posts.append(p)
            if key:
                groups[key] = p
        p["last_date"] = max(p["last_date"], m.get("date", 0))

        if m.get("rich_message") and not p["text"]:
            # пост в формате статьи: блоки (абзацы, таблицы, фото...) -> текст + медиа
            text, ents, rich_media = render_rich_message(m["rich_message"])
            p["text"], p["entities"] = text, ents
            p["media"].extend(rich_media)
        elif m.get("text") and not p["text"]:
            p["text"], p["entities"] = m["text"], m.get("entities", [])
        elif m.get("caption") and not p["text"]:
            p["text"], p["entities"] = m["caption"], m.get("caption_entities", [])

        if m.get("photo"):
            best = m["photo"][-1]
            p["media"].append({"kind": "photo", "file_id": best["file_id"], "size": best.get("file_size", 0)})
        elif m.get("video"):
            v = m["video"]
            p["media"].append({"kind": "video", "file_id": v["file_id"], "size": v.get("file_size", 0)})
        elif m.get("animation"):
            v = m["animation"]
            p["media"].append({"kind": "video", "file_id": v["file_id"], "size": v.get("file_size", 0)})
    # «тест» мог прийти сразу после пересылки (комментарий к пересылке): привязываем к ней
    for chat_id, (uid, date) in list(preview_cmd.items()):
        prev = [q for q in posts if q.get("reply_to") == chat_id and q["first_update"] < uid
                and q.get("preview_cmd") is None and 0 <= date - q["last_date"] <= 10]
        if prev:
            prev[-1]["preview_cmd"] = uid
            del preview_cmd[chat_id]
    PENDING_PREVIEW[:] = list(preview_cmd.values())
    return posts


def send_preview(p):
    """Тестовый режим: показывает админу в личке, как пост выйдет в VK и MAX."""
    link = post_link(p["chat"], p["message_id"])
    vk_text, max_html, _ = compose_texts(p["text"], p["entities"], "", link)
    photos = sum(1 for m in p["media"] if m["kind"] == "photo")
    videos = len(p["media"]) - photos
    head = [f"🔎 Тест: пост не опубликован.\nВложения: фото {photos}, видео {videos}."]
    if len(p["media"]) > VK_MAX_ATTACHMENTS:
        head.append(f"⚠️ В VK такой пост не выйдет: больше {VK_MAX_ATTACHMENTS} вложений.")
    big = [m for m in p["media"] if m.get("size", 0) > TG_LIMIT_BYTES]
    if big:
        head.append(f"⚠️ Не выйдет нигде: {len(big)} видео больше 20 МБ.")
    if len(max_html) > MAX_TEXT_LIMIT:
        head.append("⚠️ Текст длиннее лимита MAX, там он будет обрезан.")
    chat = p["reply_to"]

    def send(text, html_mode=False):
        for i in range(0, len(text), 4000):  # лимит сообщения Telegram
            chunk = text[i:i + 4000]
            try:
                if html_mode:
                    tg("sendMessage", chat_id=chat, text=chunk, parse_mode="HTML",
                       link_preview_options={"is_disabled": True})
                else:
                    tg("sendMessage", chat_id=chat, text=chunk,
                       link_preview_options={"is_disabled": True})
            except Exception:  # noqa: BLE001  (например, HTML разрезался посередине тега)
                tg("sendMessage", chat_id=chat, text=chunk)

    send("\n".join(head))
    send("— VK —\n\n" + vk_text)
    send("— MAX —\n\n" + max_html, html_mode=True)
    print(f"Пост {p['message_id']}: отправлен предпросмотр в личку")


def download(file_id, folder, name):
    info = tg("getFile", file_id=file_id)
    ext = os.path.splitext(info["file_path"])[1]
    path = os.path.join(folder, name + ext)
    with requests.get(f"{TG_FILE}/{info['file_path']}", stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)
    return path


# ───────────────────── Форматирование текста ─────────────────────
# Offset и length в Telegram считаются в единицах UTF-16.

def _dec(b):
    return b.decode("utf-16-le", errors="replace")


SCHEME_RE = re.compile(r"^https?://", re.I)
STRIP_SCHEME = os.environ.get("STRIP_SCHEME", "1") != "0"


def strip_scheme(u):
    return SCHEME_RE.sub("", u) if STRIP_SCHEME else u


# ─────────────── Подписи под постами (у каждой площадки своя) ───────────────
SIGNATURE_MARK = "STARTER:"  # с этого слова начинается подпись в исходном посте
LINK_TG = "https://t.me/starter_live"
LINK_VK = "https://vk.com/starterapp"
LINK_MAX = "https://max.ru/id7839494297_biz"

# MAX: слова со вшитыми ссылками
SIGNATURE_MAX = (
    f'STARTER: <a href="{LINK_TG}">Telegram</a> • '
    f'<a href="{LINK_VK}">VK</a> • '
    f'<a href="{LINK_MAX}">MAX</a>'
)
# VK: адреса обычным текстом (VK сам делает их ссылками), без ссылки на себя
SIGNATURE_VK = f"STARTER: {SCHEME_RE.sub('', LINK_TG)} • {SCHEME_RE.sub('', LINK_MAX)}"


def normalize_links(text, entities):
    """Убирает https:// из видимого текста ссылок.

    Голые ссылки (url) превращаются в текстовые, чтобы в MAX они оставались
    кликабельными с полным адресом, даже когда на экране адрес короче.
    Смещения остальных выделений пересчитываются.
    """
    entities = entities or []
    b = text.encode("utf-16-le")
    reps = {}
    for e in entities:
        if e["type"] in ("url", "text_link"):
            s, z = e["offset"], e["offset"] + e["length"]
            label = _dec(b[s * 2:z * 2])
            full = e.get("url") or label
            if strip_scheme(label).rstrip("/") == strip_scheme(full).rstrip("/"):
                new = strip_scheme(label)
                if new != label:
                    reps[(s, z)] = new
    if not reps:
        return text, entities

    ordered = sorted(reps.items())

    def map_pos(p):
        delta = 0
        for (s, z), new in ordered:
            n = len(new.encode("utf-16-le")) // 2
            if z <= p:
                delta += n - (z - s)
            elif s < p < z:
                return s + delta + n
        return p + delta

    pieces, pos = [], 0
    for (s, z), new in ordered:
        pieces.append(b[pos * 2:s * 2])
        pieces.append(new.encode("utf-16-le"))
        pos = z
    pieces.append(b[pos * 2:])
    new_text = _dec(b"".join(pieces))

    new_ents = []
    for e in entities:
        s, z = e["offset"], e["offset"] + e["length"]
        ms, mz = map_pos(s), map_pos(z)
        ne = dict(e, offset=ms, length=mz - ms)
        if e["type"] == "url":
            full = _dec(b[s * 2:z * 2])
            if not SCHEME_RE.match(full):
                full = "https://" + full
            ne = dict(ne, type="text_link", url=full)
        new_ents.append(ne)
    return new_text, new_ents


def to_html(text, entities):
    """Разметка Telegram -> HTML для MAX (жирный, курсив, код, ссылки)."""
    b = text.encode("utf-16-le")
    ents = []
    for e in entities or []:
        t = e["type"]
        if t == "bold":
            tags = ("<b>", "</b>")
        elif t == "italic":
            tags = ("<i>", "</i>")
        elif t == "code":
            tags = ("<code>", "</code>")
        elif t == "pre":
            tags = ("<pre>", "</pre>")
        elif t == "text_link":
            tags = (f'<a href="{html.escape(e["url"], quote=True)}">', "</a>")
        else:
            continue
        s = e["offset"] * 2
        ents.append((s, s + e["length"] * 2, tags[0], tags[1]))
    ents.sort(key=lambda x: (x[0], -x[1]))

    out, stack, pos = [], [], 0

    def seg(a, z):
        return html.escape(_dec(b[a:z]), quote=False)

    def close_until(limit):
        nonlocal pos
        while stack and stack[-1][0] <= limit:
            end, close = stack.pop()
            out.append(seg(pos, end))
            pos = end
            out.append(close)

    for start, end, op, cl in ents:
        close_until(start)
        out.append(seg(pos, start))
        pos = start
        if stack:
            end = min(end, stack[-1][0])  # не даём тегам пересекаться
        out.append(op)
        stack.append((end, cl))
    close_until(len(b))
    out.append(seg(pos, len(b)))
    return "".join(out)


# ───────────────── Статьи Telegram (rich_message) ─────────────────
# В VK и MAX нет таблиц, заголовков и блоков статей, поэтому статья
# превращается в обычный текст с разметкой Telegram (entities), а таблицы —
# в понятные списки. Дальше такой текст идёт тем же путём, что и обычный пост.

# Чем заменять кастомные эмодзи Telegram в VK и MAX (номер эмодзи -> обычный эмодзи).
# Остальные кастомные эмодзи заменяются эмодзи, который предлагает сам Telegram.
CUSTOM_EMOJI_MAP = {
    "5289864378122806873": "💬",  # значок перед автором цитаты
}


def replace_custom_emoji(text, entities):
    """В обычных постах: подставляет эмодзи из CUSTOM_EMOJI_MAP вместо кастомных."""
    targets = sorted(
        (e for e in entities or [] if e.get("type") == "custom_emoji"
         and str(e.get("custom_emoji_id")) in CUSTOM_EMOJI_MAP),
        key=lambda e: e["offset"], reverse=True)
    if not targets:
        return text, entities
    b = text.encode("utf-16-le")
    ents = [dict(e) for e in entities]
    for t in targets:
        new = CUSTOM_EMOJI_MAP[str(t["custom_emoji_id"])].encode("utf-16-le")
        s, z = t["offset"] * 2, (t["offset"] + t["length"]) * 2
        delta = len(new) // 2 - t["length"]
        b = b[:s] + new + b[z:]
        for e in ents:  # сдвигаем выделения, которые идут после или включают замену
            if e["offset"] >= t["offset"] + t["length"]:
                e["offset"] += delta
            elif e["offset"] <= t["offset"] and e["offset"] + e["length"] >= t["offset"] + t["length"]:
                e["length"] += delta
    return b.decode("utf-16-le"), ents


RICH_STYLE = {"bold": "bold", "italic": "italic", "underline": "underline",
              "strikethrough": "strikethrough", "code": "code"}


class RichBuilder:
    def __init__(self):
        self.parts, self.size, self.entities, self.media = [], 0, [], []

    def add(self, s):
        if s:
            self.parts.append(s)
            self.size += len(s.encode("utf-16-le")) // 2

    def styled(self, etype, fn, **extra):
        start = self.size
        fn()
        if self.size > start:
            self.entities.append(dict(type=etype, offset=start, length=self.size - start, **extra))

    def text(self):
        return "".join(self.parts)

    def merge(self, other, sep=""):
        """Дописывает текст другого построителя (с разметкой и медиа)."""
        self.media.extend(other.media)
        if not other.parts:
            return
        self.add(sep)
        shift = self.size
        self.parts.extend(other.parts)
        self.size += other.size
        for e in other.entities:
            self.entities.append(dict(e, offset=e["offset"] + shift))


def rich_plain(rt):
    """RichText -> обычная строка без разметки."""
    b = RichBuilder()
    rich_text(b, rt)
    return b.text().strip()


def rich_text(b, rt):
    if rt is None:
        return
    if isinstance(rt, str):
        b.add(rt)
        return
    if isinstance(rt, list):
        for part in rt:
            rich_text(b, part)
        return
    if not isinstance(rt, dict):
        return
    t = rt.get("type")
    inner = rt.get("text")
    if t == "custom_emoji":  # вместо внутреннего номера эмодзи — обычный эмодзи-заменитель
        inner = CUSTOM_EMOJI_MAP.get(str(rt.get("custom_emoji_id")), rt.get("alternative_text") or "")
    if inner is None:  # редкие типы без поля text: берём подходящую строку, но не id
        inner = next((v for k, v in rt.items() if k != "type" and not k.endswith("id")
                      and isinstance(v, str)), "")
    if t in RICH_STYLE:
        b.styled(RICH_STYLE[t], lambda: rich_text(b, inner))
    elif t == "url" and rt.get("url"):
        b.styled("text_link", lambda: rich_text(b, inner), url=rt["url"])
    else:
        rich_text(b, inner)


def rich_media_item(block):
    t = block.get("type")
    if t == "photo" and block.get("photo"):
        best = block["photo"][-1]
        return {"kind": "photo", "file_id": best["file_id"], "size": best.get("file_size", 0)}
    if t in ("video", "animation"):
        v = block.get(t) or {}
        if v.get("file_id"):
            return {"kind": "video", "file_id": v["file_id"], "size": v.get("file_size", 0)}
    return None


def rich_table(b, table):
    """Таблица -> список без маркеров, строки таблицы разделены пустой строкой.

    С заголовком и 2 столбцами:   Базовый — 990 ₽
    С заголовком и 3+ столбцами:  Базовый
                                  Цена: 990 ₽
                                  Срок: 1 месяц
    Без заголовка:                Базовый — 990 ₽ — 1 месяц
    """
    rows = [[c for c in row if c.get("text") is not None] for row in table.get("cells") or []]
    rows = [r for r in rows if any(rich_plain(c.get("text")) for c in r)]
    if not rows:
        return
    if table.get("caption"):
        b.styled("bold", lambda: rich_text(b, table["caption"]))
        b.add("\n\n")
    header = None
    if len(rows) > 1 and all(c.get("is_header") for c in rows[0]):
        header = [rich_plain(c.get("text")) for c in rows[0]]
        rows = rows[1:]
    wrote = False
    for row in rows:
        cells = [c for c in row if rich_plain(c.get("text"))]
        if not cells:
            continue
        if wrote:
            b.add("\n\n")  # пустая строка между строками таблицы
        wrote = True
        if header and len(row) >= 3:
            b.styled("bold", lambda: rich_text(b, cells[0].get("text")))
            for j, cell in enumerate(row[1:], start=1):
                value = rich_plain(cell.get("text"))
                if not value:
                    continue
                b.add("\n")
                label = header[j] if j < len(header) else ""
                if label:
                    b.add(f"{label}: ")
                rich_text(b, cell.get("text"))
        else:
            for j, cell in enumerate(cells):
                if j:
                    b.add(" — ")
                rich_text(b, cell.get("text"))


def rich_blocks(b, blocks, indent=""):
    """Блоки через пустую строку (во вложенных списках — через перенос)."""
    sep = "\n" if indent else "\n\n"
    wrote = False
    for block in blocks or []:
        part = RichBuilder()
        rich_block(part, block, indent)
        b.merge(part, sep if wrote and part.parts else "")
        wrote = wrote or bool(part.parts)


def rich_block(b, block, indent=""):
    t = block.get("type")
    media = rich_media_item(block)
    if media:
        b.media.append(media)
        cap = block.get("caption") or {}
        if cap.get("text"):
            b.styled("italic", lambda: rich_text(b, cap["text"]))
        return
    if t == "paragraph":
        rich_text(b, block.get("text"))
    elif t == "heading":
        b.styled("bold", lambda: rich_text(b, block.get("text")))
    elif t == "pre":
        b.styled("pre", lambda: rich_text(b, block.get("text")))
    elif t == "footer":
        b.styled("italic", lambda: rich_text(b, block.get("text")))
    elif t == "divider":
        b.add("———")
    elif t == "list":
        for i, item in enumerate(block.get("items") or []):
            if i:
                b.add("\n")
            label = item.get("label") or "•"
            if item.get("has_checkbox"):
                label = "☑" if item.get("is_checked") else "☐"
            b.add(f"{indent}{label} ")
            rich_blocks(b, item.get("blocks"), indent + "   ")
    elif t in ("blockquote", "pull_quote", "pullquote", "expandable_blockquote"):
        b.styled("italic", lambda: rich_blocks(b, block.get("blocks"), indent)
                 if block.get("blocks") else rich_text(b, block.get("text")))
        if block.get("credit"):
            b.add("\n— ")
            rich_text(b, block["credit"])
    elif t == "details":
        b.styled("bold", lambda: rich_text(b, block.get("summary")))
        b.add("\n")
        rich_blocks(b, block.get("blocks"), indent)
    elif t == "table":
        rich_table(b, block)
    elif t in ("collage", "slideshow"):
        for inner in block.get("blocks") or []:
            rich_block(b, inner, indent)
        cap = block.get("caption") or {}
        if cap.get("text"):
            b.styled("italic", lambda: rich_text(b, cap["text"]))
    elif block.get("blocks"):
        rich_blocks(b, block["blocks"], indent)
    elif block.get("text") is not None:
        rich_text(b, block.get("text"))
    # остальное (карты, аудио, кнопки и т.п.) в VK и MAX не переносится


def render_rich_message(rm):
    b = RichBuilder()
    rich_blocks(b, rm.get("blocks"))
    text = b.text().strip("\n")
    n = len(text.encode("utf-16-le")) // 2
    ents, seen = [], set()
    for e in b.entities:
        if e["offset"] >= n:
            continue
        e["length"] = min(e["length"], n - e["offset"])
        key = (e["type"], e["offset"], e["length"])
        if key not in seen:  # одинаковое выделение дважды (жирный в жирном) не нужно
            seen.add(key)
            ents.append(e)
    return text, ents, b.media


def to_vk_text(text, entities):
    """VK не умеет жирный и ссылки-якоря: «слово» со ссылкой -> «слово (https://…)»."""
    b = text.encode("utf-16-le")
    links = sorted(
        (e for e in entities or [] if e["type"] == "text_link"),
        key=lambda e: e["offset"],
        reverse=True,
    )
    for e in links:
        s = e["offset"] * 2
        z = s + e["length"] * 2
        label, url = _dec(b[s:z]), e["url"]
        if strip_scheme(label).rstrip("/") == strip_scheme(url).rstrip("/"):
            rep = label
        else:
            rep = f"{label} ({strip_scheme(url).rstrip('/')})"
        b = b[:s] + rep.encode("utf-16-le") + b[z:]
    return _dec(b)


# ───────────────────────────── VK ─────────────────────────────

VK_ID_URL = "https://id.vk.com/oauth2/auth"
_vk_user_token = None


def mask(value):
    """Скрывает значение в логах GitHub Actions."""
    if value and os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{value}")


def save_secret(name, value):
    """Сохраняет значение в секрет репозитория (нужен секрет GH_PAT)."""
    import subprocess

    if not GH_PAT:
        raise RuntimeError("не задан секрет GH_PAT, не могу сохранить " + name)
    subprocess.run(
        ["gh", "secret", "set", name, "--repo", os.environ["GITHUB_REPOSITORY"]],
        input=value.encode(), check=True, capture_output=True,
        env=dict(os.environ, GH_TOKEN=GH_PAT),
    )


def vk_id_request(data):
    r = requests.post(VK_ID_URL, data=data, timeout=60)
    try:
        d = r.json()
    except ValueError:
        raise RuntimeError(f"VK ID: HTTP {r.status_code} {r.text[:300]}")
    if "error" in d or "access_token" not in d:
        raise RuntimeError(f"VK ID: {d.get('error')}: {d.get('error_description') or d}")
    for k in ("access_token", "refresh_token", "id_token"):
        mask(d.get(k))
    return d


def vk_token():
    """Токен для VK API: токен пользователя через VK ID, если он настроен, иначе ключ сообщества."""
    global _vk_user_token
    if not VK_REFRESH_TOKEN:
        return VK_TOKEN
    if _vk_user_token:
        return _vk_user_token
    d = vk_id_request({
        "grant_type": "refresh_token",
        "refresh_token": VK_REFRESH_TOKEN,
        "client_id": VK_APP_ID,
        "device_id": VK_DEVICE_ID,
        "state": secrets.token_urlsafe(24),
    })
    new_refresh = d.get("refresh_token")
    if new_refresh and new_refresh != VK_REFRESH_TOKEN:
        save_secret("VK_REFRESH_TOKEN", new_refresh)  # VK выдаёт новый refresh-токен взамен старого
    _vk_user_token = d["access_token"]
    print("VK: токен пользователя обновлён")
    return _vk_user_token


def vk_auth(url):
    """Одноразовый обмен кода из ссылки VK ID на токены."""
    from urllib.parse import parse_qs, urlparse

    q = {k: v[0] for k, v in parse_qs(urlparse(url.strip()).query).items()}
    if "payload" in q:  # иногда VK присылает данные одним JSON-параметром
        import json
        q.update(json.loads(q["payload"]))
    code, device_id = q.get("code"), q.get("device_id")
    if not code or not device_id:
        sys.exit("В ссылке нет code или device_id. Скопируйте адрес целиком из адресной строки.")
    mask(code)
    if not VK_CODE_VERIFIER:
        sys.exit("Не задан секрет VK_CODE_VERIFIER")
    d = vk_id_request({
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": VK_CODE_VERIFIER,
        "client_id": VK_APP_ID,
        "device_id": device_id,
        "redirect_uri": VK_REDIRECT_URI,
        "state": q.get("state", ""),
    })
    print(f"VK ID: вход выполнен, пользователь {d.get('user_id')}")
    print(f"Выданные доступы: {d.get('scope')}")
    global _vk_user_token
    _vk_user_token = d["access_token"]
    try:
        g = vk("groups.getById", group_id=vk_group_id(), fields="is_admin")
        g = g.get("groups", g) if isinstance(g, dict) else g
        print(f"Сообщество: «{g[0].get('name')}», вы администратор: {bool(g[0].get('is_admin'))}")
    except Exception as e:  # noqa: BLE001
        print(f"Не удалось проверить сообщество: {e}")
    # проверяем на деле, что токен умеет загружать фото на стену сообщества (ничего не публикует)
    try:
        vk("photos.getWallUploadServer", group_id=vk_group_id())
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Проверка не пройдена: токен не может загружать фото в сообщество ({e}). "
                 "Секреты НЕ сохранены, VK продолжит работать с ключом сообщества.")
    print("Проверка пройдена: токен может загружать фото в сообщество.")
    save_secret("VK_REFRESH_TOKEN", d["refresh_token"])
    save_secret("VK_DEVICE_ID", device_id)
    print("Секреты VK_REFRESH_TOKEN и VK_DEVICE_ID сохранены. Теперь VK публикует с фото и видео.")


def vk(method, **params):
    params.update(access_token=vk_token(), v=VK_API_VERSION)
    r = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=120)
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"VK {method}: {d['error'].get('error_msg')}")
    return d["response"]


_vk_gid = None


def vk_group_id():
    """Числовой id сообщества. Понимает 123, -123, club123, public123, ссылку и короткое имя."""
    global _vk_gid
    if _vk_gid:
        return _vk_gid
    raw = VK_GROUP_ID.strip().rstrip("/").split("/")[-1].lstrip("-")
    m = re.fullmatch(r"(?:club|public|event)?(\d+)", raw)
    if m:
        _vk_gid = m.group(1)
    else:
        res = vk("groups.getById", group_id=raw)
        groups = res.get("groups", res) if isinstance(res, dict) else res
        _vk_gid = str(groups[0]["id"])
        print(f"VK: короткое имя сообщества преобразовано в числовой id {_vk_gid}")
    return _vk_gid


def vk_upload_photo(path):
    srv = vk("photos.getWallUploadServer", group_id=vk_group_id())
    with open(path, "rb") as f:
        up = requests.post(srv["upload_url"], files={"photo": f}, timeout=300).json()
    saved = vk("photos.saveWallPhoto", group_id=vk_group_id(),
               server=up["server"], photo=up["photo"], hash=up["hash"])
    return f"photo{saved[0]['owner_id']}_{saved[0]['id']}"


def vk_upload_video(path, name):
    r = vk("video.save", group_id=vk_group_id(), name=name[:100] or "Видео", wallpost=0)
    with open(path, "rb") as f:
        requests.post(r["upload_url"], files={"video_file": f}, timeout=900).raise_for_status()
    return f"video{r['owner_id']}_{r['video_id']}"


def with_note(text, note, signature):
    """Вставляет заметку перед подписью (или в конец, если подписи нет)."""
    if text.endswith(signature):
        return text[: -len(signature)].rstrip() + "\n\n" + note + "\n\n" + signature
    return text + "\n\n" + note


class Incomplete(Exception):
    """Не все файлы удалось загрузить: пост на площадке не публикуется."""


def post_vk(text, media, link=None):
    """Публикует пост целиком или не публикует совсем."""
    if len(media) > VK_MAX_ATTACHMENTS:
        raise Incomplete(f"в посте {len(media)} фото/видео, а VK принимает не больше "
                         f"{VK_MAX_ATTACHMENTS}. Пост не опубликован")
    atts, failed = [], 0
    title =(text.strip().splitlines() or [""])[0]
    for item in media:
        try:
            if item["kind"] == "photo":
                atts.append(vk_upload_photo(item["path"]))
            else:
                atts.append(vk_upload_video(item["path"], title))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  VK: не удалось загрузить {item['kind']}: {e}")
    if failed:
        raise Incomplete(f"не загрузились файлы ({failed} из {len(media)}), пост не опубликован")
    vk("wall.post", owner_id=-int(vk_group_id()), from_group=1,
       message=text, attachments=",".join(atts))


# ───────────────────────────── MAX ─────────────────────────────

_max_verify = None


def max_verify():
    """Сертификаты для HTTPS-запросов к MAX.

    API MAX требует доверять сертификату Минцифры. Положите скачанные
    сертификаты в корень репозитория с именами russian_trusted_* (любое
    расширение), и скрипт добавит их к обычным.
    """
    global _max_verify
    if _max_verify is not None:
        return _max_verify
    files = sorted(
        f for f in glob.glob("*")
        if os.path.isfile(f)
        and f.lower().replace(" ", "_").replace("-", "_").startswith("russian_trusted")
    )
    if not files:
        print("MAX: файлы сертификатов Минцифры не найдены в корне репозитория. "
              "Скачайте корневой и выпускающий сертификаты с https://www.gosuslugi.ru/crt "
              "и загрузите их рядом с crosspost.py (имя должно начинаться с Russian Trusted).")
        _max_verify = True
        return True
    import certifi

    pems = []
    for name in files:
        with open(name, "rb") as f:
            raw = f.read()
        if b"BEGIN CERTIFICATE" in raw:
            pems.append(raw.decode("ascii", errors="ignore"))
        else:  # сертификат в бинарном формате DER
            pems.append(ssl.DER_cert_to_PEM_cert(raw))
    path = os.path.join(tempfile.gettempdir(), "max_ca_bundle.pem")
    with open(certifi.where(), encoding="utf-8") as base, open(path, "w", encoding="utf-8") as out:
        out.write(base.read() + "\n" + "\n".join(pems))
    print(f"MAX: добавлено сертификатов из файлов: {len(files)}")
    _max_verify = path
    return path


def max_req(method, path, **kw):
    return requests.request(method, MAX_API + path, headers={"Authorization": MAX_TOKEN},
                            timeout=120, verify=max_verify(), **kw)


def short_err(text):
    """Достаёт суть из HTML-страницы ошибки."""
    m = re.search(r"<b>(?:Message|Сообщение)</b>\s*(.*?)</p>", text, re.S | re.I)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()[:300]
    m = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    return (m.group(1).strip() + " | " if m else "") + plain[-300:]


def max_upload(path, kind):
    r = max_req("POST", "/uploads", params={"type": kind})
    r.raise_for_status()
    info = r.json()
    name = os.path.basename(path)
    if "." not in name:
        name += ".mp4" if kind == "video" else ".jpg"
    mime = mimetypes.guess_type(name)[0] or ("video/mp4" if kind == "video" else "image/jpeg")
    with open(path, "rb") as f:
        # как в библиотеках для MAX: multipart, поле data, с именем и типом файла
        up = requests.post(info["url"], files={"data": (name, f, mime)},
                           timeout=900, verify=max_verify())
    if not up.ok and kind == "video":
        print(f"  MAX: обычная загрузка видео не прошла ({up.status_code}: {short_err(up.text)}), "
              "пробую загрузку одним блоком")
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            up = requests.post(info["url"], data=f, timeout=900, verify=max_verify(), headers={
                "Content-Type": "application/x-binary; charset=x-user-defined",
                "Content-Disposition": f'attachment; filename="{name}"',
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            })
    if not up.ok:
        raise RuntimeError(f"загрузка файла: HTTP {up.status_code} {short_err(up.text)}")
    try:
        data = up.json()
    except ValueError:
        data = {}
    token = None
    if kind == "image":  # для картинок токен приходит в ответе на загрузку файла
        try:
            token = next(iter(data["photos"].values()))["token"]
        except Exception:  # noqa: BLE001
            token = None
    token = token or info.get("token") or (data.get("token") if isinstance(data, dict) else None)
    if not token:
        raise RuntimeError(f"не получен токен файла. Ответ /uploads: {str(info)[:200]}; "
                           f"ответ загрузки: {up.text[:200]}")
    return {"type": "image" if kind == "image" else "video", "payload": {"token": token}}


def check_max_chat():
    """Безопасная проверка MAX_CHAT_ID: только читает данные о канале, события бота не трогает."""
    if not MAX_TOKEN:
        print("Не задан секрет MAX_TOKEN")
        return
    try:
        me = max_req("GET", "/me")
    except requests.exceptions.SSLError:
        print("MAX: ошибка проверки сертификата. Нужны сертификаты Минцифры в корне репозитория "
              "(корневой и выпускающий), см. сообщение выше.")
        sys.exit(1)
    print(f"MAX /me: HTTP {me.status_code}")
    if me.ok:
        b = me.json()
        print(f"Бот: {b.get('name') or b.get('first_name')} (@{b.get('username')}), id {b.get('user_id')}")
    else:
        print(me.text[:300])
        return
    if not MAX_CHAT_ID:
        print("Не задан секрет MAX_CHAT_ID: токен рабочий, осталось указать id канала")
        return
    ch = max_req("GET", f"/chats/{MAX_CHAT_ID}")
    print(f"MAX /chats/{MAX_CHAT_ID}: HTTP {ch.status_code}")
    if not ch.ok:
        print(ch.text[:300])
        print("Похоже, id неверный или бот не добавлен в этот канал")
        return
    c = ch.json()
    print(f"Канал: «{c.get('title')}», тип: {c.get('type')}")
    mem = max_req("GET", f"/chats/{MAX_CHAT_ID}/members/me")
    if mem.ok:
        m = mem.json()
        print(f"Бот администратор: {m.get('is_admin')}, права: {m.get('permissions')}")
    else:
        print(f"Не удалось проверить права бота: HTTP {mem.status_code} {mem.text[:200]}")


def find_max_chat():
    """Показывает chat_id чатов и каналов из последних событий бота MAX."""
    r = max_req("GET", "/updates", params={"limit": 100, "timeout": 5})
    print(f"MAX /updates: HTTP {r.status_code}")
    if not r.ok:
        print(r.text[:500])
        return
    found = {}

    def walk(obj, utype):
        if isinstance(obj, dict):
            cid = obj.get("chat_id")
            if isinstance(cid, (int, str)):
                found.setdefault(str(cid), set()).add(utype)
            for v in obj.values():
                walk(v, utype)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, utype)

    for u in r.json().get("updates", []):
        walk(u, u.get("update_type", "?"))
    if not found:
        print("Событий нет. Добавьте бота в канал администратором (или удалите и добавьте заново) "
              "и сразу запустите этот режим ещё раз.")
    for cid, types in found.items():
        print(f"chat_id: {cid}  (события: {', '.join(sorted(types))})")


def post_max(text_html, media, link=None):
    """Публикует пост целиком или не публикует совсем."""
    atts, failed = [], 0
    for m in media:
        try:
            atts.append(max_upload(m["path"], "image" if m["kind"] == "photo" else "video"))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  MAX: не удалось загрузить {m['kind']}: {e}")
    if failed:
        raise Incomplete(f"не загрузились файлы ({failed} из {len(media)}), пост не опубликован")
    body = {"text": text_html, "format": "html"}
    if atts:
        body["attachments"] = atts
    for _ in range(36):  # видео обрабатывается дольше, ждём до ~3 минут
        r = max_req("POST", "/messages", params={"chat_id": MAX_CHAT_ID}, json=body)
        if r.ok:
            return
        if "not.ready" in r.text:  # видео/картинка ещё обрабатываются
            time.sleep(5)
            continue
        raise RuntimeError(f"MAX {r.status_code}: {r.text[:300]}")
    raise RuntimeError("MAX: вложение так и не обработалось")


# ───────────────────────────── основной цикл ─────────────────────────────

def strip_signature(text, entities):
    """Убирает подпись «STARTER: …», если она последняя строка поста.

    Свою подпись для каждой площадки скрипт добавит сам.
    """
    idx = text.rfind(SIGNATURE_MARK)
    if idx == -1:
        return text, entities
    if "\n" in text[idx:].rstrip():  # после метки есть ещё строки — это не подпись
        return text, entities
    if idx > 0 and text[idx - 1] != "\n":  # метка внутри строки
        return text, entities
    new_text = text[:idx].rstrip()
    n = len(new_text.encode("utf-16-le")) // 2
    new_ents = [
        dict(e, length=min(e["length"], n - e["offset"]))
        for e in entities or []
        if e["offset"] < n
    ]
    return new_text, new_ents


SPACE_RE = re.compile(r"[ \t ​⁠﻿]+(?=\n|$)|\r")


def clean_spacing(text, entities):
    """Убирает лишние пустые строки (оставляет не больше одной подряд)
    и пробелы в конце строк. Смещения выделений пересчитываются."""
    drop = [False] * len(text)
    for m in SPACE_RE.finditer(text):  # пробелы перед переносом и в конце текста
        for i in range(m.start(), m.end()):
            drop[i] = True
    newlines = 0
    started = False
    for i, ch in enumerate(text):
        if drop[i]:
            continue
        if ch == "\n":
            if not started:  # переносы в самом начале
                drop[i] = True
                continue
            newlines += 1
            if newlines > 2:  # третий и дальше перенос подряд = лишняя пустая строка
                drop[i] = True
        else:
            newlines = 0
            started = True
    for i in range(len(text) - 1, -1, -1):  # переносы в самом конце
        if drop[i]:
            continue
        if text[i] == "\n":
            drop[i] = True
        else:
            break
    if not any(drop):
        return text, entities

    # карта смещений в единицах UTF-16 (в них Telegram считает offset)
    pos_map, new_pos, out = [], 0, []
    for i, ch in enumerate(text):
        units = len(ch.encode("utf-16-le")) // 2
        for _ in range(units):
            pos_map.append(new_pos)
        if not drop[i]:
            out.append(ch)
            new_pos += units
    pos_map.append(new_pos)

    new_ents = []
    for e in entities or []:
        start = pos_map[min(e["offset"], len(pos_map) - 1)]
        end = pos_map[min(e["offset"] + e["length"], len(pos_map) - 1)]
        if end > start:
            new_ents.append(dict(e, offset=start, length=end - start))
    return "".join(out), new_ents


def join_signature(body, signature):
    return body.rstrip() + "\n\n" + signature if body.strip() else signature


def compose_texts(text, entities, note, link):
    """Возвращает (текст для VK, HTML для MAX, пуст ли пост без подписи)."""
    text, entities = replace_custom_emoji(text, entities)
    text, entities = clean_spacing(text, entities)
    text, entities = strip_signature(text, entities)
    norm_text, norm_ents = normalize_links(text, entities)
    body_vk = to_vk_text(norm_text, norm_ents) + note
    body_max = to_html(norm_text, norm_ents) + html.escape(note, quote=False)
    empty = not body_vk.strip()

    limit = MAX_TEXT_LIMIT - len(SIGNATURE_MAX) - 4
    if len(body_max) > limit:
        cut = html.escape(body_vk[: limit - 300], quote=False)
        tail = f"\n…\nПродолжение: {link}" if link else "…"
        body_max = cut + html.escape(tail, quote=False)

    return join_signature(body_vk, SIGNATURE_VK), join_signature(body_max, SIGNATURE_MAX), empty


def process(p):
    """Публикует пост. Возвращает {площадка: None если ок, иначе причина}."""
    link = post_link(p["chat"], p["message_id"])
    results = {}
    platforms = []
    if (VK_TOKEN or VK_REFRESH_TOKEN) and VK_GROUP_ID:
        platforms.append("VK")
    if MAX_TOKEN and MAX_CHAT_ID:
        platforms.append("MAX")

    with tempfile.TemporaryDirectory() as tmp:
        media, too_big = [], 0
        for i, item in enumerate(p["media"]):
            if item["size"] and item["size"] > TG_LIMIT_BYTES:
                too_big += 1
                continue
            try:
                item["path"] = download(item["file_id"], tmp, f"m{i}")
                media.append(item)
            except Exception as e:  # noqa: BLE001
                if "too big" in str(e).lower():
                    too_big += 1
                else:
                    raise

        print(f"Пост {p['message_id']}: текст {len(p['text'])} симв., медиа {len(p['media'])}")
        if too_big:
            reason = (f"{too_big} видео больше 20 МБ, Telegram не отдаёт такие файлы боту. "
                      "Пост не опубликован нигде")
            print("  " + reason)
            return {name: reason for name in platforms}

        vk_text, max_html, empty = compose_texts(p["text"], p["entities"], "", link)
        if empty and not media:
            print("Пост без текста и медиа, пропускаю")
            return {}
        if DRY_RUN:
            print("--- VK ---\n" + vk_text + "\n--- MAX ---\n" + max_html)
            return {}

        senders = {"VK": lambda: post_vk(vk_text, media, link),
                   "MAX": lambda: post_max(max_html, media, link)}
        for name in platforms:
            try:
                senders[name]()
                results[name] = None
                print(f"  {name}: ок, опубликовано целиком")
            except Exception as e:  # noqa: BLE001
                results[name] = str(e)
                print(f"  {name}: НЕ ОПУБЛИКОВАНО: {e}")
    return results


def notify(p, results):
    """Отвечает админу в личку, если пост пришёл пересылкой."""
    if not p.get("reply_to") or DRY_RUN:
        return
    if all(v is None for v in results.values()):
        text = "✅ Готово, пост опубликован целиком: " + (", ".join(results) or "нет площадок")
    else:
        lines = ["⚠️ Пост опубликован не везде:"]
        for name, err in results.items():
            lines.append(f"{name}: " + ("✅ опубликован" if err is None else f"❌ {err[:200]}"))
        text = "\n".join(lines)
    try:
        tg("sendMessage", chat_id=p["reply_to"], text=text)
    except Exception as e:  # noqa: BLE001
        print(f"Не удалось ответить в личку: {e}")


def main():
    if os.environ.get("VK_AUTH_URL", "").strip():
        vk_auth(os.environ["VK_AUTH_URL"])
        return
    if os.environ.get("CHECK_MAX") == "1":
        check_max_chat()
        return
    if os.environ.get("FIND_MAX_CHAT") == "1":
        find_max_chat()
        return
    if not TG_TOKEN:
        sys.exit("Не задан TG_BOT_TOKEN")
    updates = tg("getUpdates", timeout=0, limit=100, allowed_updates=["channel_post", "message"])
    if not updates:
        print("Новых постов нет")
        return

    print(f"Получено обновлений: {len(updates)}")
    posts = collect_posts(updates)
    print(f"Постов к публикации: {len(posts)}")
    confirm_to = updates[-1]["update_id"]
    failed = False

    # «тест» пришёл, а пересылка за ним ещё нет: подождём её, чтобы случайно не опубликовать
    hold = [uid for uid, date in PENDING_PREVIEW if time.time() - date < SETTLE_SECONDS]
    if hold:
        confirm_to = min(hold) - 1
        print("Жду пересылку после команды «тест», вернусь на следующем запуске")

    for p in posts:
        if hold and p["first_update"] > min(hold):
            break
        if p["group"] and time.time() - p["last_date"] < SETTLE_SECONDS:
            print("Альбом ещё догружается, вернёмся к нему на следующем запуске")
            confirm_to = min(confirm_to, p["first_update"] - 1)
            if p.get("preview_cmd") is not None:
                confirm_to = min(confirm_to, p["preview_cmd"] - 1)  # «тест» сохраняем вместе с альбомом
            break
        if p.get("preview_cmd") is not None:
            try:
                if not DRY_RUN:
                    send_preview(p)
            except Exception as e:  # noqa: BLE001
                print(f"Не удалось отправить предпросмотр: {e}")
            continue
        try:
            results = process(p)
        except Exception as e:  # noqa: BLE001
            print(f"Пост {p['message_id']}: ОШИБКА {e}")
            results = {"Все площадки": f"ошибка: {e}"}
        if any(v is not None for v in results.values()):
            failed = True
        notify(p, results)

    if not DRY_RUN:
        for chat_id in ADMIN_HINTS:
            try:
                tg("sendMessage", chat_id=chat_id, text=(
                    "Это сообщение не опубликовано: нужна именно пересылка поста из канала. "
                    "При пересылке не выбирайте «Скрыть автора» и «Копировать»."
                ))
            except Exception as e:  # noqa: BLE001
                print(f"Не удалось ответить в личку: {e}")
        # подтверждаем обработанное, чтобы не публиковать повторно
        tg("getUpdates", offset=confirm_to + 1, limit=1, timeout=0)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
