"""매일 아침 뉴스 + 날씨 음성 브리핑 생성기 (무료 구성)

사용법:
  python briefing.py build   # 음성(mp3) + 웹페이지 + 이메일 내용 생성
  python briefing.py send    # 만들어 둔 이메일 발송
"""
import asyncio
import calendar
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from string import Template
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests

# ───────── 설정 (필요하면 GitHub Variables 또는 워크플로 파일에서 변경) ─────────
TZ_NAME = os.getenv("TZ_NAME", "America/Vancouver")
CITY = os.getenv("CITY_NAME", "밴쿠버")
LAT = float(os.getenv("LAT", "49.2827"))
LON = float(os.getenv("LON", "-123.1207"))
PAGE_URL = os.getenv("PAGE_URL", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TTS_VOICE = os.getenv("TTS_VOICE", "ko-KR-SunHiNeural")
ITEMS_PER_TOPIC = 3

TZ = ZoneInfo(TZ_NAME)
SITE = Path("site")
BUILD = Path("build")
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

# 주제별 검색어(구글뉴스 RSS). 영어 기사는 Gemini가 한국어로 번역·요약합니다.
def gn_ca(q):
    return "https://news.google.com/rss/search?hl=en-CA&gl=CA&ceid=CA:en&q=" + quote_plus(q)


def gn_ko(q):
    return "https://news.google.com/rss/search?hl=ko&gl=KR&ceid=KR:ko&q=" + quote_plus(q)


LOCAL = '(Coquitlam OR "Port Coquitlam" OR "Port Moody" OR "Tri-Cities")'
TOPICS = {
    "BC·연방 정부 정책": [
        gn_ca('("B.C. government" OR "British Columbia government" OR "B.C. legislature" OR "B.C. premier" OR "B.C. minister") (policy OR announces OR legislation OR funding) when:1d'),
        gn_ca('Canada ("federal government" OR "Government of Canada" OR Ottawa) (policy OR announces OR legislation OR budget) when:1d'),
        "https://www.cbc.ca/webfeed/rss/rss-politics",
    ],
    "광역밴쿠버 부동산": [
        gn_ca('("Metro Vancouver" OR Vancouver OR "B.C.") ("real estate" OR "housing market" OR housing OR rent OR mortgage) when:1d'),
        gn_ca('("B.C." OR Canada) ("housing policy" OR "Bank of Canada" OR "interest rate" OR "home sales") when:1d'),
    ],
    "코퀴틀람 개발·행사": [
        gn_ca(LOCAL + ' (development OR rezoning OR "city council" OR transit OR construction OR plan) when:3d'),
        gn_ca(LOCAL + ' (event OR festival OR community OR "this weekend") when:3d'),
    ],
    "코퀴틀람·광역밴쿠버 사건·사고": [
        gn_ca('(Coquitlam OR "Port Coquitlam" OR "Port Moody" OR Burnaby OR "Tri-Cities") (police OR RCMP OR fire OR crash OR collision OR closure OR emergency) when:1d'),
        gn_ca('("Metro Vancouver" OR Vancouver OR Surrey OR Burnaby) (fatal OR evacuation OR "road closure" OR wildfire OR "storm warning" OR major) when:1d'),
        "https://www.cbc.ca/webfeed/rss/rss-canada-britishcolumbia",
    ],
    "한국 AI·반도체 동향": [
        gn_ko("AI 반도체 시장 동향 when:1d"),
        gn_ko("삼성전자 OR SK하이닉스 OR 엔비디아 OR HBM when:1d"),
        gn_ko("파운드리 OR 반도체 수출 OR 인공지능 투자 when:1d"),
    ],
}
DEFAULT_MAX_AGE_HOURS = 30            # 기본: 최근 30시간 이내 기사만
TOPIC_MAX_AGE = {"코퀴틀람 개발·행사": 72}  # 지역 소식은 드물어서 3일까지 허용
PER_FEED = 6                          # 검색어(피드)당 최대 기사 수
MAX_PER_TOPIC = 14

WMO = {
    0: ("맑음", "☀️"), 1: ("대체로 맑음", "🌤️"), 2: ("구름 조금", "⛅"), 3: ("흐림", "☁️"),
    45: ("안개", "🌫️"), 48: ("안개", "🌫️"),
    51: ("이슬비", "🌦️"), 53: ("이슬비", "🌦️"), 55: ("이슬비", "🌦️"),
    61: ("비", "🌧️"), 63: ("비", "🌧️"), 65: ("강한 비", "🌧️"),
    71: ("눈", "🌨️"), 73: ("눈", "🌨️"), 75: ("강한 눈", "❄️"), 77: ("싸락눈", "🌨️"),
    80: ("소나기", "🌦️"), 81: ("소나기", "🌦️"), 82: ("강한 소나기", "⛈️"),
    85: ("눈 소나기", "🌨️"), 86: ("눈 소나기", "🌨️"),
    95: ("뇌우", "⛈️"), 96: ("뇌우", "⛈️"), 99: ("뇌우", "⛈️"),
}


def now_local():
    return datetime.now(TZ)


def date_label(n):
    return f"{n.month}월 {n.day}일 {WEEKDAYS[n.weekday()]}요일"


# ───────── 날씨 ─────────
def get_weather():
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LAT, "longitude": LON, "timezone": TZ_NAME, "forecast_days": 1,
                "current": "temperature_2m,apparent_temperature,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            },
            timeout=20,
        )
        r.raise_for_status()
        j = r.json()
        code = int(j["daily"]["weather_code"][0])
        desc, emoji = WMO.get(code, ("변화 있는 날씨", "🌈"))
        w = {
            "city": CITY, "desc": desc, "emoji": emoji, "code": code,
            "now": round(j["current"]["temperature_2m"]),
            "feels": round(j["current"]["apparent_temperature"]),
            "tmax": round(j["daily"]["temperature_2m_max"][0]),
            "tmin": round(j["daily"]["temperature_2m_min"][0]),
            "rain": int(j["daily"]["precipitation_probability_max"][0] or 0),
        }
        w["tip"] = outfit_tip(w)
        return w
    except Exception as e:
        print("날씨 조회 실패:", e)
        return None


