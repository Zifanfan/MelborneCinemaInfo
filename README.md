# 🎬 墨尔本电影周报生成器

自动抓取墨尔本三家影院（Lido Cinemas / Cinema Nova / ACMI）的排片信息，结合豆瓣和烂番茄评分，为你精选本周值得看的高分电影，生成一张精美的静态网页报告。

**在线预览**: [index.html](index.html)

## ✨ 功能特性

### 📡 影院排片抓取
- **Lido Cinemas** — 抓取每日排片页 + 详情页获取精确场次时间
- **Cinema Nova** — 从 now-showing 页面提取当前有场次的电影
- **ACMI** — 从 Nuxt.js SSR 数据提取影院排片
- 自动过滤非电影活动（音乐会、喜剧演出、问答等）
- 跨影院去重合并（同一电影在多家影院的场次合并展示）

### ⭐ 评分查询
- **豆瓣** — 搜索建议 API 获取中文片名 + 搜索页/详情页获取评分
- **烂番茄** — 搜索页获取 Tomatometer + 详情页 JSON-LD 获取导演/类型/海报
- 筛选标准：豆瓣 ≥ 7.5 或 🍅 ≥ 85%
- 综合排序：豆瓣为主 (70%) + 烂番茄为辅 (30%)

### 🎬 影片信息（来自真实数据源，非 AI 编造）
- **导演** — 来自 RT 详情页 JSON-LD schema.org/Movie
- **主演** — 来自 RT 搜索结果 cast 属性
- **类型** — 来自 RT JSON-LD genre
- **海报** — 来自 RT 详情页 og:image
- **中文片名** — 来自豆瓣搜索建议 API
- **年份** — 来自 RT 搜索结果 release-year

### 🤖 AI 生成内容
- 使用 Azure OpenAI / OpenAI / 兼容 API 为高分电影生成：
  - 📖 **剧情简介**（50-80 字，不剧透）
  - 💡 **观影推荐理由**（80-120 字，包含亮点和适合人群）
- 无 AI 时自动 fallback 到模板推荐语

### 🌐 HTML 静态网页
- 暗色电影主题，响应式布局
- 电影卡片：海报 + 评分标签 + 导演/主演 + 简介 + 推荐 + 场次
- **交互式筛选器**：按影院 / 按日期（`3/23 Monday` 格式）筛选
- 日期筛选时场次行也自动过滤
- 页脚作者签名 + GitHub 链接

### ⚡ 性能优化
- **并行爬取** — Lido 详情页 8 线程、RT 查询 6 线程、AI 调用 4 线程
- **豆瓣防反爬** — 串行请求 + 2.5s 间隔，避免 IP 封禁
- **本地缓存** — 豆瓣评分 (`.douban_cache.json`) + AI 推荐 (`.ai_cache.json`)
- 二次运行近乎秒出（全走缓存，0 API 调用）
- 总耗时约 30-40 秒（首次运行）

### 📅 日期处理
- 范围：今天起 7 天（不含下周同一天）
- Lido 场次通过解析 tab 文本（Today / Tomorrow / 星期名 / 日期格式）定位到具体日期
- 统一日期标签：`3/23 Monday` 格式，按日期顺序排列
- 自动过滤范围外的场次

## 🚀 使用方法

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 配置 AI（可选）

在 `.env` 中配置以下任一方式：

```bash
# Azure OpenAI
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4.1

# 或 OpenAI 原生
OPENAI_API_KEY=sk-...

# 或兼容 API (DeepSeek / Ollama 等)
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com/v1
```

不配置 AI 也能正常运行，会使用模板推荐语。

### 3. 运行
```bash
python melbourne_cinema.py
```

### 4. 输出
- `index.html` — 精美的静态网页报告（可直接打开或部署）
- `report_YYYYMMDD.md` — Markdown 格式报告
- `data_YYYYMMDD.json` — 原始数据 JSON

## 📁 项目结构

```
├── melbourne_cinema.py     # 主脚本
├── index.html              # 生成的 HTML 报告
├── requirements.txt        # Python 依赖
├── .env                    # AI 配置 (不提交)
├── .env.example            # 配置模板
├── .douban_cache.json      # 豆瓣评分缓存
├── .ai_cache.json          # AI 推荐语缓存
├── report_YYYYMMDD.md      # Markdown 报告
└── data_YYYYMMDD.json      # 原始数据
```

## 👥 作者

**Zifan Ni && Claude**

---

*数据来源: Lido Cinemas · Cinema Nova · ACMI · 豆瓣 · Rotten Tomatoes*
