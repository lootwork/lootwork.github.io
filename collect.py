#!/usr/bin/env python3
"""
collect.py — сборщик вакансий для витрины.

Ходит по публичным API систем найма, на которых сидят студии (Greenhouse, Lever),
отбирает геймдев-роли, размечает грейд и роль, проверяет живость ссылок
и перезаписывает jobs.js.

Запуск:
    python collect.py --probe     проверить, какие студии из companies.json отвечают
    python collect.py             собрать вакансии и обновить jobs.js

Зависимость одна:  pip install requests
"""

import argparse
import html as html_lib
import os
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Нужен пакет requests. Установи:  pip install requests")

HERE = Path(__file__).parent
COMPANIES = HERE / "companies.json"
OUT_JS = HERE / "jobs.js"
STATUS = HERE / "status.json"
SITEMAP = HERE / "sitemap.xml"
BLOCKLIST = HERE / "blocklist.json"   # сюда попадают id, снятые по жалобам

# hh.ru требует строгий формат подписи: НазваниеПриложения/версия (email).
# Впиши сюда свою почту — иначе hh может начать резать запросы.
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "gamedev.jobs.board@gmail.com")

UA = {"User-Agent": "gamedev-jobs/1.0 (+https://github.com/)"}
HH_UA = {"User-Agent": f"gamedev-jobs/1.0 ({CONTACT_EMAIL})"}
TIMEOUT = 20
PAUSE = 0.4          # пауза между запросами, чтобы не долбить чужие сервера
TRIES = 3            # столько раз пробуем достучаться, прежде чем сдаться
BACKOFF = 2.0        # пауза перед повтором, каждый раз вдвое длиннее


def http_get(url, headers=None, tries=TRIES, timeout=TIMEOUT, **kw):
    """
    Обычный запрос, но с повторами. Обрыв связи, таймаут, 429 и пятисотые —
    это «сервер занят», а не «ничего нет»: ждём и пробуем ещё раз.
    Именно из-за них число вакансий скакало от запуска к запуску.
    """
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, headers=headers or UA, timeout=timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < tries:
                last = f"код {r.status_code}"
            else:
                return r
        except requests.RequestException as e:
            last = str(e)[:60]
            if attempt == tries:
                raise
        time.sleep(BACKOFF * attempt)
    raise requests.RequestException(last or "не отвечает")

# ---------------------------------------------------------------- разметка

# Порядок важен: первое совпадение выигрывает, поэтому более узкие роли выше.
ROLE_RULES = [
    # Технический художник — отдельная профессия, а не «Арт»: это мост между
    # художниками и программистами, шейдеры, пайплайн, оптимизация.
    ("Технический художник", r"technical artist|tech artist|technical art\b|"
                             r"техническ\w* художник|техартист"),
    ("VFX",             r"\bvfx\b|визуальн\w* эффект|particle"),
    ("Анимация",        r"\banimator\b|аниматор|animation|анимац"),
    ("Арт",             r"\bartist\b|художник|\bart\b|concept|конце?пт|3d|2d|texture|environment"),
    ("Звук",            r"sound|audio|звук|композитор|composer"),
    ("Нарратив",        r"narrative|нарратив|сценарист|writer|локализ"),
    ("Геймдизайн",      r"game desig|геймдизайн|гейм-дизайн|level desig|левел|балансировщик|economy desig"),
    ("QA",              r"\bqa\b|тестировщик|test engineer|quality assur"),
    ("Аналитика",       r"аналитик|analyst|analytics|data scien|\bbi\b"),
    ("Маркетинг",       r"marketing|маркетолог|user acquisition|\bua\b|creative producer|asо|\baso\b"),
    ("Поддержка",       r"support|поддержк|community|комьюнити|модератор"),
    # Продакт отвечает за продукт и метрики, продюсер — за производство и сроки.
    # Это разные работы, и ищут их разные люди.
    ("Продакт",         r"product manager|product owner|продакт|продуктов\w* менеджер|"
                        r"head of product|product director|product lead"),
    ("Продюсирование",  r"producer|продюсер|project manager|проектный менеджер"),
    ("Программирование", r"developer|разработчик|программист|engineer|unity|unreal|gameplay|backend|"
                         r"frontend|client|server|\bc\+\+|\bc#|python|golang|техлид|tech lead"),
]

GRADE_RULES = [
    ("Lead",   r"\blead\b|\bhead\b|лид\b|руководител|principal|director|директор"),
    ("Senior", r"\bsenior\b|\bsr\.?\b|сеньор|ведущий|старший"),
    ("Junior", r"\bjunior\b|\bjr\.?\b|джуниор|младший|стажёр|стажер|intern|trainee"),
    ("Middle", r"\bmiddle\b|\bmid\b|мидл"),
]

REMOTE_RE = re.compile(r"remote|удал[её]нн|anywhere|worldwide|from home", re.I)

# Отсекаем вакансии, которые к геймдеву отношения не имеют,
# даже если студия их публикует на той же доске.
REJECT_RE = re.compile(
    r"бухгалтер|accountant|юрист|legal counsel|офис-менеджер|office manager|"
    r"recruiter|рекрутер|hr\b|уборщ|повар|driver|водител",
    re.I,
)


# Заявка в резерв — это не вакансия: места нет, есть приглашение прислать
# резюме на будущее. Не выбрасываем (кому-то полезно), но помечаем.
POOL_RE = re.compile(
    r"talent pool|talent community|candidate pool|open application|general application|"
    r"spontaneous application|speculative|student application|internship opportunity|"
    r"future opportunit|expression of interest|candidature libre|candidature ouverte|"
    r"кадровый резерв|в резерв", re.I)


# Часть студий кладёт в текст вакансии картинки и голые ссылки. Человеку они
# ничего не говорят, а первую строку описания портят — а она идёт в поисковик.
URL_ONLY = re.compile(r"^\s*[\[(]?https?://\S+[\])]?\s*$", re.I)
MD_IMAGE = re.compile(r"!?\[[^\]]*\]\(\s*https?://[^)]+\)")


def strip_media(text: str) -> str:
    if not text:
        return text
    out = []
    for line in text.split("\n"):
        line = MD_IMAGE.sub("", line).strip()
        if URL_ONLY.match(line):
            continue
        out.append(line)
    cleaned = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def is_pool(title: str) -> bool:
    return bool(POOL_RE.search(title or ""))


# Студии часто дублируют название на втором языке через «|»:
# «Animator – Open Application | Animateur - candidature libre».
# Читать такое невозможно, оставляем первую половину.
def trim_title(title: str) -> str:
    t = (title or "").strip()
    if "|" not in t:
        return t
    head = t.split("|")[0].strip(" -–—")
    return head if len(head) >= 12 else t


def classify_role(title: str):
    low = title.lower()
    for role, pattern in ROLE_RULES:
        if re.search(pattern, low, re.I):
            return role
    return None


# Роль «Программирование» слишком широкая: под ней и Unity, и бэкенд,
# и инструменты. Уточняем по названию — побеждает первое правило.
SPEC_RULES = [
    ("Unity",       r"\bunity\b|юнити"),
    ("Unreal",      r"\bunreal\b|\bue\s?[45]\b|анрил"),
    ("Движок",      r"engine (programmer|developer|engineer)|graphics|render|shader|physics|физик"),
    ("Геймплей",    r"gameplay|геймплей"),
    ("Данные и ML", r"data (engineer|developer|ops|platform|scien)|machine learning|"
                    r"\bml engineer\b|\bai engineer\b|data engineering"),
    ("Бэкенд",      r"back[- ]?end|бэкенд|бекенд|\bserver\b|серверн|platform engineer|"
                    r"golang|node\.js|distributed systems"),
    ("Фронтенд",    r"front[- ]?end|фронтенд|full[- ]?stack|\breact\b|\bweb developer\b|"
                    r"javascript|typescript|playable ads|\bhtml5\b"),
    ("Мобильная",   r"\bios\b|\bandroid\b|мобильн|mobile (developer|engineer)|flutter"),
    ("Инструменты", r"\btools?\b|tooling|pipeline (engineer|developer)|инструмент|build engineer"),
    ("DevOps",      r"devops|\bsre\b|site reliability|infrastructure|инфраструктур|"
                    r"kubernetes|\bcloud\b"),
    ("C++",         r"c\+\+"),
]

# Инструменты и языки. Git и Jira намеренно не берём: они есть почти везде.
STACK_RULES = [
    ("Unity", r"\bunity\b"), ("Unreal", r"\bunreal\b|\bue\s?[45]\b"),
    ("Godot", r"\bgodot\b"), ("C++", r"c\+\+"), ("C#", r"c#|c-sharp"),
    ("Python", r"\bpython\b"), ("Go", r"\bgolang\b"), ("Java", r"\bjava\b(?!script)"),
    ("Kotlin", r"\bkotlin\b"), ("Swift", r"\bswift\b"), ("TypeScript", r"\btypescript\b"),
    ("JavaScript", r"\bjavascript\b"), ("Lua", r"\blua\b"), ("SQL", r"\bsql\b"),
    ("AWS", r"\baws\b|amazon web services"), ("Docker", r"\bdocker\b"),
    ("Kubernetes", r"\bkubernetes\b|\bk8s\b"), ("Maya", r"\bmaya\b"),
    ("Blender", r"\bblender\b"), ("Houdini", r"\bhoudini\b"), ("ZBrush", r"\bzbrush\b"),
    ("Substance", r"\bsubstance\b"), ("Photoshop", r"\bphotoshop\b"), ("Spine", r"\bspine\b"),
    ("Figma", r"\bfigma\b"), ("Perforce", r"\bperforce\b|\bp4v\b"), ("Wwise", r"\bwwise\b"),
    ("FMOD", r"\bfmod\b"),
]