def outfit_tip(w):
    t = w["tmax"]
    if t >= 25:
        tip = "더운 하루예요. 시원하고 가벼운 옷차림이 좋겠어요."
    elif t >= 20:
        tip = "포근해요. 가벼운 긴팔이면 충분해요."
    elif t >= 15:
        tip = "선선해요. 얇은 겉옷을 챙기면 좋아요."
    elif t >= 10:
        tip = "쌀쌀해요. 자켓이나 가디건을 입고 나가세요."
    elif t >= 5:
        tip = "꽤 추워요. 따뜻한 코트를 챙기세요."
    else:
        tip = "많이 추워요. 두꺼운 외투와 목도리, 장갑을 챙기세요."
    if w["code"] in (71, 73, 75, 77, 85, 86):
        tip += " 눈이 올 수 있으니 길이 미끄럽지 않은지 조심하세요."
    elif w["rain"] >= 50:
        tip += " 비 소식이 있으니 우산도 꼭 챙기세요."
    return tip


def weather_sentence(w):
    if not w:
        return "오늘 날씨 정보는 불러오지 못했어요."
    return (f"오늘 {w['city']}의 날씨는 {w['desc']}입니다. 지금 기온은 {w['now']}도, "
            f"낮 최고 {w['tmax']}도, 최저 {w['tmin']}도이고, 비 올 확률은 {w['rain']}퍼센트예요. {w['tip']}")


# ───────── 뉴스 수집 ─────────
def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def fetch_feed(url):
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (morning-briefing)"})
        r.raise_for_status()
        return feedparser.parse(r.content).entries
    except Exception as e:
        print("RSS 실패:", url[:70], e)
        return []


def collect_news():
    news, counter = {}, 0
    now_ts = time.time()
    for topic, urls in TOPICS.items():
        max_age = TOPIC_MAX_AGE.get(topic, DEFAULT_MAX_AGE_HOURS) * 3600
        seen, items = set(), []
        for url in urls:
            taken = 0
            for e in fetch_feed(url):
                if taken >= PER_FEED:
                    break
                pp = e.get("published_parsed")
                if pp and now_ts - calendar.timegm(pp) > max_age:
                    continue
                title = clean(e.get("title", ""))
                source = ""
                if " - " in title:
                    title, source = title.rsplit(" - ", 1)
                key = re.sub(r"\W", "", title)[:18].lower()
                if not title or key in seen:
                    continue
                seen.add(key)
                snippet = clean(e.get("summary", ""))
                if snippet.startswith(title[:10]):
                    snippet = ""
                items.append({"id": f"n{counter}", "title": title, "source": source,
                              "link": e.get("link", ""), "snippet": snippet[:200]})
                counter += 1
                taken += 1
        news[topic] = items[:MAX_PER_TOPIC]
        print(f"[{topic}] {len(news[topic])}건 수집")
    return news


