"""
Melbourne Cinema Weekly Recommender
====================================
从 Lido Cinemas / Cinema Nova / ACMI 获取排片，
查询豆瓣 & 烂番茄评分，用 AI 生成推荐语，输出 Markdown + HTML 报告。

用法:
    1. .env 中填入  OPENAI_API_KEY=sk-...  (可选)
    2. python melbourne_cinema.py
"""

import os, re, json, time, logging, datetime as dt, html as html_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ─────────────── 数据模型 ───────────────
@dataclass
class Film:
    title: str
    cinema: str
    url: str = ""
    sessions: list[str] = field(default_factory=list)
    # 评分
    douban_score: Optional[float] = None
    douban_url: str = ""
    rt_score: Optional[int] = None
    rt_url: str = ""
    # 影片信息
    genre: str = ""
    director: str = ""
    cast: str = ""
    synopsis: str = ""
    recommendation: str = ""


# ─────────────── HTTP ───────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
})

def _get(url: str, **kw) -> requests.Response:
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=15, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            log.warning("请求失败 (%d/3): %s – %s", attempt+1, url, e)
            time.sleep(1.5 * (attempt+1))
    raise RuntimeError(f"无法获取: {url}")


# ─────────────── 日期 ───────────────
def next_week_range() -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    return today + dt.timedelta(1), today + dt.timedelta(7)

def _day_name(d: dt.date) -> str:
    return d.strftime("%A").lower()


# ═══════════════════════════════════════
#  1. 影院爬虫
# ═══════════════════════════════════════

LIDO_BASE = "https://www.lidocinemas.com.au"
NOVA_BASE = "https://www.cinemanova.com.au"
ACMI_BASE = "https://www.acmi.net.au"

# ───────── Lido ─────────
def scrape_lido(start: dt.date, end: dt.date) -> list[Film]:
    day_names = list(dict.fromkeys(_day_name(start + dt.timedelta(i)) for i in range((end-start).days+1)))

    # 收集电影列表 (只抓一个页面即可 — /now-showing/all 包含所有)
    all_hrefs: dict[str, str] = {}
    for dn in day_names:
        log.info("Lido: 抓取 /now-showing/%s", dn)
        try:
            soup = BeautifulSoup(_get(f"{LIDO_BASE}/now-showing/{dn}").text, "html.parser")
        except RuntimeError:
            continue
        for a in soup.find_all("a", href=re.compile(r"/movies/[^/]+$")):
            h, t = a.get("href",""), a.get_text(strip=True)
            if t and h not in all_hrefs:
                all_hrefs[h] = t

    # 并行抓取详情页场次
    def _fetch_one(href_title):
        href, title = href_title
        url = LIDO_BASE + href if href.startswith("/") else href
        return Film(title=title, cinema="Lido Cinemas", url=url, sessions=_lido_sessions(url))

    films = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_fetch_one, ht): ht for ht in all_hrefs.items()}
        for f in as_completed(futs):
            try:
                films.append(f.result())
            except Exception as e:
                log.warning("Lido 详情页失败: %s", e)

    log.info("Lido: %d 部电影", len(films))
    return films

def _lido_sessions(url: str) -> list[str]:
    try:
        soup = BeautifulSoup(_get(url).text, "html.parser")
    except RuntimeError:
        return []
    td = soup.find("div", class_="Tickets")
    if not td:
        return []
    tabs = [a.get_text(strip=True) for sl in td.find_all("div", class_="swiper-slide") if (a := sl.find("a"))]
    result = []
    for i, ul in enumerate(td.find_all("ul", class_="Sessions")):
        label = tabs[i] if i < len(tabs) else f"Day {i+1}"
        times = [s.get_text(strip=True) for s in ul.find_all("span", class_="Time")]
        if times:
            result.append(f"{label}: {', '.join(times)}")
    return result