def classify_spec(title: str, role: str):
    if role != "Программирование":
        return None
    for name, pattern in SPEC_RULES:
        if re.search(pattern, title or "", re.I):
            return name
    return None


def classify_stack(title: str, desc: str):
    hay = (title or "") + " " + (desc or "")
    return [name for name, pattern in STACK_RULES if re.search(pattern, hay, re.I)][:6]


def classify_grade(title: str):
    low = title.lower()
    for grade, pattern in GRADE_RULES:
        if re.search(pattern, low, re.I):
            return grade
    return None


def looks_remote(*chunks):
    return any(c and REMOTE_RE.search(c) for c in chunks)


def make_id(source: str, company: str, external_id) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    return f"{source}-{slug}-{external_id}"




# ---------------------------------------------------------------- зарплата

CUR_SIGNS = {"usd": "$", "eur": "€", "gbp": "£", "pln": "zł", "rub": "₽",
             "kzt": "₸", "uah": "₴", "cad": "CA$", "aud": "A$", "sgd": "S$"}


SUFFIX_CUR = {"₽", "zł", "₸", "₴", "PLN", "RUB", "KZT", "UAH", "CZK", "SEK"}


def money(num, currency):
    cur = CUR_SIGNS.get(str(currency or "").lower(), (currency or "").upper())
    n = f"{int(round(float(num))):,}".replace(",", "\u00a0")
    return f"{n} {cur}".strip()


def salary_range(lo, hi, currency, interval=None):
    """Собираем строку вилки. Пустая вилка лучше, чем выдуманная."""
    try:
        lo = float(lo) if lo not in (None, "") else None
        hi = float(hi) if hi not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if not lo and not hi:
        return None
    if lo and hi and hi < lo:
        lo, hi = hi, lo
    if lo and hi and lo != hi:
        text = f"{money(lo, currency)} – {money(hi, currency)}"
    else:
        text = money(lo or hi, currency)
    per = {"month": "в месяц", "monthly": "в месяц", "year": "в год", "annual": "в год",
           "yearly": "в год", "hour": "в час", "hourly": "в час", "day": "в день"}
    word = per.get(str(interval or "").lower())
    return f"{text} {word}" if word else text


# Многие студии вилку в списке не отдают, но пишут её в тексте вакансии.
# Берём только строки, где рядом стоит слово про оплату: иначе легко
# принять «$5 million funding» или «401(k)» за зарплату.
PAY_HINT = re.compile(
    r"salary|compensation|pay range|base pay|pay band|remuneration|"
    r"зарплат|вилка|оклад|доход", re.I)
# Ставка бывает и простым числом: «$63.50—$75». Двузначные и трёхзначные
# числа берём только там, где рядом стоит слово про оплату — иначе поймаем
# «team of 200 - 300 people».
NUM = r"(\d{1,3}(?:[ \u00a0.,]\d{3})+|\d{2,3}\s?[kK]|\d{1,3}\.\d{2}|\d{2,3}(?!\d))"
CUR = r"([€$£₽₴₸]|PLN|EUR|USD|GBP|RUB|UAH|KZT|CZK|SEK)"
PAY_RANGE = re.compile(
    CUR + r"?\s?" + NUM + r"\s?(?:-|–|—|to|до)\s?" +
    CUR + r"?\s?" + NUM + r"\s?" + CUR + r"?", re.U)

# «this is an hourly rate» — важная приписка: 75 в час и 75 тысяч в год
# для человека совсем разные предложения.
PER_HOUR = re.compile(r"\bhourly\b|\bper hour\b|\bв час\b", re.I)


def salary_from_text(desc):
    if not desc:
        return None
    lines = [l.strip() for l in str(desc).split("\n")]
    for i, line in enumerate(lines):
        if len(line) > 400 or not line:
            continue
        # Вилку часто пишут отдельной строкой, а слово «pay range» — строкой выше:
        #   The estimated base pay range for this role is listed below
        #   $63.50—$75 USD
        near = " ".join(lines[max(0, i - 2):i + 1])
        if not PAY_HINT.search(near):
            continue
        m = PAY_RANGE.search(line)
        if not m:
            continue
        cur = m.group(1) or m.group(3) or m.group(5) or ""
        lo, hi = m.group(2), m.group(4)
        # Точка в «63.50» — это копейки, а не разделитель тысяч. Пробелом
        # заменяем только разделители групп: 120.000 → 120 000.
        def tidy(num):
            num = num.strip()
            if re.fullmatch(r"\d{1,3}\.\d{2}", num):
                return num
            return re.sub(r"[.,\u00a0]", " ", num)
        text = f"{tidy(lo)} – {tidy(hi)}"
        text = re.sub(r"\s+", " ", text).strip()
        if PER_HOUR.search(near):
            text += " в час"
        if not cur:
            return text
        # «120 000 $» привычнее, чем «$ 120 000» — но только для тех валют,
        # где знак принято ставить после числа.
        return f"{text} {cur}" if cur in SUFFIX_CUR else f"{cur} {text}"
    return None


# ---------------------------------------------------------------- описания

TAG_RE = re.compile(r"<[^>]+>")
LIST_RE = re.compile(r"<li[^>]*>", re.I)
BREAK_RE = re.compile(r"</(p|div|h\d|ul|ol|tr)>|<br\s*/?>", re.I)
MAX_DESC = 6000


def html_to_text(raw):
    """Из HTML вакансии делаем читаемый текст: абзацы и маркеры списка."""
    if not raw:
        return None

    t = str(raw)
    # Сначала расшифровываем мнемоники, иначе теги приезжают как &lt;p&gt;
    # и остаются в тексте видимым мусором. Дважды — попадается двойное кодирование.
    t = html_lib.unescape(html_lib.unescape(t))
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.I | re.S)
    t = re.sub(r"<img[^>]*>", " ", t, flags=re.I)
    t = LIST_RE.sub("\n• ", t)
    t = BREAK_RE.sub("\n", t)
    t = TAG_RE.sub("", t)

    t = t.replace("\u00a0", " ")

    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    # Внутри <li> часто лежит ещё один блочный тег — из-за него перенос строки
    # вставал сразу после маркера, и точка оставалась одна на строке.
    # Подтягиваем текст обратно к своему маркеру.
    t = re.sub(r"•[ \t]*\n+[ \t]*", "• ", t)
    t = re.sub(r"(?:•[ \t]*){2,}", "• ", t)
    t = re.sub(r"^[ \t]*•[ \t]*$", "", t, flags=re.M)
    # Между соседними пунктами пустая строка не нужна — список должен читаться
    # как список, а не как отдельные абзацы.
    t = re.sub(r"(?m)(^•.*)\n\s*\n(?=•)", r"\1\n", t)
    t = "\n".join(line.strip() for line in t.split("\n"))
    t = t.strip()

    if len(t) > MAX_DESC:
        cut = t.rfind("\n", 0, MAX_DESC)
        t = t[:cut if cut > MAX_DESC * 0.6 else MAX_DESC].rstrip() + "…"
    return t or None


# ---------------------------------------------------------------- локации

# Студии пишут локацию как попало: «Remote», «United Kingdom-Remote»,
# «Warszawa, Masovian Voivodeship, Poland», «Ljubljana; Barcelona; Limassol».
# Приводим к списку нормальных мест плюс отдельный признак удалёнки.

REMOTE_WORDS = re.compile(
    r"\b(remote|remotely|anywhere|worldwide|work from home|wfh|hybrid/remote|"
    r"удал[её]нн\w*|удал[её]нка)\b", re.I)

# Строки вроде «within EU» или «within ±2 hours of CET» описывают не город,
# а откуда можно работать. Считаем это удалёнкой и в список мест не кладём.
REMOTE_ZONE = re.compile(r"\bwithin\b.*\b(eu|europe|cet|cest|est|utc|gmt|hours?)\b", re.I)

# лишние административные слова, которые ничего не говорят кандидату
NOISE = re.compile(
    r"\b(voivodeship|province|prefecture|county|oblast|region|district|"
    r"metropolitan area|greater|state of)\b", re.I)


# Одной метки «Удалёнка» мало: «работайте откуда угодно» и «удалённо, но
# только из Канады» — это совсем разные предложения. Разбираем на три вида.
WORLDWIDE_RE = re.compile(
    r"\b(anywhere|worldwide|work from anywhere|fully remote|globally|"
    r"any (country|location)|из любой точки)\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b|гибрид", re.I)
ZONE_RE = re.compile(
    r"\bwithin\b|\bremote\b[^.]{0,20}\b(in|from|within|only)\b|"
    r"\b(eu|europe|cet|cest|est|utc|gmt)\b|"
    r"time ?zone|based in", re.I)