# ───────── AI 선별·요약 (Gemini 무료 키, 없으면 헤드라인만) ─────────
def call_gemini(prompt, key):
    """무료 키는 가끔 429/503이 나므로 재시도하고, 안 되면 다른 모델로 넘어갑니다."""
    models = [GEMINI_MODEL, "gemini-2.5-flash-lite", "gemini-2.0-flash"]
    for model in dict.fromkeys(models):
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": key},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json", "temperature": 0.5}},
                    timeout=120,
                )
                if r.status_code in (429, 500, 503):
                    print(f"Gemini {model} 일시 오류 {r.status_code} (시도 {attempt + 1}/3): {r.text[:200]}")
                    time.sleep(8 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    print(f"Gemini {model} 오류 {r.status_code}: {r.text[:300]}")
                    break  # 이 모델은 포기하고 다음 모델
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                print(f"Gemini 성공: {model}")
                return text
            except Exception as e:
                print(f"Gemini {model} 예외: {e}")
                time.sleep(3)
    return None


def has_korean(t):
    return bool(re.search(r"[가-힣]", t or ""))


def translate_ko(text):
    """구글 번역(무료 공개 주소)으로 한국어 번역. 별도 패키지 불필요."""
    r = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t", "q": text},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
    )
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0]).strip()


def ensure_korean(brief):
    """한글이 없는 제목·요약은 한국어로 번역합니다(최종 안전장치)."""
    done, failed = 0, 0
    for s in brief["sections"]:
        for it in s["items"]:
            for k in ("headline", "summary"):
                if it.get(k) and not has_korean(it[k]):
                    try:
                        it[k] = translate_ko(it[k]) or it[k]
                        done += 1
                    except Exception as e:
                        failed += 1
                        print("번역 실패:", e)
    print(f"번역 보정: 성공 {done}건, 실패 {failed}건")
    return brief


def curate_with_gemini(news, weather, n):
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        print("★ GEMINI_API_KEY가 비어 있습니다. Secrets 이름/값을 확인하세요 → 번역 보정 모드로 진행")
        return None
    print(f"GEMINI_API_KEY 확인됨 (길이 {len(key)})")
    lines = []
    for topic, items in news.items():
        lines.append(f"## {topic}")
        for it in items:
            lines.append(f"- id={it['id']} | {it['title']} ({it['source']}) {it['snippet']}")
    prompt = f"""당신은 기분 좋은 하루를 열어 주는 아침 뉴스 브리핑 작가입니다.
독자는 캐나다 BC주 코퀴틀람(광역밴쿠버)에 사는 한국어 사용자입니다.
오늘은 {date_label(n)}입니다. 아래 뉴스 목록에서 주제별로 가장 중요한 {ITEMS_PER_TOPIC}개를 고르고(중복·유사 기사는 하나만), 음성으로 읽기 좋은 구어체 한국어로 정리하세요.

규칙:
- 목록의 제목과 설명에 있는 사실만 사용하고, 없는 내용은 절대 지어내지 마세요.
- 모든 출력(headline, summary, greeting, cheer)은 반드시 한국어로만 쓰세요. 영어 문장을 그대로 두지 마세요.
- 영어 기사는 자연스러운 한국어로 번역·요약하고, 지명·기관명은 한국에서 통용되는 표기를 쓰세요(예: 코퀴틀람, 브리티시컬럼비아주).
- 정부 정책·부동산 소식은 주민 생활이나 집값·금리에 어떤 의미인지 기사에 나온 범위 안에서 한 문장으로 덧붙이세요.
- 지역 소식(개발·행사·사건사고)은 코퀴틀람, 포트코퀴틀람, 포트무디, 버나비 등 인근 소식을 우선하세요.
- 주제는 목록의 순서를 그대로 유지하세요.
- headline은 20자 안팎으로 간결하게, summary는 1~2문장으로 쉽게 쓰세요.
- 무거운 소식도 차분하고 담담하게 전하세요. 과장·선정적 표현은 금지입니다.
- greeting은 따뜻하고 밝은 아침 인사 2문장, cheer는 오늘을 응원하는 짧은 한마디 1~2문장입니다.
- 뉴스가 없는 주제는 items를 빈 배열로 두세요.
- 오직 JSON만 출력하세요. 형식:
{{"greeting": "...", "sections": [{{"topic": "주제명", "items": [{{"id": "n0", "headline": "...", "summary": "..."}}]}}], "cheer": "..."}}

뉴스 목록:
{chr(10).join(lines)}
"""
    try:
        text = call_gemini(prompt, key)
        if not text:
            return None
        data = json.loads(text)
        by_id = {it["id"]: it for items in news.values() for it in items}
        sections = []
        for s in data.get("sections", []):
            items = []
            for it in s.get("items", []):
                src = by_id.get(it.get("id"))
                if src:
                    items.append({"headline": it.get("headline") or src["title"], "summary": it.get("summary", ""),
                                  "link": src["link"], "source": src["source"]})
            if items:
                sections.append({"topic": s.get("topic", ""), "items": items})
        if not sections:
            return None
        return {"greeting": data.get("greeting", ""), "sections": sections, "cheer": data.get("cheer", "")}
    except Exception as e:
        print("Gemini 실패 → 헤드라인 모드로 대체:", e)
        return None