# ───────── Cinema Nova ─────────
def scrape_nova(start: dt.date, end: dt.date) -> list[Film]:
    log.info("Nova: 抓取 /films-now-showing")
    try:
        soup = BeautifulSoup(_get(f"{NOVA_BASE}/films-now-showing").text, "html.parser")
    except RuntimeError:
        return []

    films = {}
    for panel in soup.find_all("div", class_="panel-film"):
        h4 = panel.find("h4")
        if not h4:
            continue
        title = h4.get_text(strip=True)
        link = panel.find("a", href=re.compile(r"/films/"))
        href = link.get("href","") if link else ""
        full_url = href if href.startswith("http") else NOVA_BASE + href

        sessions = []
        for dd in panel.find_all("div", class_="start-times-date"):
            date_text = dd.get_text(strip=True)
            times, sib = [], dd.find_next_sibling()
            while sib:
                if sib.name == "div" and "start-times-date" in (sib.get("class") or []):
                    break
                if hasattr(sib, 'find_all'):
                    times.extend(s.get_text(strip=True) for s in sib.find_all("a", class_="showtime"))
                sib = sib.find_next_sibling()
            sessions.append(f"{date_text}: {', '.join(times)}" if times else date_text)

        if title not in films:
            films[title] = Film(title=title, cinema="Cinema Nova", url=full_url, sessions=sessions)

    log.info("Nova: %d 部电影", len(films))
    return list(films.values())

# ───────── ACMI ─────────
def scrape_acmi(start: dt.date, end: dt.date) -> list[Film]:
    log.info("ACMI: 抓取排片")
    try:
        text = _get(f"{ACMI_BASE}/whats-on/").text
    except RuntimeError:
        return []
    decoded = text.replace("\\u002F", "/")
    films = []
    seen = set()
    for slug in dict.fromkeys(re.findall(r'/whats-on/(in-cinemas-[a-z0-9-]+)/', decoded)):
        if slug in seen:
            continue
        seen.add(slug)
        name = re.sub(r'-with-live-score.*|-live-score.*', '', slug.replace("in-cinemas-","",1))
        films.append(Film(title=name.replace("-"," ").title(), cinema="ACMI", url=f"{ACMI_BASE}/whats-on/{slug}/"))
    log.info("ACMI: %d 部电影", len(films))
    return films


# ═══════════════════════════════════════
#  2. 评分查询
# ═══════════════════════════════════════

def _clean_title(t: str) -> str:
    if ", The" in t: t = "The " + t.replace(", The","")
    t = re.sub(r'\s*\(\d{4}\)\s*',' ',t)
    t = re.sub(r'^AFFA\d+\s+','',t)
    return t.strip()

def _search_title(t: str) -> str:
    c = _clean_title(t)
    return re.sub(r'\s+',' ', re.sub(r"[^\w\s'-]",' ',c)).strip()

def _normalize(s: str) -> str:
    return re.sub(r'\s+',' ', re.sub(r'[^\w\s]','',s.lower())).strip()

def _title_similar(q: str, c: str) -> bool:
    qn, cn = _normalize(q), _normalize(c)
    if not qn or not cn: return False
    if qn == cn or qn in cn or cn in qn: return True
    qw, cw = set(qn.split()), set(cn.split())
    if not qw or not cw: return False
    return len(qw & cw)/len(qw) >= 0.6 and len(qw & cw)/len(cw) >= 0.4