def remote_kind(raw_location, title, desc):
    """worldwide — откуда угодно, zone — только из своего региона, hybrid — часть дней в офисе."""
    loc = str(raw_location or "")
    head = (title or "") + " " + loc + " " + (desc or "")[:600]
    if HYBRID_RE.search(loc) or HYBRID_RE.search(title or ""):
        return "hybrid"
    if WORLDWIDE_RE.search(head):
        return "worldwide"
    if ZONE_RE.search(loc) or ZONE_RE.search(title or ""):
        return "zone"
    # «Remote (Canada)», «Remote, US» — удалёнка, но привязанная к месту.
    # Если рядом со словом «remote» осталось ещё что-то осмысленное — это регион.
    rest = REMOTE_WORDS.sub(" ", loc)
    rest = re.sub(r"[^\w\s]", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip()
    if REMOTE_WORDS.search(loc) and len(rest) >= 2:
        return "zone"
    return None


# Студии пишут одну и ту же страну по-разному: UK и United Kingdom, USA и
# United States. Для фильтра это разные строки, и список начинает дробиться.
COUNTRY_FIX = [
    (r"\bU\.?K\.?$",                 "United Kingdom"),
    (r"\bU\.?S\.?A?\.?$",            "United States"),
    (r"\bUnited States of America$",  "United States"),
    (r"\bDeutschland$",               "Germany"),
    (r"\bEspaña$",                    "Spain"),
    (r"\bPolska$",                    "Poland"),
    (r"\bSrbija$",                    "Serbia"),
    (r"\bTürkiye$",                   "Turkey"),
]

# Строки, которые не являются местом ни в каком виде.
NOT_A_PLACE = re.compile(
    r"^(blank|multiple locations?|various locations?|several locations?|"
    r"location flexible|worldwide|global|n/?a)$", re.I)


def normalize_place(part: str) -> str:
    """«London, UK» → «London, United Kingdom», «Singapore-Guoco Midtown» → «Singapore»."""
    p = part.strip(" ,;·")
    if not p or NOT_A_PLACE.match(p):
        return ""
    # адрес офиса после дефиса — это уже не город: «Singapore-Guoco Midtown»
    p = re.sub(r"^([A-Za-zА-Яа-яЁё\s]{3,})-[A-Za-z].*$", r"\1", p).strip()
    for pattern, full in COUNTRY_FIX:
        p = re.sub(pattern, full, p, flags=re.I)
    # «BLANK, Multiple Locations» — выкидываем куски-пустышки внутри строки
    bits = [b.strip() for b in p.split(",")]
    bits = [b for b in bits if b and not NOT_A_PLACE.match(b)]
    return ", ".join(bits)


def clean_locations(raw):
    """Возвращает (список мест, признак удалёнки)."""
    if not raw:
        return [], False

    text = str(raw)
    remote = bool(REMOTE_WORDS.search(text)) or bool(REMOTE_ZONE.search(text))
    text = REMOTE_ZONE.sub(" ", text)

    # содержимое скобок к месту работы отношения не имеет: (Hybrid), (Office), (f/m/d)
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)
    text = REMOTE_WORDS.sub(" ", text)

    parts = re.split(r"[;/|]|\bи\b|\band\b", text)
    out = []
    for part in parts:
        p = clean_one(part)
        if p and p.lower() not in [o.lower() for o in out]:
            out.append(p)
    return out, remote


def clean_one(part):
    p = re.sub(r"\s+", " ", part).strip(" ,-–—\t")
    if not p:
        return None

    chunks = [c.strip() for c in p.split(",") if c.strip()]
    chunks = [c for c in chunks if not NOISE.search(c)]
    if not chunks:
        return None

    # «Warszawa, Masovian Voivodeship, Poland» → «Warszawa, Poland»
    if len(chunks) > 2:
        chunks = [chunks[0], chunks[-1]]

    p = ", ".join(chunks)
    if len(p) < 2 or len(p) > 60:
        return None
    # осталось что-то бессодержательное
    if p.lower() in {"n/a", "na", "various", "multiple", "other", "-", "office",
                     "europe", "any", "any location", "global", "international",
                     "tbd", "flexible", "unknown", "hybrid", "onsite", "on-site",
                     "on site", "in office", "in-office", "worldwide", "тбд"}:
        return None
    # «Hybrid, SP», «Hybrid — Berlin»: само слово «гибрид» местом не является,
    # но то, что стоит рядом, может им быть.
    if re.match(r"^hybrid\b", p, re.I):
        rest = re.sub(r"^hybrid\b[\s,;:-]*", "", p, flags=re.I).strip()
        p = rest if len(rest) > 2 else ""
    p = normalize_place(p)
    return p or None


# ---------------------------------------------------------------- источники

