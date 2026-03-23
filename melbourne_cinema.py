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
    # 影片信息 (从 RT/豆瓣获取, 非 AI)
    title_cn: str = ""         # 中文片名
    year: str = ""             # 年份
    genre: str = ""
    director: str = ""
    cast: str = ""
    poster: str = ""           # 海报 URL
    duration: str = ""         # 时长 (如 "2h 10m")
    synopsis: str = ""
    recommendation: str = ""   # 影片亮点/看点
    awards: str = ""           # 电影节获奖信息
    hot_comment: str = ""      # 豆瓣最热短评


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
    """返回从今天起的 7 天"""
    today = dt.date.today()
    return today, today + dt.timedelta(days=6)

def _day_name(d: dt.date) -> str:
    return d.strftime("%A").lower()

def _day_label(d: dt.date) -> str:
    """生成日期标签: '3/24 Monday'"""
    return f"{d.month}/{d.day} {d.strftime('%A')}"


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
        return Film(title=title, cinema="Lido Cinemas", url=url, sessions=_lido_sessions(url, start, end))

    films = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_fetch_one, ht): ht for ht in all_hrefs.items()}
        for f in as_completed(futs):
            try:
                films.append(f.result())
            except Exception as e:
                log.warning("Lido 详情页失败: %s", e)

    # 过滤掉本周无场次的电影
    films = [f for f in films if f.sessions]
    log.info("Lido: %d 部电影 (本周有场次)", len(films))
    return films

def _lido_sessions(url: str, start: dt.date, end: dt.date) -> list[str]:
    """从 Lido 详情页提取场次，只保留 start~end 范围内的天。
    Lido 的 tabs 不是每天一个，而是只显示有场次的天。
    需要从 tab 文本解析出实际日期来判断是否在范围内。
    """
    try:
        soup = BeautifulSoup(_get(url).text, "html.parser")
    except RuntimeError:
        return []
    td = soup.find("div", class_="Tickets")
    if not td:
        return []
    tabs = [a.get_text(strip=True) for sl in td.find_all("div", class_="swiper-slide") if (a := sl.find("a"))]
    today = dt.date.today()
    result = []
    for i, ul in enumerate(td.find_all("ul", class_="Sessions")):
        label = tabs[i] if i < len(tabs) else ""
        if not label:
            continue
        # 解析 tab label 到实际日期
        tab_date = _parse_lido_tab_date(label, today)
        if tab_date is None or tab_date < start or tab_date > end:
            continue
        # 用 "3/24 Monday" 格式作为统一标签
        day_label = _day_label(tab_date)
        times = [s.get_text(strip=True) for s in ul.find_all("span", class_="Time")]
        if times:
            result.append(f"{day_label}: {', '.join(times)}")
    return result


def _parse_lido_tab_date(label: str, today: dt.date) -> Optional[dt.date]:
    """将 Lido tab 文本解析为日期。
    格式: 'Today' / 'Tomorrow' / 'Sunday' / 'Tue 31 Mar' / 'Fri 10 Apr'
    """
    label = label.strip()
    if label == "Today":
        return today
    if label == "Tomorrow":
        return today + dt.timedelta(days=1)
    # 纯星期名: "Sunday", "Monday", ...
    # Lido 的 tabs 指的是从今天开始最近的那个星期几
    weekdays = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
    if label.lower() in weekdays:
        target_wd = weekdays[label.lower()]
        delta = (target_wd - today.weekday()) % 7
        # delta=0 表示今天，不跳到下周
        return today + dt.timedelta(days=delta)
    # "Tue 31 Mar", "Fri 10 Apr", "Sat 19 Dec" 等
    m = re.match(r'[A-Za-z]+\s+(\d+)\s+([A-Za-z]+)', label)
    if m:
        day_num = int(m.group(1))
        month_str = m.group(2)
        months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                  "jul":7,"aug":8,"sep":9,"sept":9,"oct":10,"nov":11,"dec":12}
        month = months.get(month_str.lower()[:3])
        if month:
            year = today.year
            try:
                d = dt.date(year, month, day_num)
            except ValueError:
                return None
            # 如果日期已过，可能是明年
            if d < today - dt.timedelta(days=30):
                d = dt.date(year + 1, month, day_num)
            return d
    return None