# ── 豆瓣 ──
def search_douban(title: str) -> tuple[Optional[float], str]:
    clean = _search_title(title)
    # 搜索页面 (一次请求搞定)
    try:
        resp = SESSION.get("https://www.douban.com/search", params={"q": clean, "cat": "1002"}, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            rating = soup.find("span", class_="rating_nums")
            if rating and (txt := rating.get_text(strip=True)):
                link = soup.find("a", href=re.compile(r"movie\.douban\.com/subject/\d+"))
                return float(txt), (link["href"] if link else "")
    except Exception:
        pass
    return None, ""

# ── 烂番茄 ──
def search_rotten_tomatoes(title: str) -> tuple[Optional[int], str, str]:
    """返回 (tomatometer, movie_url, cast_str)"""
    clean = _search_title(title)
    try:
        resp = SESSION.get("https://www.rottentomatoes.com/search", params={"search": clean}, timeout=10)
        if resp.status_code != 200:
            return None, "", ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for row in soup.find_all("search-page-media-row"):
            tl = row.find("a", attrs={"slot": "title"})
            if not tl: continue
            if not _title_similar(clean, tl.get_text(strip=True)): continue
            score_s = row.get("tomatometer-score","")
            href = tl.get("href","")
            url = href if href.startswith("http") else f"https://www.rottentomatoes.com{href}"
            cast = row.get("cast","")  # "Actor1,Actor2,..."
            return (int(score_s) if score_s.isdigit() else None), url, cast
    except Exception:
        pass
    return None, "", ""

def _query_ratings(film: Film) -> Film:
    """查询一部电影的豆瓣 + 烂番茄评分 (用于线程池)"""
    try:
        film.douban_score, film.douban_url = search_douban(film.title)
    except Exception:
        pass
    try:
        film.rt_score, film.rt_url, cast = search_rotten_tomatoes(film.title)
        if cast and not film.cast:
            film.cast = cast.replace(",", ", ")
    except Exception:
        pass
    return film


# ═══════════════════════════════════════
#  3. AI 丰富信息 + 推荐语
# ═══════════════════════════════════════

def _get_openai_client():
    """
    自动检测 AI 配置，支持三种模式:
      1. Azure OpenAI  — AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT
      2. OpenAI 原生    — OPENAI_API_KEY (sk-...)
      3. 兼容 API       — OPENAI_API_KEY + OPENAI_BASE_URL
    返回 (client, model_name) 或 (None, None)
    """
    try:
        import openai
    except ImportError:
        log.warning("openai 包未安装，跳过 AI 功能")
        return None, None

    # ── Azure OpenAI ──
    azure_key = os.getenv("AZURE_OPENAI_API_KEY", "")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    azure_deploy = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    if azure_key and azure_endpoint:
        log.info("  AI 模式: Azure OpenAI (%s)", azure_endpoint)
        client = openai.AzureOpenAI(
            api_key=azure_key,
            azure_endpoint=azure_endpoint,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        )
        return client, azure_deploy

    # ── OpenAI 原生 / 兼容 API ──
    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key:
        base_url = os.getenv("OPENAI_BASE_URL", "")
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
            log.info("  AI 模式: 兼容 API (%s)", base_url)
        else:
            log.info("  AI 模式: OpenAI 原生")
        return openai.OpenAI(**kwargs), os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    return None, None


def enrich_with_ai(film: Film, client, model: str) -> Film:
    """用一次 AI 调用获取: 类型/导演/主演/简介/推荐语"""
    if not client:
        film.recommendation = _template_recommendation(film)
        return film

    scores = []
    if film.douban_score: scores.append(f"豆瓣 {film.douban_score}")
    if film.rt_score is not None: scores.append(f"烂番茄 {film.rt_score}%")

    prompt = f"""你是一位资深电影评论人。请根据电影名找到对应电影，提供详细信息。

电影名: {film.title}
已知评分: {', '.join(scores) if scores else '无'}
已知演员: {film.cast if film.cast else '未知'}

请严格按以下 JSON 格式回复，不要多余文字:
{{
  "genre": "类型标签，如 剧情/科幻/悬疑",
  "director": "导演姓名",
  "cast": "主要演员，最多3人，逗号分隔",
  "synopsis": "剧情简介，50-80字，概括核心故事线，不要剧透结局",
  "recommendation": "观影推荐理由，80-120字，需包含: 这部电影的独特亮点、适合什么样的观众、为什么值得去影院看。语气热情但不浮夸"
}}"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500, temperature=0.7,
        )
        text = resp.choices[0].message.content.strip()
        # 提取 JSON
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            film.genre = data.get("genre", film.genre) or film.genre
            film.director = data.get("director", film.director) or film.director
            if not film.cast:
                film.cast = data.get("cast", "") or ""
            film.synopsis = data.get("synopsis", film.synopsis) or film.synopsis
            film.recommendation = data.get("recommendation", "") or _template_recommendation(film)
            return film
    except Exception as exc:
        log.warning("  AI enrichment 失败 (%s): %s", film.title, exc)

    film.recommendation = _template_recommendation(film)
    return film

def _template_recommendation(film: Film) -> str:
    parts = []
    if film.douban_score and film.douban_score >= 8.0:
        parts.append(f"豆瓣高分 {film.douban_score}")
    elif film.douban_score and film.douban_score >= 7.0:
        parts.append(f"豆瓣 {film.douban_score}")
    if film.rt_score is not None and film.rt_score >= 80:
        parts.append(f"烂番茄 {film.rt_score}%")
    return f"🎬 {'、'.join(parts)}，推荐观看！" if parts else f"🎬 正在热映，值得关注！"


# ═══════════════════════════════════════
#  4. 过滤 & 去重
# ═══════════════════════════════════════

_NON_MOVIE = ["quartet","quintet","comedy:","trivia","jukebox","sings","we are jeni","reverse swing","live music","lido comedy"]

def _is_movie(f: Film) -> bool:
    return not any(kw in f.title.lower() for kw in _NON_MOVIE)

def is_high_rated(f: Film) -> bool:
    return (f.douban_score or 0) >= 7.5

def deduplicate(films: list[Film]) -> list[Film]:
    merged: dict[str, Film] = {}
    for f in films:
        key = _search_title(f.title).lower()
        if key in merged:
            ex = merged[key]
            if f.cinema not in ex.cinema:
                ex.cinema += f" / {f.cinema}"
            if f.sessions:
                tag = f.cinema.split("/")[0].strip()
                ex.sessions.extend(f"[{tag}] {s}" for s in f.sessions)
            if not ex.url and f.url: ex.url = f.url
        else:
            if f.sessions:
                tag = f.cinema.split("/")[0].strip()
                f.sessions = [f"[{tag}] {s}" for s in f.sessions]
            merged[key] = f
    return list(merged.values())


# ═══════════════════════════════════════
#  5. 报告生成
# ═══════════════════════════════════════

def generate_report(films: list[Film], start: dt.date, end: dt.date) -> str:
    rec = sorted([f for f in films if is_high_rated(f)], key=lambda f: -(f.douban_score or 0))
    lines = [
        "# 🎬 墨尔本电影周报 — 本周值得看",
        f"**{start.strftime('%Y.%m.%d')}–{end.strftime('%Y.%m.%d')}**\n",
        f"筛选: 豆瓣 ≥ 7.5 | 来源: Lido · Nova · ACMI | {dt.datetime.now().strftime('%m-%d %H:%M')}\n",
        "---\n",
    ]
    if not rec:
        lines.append("本周暂无符合条件的高分电影 😢\n")
    for i, f in enumerate(rec, 1):
        tags = []
        if f.douban_score: tags.append(f"豆瓣 {f.douban_score}")
        if f.rt_score is not None: tags.append(f"🍅 {f.rt_score}%")
        tag_s = f"  `{'  '.join(tags)}`" if tags else ""
        lines.append(f"### {i}. {f.title}{tag_s}\n")
        # 影片信息行
        info = []
        if f.genre: info.append(f.genre)
        if f.director: info.append(f"导演: {f.director}")
        if f.cast: info.append(f"主演: {f.cast}")
        if info:
            lines.append(f"*{' | '.join(info)}*\n")
        if f.synopsis:
            lines.append(f"📖 {f.synopsis}\n")
        if f.recommendation:
            lines.append(f"> {f.recommendation}\n")
        lines.append(f"**🎟️ 排片** — {f.cinema}")
        if f.sessions:
            lines.extend(f"- {s}" for s in f.sessions)
        else:
            lines.append(f"- [查看场次]({f.url})")
        lnk = []
        if f.url: lnk.append(f"[购票]({f.url})")
        if f.douban_url: lnk.append(f"[豆瓣]({f.douban_url})")
        if f.rt_url: lnk.append(f"[烂番茄]({f.rt_url})")
        if lnk: lines.append(f"\n🔗 {' · '.join(lnk)}")
        lines.append("\n")
    lines.append(f"---\n共 **{len(rec)}** 部推荐 (从 {len(films)} 部排片中筛选)")
    return "\n".join(lines)


# ─── HTML ───
def _esc(s: str) -> str:
    return html_mod.escape(s)

def generate_html(films: list[Film], start: dt.date, end: dt.date) -> str:
    rec = sorted([f for f in films if is_high_rated(f)], key=lambda f: -(f.douban_score or 0))
    cards = "\n".join(_html_card(f, i) for i, f in enumerate(rec, 1))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>墨尔本电影周报 {start.strftime('%m.%d')}–{end.strftime('%m.%d')}</title>
<style>
:root{{--bg:#0f0f0f;--card:#1a1a2e;--ch:#22223a;--acc:#e6c84c;--t:#e0e0e0;--t2:#999;--g:#67c23a;--r:#fa5252;--b:#2a2a3e}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;background:var(--bg);color:var(--t);line-height:1.7;padding-bottom:60px}}
.hd{{text-align:center;padding:48px 20px 28px;background:linear-gradient(135deg,#1a1a2e,#16213e);border-bottom:1px solid var(--b)}}
.hd h1{{font-size:2em;color:var(--acc);margin-bottom:6px;letter-spacing:2px}}.hd .sub{{color:var(--t2);font-size:.92em}}
.ct{{max-width:820px;margin:0 auto;padding:20px 16px}}.stat{{text-align:center;color:var(--t2);font-size:.9em;margin:8px 0 20px}}
.fc{{background:var(--card);border:1px solid var(--b);border-radius:12px;padding:22px 26px;margin-bottom:18px;transition:background .2s}}
.fc:hover{{background:var(--ch)}}
.fh{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}}
.rk{{color:var(--acc);font-weight:700;font-size:1.35em;min-width:26px}}
.tt{{font-size:1.2em;font-weight:700;color:#fff}}
.bd{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.76em;font-weight:600}}
.bd-d{{background:#1a3a1a;color:var(--g);border:1px solid #2d5a2d}}
.bd-r{{background:#3a1a1a;color:var(--r);border:1px solid #5a2d2d}}
.meta{{font-size:.84em;color:var(--t2);margin:4px 0 8px}}.meta b{{color:var(--t);font-weight:600}}
.syn{{color:var(--t2);font-size:.9em;margin:8px 0;padding:9px 13px;border-left:3px solid var(--acc);background:rgba(230,200,76,.04);border-radius:0 6px 6px 0}}
.rec{{font-size:.9em;margin:8px 0;padding:9px 13px;background:rgba(255,255,255,.03);border-radius:6px;font-style:italic}}
.ss{{margin:10px 0 4px}}.ss-t{{font-size:.83em;color:var(--acc);font-weight:600;margin-bottom:4px}}
.ss ul{{list-style:none;padding:0}}.ss li{{font-size:.83em;color:var(--t2);padding:2px 0 2px 16px;position:relative}}
.ss li::before{{content:"▸";position:absolute;left:0;color:var(--acc)}}
.ct-tag{{display:inline-block;font-size:.7em;background:#2a2a3e;color:var(--t2);padding:1px 6px;border-radius:4px;margin-right:4px}}
.lk{{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}}
.lk a{{font-size:.8em;color:var(--acc);text-decoration:none;padding:3px 12px;border:1px solid var(--b);border-radius:6px;transition:all .2s}}
.lk a:hover{{background:var(--acc);color:var(--bg)}}
.ft{{text-align:center;color:var(--t2);font-size:.8em;padding:28px 20px;border-top:1px solid var(--b)}}
</style></head><body>
<div class="hd"><h1>🎬 墨尔本电影周报</h1>
<div class="sub">{start.strftime('%Y年%m月%d日')} — {end.strftime('%Y年%m月%d日')} · 豆瓣 ≥ 7.5</div></div>
<div class="ct">
<p class="stat">从 {len(films)} 部排片中精选 {len(rec)} 部高分佳作</p>
{cards}
</div>
<div class="ft">Lido Cinemas · Cinema Nova · ACMI · 豆瓣 · Rotten Tomatoes<br>{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
</body></html>"""

def _html_card(f: Film, idx: int) -> str:
    badges = ""
    if f.douban_score: badges += f'<span class="bd bd-d">豆瓣 {f.douban_score}</span> '
    if f.rt_score is not None: badges += f'<span class="bd bd-r">🍅 {f.rt_score}%</span> '

    meta_parts = []
    if f.genre: meta_parts.append(f.genre)
    if f.director: meta_parts.append(f"<b>导演</b> {_esc(f.director)}")
    if f.cast: meta_parts.append(f"<b>主演</b> {_esc(f.cast)}")
    meta = f'<div class="meta">{" · ".join(meta_parts)}</div>' if meta_parts else ""

    syn = f'<div class="syn">{_esc(f.synopsis)}</div>' if f.synopsis else ""
    rec = f'<div class="rec">💡 {_esc(f.recommendation)}</div>' if f.recommendation else ""

    sess = ""
    if f.sessions:
        items = ""
        for s in f.sessions:
            m = re.match(r'\[([^\]]+)\]\s*(.*)', s)
            if m:
                items += f'<li><span class="ct-tag">{_esc(m.group(1))}</span>{_esc(m.group(2))}</li>'
            else:
                items += f'<li>{_esc(s)}</li>'
        sess = f'<div class="ss"><div class="ss-t">🎟️ 场次</div><ul>{items}</ul></div>'
    elif f.url:
        sess = f'<div class="ss"><div class="ss-t">🎟️ 场次</div><ul><li>请前往购票页面查看</li></ul></div>'

    lnk = ""
    parts = []
    if f.url: parts.append(f'<a href="{_esc(f.url)}" target="_blank">🎟 购票</a>')
    if f.douban_url: parts.append(f'<a href="{_esc(f.douban_url)}" target="_blank">🟢 豆瓣</a>')
    if f.rt_url: parts.append(f'<a href="{_esc(f.rt_url)}" target="_blank">🍅 烂番茄</a>')
    if parts: lnk = f'<div class="lk">{"".join(parts)}</div>'

    return f"""<div class="fc">
<div class="fh"><span class="rk">#{idx}</span><span class="tt">{_esc(f.title)}</span>{badges}</div>
<div style="font-size:.83em;color:var(--t2)">📍 {_esc(f.cinema)}</div>
{meta}{syn}{rec}{sess}{lnk}
</div>"""


# ═══════════════════════════════════════
#  6. 主流程
# ═══════════════════════════════════════

def main():
    start, end = next_week_range()
    log.info("=" * 50)
    log.info("墨尔本电影推荐 %s ~ %s", start, end)
    log.info("=" * 50)

    # ① 爬取 (Nova/ACMI 串行快, Lido 内部已并行)
    log.info("📡 爬取排片...")
    all_films = scrape_lido(start, end) + scrape_nova(start, end) + scrape_acmi(start, end)
    if not all_films:
        log.error("❌ 未获取到电影!"); return

    films = [f for f in deduplicate(all_films) if _is_movie(f)]
    log.info("📋 去重+过滤后 %d 部电影", len(films))

    # ② 并行查询评分
    log.info("📊 并行查询评分 (%d 部)...", len(films))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_query_ratings, films))
    log.info("📊 评分查询完成 (%.1fs)", time.time()-t0)

    # ③ AI 丰富高分电影
    high = [f for f in films if is_high_rated(f)]
    log.info("💡 %d 部高分电影，AI enrichment...", len(high))
    client, model = _get_openai_client()
    if client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(enrich_with_ai, f, client, model): f for f in high}
            for fut in as_completed(futs):
                try: fut.result()
                except Exception as e: log.warning("AI error: %s", e)
    else:
        log.info("  (未配置 AI，使用模板推荐)")
        for f in high:
            f.recommendation = _template_recommendation(f)

    # ④ 输出
    base = os.path.dirname(os.path.abspath(__file__))
    stamp = dt.date.today().strftime('%Y%m%d')

    md = generate_report(films, start, end)
    md_path = os.path.join(base, f"report_{stamp}.md")
    with open(md_path, "w", encoding="utf-8") as fp: fp.write(md)
    log.info("✅ Markdown: %s", md_path)

    html = generate_html(films, start, end)
    html_path = os.path.join(base, f"report_{stamp}.html")
    with open(html_path, "w", encoding="utf-8") as fp: fp.write(html)
    log.info("✅ HTML: %s", html_path)

    print("\n" + "=" * 50)
    print(md)
    print("=" * 50)

    # JSON
    jd = [{"title":f.title,"cinema":f.cinema,"url":f.url,"sessions":f.sessions,
           "genre":f.genre,"director":f.director,"cast":f.cast,"synopsis":f.synopsis,
           "douban_score":f.douban_score,"douban_url":f.douban_url,
           "rt_score":f.rt_score,"rt_url":f.rt_url,"recommendation":f.recommendation} for f in films]
    jp = os.path.join(base, f"data_{stamp}.json")
    with open(jp, "w", encoding="utf-8") as fp: json.dump(jd, fp, ensure_ascii=False, indent=2)
    log.info("📦 JSON: %s", jp)


if __name__ == "__main__":
    main()