def fetch_greenhouse(company: dict):
    """Greenhouse отдаёт публичный список вакансий без ключа."""
    token = company["token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = http_get(url)
    r.raise_for_status()
    out = []
    for job in r.json().get("jobs", []):
        title = (job.get("title") or "").strip()
        location = (job.get("location") or {}).get("name")
        locs, rem = clean_locations(location)
        out.append({
            "id": make_id("gh", company["name"], job.get("id")),
            "title": title,
            "company": company["name"],
            "locations": locs,
            "remote": rem or looks_remote(title),
            "rkind": remote_kind(location, title, html_to_text(job.get("content"))),
            "salary": greenhouse_salary(job) or salary_from_text(html_to_text(job.get("content"))),
            "posted": (job.get("updated_at") or "")[:10] or None,
            "url": job.get("absolute_url"),
            "desc": html_to_text(job.get("content")),
            "site": company.get("site"),
            "source": "greenhouse",
        })
    return out


def greenhouse_salary(job):
    """Greenhouse иногда кладёт вилку в pay_input_ranges — в центах."""
    for rng in (job.get("pay_input_ranges") or []):
        lo, hi = rng.get("min_cents"), rng.get("max_cents")
        if lo or hi:
            return salary_range(lo and lo / 100, hi and hi / 100,
                                rng.get("currency_type"), rng.get("title"))
    return None


def fetch_lever(company: dict):
    token = company["token"]
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = http_get(url)
    r.raise_for_status()
    out = []
    for job in r.json():
        cats = job.get("categories") or {}
        title = (job.get("text") or "").strip()
        location = cats.get("location")
        posted = job.get("createdAt")
        posted_iso = (
            datetime.fromtimestamp(posted / 1000, tz=timezone.utc).date().isoformat()
            if isinstance(posted, (int, float)) else None
        )
        locs, rem = clean_locations(location)
        out.append({
            "id": make_id("lv", company["name"], job.get("id")),
            "title": title,
            "company": company["name"],
            "locations": locs,
            "remote": rem or looks_remote(cats.get("commitment"), title),
            "rkind": remote_kind(str(location) + " " + str(cats.get("commitment") or ""),
                                 title, lever_desc(job)),
            "salary": lever_salary(job) or salary_from_text(lever_desc(job)),
            "posted": posted_iso,
            "url": job.get("hostedUrl"),
            "desc": lever_desc(job),
            "source": "lever",
                "site": company.get("site"),
        })
    return out


def lever_salary(job):
    r = job.get("salaryRange") or {}
    return salary_range(r.get("min"), r.get("max"), r.get("currency"),
                        r.get("interval") or r.get("period"))


def fetch_recruitee(company: dict):
    """Recruitee — на нём много студий в Польше и Восточной Европе."""
    token = company["token"]
    url = f"https://{token}.recruitee.com/api/offers/"
    r = http_get(url)
    r.raise_for_status()
    out = []
    for job in r.json().get("offers", []):
        title = (job.get("title") or "").strip()
        city = job.get("city") or job.get("location")
        locs, rem = clean_locations(city)
        out.append({
            "id": make_id("rc", company["name"], job.get("id")),
            "title": title,
            "company": company["name"],
            "locations": locs,
            "remote": bool(job.get("remote")) or rem or looks_remote(title),
            "rkind": remote_kind(city, title, html_to_text(job.get("description"))),
            "salary": recruitee_salary(job) or salary_from_text(html_to_text(job.get("description"))),
            "posted": (job.get("published_at") or job.get("created_at") or "")[:10] or None,
            "url": job.get("careers_url") or job.get("url"),
            "desc": html_to_text(job.get("description")),
            "source": "recruitee",
                "site": company.get("site"),
        })
    return out


def recruitee_salary(job):
    r = job.get("salary") or {}
    if isinstance(r, dict):
        return salary_range(r.get("min"), r.get("max"), r.get("currency"),
                            r.get("period") or r.get("interval"))
    return None


# --- hh.ru -------------------------------------------------------------
# Берём вакансии ТОЛЬКО у студий из белого списка. Открытый поиск по hh
# даёт мусор и скам — именно поэтому им неудобно пользоваться.
# id работодателя ищется по названию автоматически, руками ничего искать не надо.

def hh_salary(s):
    if not s:
        return None
    cur = {"RUR": "\u20bd", "USD": "$", "EUR": "\u20ac", "KZT": "\u20b8",
           "BYR": "Br", "BYN": "Br", "UZS": "\u0441\u1d1b\u043c"}.get(s.get("currency"), "")
    lo, hi = s.get("from"), s.get("to")
    fmt = lambda v: f"{v:,}".replace(",", " ")
    if lo and hi:
        return f"{fmt(lo)} \u2013 {fmt(hi)} {cur}".strip()
    if lo:
        return f"\u043e\u0442 {fmt(lo)} {cur}".strip()
    if hi:
        return f"\u0434\u043e {fmt(hi)} {cur}".strip()
    return None


# Оставлено в коде на случай, если hh когда-нибудь снова откроет доступ.
# В рабочий список источников не входит: hh — чужая доска объявлений,
# а сайт обещает вакансии с карьерных страниц самих студий.
def fetch_hh(company: dict):
    """
    Ищем вакансии сразу по названию работодателя.
    Отдельный запрос за id работодателя hh отклоняет, поэтому обходимся без него.
    Лишнее отсекаем сами: оставляем только те вакансии, где название компании
    действительно совпадает с нужной.
    """
    name = company["name"]
    want = name.lower().replace(".", "").replace("-", " ").strip()

    out, page = [], 0
    while page < 3:
        params = {
            "text": name,
            "search_field": "company_name",
            "per_page": 100,
            "page": page,
        }
        r = http_get("https://api.hh.ru/vacancies", params=params,
                     headers=HH_UA)
        if r.status_code >= 400:
            # hh объясняет причину в теле ответа — без него ошибка бесполезна
            raise RuntimeError(f"hh {r.status_code}: {r.text[:300]}")
        data = r.json()

        for v in data.get("items", []):
            emp = ((v.get("employer") or {}).get("name") or "")
            emp_norm = emp.lower().replace(".", "").replace("-", " ").strip()
            if want not in emp_norm and emp_norm not in want:
                continue

            area = (v.get("area") or {}).get("name")
            schedule = (v.get("schedule") or {}).get("name", "")
            locs, rem = clean_locations(area)
            out.append({
                "id": make_id("hh", name, v.get("id")),
                "title": (v.get("name") or "").strip(),
                "company": emp or name,
                "locations": locs,
                "remote": rem or looks_remote(schedule, v.get("name")),
                "rkind": remote_kind(str(schedule), v.get("name"), None),
                "salary": hh_salary(v.get("salary")),
                "posted": (v.get("published_at") or "")[:10] or None,
                "url": v.get("alternate_url"),
                "source": "hh",
                "site": company.get("site"),
            })

        if page >= data.get("pages", 1) - 1:
            break
        page += 1
        time.sleep(PAUSE)

    return out


def lever_desc(job):
    """Lever отдаёт описание кусками: вступление, блоки списков и хвост."""
    parts = [html_to_text(job.get("description"))]
    for block in (job.get("lists") or []):
        title = (block.get("text") or "").strip()
        body = html_to_text(block.get("content"))
        if title:
            parts.append("\n" + title.upper())
        if body:
            parts.append(body)
    parts.append(html_to_text(job.get("additional")))
    text = "\n".join(p for p in parts if p)
    return text[:MAX_DESC] or None




# --- Ashby ---------------------------------------------------------------
# Отдаёт и текст вакансии, и вилку — из новых систем самая щедрая.

def fetch_ashby(company: dict):
    token = company["token"]
    url = (f"https://api.ashbyhq.com/posting-api/job-board/{token}"
           "?includeCompensation=true")
    r = http_get(url)
    r.raise_for_status()
    out = []
    for job in r.json().get("jobs", []):
        title = (job.get("title") or "").strip()
        location = job.get("location") or ""
        extra = [x.get("location") for x in (job.get("secondaryLocations") or [])
                 if isinstance(x, dict)]
        # через «;», иначе «Warsaw, Poland» и «Remote - EU» склеятся в одно место
        place = "; ".join([location] + [e for e in extra if e])
        locs, rem = clean_locations(place)
        comp = job.get("compensation") or {}
        desc = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml"))
        out.append({
            "id": make_id("ab", company["name"], job.get("id")),
            "title": title,
            "company": company["name"],
            "locations": locs,
            "remote": bool(job.get("isRemote")) or rem or looks_remote(title),
            "rkind": remote_kind(place, title, desc),
            "salary": comp.get("compensationTierSummary") or salary_from_text(desc),
            "posted": (job.get("publishedAt") or "")[:10] or None,
            "url": job.get("jobUrl") or job.get("applyUrl"),
            "desc": desc[:MAX_DESC] if desc else None,
            "source": "ashby",
            "site": company.get("site"),
        })
    return out


# --- Workable ------------------------------------------------------------

def fetch_workable(company: dict):
    token = company["token"]
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    r = http_get(url)
    r.raise_for_status()
    data = r.json()
    out = []
    for job in data.get("jobs", []):
        title = (job.get("title") or "").strip()
        place = ", ".join(x for x in [job.get("city"), job.get("country")] if x)
        if not place:
            loc = job.get("location") or {}
            if isinstance(loc, dict):
                place = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
        locs, rem = clean_locations(place)
        desc = html_to_text(job.get("description")) or html_to_text(job.get("requirements"))
        out.append({
            "id": make_id("wk", company["name"], job.get("shortcode") or job.get("id")),
            "title": title,
            "company": company["name"],
            "locations": locs,
            "remote": bool(job.get("telecommuting")) or rem or looks_remote(title),
            "rkind": remote_kind(place, title, desc),
            "salary": salary_from_text(desc),
            "posted": (job.get("published_on") or job.get("created_at") or "")[:10] or None,
            "url": job.get("url") or job.get("application_url") or job.get("shortlink"),
            "desc": desc,
            "source": "workable",
            "site": company.get("site"),
        })
    return out


# --- SmartRecruiters -----------------------------------------------------
# Список отдаёт без текста вакансии, поэтому за описанием ходим отдельно.
# Ограничиваем: иначе одна крупная студия растянет сбор на полчаса.

SR_DETAILS_LIMIT = 60


def fetch_smartrecruiters(company: dict):
    token = company["token"]
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    r = http_get(url)
    r.raise_for_status()
    out = []
    for i, job in enumerate(r.json().get("content", [])):
        title = (job.get("name") or "").strip()
        loc = job.get("location") or {}
        place = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
        remote_flag = bool(loc.get("remote"))
        desc = None
        if i < SR_DETAILS_LIMIT:
            desc = smartrecruiters_desc(token, job.get("id"))
            time.sleep(PAUSE / 2)
        locs, rem = clean_locations(place)
        out.append({
            "id": make_id("sr", company["name"], job.get("id")),
            "title": title,
            "company": company["name"],
            "locations": locs,
            "remote": remote_flag or rem or looks_remote(title),
            "rkind": remote_kind(place + (" remote" if remote_flag else ""), title, desc),
            "salary": salary_from_text(desc),
            "posted": (job.get("releasedDate") or "")[:10] or None,
            "url": (job.get("applyUrl")
                    or f"https://jobs.smartrecruiters.com/{token}/{job.get('id')}"),
            "desc": desc,
            "source": "smartrecruiters",
            "site": company.get("site"),
        })
    return out


def smartrecruiters_desc(token, job_id):
    """Текст вакансии лежит кусками: обязанности, требования, о компании."""
    if not job_id:
        return None
    try:
        r = http_get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}",
                     tries=2)
        if r.status_code >= 400:
            return None
        sections = (((r.json().get("jobAd") or {}).get("sections")) or {})
    except Exception:
        return None
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        block = sections.get(key) or {}
        text = html_to_text(block.get("text"))
        if text:
            title = (block.get("title") or "").strip()
            parts.append((title + "\n" if title else "") + text)
    joined = "\n\n".join(parts)
    return joined[:MAX_DESC] or None


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "recruitee": fetch_recruitee,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
}


# ---------------------------------------------------------------- разведка

def probe(companies):
    """
    Проверяет, действительно ли студия сидит на указанной доске.
    Нужно один раз при заполнении companies.json: слуги в нём — догадки,
    и половина из них не подтвердится. Это нормально.
    """
    alive, dead = [], []
    for c in companies:
        fetcher = FETCHERS.get(c.get("ats"))
        if not fetcher:
            dead.append((c, "неизвестная система: " + str(c.get("ats"))))
            continue
        try:
            jobs = fetcher(c)
            alive.append((c, len(jobs)))
            print(f"  OK    {c['name']:<28} {c['ats']}/{c['token']}  вакансий: {len(jobs)}")
        except Exception as e:
            dead.append((c, str(e)[:70]))
            print(f"  нет   {c['name']:<28} {c['ats']}/{c['token']}  {str(e)[:60]}")
        time.sleep(PAUSE)

    print(f"\nПодтвердилось: {len(alive)} из {len(companies)}")
    if dead:
        print("Не отозвались (проверь слаг или систему найма вручную):")
        for c, why in dead:
            print(f"  · {c['name']} — {why}")
    return alive


# ---------------------------------------------------------------- сборка

# Коды, которые честно означают «такой страницы больше нет».
# Всё остальное — 403 «робота не пустили», 429 «слишком часто»,
# пятисотые, таймауты — не повод хоронить вакансию: так мы каждую неделю
# теряли восемь вакансий Nordeus, которые на самом деле живы.
GONE_CODES = (404, 410)