# ───────── Cinema Nova ─────────
def _nova_date_to_label(text: str) -> str:
    """将 Nova 的日期文本转为统一格式。
    'Monday, 23rd March' → '3/23 Monday'
    'Friday, 20th March' → '3/20 Friday'
    """
    m = re.match(r'(\w+),?\s+(\d+)\w*\s+(\w+)', text.strip())
    if m:
        weekday, day_num, month_str = m.group(1), int(m.group(2)), m.group(3)
        months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
                  "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
        month = months.get(month_str.lower())
        if month:
            return f"{month}/{day_num} {weekday}"
    return text

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
        # Nova 的场次结构: show-times > start-times > col-xs-12 > start-times-date / start-times-time
        st_div = panel.find("div", class_="show-times")
        if st_div:
            for dd in st_div.find_all("div", class_="start-times-date"):
                date_text = dd.get_text(strip=True)
                label = _nova_date_to_label(date_text)
                # 从整个 start-times 容器中找同组的时间
                parent_start_times = dd.find_parent("div", class_="start-times")
                times = []
                if parent_start_times:
                    for time_div in parent_start_times.find_all("div", class_="start-times-time"):
                        for a in time_div.find_all("a", class_="showtime"):
                            t = a.find("p")
                            if t:
                                time_text = t.get_text(strip=True)
                                if re.match(r'\d{1,2}:\d{2}', time_text):
                                    # 转为12小时制
                                    h, m = int(time_text[:2]), int(time_text[3:5])
                                    ampm = "am" if h < 12 else "pm"
                                    h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
                                    times.append(f"{h12}:{m:02d} {ampm}")
                sessions.append(f"{label}: {', '.join(times)}" if times else label)

        if title not in films:
            films[title] = Film(title=title, cinema="Cinema Nova", url=full_url, sessions=sessions)

    log.info("Nova: %d 部电影", len(films))
    return list(films.values())

# ───────── ACMI ─────────
ACMI_API = "https://admin.acmi.net.au/api/v2/calendar"