def fallback_brief(news, n):
    sections = []
    for topic, items in news.items():
        top = [{"headline": i["title"], "summary": "", "link": i["link"], "source": i["source"]}
               for i in items[:ITEMS_PER_TOPIC]]
        if top:
            sections.append({"topic": topic, "items": top})
    return {"greeting": f"좋은 아침입니다! {date_label(n)} 아침 브리핑을 시작할게요.",
            "sections": sections, "cheer": "오늘도 좋은 일이 가득한 하루 되세요!"}


# ───────── 음성 ─────────
def build_script(brief, weather, n):
    parts = [brief["greeting"], weather_sentence(weather)]
    for s in brief["sections"]:
        parts.append(f"{s['topic']} 소식입니다.")
        for it in s["items"]:
            parts.append(f"{it['headline']}. {it['summary']}".strip())
    parts.append(brief["cheer"] or "오늘도 좋은 하루 보내세요!")
    parts.append("이상으로 아침 브리핑을 마칩니다. 힘찬 하루 되세요!")
    return "\n".join(p for p in parts if p)


def make_audio(text, path):
    try:
        import edge_tts

        async def run():
            await edge_tts.Communicate(text, TTS_VOICE).save(str(path))

        asyncio.run(run())
        if Path(path).stat().st_size < 1000:
            raise RuntimeError("음성 파일이 비어 있음")
        print("Edge TTS 완료")
    except Exception as e:
        print("Edge TTS 실패 → gTTS로 대체:", e)
        from gtts import gTTS
        gTTS(text, lang="ko").save(str(path))


# ───────── 웹페이지 / 이메일 ─────────
PAGE_TEMPLATE = Template("""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>아침 브리핑 · $date</title>
<style>
:root{--bg:#f6f8fb;--card:#fff;--text:#1d2733;--sub:#667085;--accent:#ff7a45;--line:#e6eaf0}
@media (prefers-color-scheme:dark){:root{--bg:#10151c;--card:#19212b;--text:#e8edf3;--sub:#9aa7b6;--line:#2a3441}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Noto Sans KR","Malgun Gothic",sans-serif;line-height:1.6}
main{max-width:640px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:8px 0 2px}.date{color:var(--sub);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:14px}
.hello{font-size:16px}audio{width:100%;margin-top:8px}
.weather{display:flex;gap:14px;align-items:center}.weather .big{font-size:44px}
.weather b{font-size:20px}.tip{color:var(--sub);margin-top:6px;font-size:14px}
h2{font-size:16px;margin:0 0 10px;color:var(--accent)}
.item{padding:10px 0;border-top:1px solid var(--line)}.item:first-of-type{border-top:0}
.item a{color:var(--text);font-weight:600;text-decoration:none}.item p{margin:4px 0 0;color:var(--sub);font-size:14px}
.src{font-size:12px;color:var(--sub)}.cheer{text-align:center;font-size:16px}
</style></head><body><main>
<h1>☀️ 아침 브리핑</h1><p class="date">$date</p>
<div class="card"><div class="hello">$greeting</div>
<audio controls preload="auto" src="briefing.mp3"></audio></div>
$weather_html
$sections_html
<div class="card cheer">💛 $cheer</div>
</main></body></html>""")


def weather_html(w):
    if not w:
        return ""
    return (f'<div class="card"><div class="weather"><div class="big">{w["emoji"]}</div><div>'
            f'<b>{html.escape(w["city"])} · {html.escape(w["desc"])}</b><br>'
            f'지금 {w["now"]}° (체감 {w["feels"]}°) · 최고 {w["tmax"]}° / 최저 {w["tmin"]}° · 비 {w["rain"]}%'
            f'</div></div><div class="tip">👕 {html.escape(w["tip"])}</div></div>')