def check_alive(url: str) -> bool:
    """Хороним ссылку, только если сервер прямо сказал, что страницы нет."""
    try:
        r = http_get(url, tries=2, allow_redirects=True)
        if r.status_code in GONE_CODES:
            return False
        if r.status_code >= 400:
            return True          # не пустили или сервер лёг — вакансию оставляем
        low = r.text[:200_000].lower()
        dead_marks = (
            "no longer accepting", "position has been filled", "job is closed",
            "this job is no longer available", "this posting has expired",
            "position closed", "job not found", "the role has been filled",
            "no longer available", "vacancy is closed",
            "вакансия закрыта", "вакансия неактуальна", "вакансия в архиве",
        )
        return not any(m in low for m in dead_marks)
    except Exception:
        return True              # не достучались — это про связь, а не про вакансию


def read_previous():
    """Достаём вакансии из прошлой выгрузки — они пригодятся, если студия
    сегодня не ответила. Лучше показать вчерашнее, чем потерять полсотни строк."""
    if not OUT_JS.exists():
        return {}
    try:
        text = OUT_JS.read_text(encoding="utf-8")
        start = text.index("window.JOBS = ") + len("window.JOBS = ")
        data = json.loads(text[start:text.rindex("]") + 1])
    except Exception:
        return {}
    by_company = {}
    for j in data:
        j.pop("stack", None)          # старое мусорное поле, больше не пишем
        by_company.setdefault(j.get("company"), []).append(j)
    return by_company


RUN = {"failed": [], "reused": [], "empty": [], "dead": 0, "before": 0}


def collect(companies, verify_links: bool):
    previous = read_previous()
    RUN["before"] = sum(len(v) for v in previous.values())
    raw = []
    for c in companies:
        fetcher = FETCHERS.get(c.get("ats"))
        if not fetcher:
            continue
        try:
            got = fetcher(c)
            raw.extend(got)
            if not got:
                RUN["empty"].append(c["name"])
            print(f"  {c['name']:<28} +{len(got)}")
        except Exception as e:
            old = previous.get(c["name"], [])
            if old:
                raw.extend(old)
                RUN["reused"].append(c["name"])
                print(f"  {c['name']:<28} не ответила ({str(e)[:40]}) — беру прошлые {len(old)}")
            else:
                RUN["failed"].append(c["name"])
                print(f"  {c['name']:<28} ошибка: {str(e)[:60]}")
        time.sleep(PAUSE)

    blocked = set()
    if BLOCKLIST.exists():
        blocked = set(json.loads(BLOCKLIST.read_text(encoding="utf-8")))

    jobs, seen = [], set()
    for j in raw:
        if not j["title"] or not j["url"]:
            continue
        if j["id"] in blocked:
            continue
        if REJECT_RE.search(j["title"]):
            continue

        j["title"] = trim_title(j["title"])
        j["desc"] = strip_media(j.get("desc"))
        if is_pool(j["title"]):
            j["pool"] = True
        role = classify_role(j["title"])
        if not role:
            continue                      # не смогли отнести к геймдеву — пропускаем

        key = (j["company"].lower(), j["title"].lower())
        if key in seen:
            continue
        seen.add(key)

        j["role"] = role
        j["grade"] = classify_grade(j["title"])
        j["spec"] = classify_spec(j["title"], role)
        j["stack"] = classify_stack(j["title"], j.get("desc"))
        jobs.append(j)

    if verify_links:
        print(f"\nПроверяю живость {len(jobs)} ссылок…")
        live = []
        for i, j in enumerate(jobs, 1):
            if check_alive(j["url"]):
                live.append(j)
            else:
                RUN["dead"] += 1
                print(f"  мертво: {j['company']} — {j['title']}")
            if i % 25 == 0:
                print(f"  …{i}/{len(jobs)}")
            time.sleep(PAUSE)
        jobs = live

    jobs.sort(key=lambda j: (j.get("posted") or ""), reverse=True)
    return jobs


def write_js(jobs):
    today = datetime.now(timezone.utc).date().isoformat()
    body = json.dumps(jobs, ensure_ascii=False, indent=2)
    studios = len({j.get("company") for j in jobs if j.get("company")})
    OUT_JS.write_text(
        "// jobs.js — сгенерировано collect.py, руками не править.\n"
        f"// Обновлено: {today}. Вакансий: {len(jobs)}.\n\n"
        "window.JOBS_DEMO = false;\n"
        f'window.JOBS_UPDATED = "{today}";\n'
        f"window.JOBS_STUDIOS = {studios};\n\n"
        f"window.JOBS = {body};\n",
        encoding="utf-8",
    )
    print(f"\nЗаписано {len(jobs)} вакансий в {OUT_JS.name}")
    write_status(jobs, today, studios)
    location_report(jobs)
    urls = write_pages(jobs, today)
    write_sitemap(today, urls)


def write_status(jobs, today, studios):
    """Короткий отчёт о сборе: сайт показывает его в подвале, чтобы человек видел,
    что база живая, а не заброшена полгода назад."""
    data = {
        "date": today,
        "jobs": len(jobs),
        "studios": studios,
        "withSalary": sum(1 for j in jobs if j.get("salary")),
        "remote": sum(1 for j in jobs if j.get("remote") or j.get("rkind")),
        "dropped": RUN["dead"],
        "failedSources": len(RUN["failed"]),
        "reusedSources": len(RUN["reused"]),
        "emptySources": len(RUN["empty"]),
        "sources": sorted({j.get("source") for j in jobs if j.get("source")}),
    }
    STATUS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Итог: студий {studios}, с зарплатой {data['withSalary']}, "
          f"выброшено мёртвых {RUN['dead']}, источников с ошибкой {len(RUN['failed'])}")


def location_report(jobs, top=20):
    """Раз в неделю показываем, какие места встречаются чаще всего.
    По этому списку видно, что пора добавить в словарь стран на сайте."""
    counts = {}
    for j in jobs:
        for loc in j.get("locations") or []:
            counts[loc] = counts.get(loc, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: -kv[1])[:top]
    print(f"\nСамые частые локации (проверь, все ли опознаются как страны):")
    print("  " + " · ".join(f"{name} {n}" for name, n in rows))


def write_sitemap(today: str, urls=None):
    """Карта сайта со всеми страницами: по ней поисковик их и найдёт."""
    urls = urls or ["https://lootwork.github.io/"]
    rows = []
    for u in urls:
        prio = "1.0" if u.rstrip("/").endswith("github.io") else "0.7"
        rows.append("  <url>\n"
                    f"    <loc>{u}</loc>\n"
                    f"    <lastmod>{today}</lastmod>\n"
                    "    <changefreq>daily</changefreq>\n"
                    f"    <priority>{prio}</priority>\n"
                    "  </url>")
    SITEMAP.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(rows) + "\n</urlset>\n",
        encoding="utf-8",
    )
    print(f"В карте сайта {len(urls)} адресов")


# ---------------------------------------------------------------- страницы
# Витрина живёт в одном index.html, и поисковик видит там ровно одну страницу.
# Люди же ищут «unity developer вакансии» или «работа в Wargaming» — под такие
# запросы нужен отдельный адрес. Поэтому сборщик раскладывает статические
# страницы: на каждую вакансию, студию, роль и на удалёнку.

SITE = "https://lootwork.github.io"
PAGE_DIRS = ["job", "company", "role", "spec", "jobs", "remote", "privacy", "about", "where", "terms"]

TRANSLIT = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i",
    "й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t",
    "у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"",
    "э":"e","ю":"yu","я":"ya",
}


