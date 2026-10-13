#!/usr/bin/env python3
"""Кросспостинг: новые посты из Telegram-канала -> VK и MAX.

Запускается по расписанию (GitHub Actions). Состояние хранить не нужно:
Telegram держит неподтверждённые обновления до 24 часов, а мы подтверждаем
их только после обработки.
"""
import glob
import html
import os
import re
import ssl
import sys
import tempfile
import time

import requests

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID", "").strip()  # необязательно, например -1001234567890
VK_TOKEN = os.environ.get("VK_TOKEN", "")
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
MAX_TOKEN = os.environ.get("MAX_TOKEN", "")
MAX_CHAT_ID = os.environ.get("MAX_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# Telegram-id людей, чьи пересылки боту в личку публикуются в VK и MAX (через запятую)
ADMIN_IDS = {
    int(x)
    for x in os.environ.get("TG_ADMIN_IDS", "").replace(";", ",").split(",")
    if x.strip().lstrip("-").isdigit()
}
ADMIN_HINTS = set()  # чаты админов, которым нужно ответить подсказкой

SETTLE_SECONDS = 90  # ждём, пока долетят все части альбома
TG_LIMIT_BYTES = 20 * 1024 * 1024  # лимит скачивания файлов через Bot API
MAX_TEXT_LIMIT = 4000
VK_API_VERSION = "5.199"

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


def collect_posts(updates):
    """Склеивает обновления в посты (альбом = один пост)."""
    posts, groups = [], {}
    for u in updates:
        m = u.get("channel_post")
        reply_to = None
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
                print("Сообщение от админа пришло не как пересылка поста, пропускаю")
                ADMIN_HINTS.add(m["chat"]["id"])
                continue
            print(f"Получена пересылка от админа (сообщение {m['message_id']})")
            reply_to = m["chat"]["id"]
            if origin.get("type") == "channel":
                src_chat, src_msg_id = origin["chat"], origin["message_id"]
            else:
                src_chat, src_msg_id = None, m["message_id"]
        gid = m.get("media_group_id")
        key = (m["chat"]["id"], gid) if gid else None
        if key and key in groups:
            p = groups[key]
        else:
            p = {
                "group": bool(gid),
                "first_update": u["update_id"],
                "last_date": 0,
                "message_id": src_msg_id,
                "chat": src_chat,
                "reply_to": reply_to,
                "text": "",
                "entities": [],
                "media": [],
            }
            posts.append(p)
            if key:
                groups[key] = p
        p["last_date"] = max(p["last_date"], m.get("date", 0))

        if m.get("text") and not p["text"]:
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
    return posts


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
            rep = f"{label} ({strip_scheme(url)})"
        b = b[:s] + rep.encode("utf-16-le") + b[z:]
    return _dec(b)


# ───────────────────────────── VK ─────────────────────────────

def vk(method, **params):
    params.update(access_token=VK_TOKEN, v=VK_API_VERSION)
    r = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=120)
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"VK {method}: {d['error'].get('error_msg')}")
    return d["response"]


def vk_upload_photo(path):
    srv = vk("photos.getWallUploadServer", group_id=VK_GROUP_ID)
    with open(path, "rb") as f:
        up = requests.post(srv["upload_url"], files={"photo": f}, timeout=300).json()
    saved = vk("photos.saveWallPhoto", group_id=VK_GROUP_ID,
               server=up["server"], photo=up["photo"], hash=up["hash"])
    return f"photo{saved[0]['owner_id']}_{saved[0]['id']}"


def vk_upload_video(path, name):
    r = vk("video.save", group_id=VK_GROUP_ID, name=name[:100] or "Видео", wallpost=0)
    with open(path, "rb") as f:
        requests.post(r["upload_url"], files={"video_file": f}, timeout=900).raise_for_status()
    return f"video{r['owner_id']}_{r['video_id']}"


def with_note(text, note, signature):
    """Вставляет заметку перед подписью (или в конец, если подписи нет)."""
    if text.endswith(signature):
        return text[: -len(signature)].rstrip() + "\n\n" + note + "\n\n" + signature
    return text + "\n\n" + note


def post_vk(text, media, link=None):
    """Публикует пост. Возвращает число файлов, которые не удалось загрузить."""
    atts, failed = [], 0
    title = (text.strip().splitlines() or [""])[0]
    for item in media:
        try:
            if item["kind"] == "photo":
                atts.append(vk_upload_photo(item["path"]))
            else:
                atts.append(vk_upload_video(item["path"], title))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  VK: не удалось загрузить {item['kind']}: {e}")
    if failed and link:
        text = with_note(text, f"Полная версия в Telegram: {link}", SIGNATURE_VK)
    vk("wall.post", owner_id=-int(VK_GROUP_ID), from_group=1,
       message=text, attachments=",".join(atts[:10]))
    return failed


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
    files = sorted(glob.glob("russian_trusted_*"))
    if not files:
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


