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
import json
import re
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
BLOCKLIST = HERE / "blocklist.json"   # сюда попадают id, снятые по жалобам

# hh.ru требует строгий формат подписи: НазваниеПриложения/версия (email).
# Впиши сюда свою почту — иначе hh может начать резать запросы.
CONTACT_EMAIL = "gamedev.jobs.board@gmail.com"

UA = {"User-Agent": "gamedev-jobs/1.0 (+https://github.com/)"}
HH_UA = {"User-Agent": f"gamedev-jobs/1.0 ({CONTACT_EMAIL})"}
TIMEOUT = 20
PAUSE = 0.4          # пауза между запросами, чтобы не долбить чужие сервера

# ---------------------------------------------------------------- разметка

# Порядок важен: первое совпадение выигрывает, поэтому более узкие роли выше.
ROLE_RULES = [
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
    ("Продюсирование",  r"producer|продюсер|project manager|проектный менеджер|product manager|продакт"),
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


def classify_role(title: str):
    low = title.lower()
    for role, pattern in ROLE_RULES:
        if re.search(pattern, low, re.I):
            return role
    return None


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

# лишние административные слова, которые ничего не говорят кандидату
NOISE = re.compile(
    r"\b(voivodeship|province|prefecture|county|oblast|region|district|"
    r"metropolitan area|greater|state of)\b", re.I)


def clean_locations(raw):
    """Возвращает (список мест, признак удалёнки)."""
    if not raw:
        return [], False

    text = str(raw)
    remote = bool(REMOTE_WORDS.search(text))

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
    if p.lower() in {"n/a", "various", "multiple", "other", "-", "office", "europe"}:
        return None
    return p


# ---------------------------------------------------------------- источники

def fetch_greenhouse(company: dict):
    """Greenhouse отдаёт публичный список вакансий без ключа."""
    token = company["token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
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
            "salary": None,          # Greenhouse вилку в списке не отдаёт
            "posted": (job.get("updated_at") or "")[:10] or None,
            "url": job.get("absolute_url"),
            "desc": html_to_text(job.get("content")),
            "site": company.get("site"),
            "source": "greenhouse",
        })
    return out


def fetch_lever(company: dict):
    token = company["token"]
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
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
            "salary": None,
            "posted": posted_iso,
            "url": job.get("hostedUrl"),
            "desc": lever_desc(job),
            "source": "lever",
                "site": company.get("site"),
        })
    return out


def fetch_recruitee(company: dict):
    """Recruitee — на нём много студий в Польше и Восточной Европе."""
    token = company["token"]
    url = f"https://{token}.recruitee.com/api/offers/"
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
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
            "salary": None,
            "posted": (job.get("published_at") or job.get("created_at") or "")[:10] or None,
            "url": job.get("careers_url") or job.get("url"),
            "desc": html_to_text(job.get("description")),
            "source": "recruitee",
                "site": company.get("site"),
        })
    return out


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
        r = requests.get("https://api.hh.ru/vacancies", params=params,
                         headers=HH_UA, timeout=TIMEOUT)
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


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "recruitee": fetch_recruitee,
    "hh": fetch_hh,
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

def check_alive(url: str) -> bool:
    """Ссылка живая, если страница отвечает и на ней нет пометки о закрытии."""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return False
        low = r.text[:200_000].lower()
        dead_marks = ("no longer accepting", "position has been filled",
                      "вакансия закрыта", "вакансия неактуальна", "job is closed")
        return not any(m in low for m in dead_marks)
    except Exception:
        return False


def collect(companies, verify_links: bool):
    raw = []
    for c in companies:
        fetcher = FETCHERS.get(c.get("ats"))
        if not fetcher:
            continue
        try:
            got = fetcher(c)
            raw.extend(got)
            print(f"  {c['name']:<28} +{len(got)}")
        except Exception as e:
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

        role = classify_role(j["title"])
        if not role:
            continue                      # не смогли отнести к геймдеву — пропускаем

        key = (j["company"].lower(), j["title"].lower())
        if key in seen:
            continue
        seen.add(key)

        j["role"] = role
        j["grade"] = classify_grade(j["title"])
        j["stack"] = j["title"].lower()
        jobs.append(j)

    if verify_links:
        print(f"\nПроверяю живость {len(jobs)} ссылок…")
        live = []
        for i, j in enumerate(jobs, 1):
            if check_alive(j["url"]):
                live.append(j)
            else:
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
    OUT_JS.write_text(
        "// jobs.js — сгенерировано collect.py, руками не править.\n"
        f"// Обновлено: {today}. Вакансий: {len(jobs)}.\n\n"
        "window.JOBS_DEMO = false;\n"
        f'window.JOBS_UPDATED = "{today}";\n'
        "window.REPORT_FORM = null;   // сюда — ссылка на форму жалоб\n\n"
        f"window.JOBS = {body};\n",
        encoding="utf-8",
    )
    print(f"\nЗаписано {len(jobs)} вакансий в {OUT_JS.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="только проверить доски студий, ничего не записывать")
    ap.add_argument("--no-verify", action="store_true",
                    help="пропустить проверку живости ссылок (быстрее)")
    args = ap.parse_args()

    if not COMPANIES.exists():
        sys.exit(f"Нет файла {COMPANIES.name} — заполни список студий.")

    companies = json.loads(COMPANIES.read_text(encoding="utf-8"))
    companies = [c for c in companies if c.get("enabled", True)]
    print(f"Студий в списке: {len(companies)}\n")

    if args.probe:
        probe(companies)
        return

    jobs = collect(companies, verify_links=not args.no_verify)
    write_js(jobs)


if __name__ == "__main__":
    main()