def plural(n: int, one: str, few: str, many: str) -> str:
    """1 вакансия, 2 вакансии, 5 вакансий. Мелочь, по которой видно халтуру."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def jobs_word(n: int) -> str:
    return f"{n} " + plural(n, "вакансия", "вакансии", "вакансий")


MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def human_date(iso: str) -> str:
    """2026-08-07 → 7 августа. Дата в машинном виде выглядит как недоделка."""
    try:
        y, m, d = str(iso)[:10].split("-")
        return f"{int(d)} {MONTHS_RU[int(m) - 1]}"
    except Exception:
        return str(iso or "")


def slugify(text: str) -> str:
    """«Технический художник» → «tehnicheskiy-hudozhnik». Адрес должен читаться."""
    s = str(text or "").lower()
    s = "".join(TRANSLIT.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:70] or "x"


def esc(text) -> str:
    return (str(text if text is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# Студии не размечают заголовки внутри текста — это просто короткая строка
# без точки, за которой идёт список. Ловим по форме, а не по словарю.
HEAD_WORDS = re.compile(
    r"^(job overview|overview|about|who|what|your|our|the role|role|responsibilit|"
    r"requirement|qualification|skills|benefit|perks|we offer|nice to have|bonus|"
    r"why |how |compensation|salary|process|next steps|apply|stack|tools|"
    r"о (нас|компании|проекте|вакансии)|что (нужно|предстоит|мы|ты)|наши|твои|ваши|"
    r"требовани|обязанност|условия|мы предлагаем|будет плюсом|зарплат|бенефит|льгот)", re.I)


def looks_like_heading(line: str, nxt: str) -> bool:
    clean = (line or "").strip()
    if not clean or len(clean) > 80 or clean.startswith("•"):
        return False
    if re.search(r"[.,;]$", clean):
        return False
    if len(clean.split()) > 10:
        return False
    if nxt and nxt.strip().startswith("•"):
        return True
    if re.search(r"[:?]$", clean):
        return True
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", clean))
    if clean == clean.upper() and letters >= 4:
        return True
    if letters < 4:
        return False
    return bool(HEAD_WORDS.match(clean))


def desc_html(text: str) -> str:
    """Текст вакансии в заголовки, списки и абзацы — сплошную простыню не читают."""
    if not text:
        return ""
    lines = [l.strip() for l in text.split("\n")]
    out, bullets = [], []

    def flush():
        if bullets:
            out.append("<ul>" + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    waiting = False        # маркер приехал пустым — текст пункта будет следующей строкой
    for i, line in enumerate(lines):
        if not line:
            continue
        if line.startswith("•"):
            rest = line.lstrip("• ").strip()
            if rest:
                bullets.append(rest)
                waiting = False
            else:
                waiting = True
            continue
        if waiting:
            bullets.append(line)
            waiting = False
            continue
        flush()
        nxt = next((l for l in lines[i + 1:] if l), "")
        if looks_like_heading(line, nxt):
            out.append(f"<h2>{esc(line.rstrip(':'))}</h2>")
        else:
            out.append(f"<p>{esc(line)}</p>")
    flush()
    return "\n".join(out)


PAGE_CSS = """:root{--void:#0c0a1a;--panel:#15122b;--panel-2:#1c1838;--line:#2c2554;
--line-soft:#231e45;--text:#eceaff;--muted:#948cc0;--amber:#ffb03f;--cyan:#5fe3ff;
--apply-text:#fff}
/* Тема берётся из настройки сайта, а если человек сюда попал из поиска —
   из настройки его системы. Иначе страница вакансии светит тёмным в глаза. */
:root[data-theme="light"]{--void:#f6f5fb;--panel:#ffffff;--panel-2:#f1eff9;
--line:#d9d5ec;--line-soft:#e7e4f3;--text:#191634;--muted:#6a6392;
--amber:#b06c00;--cyan:#0a7c9e;--apply-text:#fff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--void);color:var(--text);line-height:1.6;
font-family:"Manrope",system-ui,-apple-system,"Segoe UI",sans-serif;font-size:15px}
a{color:var(--cyan)}
.wrap{max-width:880px;margin:0 auto;padding:26px 20px 60px}
header{border-bottom:1px solid var(--line-soft);margin-bottom:26px;padding-bottom:16px}
.logo{font-family:"Unbounded",system-ui,sans-serif;font-weight:700;font-size:22px;
letter-spacing:.02em;color:var(--text);text-decoration:none}
.logo b{color:var(--amber)}
h1{font-size:26px;line-height:1.25;margin:0 0 8px}
h2{font-size:17px;margin:26px 0 10px}
.sub{color:var(--muted);font-size:14px}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0}
.tag{font-size:12px;color:var(--muted);background:var(--panel-2);border:1px solid var(--line-soft);
border-radius:7px;padding:3px 9px}
.tag.remote{color:var(--cyan);border-color:var(--line-soft);background:var(--panel-2)}
.apply{display:inline-block;background:var(--amber);color:var(--apply-text);text-decoration:none;
font-weight:700;padding:12px 24px;border-radius:9px;margin:18px 0}
.apply:hover{filter:brightness(1.1)}
.desc p{margin:0 0 12px}
.desc h2{font-family:"Unbounded",system-ui,sans-serif;font-size:13px;font-weight:700;
letter-spacing:.05em;text-transform:uppercase;color:var(--text);
margin:26px 0 10px;padding-top:16px;border-top:1px solid var(--line-soft)}
.desc h2:first-child{margin-top:0;padding-top:0;border-top:none}
.desc ul{list-style:none;margin:0 0 16px;padding:0}
.desc li{position:relative;padding-left:20px;margin:0 0 7px}
.desc li::before{content:"\u25B8";position:absolute;left:2px;color:var(--cyan);font-size:12px}
.card{display:block;border:1px solid var(--line-soft);background:var(--panel);border-radius:12px;
padding:13px 15px;margin-bottom:9px;text-decoration:none;color:var(--text)}
.card:hover{border-color:var(--line)}
.card .cmp{color:var(--muted);font-size:13px;margin-top:3px}
footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--line-soft);
color:var(--muted);font-size:13px}
.crumbs{font-size:13px;color:var(--muted);margin-bottom:14px}
.crumbs a{color:var(--muted)}
.note{color:var(--muted);font-size:13px;margin-top:10px}
"""


def page_shell(title, description, canonical, body, ld=None, depth=1):
    """Общая обёртка страницы. depth — сколько ../ до корня сайта."""
    up = "../" * depth
    ld_block = ""
    if ld:
        ld_block = ('<script type="application/ld+json">'
                    + json.dumps(ld, ensure_ascii=False) + "</script>\n")
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)[:300]}">
<link rel="canonical" href="{esc(canonical)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)[:300]}">
<meta property="og:type" content="website">
<meta property="og:url" content="{esc(canonical)}">
<meta property="og:image" content="{SITE}/og.png">
<meta name="theme-color" content="#0c0a1a">
<script>
  // Ставим тему до отрисовки, иначе страница мигнёт тёмным и станет светлой.
  // Фигурные скобки удвоены: этот кусок лежит внутри f-строки Python.
  try {{
    var t = localStorage.getItem("theme");
    if (!t) t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    document.documentElement.dataset.theme = t;
    if (t === "light") {{
      document.querySelector('meta[name="theme-color"]').setAttribute("content", "#f6f5fb");
    }}
  }} catch (e) {{}}
</script>
<link rel="icon" href="{up}favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@700&family=Manrope:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{up}page.css">
{ld_block}</head>
<body>
<div class="wrap">
<header><a class="logo" href="{up}">LOOT<b>WORK</b></a></header>
{body}
<footer>
  Вакансии собираются автоматически с карьерных страниц игровых студий.
  Мы не работодатель и не принимаем отклики — откликаться нужно на сайте студии.<br>
  <a href="{up}">Все вакансии на LOOTWORK</a>
</footer>
</div>
</body>
</html>
"""


def job_ld(j):
    """Разметка вакансии для поисковиков — из-за неё вакансия может попасть
    в отдельный блок с карточками работы в выдаче Google."""
    ld = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": j.get("title"),
        "description": (j.get("desc") or j.get("title") or "")[:5000],
        "identifier": {"@type": "PropertyValue", "name": j.get("company"), "value": j.get("id")},
        "hiringOrganization": {"@type": "Organization", "name": j.get("company")},
        "directApply": False,
        "url": f"{SITE}/job/{j['id']}/",
    }
    if j.get("posted"):
        ld["datePosted"] = j["posted"]
    locs = j.get("locations") or []
    if locs:
        ld["jobLocation"] = [{"@type": "Place",
                              "address": {"@type": "PostalAddress", "addressLocality": l}}
                             for l in locs[:3]]
    if j.get("remote"):
        ld["jobLocationType"] = "TELECOMMUTE"
        if not locs:
            ld["applicantLocationRequirements"] = {"@type": "Country", "name": "Worldwide"}
    return ld


RKIND_WORD = {"worldwide": "удалёнка по миру", "zone": "удалёнка в регионе", "hybrid": "гибрид"}


def job_page(j, same_company):
    where = ", ".join(j.get("locations") or []) or "локация не указана"
    tags = []
    if j.get("remote") or j.get("rkind"):
        tags.append(('<span class="tag remote">'
                     + esc(RKIND_WORD.get(j.get("rkind"), "удалёнка")) + "</span>"))
    if j.get("pool"):
        tags.append('<span class="tag">заявка в резерв</span>')
    for v in [j.get("grade"), j.get("role"), j.get("spec")]:
        if v:
            tags.append(f'<span class="tag">{esc(v)}</span>')
    for v in (j.get("stack") or [])[:5]:
        tags.append(f'<span class="tag">{esc(v)}</span>')

    near = ""
    if same_company:
        rows = "".join(
            f'<a class="card" href="../{esc(o["id"])}/"><div>{esc(o["title"])}</div>'
            f'<div class="cmp">{esc(", ".join(o.get("locations") or []) or "—")}</div></a>'
            for o in same_company[:6])
        near = f'<h2>Ещё в {esc(j["company"])}</h2>{rows}'

    body = f"""<div class="crumbs"><a href="../../">Вакансии</a> ·
  <a href="../../company/{slugify(j['company'])}/">{esc(j['company'])}</a></div>
<h1>{esc(j['title'])}</h1>
<div class="sub">{esc(j['company'])} · {esc(where)}{(' · опубликовано ' + esc(human_date(j['posted']))) if j.get('posted') else ''}</div>
<div class="tags">{''.join(tags)}</div>
<a class="apply" href="{esc(j['url'])}" target="_blank" rel="nofollow noopener">Откликнуться на сайте студии</a>
<div class="desc">{desc_html(j.get('desc'))}</div>
<div class="note">Отклик принимает студия на своём сайте. LOOTWORK только показывает вакансию.</div>
{near}
"""
    title = f"{j['title']} — {j['company']} | LOOTWORK"
    raw = re.sub(r"\\s+", " ", (j.get("desc") or "")).strip()
    descr = f"{j['title']} — {j['company']}, {where}."
    if raw:
        cut = raw[:165]
        if len(raw) > 165:
            cut = cut[:cut.rfind(" ")] + "…"
        descr = cut
    return page_shell(title, descr, f"{SITE}/job/{j['id']}/", body, job_ld(j), depth=2)