def scrape_acmi(start: dt.date, end: dt.date) -> list[Film]:
    """
    通过 ACMI Wagtail API 获取影院排片。
    API 返回所有 calendar 事件（含场次时间和场地），
    我们筛选出 venue 包含 "Cinema" 且在日期范围内的放映。
    排除 Online/线上放映。
    """
    log.info("ACMI: 通过 API 抓取排片")
    films_dict: dict[str, Film] = {}

    try:
        # 分页获取所有 calendar items
        all_items = []
        offset = 0
        while True:
            resp = _get(f"{ACMI_API}/?fields=event(title,url)&limit=100&offset={offset}")
            data = resp.json()
            items = data.get("items", [])
            all_items.extend(items)
            total = data.get("meta", {}).get("total_count", 0)
            offset += len(items)
            if offset >= total or not items:
                break

        # 筛选: venue 包含 "Cinema" 且不含 "Online"，日期在 start~end 范围内
        for item in all_items:
            venue = item.get("venue", "")
            if "cinema" not in venue.lower() or "online" in venue.lower():
                continue

            start_dt_str = item.get("start_datetime", "")
            if not start_dt_str:
                continue
            # 解析日期 "2026-03-23T18:30:00+11:00"
            try:
                item_date = dt.date.fromisoformat(start_dt_str[:10])
            except ValueError:
                continue
            if item_date < start or item_date > end:
                continue

            ev = item.get("event", {})
            title = ev.get("title", "")
            url_path = ev.get("url", "")
            if not title:
                continue

            full_url = f"{ACMI_BASE}{url_path}" if url_path.startswith("/") else url_path
            time_str = start_dt_str[11:16]  # "18:30"
            # 转换为 12 小时制
            try:
                h, m = int(time_str[:2]), int(time_str[3:5])
                ampm = "am" if h < 12 else "pm"
                h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
                time_12 = f"{h12}:{m:02d} {ampm}"
            except ValueError:
                time_12 = time_str

            day_label = _day_label(item_date)

            if title not in films_dict:
                films_dict[title] = Film(title=title, cinema="ACMI", url=full_url, sessions=[])

            # 添加场次 (格式与 Lido 一致)
            session_str = f"{day_label}: {time_12}"
            if session_str not in films_dict[title].sessions:
                films_dict[title].sessions.append(session_str)

    except Exception as exc:
        log.warning("ACMI API 失败: %s", exc)

    # 按日期排序场次
    for f in films_dict.values():
        f.sessions.sort()

    log.info("ACMI: %d 部电影 (本周影院放映)", len(films_dict))
    return list(films_dict.values())


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
def search_douban(title: str) -> tuple[Optional[float], str, str, str]:
    """返回 (score, douban_url, title_cn, hot_comment)
    策略: suggest API (轻量, 拿ID+中文名) → 搜索页 (拿评分)
    不访问详情页（403风险高），短评从搜索页拿不到则留空。
    """
    clean = _search_title(title)
    title_cn = ""
    douban_url = ""
    score = None
    hot_comment = ""

    # Step1: suggest API — 获取中文片名 + douban ID (轻量接口, 成功率高)
    try:
        resp = SESSION.get("https://movie.douban.com/j/subject_suggest",
                           params={"q": clean}, timeout=8)
        if resp.status_code == 200:
            for item in (resp.json() or []):
                if item.get("type") == "movie":
                    title_cn = item.get("title", "")
                    did = item.get("id", "")
                    if did:
                        douban_url = f"https://movie.douban.com/subject/{did}/"
                    break
    except Exception:
        pass

    # Step2: 搜索页 — 获取评分 (比详情页稳定)
    try:
        resp = SESSION.get("https://www.douban.com/search",
                           params={"q": clean, "cat": "1002"}, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            rating = soup.find("span", class_="rating_nums")
            if rating and (txt := rating.get_text(strip=True)):
                score = float(txt)
            # 如果 suggest 没拿到 url，从搜索页补
            if not douban_url:
                link = soup.find("a", href=re.compile(r"movie\.douban\.com/subject/\d+"))
                if link:
                    douban_url = link["href"]
    except Exception:
        pass

    # Step3: 如果有 url 且搜索页拿不到评分，尝试详情页 (最后手段)
    if score is None and douban_url:
        try:
            resp = SESSION.get(douban_url, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                se = soup.find("strong", class_="ll rating_num")
                if se and (t := se.get_text(strip=True)):
                    score = float(t)
                # 顺便拿短评
                ce = soup.find("span", class_="short")
                if ce:
                    hot_comment = ce.get_text(strip=True)[:120]
        except Exception:
            pass

    return score, douban_url, title_cn, hot_comment

# ── 烂番茄 ──
def search_rotten_tomatoes(title: str) -> tuple[Optional[int], str, str, str]:
    """返回 (tomatometer, movie_url, cast_str, year)"""
    clean = _search_title(title)
    try:
        resp = SESSION.get("https://www.rottentomatoes.com/search", params={"search": clean}, timeout=10)
        if resp.status_code != 200:
            return None, "", "", ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for row in soup.find_all("search-page-media-row"):
            tl = row.find("a", attrs={"slot": "title"})
            if not tl: continue
            if not _title_similar(clean, tl.get_text(strip=True)): continue
            score_s = row.get("tomatometer-score", "")
            href = tl.get("href", "")
            url = href if href.startswith("http") else f"https://www.rottentomatoes.com{href}"
            cast = row.get("cast", "")
            year = row.get("release-year", "")
            return (int(score_s) if score_s.isdigit() else None), url, cast, year
    except Exception:
        pass
    return None, "", "", ""

def _fetch_rt_detail(url: str) -> dict:
    """从 RT 电影详情页获取导演/类型/海报 (准确数据)"""
    if not url:
        return {}
    try:
        resp = SESSION.get(url, timeout=12)
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "html.parser")
        result = {}
        # 海报: 优先使用 og:image (高质量电影海报)
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            result["poster"] = og["content"]
        # JSON-LD: 导演/类型
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "Movie":
                    dirs = data.get("director", [])
                    if isinstance(dirs, list):
                        result["director"] = ", ".join(d.get("name","") for d in dirs if d.get("name"))
                    elif isinstance(dirs, dict):
                        result["director"] = dirs.get("name", "")
                    genres = data.get("genre", [])
                    if isinstance(genres, list):
                        result["genre"] = " / ".join(genres)
                    # 如果没有 og:image，用 JSON-LD 的 image
                    if "poster" not in result and data.get("image"):
                        result["poster"] = data["image"]
                    # 时长: "PT2H10M" → "2h 10m"
                    dur = data.get("duration", "")
                    if dur:
                        dm = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?', dur)
                        if dm:
                            parts = []
                            if dm.group(1): parts.append(f"{dm.group(1)}h")
                            if dm.group(2): parts.append(f"{dm.group(2)}m")
                            result["duration"] = " ".join(parts)
                    break
            except (json.JSONDecodeError, KeyError):
                continue
        return result
    except Exception:
        pass
    return {}

def _query_rt(film: Film) -> Film:
    """查询烂番茄评分 + 导演/类型/海报 (用于线程池并行)"""
    try:
        film.rt_score, film.rt_url, cast, year = search_rotten_tomatoes(film.title)
        if cast and not film.cast:
            film.cast = cast.replace(",", ", ")
        if year and not film.year:
            film.year = year
    except Exception:
        pass
    if film.rt_url:
        try:
            detail = _fetch_rt_detail(film.rt_url)
            if detail.get("director"): film.director = detail["director"]
            if detail.get("genre"): film.genre = detail["genre"]
            if detail.get("poster"): film.poster = detail["poster"]
            if detail.get("duration"): film.duration = detail["duration"]
        except Exception:
            pass
    return film

def _query_douban_serial(films: list[Film]) -> None:
    """串行查询豆瓣评分，带本地缓存避免重复请求"""
    # 加载缓存
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".douban_cache.json")
    cache: dict = {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    queried = 0
    for i, film in enumerate(films):
        key = _dedup_key(film.title)
        # 从缓存读取
        if key in cache:
            c = cache[key]
            film.douban_score = c.get("score")
            film.douban_url = c.get("url", "")
            film.title_cn = c.get("title_cn", "") or film.title_cn
            film.hot_comment = c.get("hot_comment", "")
            log.info("  豆瓣 [%d/%d] %s → 缓存 %s", i+1, len(films), film.title,
                     f"✓ {film.douban_score}" if film.douban_score else "✗")
            continue

        log.info("  豆瓣 [%d/%d] %s", i+1, len(films), film.title)
        try:
            film.douban_score, film.douban_url, film.title_cn, film.hot_comment = search_douban(film.title)
            status = f"✓ {film.douban_score}" if film.douban_score else "✗ 无评分"
            if film.title_cn:
                status += f" ({film.title_cn})"
            log.info("    %s", status)
        except Exception as e:
            log.warning("    豆瓣失败: %s", e)

        # 写入缓存 (即使没评分也缓存, 但只缓存有 url 的)
        if film.douban_url or film.douban_score or film.title_cn:
            cache[key] = {
                "score": film.douban_score,
                "url": film.douban_url,
                "title_cn": film.title_cn,
                "hot_comment": film.hot_comment,
            }

        queried += 1
        time.sleep(2.5)  # 间隔 2.5 秒避免豆瓣反爬

    # 保存缓存
    if queried > 0:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            log.info("  豆瓣缓存已保存 (%d 条)", len(cache))
        except Exception:
            pass


# ═══════════════════════════════════════
#  3. AI 推荐语
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
    """用 AI 生成剧情简介 + 影片亮点 + 获奖信息"""
    if not client:
        film.recommendation = _template_recommendation(film)
        return film

    info_parts = []
    if film.genre: info_parts.append(f"类型: {film.genre}")
    if film.director: info_parts.append(f"导演: {film.director}")
    if film.cast: info_parts.append(f"主演: {film.cast}")
    scores = []
    if film.douban_score: scores.append(f"豆瓣 {film.douban_score}")
    if film.rt_score is not None: scores.append(f"烂番茄 {film.rt_score}%")
    if scores: info_parts.append(f"评分: {', '.join(scores)}")

    prompt = f"""你是一位资深电影评论人和选片顾问。请根据以下电影信息，帮助观众快速判断是否值得去影院观看。

电影名: {film.title} ({film.year or '未知年份'})
{chr(10).join(info_parts) if info_parts else '暂无更多信息'}

请严格按以下 JSON 格式回复，不要多余文字:
{{
  "synopsis": "剧情简介，50-80字，概括主角困境和核心冲突，不剧透。如果是续集/系列作品请注明（如'《28天后》系列第三部'）",
  "highlights": "影片核心看点，100-150字。用▸分隔2-3个最突出的看点（不要凑数），只写最有信息量的内容。可以涉及: 导演独特手法/演员突破表演/获奖加持/视听语言创新/系列关联/文化意义等。避免'值得一看''不容错过'等空话。每个▸后直接写内容，不需要小标题。",
  "awards": "电影节获奖/提名，如'2023戛纳金棕榈提名'。无则留空字符串"
}}"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600, temperature=0.7,
        )
        text = resp.choices[0].message.content.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            film.synopsis = data.get("synopsis", "") or film.synopsis
            film.recommendation = data.get("highlights", "") or _template_recommendation(film)
            film.awards = data.get("awards", "") or ""
            return film
    except Exception as exc:
        log.warning("  AI 失败 (%s): %s", film.title, exc)

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

def _display_title(f: Film) -> str:
    """生成显示标题: 中文名 英文名 (年份)
    - 如果 title 已含年份如 '12 Monkeys (1995)', 先去掉再统一添加
    - 只有官方中文译名才展示，否则只展示英文名
    """
    # 从 title 中提取并移除年份
    base_title = re.sub(r'\s*\(\d{4}\)\s*', '', f.title).strip()
    # 也移除 (Dubbed) 等后缀
    base_title = re.sub(r'\s*\(Dubbed\)\s*', '', base_title, flags=re.I).strip()

    parts = []
    if f.title_cn and f.title_cn != base_title:
        parts.append(f.title_cn)
    parts.append(base_title)
    result = " ".join(parts)

    # 统一添加年份
    year = f.year or ""
    if not year:
        # 从原始 title 提取
        m = re.search(r'\((\d{4})\)', f.title)
        if m:
            year = m.group(1)
    if year:
        result += f" ({year})"
    return result


# ═══════════════════════════════════════
#  4. 过滤 & 去重
# ═══════════════════════════════════════

_NON_MOVIE = ["quartet","quintet","comedy:","trivia","jukebox","sings","we are jeni","reverse swing","live music","lido comedy"]

def _is_movie(f: Film) -> bool:
    return not any(kw in f.title.lower() for kw in _NON_MOVIE)

def is_high_rated(f: Film) -> bool:
    """豆瓣 ≥ 7.5 或 烂番茄 ≥ 85%"""
    if (f.douban_score or 0) >= 7.5:
        return True
    if (f.rt_score or 0) >= 85:
        return True
    return False

def _sort_score(f: Film) -> float:
    """综合排序分数: 豆瓣为主 (权重 70%), 烂番茄为辅 (权重 30%)
    豆瓣 10 分制 → ×10 归一化为 100; 烂番茄已经是 0-100"""
    d = (f.douban_score or 0) * 10   # 0-100
    r = f.rt_score or 0              # 0-100
    if d > 0 and r > 0:
        return -(d * 0.7 + r * 0.3)
    elif d > 0:
        return -(d * 0.85)  # 只有豆瓣，给予稍低权重
    else:
        return -(r * 0.6)   # 只有 RT，权重更低

def _dedup_key(title: str) -> str:
    """生成去重用的 key: 去除年份、(Dubbed)、标点，统一小写"""
    t = _search_title(title)
    t = re.sub(r'\bdubbed\b', '', t, flags=re.I)
    return re.sub(r'\s+', ' ', t).strip().lower()

def deduplicate(films: list[Film]) -> list[Film]:
    merged: dict[str, Film] = {}
    for f in films:
        key = _dedup_key(f.title)
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
    rec = sorted([f for f in films if is_high_rated(f)], key=_sort_score)
    lines = [
        "# 🎬 墨尔本电影周报 — 本周值得看",
        f"**{start.strftime('%Y.%m.%d')}–{end.strftime('%Y.%m.%d')}**\n",
        f"筛选: 豆瓣 ≥ 7.5 或 🍅 ≥ 85% | 来源: Lido · Nova · ACMI | {dt.datetime.now().strftime('%m-%d %H:%M')}\n",
        "---\n",
    ]
    if not rec:
        lines.append("本周暂无符合条件的高分电影 😢\n")
    for i, f in enumerate(rec, 1):
        tags = []
        if f.douban_score: tags.append(f"豆瓣 {f.douban_score}")
        if f.rt_score is not None: tags.append(f"🍅 {f.rt_score}%")
        tag_s = f"  `{'  '.join(tags)}`" if tags else ""
        lines.append(f"### {i}. {_display_title(f)}{tag_s}\n")
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
        if f.hot_comment:
            lines.append(f'> 🗣 豆瓣热评: "{f.hot_comment}"\n')
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
    rec = sorted([f for f in films if is_high_rated(f)], key=_sort_score)

    # 提取所有影院和日期用于筛选器
    all_cinemas = sorted(set(c.strip() for f in rec for c in f.cinema.split("/")))

    # 按日期排序 day tabs: "3/23 Monday" → 提取月/日排序
    _weekday_order = {"Monday":0,"Tuesday":1,"Wednesday":2,"Thursday":3,"Friday":4,"Saturday":5,"Sunday":6}
    day_set = set()
    for f in rec:
        for s in f.sessions:
            m = re.match(r'\[[^\]]+\]\s*([^:]+)', s)
            if m:
                day = m.group(1).strip().split(",")[0].strip()
                day_set.add(day)
    def _day_sort_key(d: str) -> tuple:
        # "3/24 Monday" → (3, 24)
        m = re.match(r'(\d+)/(\d+)', d)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return (99, _weekday_order.get(d, 99))
    all_days = sorted(day_set, key=_day_sort_key)

    # 生成筛选按钮 HTML
    cinema_btns = ''.join(f'<button class="fb" data-filter-cinema="{_esc(c)}">{_esc(c)}</button>' for c in all_cinemas)
    day_btns = ''.join(f'<button class="fb" data-filter-day="{_esc(d)}">{_esc(d)}</button>' for d in all_days)

    cards = "\n".join(_html_card(f, i) for i, f in enumerate(rec, 1))

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>墨尔本电影周报 {start.strftime('%m.%d')}–{end.strftime('%m.%d')}</title>
<style>
:root{{--bg:#0f0f0f;--card:#1a1a2e;--ch:#22223a;--acc:#e6c84c;--t:#e0e0e0;--t2:#999;--g:#67c23a;--r:#fa5252;--b:#2a2a3e}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;background:var(--bg);color:var(--t);line-height:1.6;padding-bottom:60px;font-size:14px}}
.hd{{text-align:center;padding:36px 20px 16px;background:linear-gradient(135deg,#1a1a2e,#16213e);border-bottom:1px solid var(--b)}}
.hd h1{{font-size:1.6em;color:var(--acc);margin-bottom:4px;letter-spacing:2px}}.hd .sub{{color:var(--t2);font-size:.85em}}
.filters{{background:#141425;border-bottom:1px solid var(--b);padding:12px 20px;position:sticky;top:0;z-index:10}}
.filters-inner{{max-width:800px;margin:0 auto}}
.fg{{margin-bottom:8px}}.fg:last-child{{margin-bottom:0}}
.fg-label{{font-size:.72em;color:var(--t2);margin-bottom:4px;font-weight:600;text-transform:uppercase;letter-spacing:1px}}
.fg-btns{{display:flex;flex-wrap:wrap;gap:5px}}
.fb{{font-size:.72em;padding:3px 12px;border-radius:999px;border:1px solid var(--b);background:transparent;color:var(--t2);cursor:pointer;transition:all .15s;font-family:inherit}}
.fb:hover{{border-color:var(--acc);color:var(--acc)}}
.fb.active{{background:var(--acc);color:var(--bg);border-color:var(--acc);font-weight:600}}
.ct{{max-width:800px;margin:0 auto;padding:16px 14px}}.stat{{text-align:center;color:var(--t2);font-size:.84em;margin:6px 0 16px}}
.fc{{background:var(--card);border:1px solid var(--b);border-radius:10px;padding:18px 22px;margin-bottom:14px;transition:all .25s}}
.fc:hover{{background:var(--ch)}}
.fc.hidden{{display:none}}
.fh{{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:4px}}
.rk{{color:var(--acc);font-weight:700;font-size:1.15em;min-width:22px}}
.tt{{font-size:1.05em;font-weight:700;color:#fff}}
.bd{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:.7em;font-weight:600}}
.bd-d{{background:#1a3a1a;color:var(--g);border:1px solid #2d5a2d}}
.bd-r{{background:#3a1a1a;color:var(--r);border:1px solid #5a2d2d}}
.meta{{font-size:.78em;color:var(--t2);margin:3px 0 6px}}.meta b{{color:var(--t);font-weight:600}}
.syn{{color:var(--t2);font-size:.82em;margin:6px 0;padding:7px 11px;border-left:3px solid var(--acc);background:rgba(230,200,76,.04);border-radius:0 6px 6px 0}}
.rec{{font-size:.8em;margin:6px 0;padding:8px 12px;background:rgba(255,255,255,.03);border-radius:6px;line-height:1.65;color:var(--t2)}}
.hl{{list-style:none;padding:0;margin:0}}.hl li{{padding:2px 0 2px 16px;position:relative}}.hl li::before{{content:"▸";position:absolute;left:0;color:var(--acc);font-weight:700}}
.hc{{font-size:.76em;color:var(--t2);margin:5px 0;padding:6px 10px;border-left:2px solid var(--g);background:rgba(103,194,58,.04);border-radius:0 6px 6px 0}}.hc em{{color:var(--g);font-style:normal}}
.ss{{margin:8px 0 3px}}.ss-t{{font-size:.76em;color:var(--acc);font-weight:600;margin-bottom:3px}}
.ss ul{{list-style:none;padding:0}}.ss li{{font-size:.76em;color:var(--t2);padding:1px 0 1px 14px;position:relative}}
.ss li::before{{content:"▸";position:absolute;left:0;color:var(--acc)}}
.ss li.s-hide{{display:none}}
.ct-tag{{display:inline-block;font-size:.68em;background:#2a2a3e;color:var(--t2);padding:1px 5px;border-radius:4px;margin-right:3px}}
.lk{{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}}
.lk a{{font-size:.73em;color:var(--acc);text-decoration:none;padding:2px 10px;border:1px solid var(--b);border-radius:6px;transition:all .2s}}
.lk a:hover{{background:var(--acc);color:var(--bg)}}
.fc-body{{display:flex;gap:14px}}
.fc-poster{{flex-shrink:0;width:88px}}
.fc-poster img{{width:88px;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.4)}}
.fc-info{{flex:1;min-width:0}}
.no-results{{text-align:center;color:var(--t2);padding:32px 20px;font-size:.88em}}
@media(max-width:500px){{.fc-poster{{width:64px}}.fc-poster img{{width:64px}}.fc{{padding:14px 16px}}}}
.ft{{text-align:center;color:var(--t2);font-size:.73em;padding:24px 20px;border-top:1px solid var(--b)}}
</style></head><body>

<div class="hd"><h1>🎬 墨尔本电影周报</h1>
<div class="sub">{start.strftime('%Y年%m月%d日')} — {end.strftime('%Y年%m月%d日')} · 豆瓣 ≥ 7.5 / 🍅 ≥ 85%</div></div>

<div class="filters"><div class="filters-inner">
<div class="fg"><div class="fg-label">🎬 影院</div><div class="fg-btns">
<button class="fb active" data-filter-cinema="all">全部</button>{cinema_btns}
</div></div>
<div class="fg"><div class="fg-label">📅 日期</div><div class="fg-btns">
<button class="fb active" data-filter-day="all">全部</button>{day_btns}
</div></div>
</div></div>

<div class="ct">
<p class="stat" id="stat-text">从 {len(films)} 部排片中精选 {len(rec)} 部高分佳作</p>
{cards}
<div class="no-results" id="no-results" style="display:none">没有符合筛选条件的电影</div>
</div>

<div class="ft">
  Lido Cinemas · Cinema Nova · ACMI · 豆瓣 · Rotten Tomatoes<br>
  {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}<br><br>
  <span style="font-size:.9em;color:var(--acc)">Authored by Zifan Ni && Claude</span><br>
  <a href="https://github.com/Zifanfan/MelborneCinemaInfo" target="_blank" style="color:var(--t2);text-decoration:none;font-size:.85em">
    github.com/Zifanfan/MelborneCinemaInfo
  </a>
</div>

<script>
(function(){{
  let activeCinema='all', activeDay='all';
  const cards=document.querySelectorAll('.fc[data-cinemas]');
  const statEl=document.getElementById('stat-text');
  const noRes=document.getElementById('no-results');

  function applyFilters(){{
    let visible=0;
    cards.forEach(card=>{{
      const cinemas=card.dataset.cinemas||'';
      const days=card.dataset.days||'';
      const matchC=activeCinema==='all'||cinemas.includes(activeCinema);
      const matchD=activeDay==='all'||days.includes(activeDay);
      const show=matchC&&matchD;
      card.classList.toggle('hidden',!show);
      if(show) visible++;
      // 场次行也按日期过滤
      card.querySelectorAll('.ss li[data-day]').forEach(li=>{{
        if(activeDay==='all'){{ li.classList.remove('s-hide'); }}
        else{{ li.classList.toggle('s-hide',!li.dataset.day.includes(activeDay)); }}
      }});
    }});
    statEl.textContent=activeCinema==='all'&&activeDay==='all'
      ? '从 {len(films)} 部排片中精选 {len(rec)} 部高分佳作'
      : '筛选结果: '+visible+' 部电影';
    noRes.style.display=visible===0?'block':'none';
  }}

  document.querySelectorAll('[data-filter-cinema]').forEach(btn=>{{
    btn.addEventListener('click',()=>{{
      activeCinema=btn.dataset.filterCinema;
      document.querySelectorAll('[data-filter-cinema]').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      applyFilters();
    }});
  }});

  document.querySelectorAll('[data-filter-day]').forEach(btn=>{{
    btn.addEventListener('click',()=>{{
      activeDay=btn.dataset.filterDay;
      document.querySelectorAll('[data-filter-day]').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      applyFilters();
    }});
  }});
}})();
</script>
</body></html>"""

def _html_card(f: Film, idx: int) -> str:
    title = _esc(_display_title(f))
    badges = ""
    if f.douban_score:
        badges += f'<span class="bd bd-d">豆瓣 {f.douban_score}</span> '
    else:
        badges += '<span class="bd" style="background:#2a2a3e;color:var(--t2);border:1px solid var(--b)">豆瓣 暂无评分</span> '
    if f.rt_score is not None: badges += f'<span class="bd bd-r">🍅 {f.rt_score}%</span> '

    meta_parts = []
    if f.genre: meta_parts.append(f.genre)
    if f.duration: meta_parts.append(f"⏱ {f.duration}")
    if f.director: meta_parts.append(f"<b>导演</b> {_esc(f.director)}")
    if f.cast: meta_parts.append(f"<b>主演</b> {_esc(f.cast)}")
    meta = f'<div class="meta">{" · ".join(meta_parts)}</div>' if meta_parts else ""

    syn = f'<div class="syn">{_esc(f.synopsis)}</div>' if f.synopsis else ""
    # 获奖信息
    awards_html = ""
    if f.awards:
        awards_html = f'<div style="margin:6px 0;font-size:.82em"><span class="bd" style="background:#3a2a1a;color:#f0c040;border:1px solid #5a4a2d">🏆 {_esc(f.awards)}</span></div>'
    # 影片亮点 (支持 ▸ 分点)
    rec = ""
    if f.recommendation:
        # 将 ▸ 转为列表项, 避免空行
        lines = [l.strip() for l in f.recommendation.split("▸") if l.strip()]
        if lines:
            items = "".join(f"<li>{_esc(l)}</li>" for l in lines)
            rec = f'<div class="rec"><ul class="hl">{items}</ul></div>'
    hc = f'<div class="hc"><em>🗣 豆瓣热评</em> "{_esc(f.hot_comment)}"</div>' if f.hot_comment else ""

    # 场次 + 提取日期/影院用于筛选
    sess_days = set()
    sess_cinemas = set()
    sess = ""
    if f.sessions:
        items = ""
        for s in f.sessions:
            m = re.match(r'\[([^\]]+)\]\s*(.*)', s)
            if m:
                cinema_name, time_info = m.group(1), m.group(2)
                sess_cinemas.add(cinema_name)
                day_label = time_info.split(":")[0].strip().split(",")[0].strip() if ":" in time_info else time_info.split(",")[0].strip()
                sess_days.add(day_label)
                items += f'<li data-day="{_esc(day_label)}" data-cinema="{_esc(cinema_name)}"><span class="ct-tag">{_esc(cinema_name)}</span>{_esc(time_info)}</li>'
            else:
                items += f'<li>{_esc(s)}</li>'
        sess = f'<div class="ss"><div class="ss-t">🎟️ 场次</div><ul>{items}</ul></div>'
    elif f.url:
        sess = f'<div class="ss"><div class="ss-t">🎟️ 场次</div><ul><li>请前往购票页面查看</li></ul></div>'

    for c in f.cinema.split("/"):
        sess_cinemas.add(c.strip())

    lnk = ""
    parts = []
    if f.url: parts.append(f'<a href="{_esc(f.url)}" target="_blank">🎟 购票</a>')
    if f.douban_url: parts.append(f'<a href="{_esc(f.douban_url)}" target="_blank">🟢 豆瓣</a>')
    if f.rt_url: parts.append(f'<a href="{_esc(f.rt_url)}" target="_blank">🍅 烂番茄</a>')
    if parts: lnk = f'<div class="lk">{"".join(parts)}</div>'

    poster_html = ""
    if f.poster:
        poster_html = f'<div class="fc-poster"><img src="{_esc(f.poster)}" alt="{_esc(f.title)}" loading="lazy"></div>'

    data_cinemas = _esc("|".join(sess_cinemas))
    data_days = _esc("|".join(sess_days))

    return f"""<div class="fc" data-cinemas="{data_cinemas}" data-days="{data_days}">
<div class="fh"><span class="rk">#{idx}</span><span class="tt">{title}</span>{badges}</div>
<div style="font-size:.83em;color:var(--t2);margin-bottom:8px">📍 {_esc(f.cinema)}</div>
<div class="fc-body">
{poster_html}
<div class="fc-info">
{meta}{awards_html}{syn}{rec}{hc}{sess}{lnk}
</div>
</div>
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

    # ② 评分查询: RT 并行 → 豆瓣串行
    log.info("📊 烂番茄并行查询 (%d 部)...", len(films))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_query_rt, films))
    log.info("📊 烂番茄完成 (%.1fs)", time.time()-t0)

    log.info("📊 豆瓣串行查询 (%d 部, 避免反爬)...", len(films))
    t0 = time.time()
    _query_douban_serial(films)
    log.info("📊 豆瓣完成 (%.1fs)", time.time()-t0)

    # ②b 豆瓣补查: 对无评分的电影重试一次 (可能是临时限流导致)
    no_score = [f for f in films if f.douban_score is None and f.douban_url]
    if no_score:
        log.info("📊 豆瓣补查 %d 部 (重试无评分的电影)...", len(no_score))
        for f in no_score:
            try:
                resp = SESSION.get(f.douban_url, timeout=10)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    se = soup.find("strong", class_="ll rating_num")
                    if se and (t := se.get_text(strip=True)):
                        f.douban_score = float(t)
                        log.info("  补查成功: %s → %s", f.title, f.douban_score)
                        # 更新缓存
                        cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".douban_cache.json")
                        try:
                            dc = json.load(open(cache_path, "r", encoding="utf-8"))
                            key = _dedup_key(f.title)
                            if key in dc:
                                dc[key]["score"] = f.douban_score
                                json.dump(dc, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(2.5)

    # ③ AI 丰富高分电影 (带缓存)
    high = [f for f in films if is_high_rated(f)]
    log.info("💡 %d 部高分电影，AI enrichment...", len(high))

    # 加载 AI 缓存
    ai_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ai_cache.json")
    ai_cache: dict = {}
    try:
        with open(ai_cache_path, "r", encoding="utf-8") as fp:
            ai_cache = json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # 分出需要调用 AI 的 vs 走缓存的
    need_ai = []
    for f in high:
        key = _dedup_key(f.title)
        if key in ai_cache:
            c = ai_cache[key]
            f.synopsis = c.get("synopsis", "") or f.synopsis
            f.recommendation = c.get("recommendation", "") or _template_recommendation(f)
            f.awards = c.get("awards", "") or f.awards
            log.info("  AI 缓存: %s", f.title)
        else:
            need_ai.append(f)

    client, model = _get_openai_client()
    if need_ai and client:
        log.info("  需要调用 AI: %d 部", len(need_ai))
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(enrich_with_ai, f, client, model): f for f in need_ai}
            for fut in as_completed(futs):
                try: fut.result()
                except Exception as e: log.warning("AI error: %s", e)
        # 写入缓存
        for f in need_ai:
            if f.synopsis or f.recommendation:
                ai_cache[_dedup_key(f.title)] = {
                    "synopsis": f.synopsis,
                    "recommendation": f.recommendation,
                    "awards": f.awards,
                }
    elif need_ai:
        log.info("  (未配置 AI，使用模板推荐)")
        for f in need_ai:
            f.recommendation = _template_recommendation(f)

    # 保存 AI 缓存
    if need_ai:
        try:
            with open(ai_cache_path, "w", encoding="utf-8") as fp:
                json.dump(ai_cache, fp, ensure_ascii=False, indent=2)
            log.info("  AI 缓存已保存 (%d 条)", len(ai_cache))
        except Exception:
            pass

    # ④ 输出
    base = os.path.dirname(os.path.abspath(__file__))
    stamp = dt.date.today().strftime('%Y%m%d')

    md = generate_report(films, start, end)
    md_path = os.path.join(base, f"report_{stamp}.md")
    with open(md_path, "w", encoding="utf-8") as fp: fp.write(md)
    log.info("✅ Markdown: %s", md_path)

    html = generate_html(films, start, end)
    html_path = os.path.join(base, "index.html")
    with open(html_path, "w", encoding="utf-8") as fp: fp.write(html)
    log.info("✅ HTML: %s", html_path)

    print("\n" + "=" * 50)
    try:
        print(md)
    except UnicodeEncodeError:
        print(md.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))
    print("=" * 50)

    # JSON
    jd = [{"title":f.title,"title_cn":f.title_cn,"year":f.year,"cinema":f.cinema,"url":f.url,
           "sessions":f.sessions,"genre":f.genre,"director":f.director,"cast":f.cast,
           "poster":f.poster,"duration":f.duration,"synopsis":f.synopsis,"hot_comment":f.hot_comment,"awards":f.awards,
           "douban_score":f.douban_score,"douban_url":f.douban_url,
           "rt_score":f.rt_score,"rt_url":f.rt_url,"recommendation":f.recommendation} for f in films]
    jp = os.path.join(base, f"data_{stamp}.json")
    with open(jp, "w", encoding="utf-8") as fp: json.dump(jd, fp, ensure_ascii=False, indent=2)
    log.info("📦 JSON: %s", jp)


if __name__ == "__main__":
    main()