def max_upload(path, kind):
    r = max_req("POST", "/uploads", params={"type": kind})
    r.raise_for_status()
    info = r.json()
    with open(path, "rb") as f:
        up = requests.post(info["url"], files={"data": f}, timeout=900, verify=max_verify())
    up.raise_for_status()
    token = None
    if kind == "image":  # для картинок токен приходит в ответе на загрузку файла
        try:
            token = next(iter(up.json()["photos"].values()))["token"]
        except Exception:  # noqa: BLE001
            token = None
    token = token or info.get("token")
    if not token:
        raise RuntimeError("MAX: не получен токен загруженного файла")
    return {"type": "image" if kind == "image" else "video", "payload": {"token": token}}


def check_max_chat():
    """Безопасная проверка MAX_CHAT_ID: только читает данные о канале, события бота не трогает."""
    if not MAX_TOKEN:
        print("Не задан секрет MAX_TOKEN")
        return
    me = max_req("GET", "/me")
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
    """Публикует пост. Возвращает число файлов, которые не удалось загрузить."""
    atts, failed = [], 0
    for m in media:
        try:
            atts.append(max_upload(m["path"], "image" if m["kind"] == "photo" else "video"))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  MAX: не удалось загрузить {m['kind']}: {e}")
    if failed and link:
        note = html.escape(f"Полная версия в Telegram: {link}", quote=False)
        text_html = with_note(text_html, note, SIGNATURE_MAX)
    body = {"text": text_html, "format": "html"}
    if atts:
        body["attachments"] = atts
    for _ in range(36):  # видео обрабатывается дольше, ждём до ~3 минут
        r = max_req("POST", "/messages", params={"chat_id": MAX_CHAT_ID}, json=body)
        if r.ok:
            return failed
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


def join_signature(body, signature):
    return body.rstrip() + "\n\n" + signature if body.strip() else signature


def compose_texts(text, entities, note, link):
    """Возвращает (текст для VK, HTML для MAX, пуст ли пост без подписи)."""
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
    link = post_link(p["chat"], p["message_id"])
    ok = True
    too_big = False

    with tempfile.TemporaryDirectory() as tmp:
        media = []
        for i, item in enumerate(p["media"]):
            if item["size"] and item["size"] > TG_LIMIT_BYTES:
                too_big = True
                continue
            try:
                item["path"] = download(item["file_id"], tmp, f"m{i}")
                media.append(item)
            except Exception as e:  # noqa: BLE001
                if "too big" in str(e).lower():
                    too_big = True
                else:
                    raise

        note = ""
        if too_big and link:
            note = f"\n\nПолная версия с видео: {link}"

        vk_text, max_html, empty = compose_texts(p["text"], p["entities"], note, link)

        if empty and not media:
            print("Пост без текста и медиа, пропускаю")
            return True

        print(f"Пост {p['message_id']}: текст {len(p['text'])} симв., медиа {len(media)}")
        if DRY_RUN:
            print("--- VK ---\n" + vk_text + "\n--- MAX ---\n" + max_html)
            return True

        if VK_TOKEN and VK_GROUP_ID:
            try:
                bad = post_vk(vk_text, media, link)
                if bad:
                    ok = False
                    print(f"  VK: опубликовано без {bad} файл(ов), см. ошибку выше")
                else:
                    print("  VK: ок")
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  VK: ОШИБКА {e}")
        if MAX_TOKEN and MAX_CHAT_ID:
            try:
                bad = post_max(max_html, media, link)
                if bad:
                    ok = False
                    print(f"  MAX: опубликовано без {bad} файл(ов), см. ошибку выше")
                else:
                    print("  MAX: ок")
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  MAX: ОШИБКА {e}")
    return ok


def notify(p, ok):
    """Отвечает админу в личку, если пост пришёл пересылкой."""
    if not p.get("reply_to") or DRY_RUN:
        return
    text = (
        "✅ Готово, пост опубликован."
        if ok
        else "⚠️ Пост опубликован не везде. Подробности в логе GitHub Actions."
    )
    try:
        tg("sendMessage", chat_id=p["reply_to"], text=text)
    except Exception as e:  # noqa: BLE001
        print(f"Не удалось ответить в личку: {e}")


def main():
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

    for p in posts:
        if p["group"] and time.time() - p["last_date"] < SETTLE_SECONDS:
            print("Альбом ещё догружается, вернёмся к нему на следующем запуске")
            confirm_to = p["first_update"] - 1
            break
        try:
            ok = process(p)
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"Пост {p['message_id']}: ОШИБКА {e}")
        if not ok:
            failed = True
        notify(p, ok)

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