def facts_block(jobs, depth):
    """Немного живых цифр и ссылок вглубь: страница со сплошным списком
    ссылок для поисковика выглядит пустой, а такие блоки дают ей смысл."""
    remote = sum(1 for j in jobs if j.get("remote") or j.get("rkind"))
    with_pay = sum(1 for j in jobs if j.get("salary"))
    companies = {}
    places = {}
    for j in jobs:
        if j.get("company"):
            companies[j["company"]] = companies.get(j["company"], 0) + 1
        for loc in j.get("locations") or []:
            places[loc] = places.get(loc, 0) + 1

    top_c = sorted(companies.items(), key=lambda kv: -kv[1])[:8]
    top_p = sorted(places.items(), key=lambda kv: -kv[1])[:8]
    up = "../" * depth

    parts = [f"<p>Из них с удалёнкой — {remote}, с указанной зарплатой — {with_pay}. "
             f"Список пересобирается каждый день, закрытые вакансии удаляются автоматически.</p>"]
    if top_c:
        links = ", ".join(f'<a href="{up}company/{slugify(n)}/">{esc(n)}</a> ({c})'
                          for n, c in top_c)
        parts.append(f"<h2>Студии</h2><p>{links}</p>")
    if top_p:
        parts.append("<h2>Города и страны</h2><p>" +
                     ", ".join(f"{esc(n)} ({c})" for n, c in top_p) + "</p>")
    return "".join(parts)


def list_page(heading, intro, jobs, canonical, depth, extra=""):
    rows = "".join(
        f'<a class="card" href="{"../" * depth}job/{esc(j["id"])}/"><div>{esc(j["title"])}</div>'
        f'<div class="cmp">{esc(j["company"])} · '
        f'{esc(", ".join(j.get("locations") or []) or "локация не указана")}</div></a>'
        for j in jobs)
    body = (f"<h1>{esc(heading)}</h1><div class=\"sub\">{esc(intro)}</div>"
            f"{extra}"
            f"<h2>Открытые вакансии</h2><div>{rows}</div>"
            f"{facts_block(jobs, depth)}")
    return page_shell(f"{heading} | LOOTWORK", intro, canonical, body, None, depth)


def write_terms():
    """Условия и, главное, понятный путь для студии, которая не хочет тут быть.
    Письмо «уберите» решается за пять минут, претензия — нет."""
    body = """<h1>Условия использования</h1>
<div class="sub">Коротко: сервис бесплатный, работает как есть, откликами и наймом мы не занимаемся.</div>

<h2>Что такое LOOTWORK</h2>
<p>Это витрина открытых вакансий, которые автоматически собираются с публичных
карьерных страниц игровых студий. Мы не работодатель, не кадровое агентство
и не посредник. Отклик отправляется на сайте студии, напрямую ей.</p>

<h2>За что мы не отвечаем</h2>
<p>Тексты вакансий, условия работы, зарплаты и сроки публикуют студии и их
системы найма. LOOTWORK автоматически получает эти данные, приводит их
к единому виду и показывает. Мы не гарантируем полноту и отсутствие ошибок:
перед откликом проверяйте вакансию на сайте студии.</p>
<p>Роль, специализация, грейд, формат работы, стек и зарплатная вилка часто
определяются нашим сборщиком автоматически, по тексту вакансии. Эти метки
иногда бывают неточными — первоисточником всегда остаётся сама вакансия.</p>
<p>Вакансия может закрыться в любой момент, даже если у нас она ещё показана:
база пересобирается раз в сутки.</p>
<p>Сервис работает как есть, без гарантий доступности. Мы не отвечаем за
решения, принятые на основании информации с сайта, и за то, что происходит
после перехода на сайт студии.</p>

<h2>Если вы студия</h2>
<p>Мы читаем только то, что вы опубликовали открыто, и всегда ведём человека
на ваш сайт: мы не принимаем отклики и не храним резюме. Показ вакансий здесь
не подразумевает партнёрства и не даёт нам никаких прав на ваши материалы.</p>
<p>Если вы не хотите, чтобы вакансии вашей компании были на LOOTWORK, напишите
на <a href="mailto:gamesandfinbusiness@gmail.com">gamesandfinbusiness@gmail.com</a>
с корпоративной почты или другого официального канала компании. После проверки
запроса уберём студию из сбора — как правило, в течение суток. То же касается
отдельных вакансий, логотипов и других материалов.</p>

<h2>Если вы соискатель</h2>
<p>Проверяйте вакансию на сайте студии перед откликом. Если ссылка не работает
или вакансия закрыта, нажмите на карточке «Сообщить о проблеме» — это помогает
чистить базу.</p>

<h2>Изменения</h2>
<p>Условия могут меняться вместе с сервисом. Актуальная версия всегда на этой странице.</p>
"""
    d = HERE / "terms"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(page_shell(
        "Условия использования | LOOTWORK",
        "LOOTWORK не работодатель и не агентство. Как убрать студию из сбора и за что мы не отвечаем.",
        f"{SITE}/terms/", body, None, 1), encoding="utf-8")


def write_privacy():
    """Что собираем и что храним. Коротко и без юридического тумана."""
    body = """<h1>Приватность</h1>
<div class="sub">Коротко: аккаунтов нет, резюме мы не собираем, отклики идут мимо нас.</div>

<h2>Что остаётся в вашем браузере</h2>
<p>Избранное, скрытые вакансии, отметки о жалобах и выбранный язык хранятся
локально, в самом браузере. На сервер они не уходят и нам не видны.
Очистка данных сайта в браузере стирает их полностью.</p>

<h2>Аналитика</h2>
<p>Счётчик посещений подключается <b>только после вашего согласия</b>. Пока вы не
ответили или отказались, Яндекс.Метрика не загружается вовсе.</p>
<p>Если вы согласились, Метрика может получать техническую информацию о визите:
просмотренные страницы, источник перехода, сведения об устройстве и браузере,
приблизительное местоположение и IP-адрес. Мы используем это только в общем виде —
чтобы понимать, сколько людей заходит и что им пригодилось.</p>
<p>Изменить решение можно в любой момент: ссылка «Настройки аналитики» в самом низу
главной страницы.</p>

<h2>Другие внешние сервисы</h2>
<p>Шрифты подгружаются с серверов Google Fonts, поэтому при открытии страницы
браузер обращается к ним. Отдельных данных о вас мы туда не передаём.</p>

<h2>Формы</h2>
<p>Если вы жалуетесь на вакансию, уходит номер и название вакансии, студия,
ссылка, причина и когда вы столкнулись с проблемой. Если пишете нам —
тема, текст и контакт, который вы указали сами.</p>
<p>Обе формы работают через Google Формы: данные проходят через сервисы Google
и доступны владельцу LOOTWORK для разбора обращения.</p>

<h2>Отклики</h2>
<p>Кнопка «Откликнуться» ведёт на сайт студии. Резюме, письма и анкеты
мы не принимаем и не пересылаем — всё это происходит между вами и студией.</p>

<h2>Вопросы</h2>
<p>Пишите через кнопку «Написать нам» на главной.</p>
"""
    d = HERE / "privacy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(page_shell(
        "Приватность | LOOTWORK",
        "Что LOOTWORK хранит в браузере, что видит в статистике и что уходит через формы.",
        f"{SITE}/privacy/", body, None, 1), encoding="utf-8")


def write_about(jobs, by_company):
    """Открытая статистика покрытия: сколько студий, откуда берём, чего нет."""
    sources = {}
    for j in jobs:
        src = j.get("source")
        if src:
            sources[src] = sources.get(src, 0) + 1
    src_rows = "".join(f"<li>{esc(k)} — {v}</li>"
                       for k, v in sorted(sources.items(), key=lambda kv: -kv[1]))
    top = sorted(by_company.items(), key=lambda kv: -len(kv[1]))
    comp_rows = "".join(
        f'<a class="card" href="../company/{slugify(name)}/"><div>{esc(name)}</div>'
        f'<div class="cmp">{jobs_word(len(items))}</div></a>'
        for name, items in top if name)
    with_pay = sum(1 for j in jobs if j.get("salary"))
    remote = sum(1 for j in jobs if j.get("remote") or j.get("rkind"))

    body = f"""<h1>Покрытие базы</h1>
<div class="sub">Честно о том, что здесь есть и чего нет.</div>

<h2>Цифры</h2>
<p>{jobs_word(len(jobs))} от {len(by_company)} студий.
С указанной зарплатой — {with_pay}. С удалёнкой — {remote}.
База пересобирается каждый день: мы заново читаем карьерные страницы студий
и проверяем, что ссылки на вакансии ещё работают.</p>

<h2>Откуда берём</h2>
<ul>{src_rows}</ul>
<p>Это системы найма, к которым студии подключают свои карьерные страницы.
Мы читаем их напрямую, поэтому здесь нет агентств, перепостов и одной
вакансии в пяти местах.</p>

<h2>Чего пока нет</h2>
<p>Российских студий: hh закрыл доступ для сторонних сервисов в декабре 2025.
Вакансий из Telegram-каналов. Зарплат у большинства вакансий — их указывают
далеко не все студии. И это не все вакансии геймдева: только те студии,
которых мы читаем.</p>

<h2>Студии в базе</h2>
{comp_rows}
"""
    d = HERE / "about"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(page_shell(
        "Покрытие базы | LOOTWORK",
        f"{jobs_word(len(jobs))} от {len(by_company)} студий. Откуда берём и чего пока нет.",
        f"{SITE}/about/", body, None, 1), encoding="utf-8")