def sections_html(brief):
    out = []
    for s in brief["sections"]:
        items = []
        for it in s["items"]:
            link = html.escape(it["link"], quote=True)
            summ = f'<p>{html.escape(it["summary"])}</p>' if it["summary"] else ""
            src = f' <span class="src">· {html.escape(it["source"])}</span>' if it["source"] else ""
            items.append(f'<div class="item"><a href="{link}" target="_blank" rel="noopener">{html.escape(it["headline"])}</a>{src}{summ}</div>')
        out.append(f'<div class="card"><h2>{html.escape(s["topic"])}</h2>{"".join(items)}</div>')
    return "\n".join(out)


def email_html(brief, w, n):
    btn = ""
    if PAGE_URL:
        btn = (f'<p style="text-align:center;margin:20px 0"><a href="{html.escape(PAGE_URL, quote=True)}" '
               'style="background:#ff7a45;color:#fff;padding:14px 26px;border-radius:30px;text-decoration:none;'
               'font-weight:bold;font-size:16px">🎧 음성 브리핑 듣기</a></p>')
    sec = ""
    for s in brief["sections"]:
        sec += f'<h3 style="color:#ff7a45;margin:18px 0 6px">{html.escape(s["topic"])}</h3>'
        for it in s["items"]:
            sec += (f'<p style="margin:6px 0"><a href="{html.escape(it["link"], quote=True)}" style="color:#1d2733;'
                    f'font-weight:600;text-decoration:none">{html.escape(it["headline"])}</a>'
                    f'<br><span style="color:#667085;font-size:14px">{html.escape(it["summary"])}</span></p>')
    wx = ""
    if w:
        wx = (f'<div style="background:#f6f8fb;border-radius:12px;padding:12px 14px;margin:12px 0">'
              f'<b>{w["emoji"]} {html.escape(w["city"])} · {html.escape(w["desc"])}</b><br>'
              f'지금 {w["now"]}° · 최고 {w["tmax"]}° / 최저 {w["tmin"]}° · 비 {w["rain"]}%<br>'
              f'<span style="color:#667085">👕 {html.escape(w["tip"])}</span></div>')
    return (f'<div style="max-width:600px;margin:auto;font-family:-apple-system,\'Malgun Gothic\',sans-serif;color:#1d2733">'
            f'<h2>☀️ {date_label(n)} 아침 브리핑</h2><p>{html.escape(brief["greeting"])}</p>{btn}{wx}{sec}'
            f'<p style="text-align:center;margin-top:24px">💛 {html.escape(brief["cheer"])}</p></div>')


# ───────── 실행 ─────────
def build():
    n = now_local()
    weather = get_weather()
    news = collect_news()
    brief = curate_with_gemini(news, weather, n) or fallback_brief(news, n)
    brief = ensure_korean(brief)
    script = build_script(brief, weather, n)

    SITE.mkdir(exist_ok=True)
    BUILD.mkdir(exist_ok=True)
    make_audio(script, SITE / "briefing.mp3")
    page = PAGE_TEMPLATE.substitute(
        date=date_label(n), greeting=html.escape(brief["greeting"]), weather_html=weather_html(weather),
        sections_html=sections_html(brief), cheer=html.escape(brief["cheer"]))
    (SITE / "index.html").write_text(page, encoding="utf-8")

    temp = f" {weather['emoji']} {weather['tmax']}°C" if weather else ""
    (BUILD / "subject.txt").write_text(f"[아침 브리핑] {date_label(n)}{temp}", encoding="utf-8")
    (BUILD / "email.html").write_text(email_html(brief, weather, n), encoding="utf-8")
    (BUILD / "script.txt").write_text(script, encoding="utf-8")
    print("빌드 완료")


def send():
    addr, pw = os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"]
    to = os.getenv("MAIL_TO") or addr
    subject = (BUILD / "subject.txt").read_text(encoding="utf-8")
    body = (BUILD / "email.html").read_text(encoding="utf-8")
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, addr, to
    plain = f"{subject}\n\n음성 브리핑: {PAGE_URL}\n\n" + (BUILD / "script.txt").read_text(encoding="utf-8")
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(addr, pw.replace(" ", ""))
        s.sendmail(addr, [to], msg.as_string())
    print("이메일 발송 완료 →", to)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    {"build": build, "send": send}[cmd]()
