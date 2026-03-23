"""
豆瓣缓存手动补充工具
====================
当主脚本因豆瓣反爬无法获取评分时，可用此工具手动补充。

用法:
    1. 编辑下方 FILMS 列表，填入需要查询的电影英文名
    2. 运行: python fill_douban_cache.py
    3. 再运行主脚本即可使用新缓存

也可以直接修改 .douban_cache.json 手动添加条目。
"""

import json, requests, re, time, os
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".douban_cache.json")

# ═══════════════════════════════════════
# 在这里填入需要查询的电影名
# ═══════════════════════════════════════
FILMS = [
    # "Perfect Days",
    # "Paris, Texas",
    # "Buena Vista Social Club",
]

# 可选: 手动指定中文名 (suggest API 被封时使用)
MANUAL_CN_NAMES = {
    # "perfect days": "完美的日子",
    # "paris texas": "德州巴黎",
}

# ═══════════════════════════════════════


def dedup_key(t: str) -> str:
    if ", The" in t: t = "The " + t.replace(", The", "")
    t = re.sub(r'\s*\(\d{4}\)\s*', ' ', t)
    t = re.sub(r'\s*\(Dubbed\)\s*', '', t, flags=re.I)
    t = re.sub(r'\bdubbed\b', '', t, flags=re.I)
    t = re.sub(r"[^\w\s'-]", ' ', t)
    return re.sub(r'\s+', ' ', t).strip().lower()


def search_title(t: str) -> str:
    if ", The" in t: t = "The " + t.replace(", The", "")
    t = re.sub(r'\s*\(\d{4}\)\s*', ' ', t)
    t = re.sub(r"[^\w\s'-]", ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def search_douban(title: str) -> dict:
    """查询豆瓣评分，返回 {score, url, title_cn, hot_comment}"""
    clean = search_title(title)
    result = {"score": None, "url": "", "title_cn": "", "hot_comment": ""}

    # suggest API (轻量, 拿中文名和ID)
    try:
        r = SESSION.get("https://movie.douban.com/j/subject_suggest",
                        params={"q": clean}, timeout=8)
        if r.status_code == 200:
            for item in (r.json() or []):
                if item.get("type") == "movie":
                    result["title_cn"] = item.get("title", "")
                    did = item.get("id", "")
                    if did:
                        result["url"] = f"https://movie.douban.com/subject/{did}/"
                    break
    except Exception:
        pass

    # 搜索页 (拿评分)
    try:
        r = SESSION.get("https://www.douban.com/search",
                        params={"q": clean, "cat": "1002"}, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            rating = soup.find("span", class_="rating_nums")
            if rating and (txt := rating.get_text(strip=True)):
                result["score"] = float(txt)
    except Exception:
        pass

    # 详情页 fallback (如果搜索页没拿到评分)
    if result["score"] is None and result["url"]:
        try:
            r = SESSION.get(result["url"], timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                se = soup.find("strong", class_="ll rating_num")
                if se and (t := se.get_text(strip=True)):
                    result["score"] = float(t)
                ce = soup.find("span", class_="short")
                if ce:
                    result["hot_comment"] = ce.get_text(strip=True)[:120]
        except Exception:
            pass

    return result


def main():
    if not FILMS:
        print("请在 FILMS 列表中填入需要查询的电影名，然后重新运行。")
        print("示例:")
        print('  FILMS = ["Perfect Days", "Paris, Texas", "Sinners"]')
        return

    # 加载缓存
    cache = {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    print(f"当前缓存: {len(cache)} 条")
    print(f"待查询: {len(FILMS)} 部\n")

    queried = 0
    for i, title in enumerate(FILMS):
        key = dedup_key(title)

        # 跳过已缓存
        if key in cache and cache[key].get("score") is not None:
            print(f"  [{i+1}/{len(FILMS)}] {title} → 已缓存 ✓ {cache[key]['score']}")
            continue

        print(f"  [{i+1}/{len(FILMS)}] {title}")
        result = search_douban(title)

        # 应用手动中文名
        if key in MANUAL_CN_NAMES:
            result["title_cn"] = MANUAL_CN_NAMES[key]

        # 显示结果
        status = f"✓ {result['score']}" if result['score'] else "✗ 无评分"
        if result['title_cn']:
            status += f" ({result['title_cn']})"
        print(f"    {status}")

        # 写入缓存
        cache[key] = result
        queried += 1
        time.sleep(2.5)

    # 保存
    if queried > 0:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"\n缓存已更新: {len(cache)} 条 (新增 {queried} 条)")
    else:
        print("\n无需更新")


if __name__ == "__main__":
    main()