def write_pages(jobs, today):
    """Раскладываем страницы заново. Старые удаляем целиком: вакансии умирают,
    и оставлять их адреса нельзя — поисковик накажет за мёртвые страницы."""
    for name in PAGE_DIRS:
        shutil.rmtree(HERE / name, ignore_errors=True)

    (HERE / "page.css").write_text(PAGE_CSS, encoding="utf-8")

    by_company, by_role, by_spec = {}, {}, {}
    for j in jobs:
        by_company.setdefault(j.get("company"), []).append(j)
        if j.get("role"):
            by_role.setdefault(j["role"], []).append(j)
        if j.get("spec"):
            by_spec.setdefault(j["spec"], []).append(j)

    urls = [f"{SITE}/"]

    for j in jobs:
        others = [o for o in by_company.get(j.get("company"), []) if o["id"] != j["id"]]
        d = HERE / "job" / j["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(job_page(j, others), encoding="utf-8")
        urls.append(f"{SITE}/job/{j['id']}/")

    for company, items in by_company.items():
        if not company:
            continue
        slug = slugify(company)
        d = HERE / "company" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(list_page(
            f"Вакансии {company}",
            f"{jobs_word(len(items))} в {company} — напрямую с карьерной страницы студии.",
            items, f"{SITE}/company/{slug}/", 2), encoding="utf-8")
        urls.append(f"{SITE}/company/{slug}/")

    for role, items in by_role.items():
        slug = slugify(role)
        d = HERE / "role" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(list_page(
            f"{role} — вакансии в геймдеве",
            f"{jobs_word(len(items))} по направлению «{role}» напрямую от игровых студий.",
            items, f"{SITE}/role/{slug}/", 2), encoding="utf-8")
        urls.append(f"{SITE}/role/{slug}/")

    for spec, items in by_spec.items():
        slug = slugify(spec)
        d = HERE / "spec" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(list_page(
            f"{spec} — вакансии в геймдеве",
            f"{jobs_word(len(items))}: {spec}. Напрямую от игровых студий.",
            items, f"{SITE}/spec/{slug}/", 2), encoding="utf-8")
        urls.append(f"{SITE}/spec/{slug}/")

    # «unity разработчик удалённо» и «работа в геймдеве варшава» — запросы,
    # где конкуренция в разы слабее, чем у общего «вакансии геймдев».
    # Под каждый такой запрос нужна своя страница, иначе показывать нечего.
    for role, items in by_role.items():
        rem = [j for j in items if j.get("remote") or j.get("rkind")]
        if len(rem) < 3:
            continue
        slug = slugify(role)
        d = HERE / "role" / slug / "remote"
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(list_page(
            f"{role} удалённо — вакансии в геймдеве",
            f"{jobs_word(len(rem))} по направлению «{role}» с удалённой работой. "
            f"Напрямую с карьерных страниц игровых студий.",
            rem, f"{SITE}/role/{slug}/remote/", 3), encoding="utf-8")
        urls.append(f"{SITE}/role/{slug}/remote/")

    for spec, items in by_spec.items():
        rem = [j for j in items if j.get("remote") or j.get("rkind")]
        if len(rem) < 3:
            continue
        slug = slugify(spec)
        d = HERE / "spec" / slug / "remote"
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(list_page(
            f"{spec} удалённо — вакансии в геймдеве",
            f"{jobs_word(len(rem))}: {spec}, удалённая работа. От игровых студий напрямую.",
            rem, f"{SITE}/spec/{slug}/remote/", 3), encoding="utf-8")
        urls.append(f"{SITE}/spec/{slug}/remote/")

    by_place = {}
    for j in jobs:
        for loc in j.get("locations") or []:
            by_place.setdefault(loc, []).append(j)
    for place, items in by_place.items():
        if len(items) < 4:
            continue
        slug = slugify(place)
        d = HERE / "where" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(list_page(
            f"Работа в геймдеве: {place}",
            f"{jobs_word(len(items))} в игровых студиях, {place}. "
            f"Собрано с карьерных страниц студий, обновляется каждый день.",
            items, f"{SITE}/where/{slug}/", 2), encoding="utf-8")
        urls.append(f"{SITE}/where/{slug}/")

    remote = [j for j in jobs if j.get("remote") or j.get("rkind")]
    if remote:
        d = HERE / "remote"
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(list_page(
            "Удалённая работа в геймдеве",
            f"{jobs_word(len(remote))} с удалёнкой от игровых студий.",
            remote, f"{SITE}/remote/", 1), encoding="utf-8")
        urls.append(f"{SITE}/remote/")

    write_privacy()
    write_terms()
    urls.append(f"{SITE}/terms/")
    write_about(jobs, by_company)
    urls.append(f"{SITE}/privacy/")
    urls.append(f"{SITE}/about/")

    d = HERE / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(list_page(
        "Все вакансии в геймдеве",
        f"{jobs_word(len(jobs))} от {len(by_company)} студий. Обновляется каждый день.",
        jobs, f"{SITE}/jobs/", 1), encoding="utf-8")
    urls.append(f"{SITE}/jobs/")

    print(f"Страниц разложено: {len(urls)}")
    return urls


# ------------------------------------------------------------- поиск студии
# Угадывать адрес студии в чужой системе найма — гиблое дело. Проще открыть
# её страницу «Карьера» и посмотреть, куда она на самом деле ведёт.

ATS_PATTERNS = [
    ("greenhouse",      r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"),
    ("greenhouse",      r"api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)"),
    ("lever",           r"jobs\.lever\.co/([a-z0-9_-]+)"),
    ("lever",           r"api\.lever\.co/v0/postings/([a-z0-9_-]+)"),
    ("recruitee",       r"([a-z0-9_-]+)\.recruitee\.com"),
    ("ashby",           r"jobs\.ashbyhq\.com/([a-zA-Z0-9_.-]+)"),
    ("ashby",           r"api\.ashbyhq\.com/posting-api/job-board/([a-zA-Z0-9_.-]+)"),
    ("workable",        r"apply\.workable\.com/([a-z0-9_-]+)"),
    ("workable",        r"([a-z0-9_-]+)\.workable\.com"),
    ("smartrecruiters", r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)"),
]

SKIP_TOKENS = {"www", "apply", "jobs", "careers", "api", "embed", "job", "boards"}


def detect(url: str):
    """Смотрим страницу карьеры и вытаскиваем систему найма с адресом студии."""
    print(f"Смотрю {url}")
    try:
        r = http_get(url, tries=2)
        html = r.text
    except Exception as e:
        print(f"  не открылась: {str(e)[:70]}")
        return

    found, seen = [], set()
    for ats, pattern in ATS_PATTERNS:
        for m in re.finditer(pattern, html, re.I):
            token = m.group(1)
            if token.lower() in SKIP_TOKENS or (ats, token) in seen:
                continue
            seen.add((ats, token))
            found.append((ats, token))

    if not found:
        print("  ничего не нашлось — похоже, своя система найма")
        return

    for ats, token in found:
        probe = {"name": "проверка", "token": token, "site": ""}
        try:
            jobs = FETCHERS[ats](probe)
            mark = "OK " if jobs else "пусто"
            print(f'  {mark} {ats}/{token} — вакансий: {len(jobs)}')
            if jobs:
                print(f'    {{"name": "СТУДИЯ", "ats": "{ats}", "token": "{token}", '
                      f'"site": "", "enabled": true}},')
        except Exception as e:
            print(f"  нет  {ats}/{token} — {str(e)[:60]}")
        time.sleep(PAUSE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="только проверить доски студий, ничего не записывать")
    ap.add_argument("--all", action="store_true",
                    help="при проверке брать и выключенных кандидатов тоже")
    ap.add_argument("--detect", metavar="URL",
                    help="найти систему найма по странице карьеры студии")
    ap.add_argument("--no-verify", action="store_true",
                    help="пропустить проверку живости ссылок (быстрее)")
    args = ap.parse_args()

    if not COMPANIES.exists():
        sys.exit(f"Нет файла {COMPANIES.name} — заполни список студий.")

    if args.detect:
        detect(args.detect)
        return

    companies = json.loads(COMPANIES.read_text(encoding="utf-8"))
    if not (args.probe and args.all):
        companies = [c for c in companies if c.get("enabled", True)]
    print(f"Студий в списке: {len(companies)}\n")

    if args.probe:
        probe(companies)
        return

    jobs = collect(companies, verify_links=not args.no_verify)
    write_js(jobs)


if __name__ == "__main__":
    main()
