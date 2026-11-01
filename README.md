# Nashville 演唱会追踪 🎸

一个自动更新的静态网站，用来追踪纳什维尔（Nashville, TN）的演唱会。

- **关注歌手面板**：每位关注歌手在纳什维尔的下一场演出；如果只在周边城市（亚特兰大、路易斯维尔、孟菲斯等 350 英里内），也会显示出来
- **演出列表**：「关注 / 主要场馆 / 全部」三种视图，可以按曲风、场馆、月份筛选，也能搜索
- **开售提醒**：显示公开开售和预售时间（纳什维尔时间），新上架的演出带 `NEW` 标记
- **新演出通知**：关注歌手出现新场次时，GitHub Action 会自动在仓库里开一个 Issue，GitHub 会发邮件通知你
- **日历**：每场演出都能一键导出 `.ics`；也可以订阅 `data/watchlist.ics`，关注歌手的场次会自动同步到手机日历
- **本地关注**：在网页上临时加几个歌手，只保存在当前浏览器

数据来源：[Ticketmaster Discovery API](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/)。纳什维尔的大场馆（Bridgestone Arena、Nissan Stadium、Ascend Amphitheater、Ryman 等）基本都在 Ticketmaster 售票。

## 一次性设置（约 5 分钟）

1. **申请免费 API key**：在 <https://developer-acct.ticketmaster.com/user/register> 注册，登录后进入 *My Apps*，复制默认 App 的 **Consumer Key**。
2. **添加到仓库 Secret**：仓库 → *Settings* → *Secrets and variables* → *Actions* → *New repository secret*
   - Name：`TICKETMASTER_API_KEY`
   - Secret：上一步的 Consumer Key
3. **打开 GitHub Pages**：仓库 → *Settings* → *Pages* → *Build and deployment* → *Source* 选 **GitHub Actions**。
4. **运行第一次抓取**：仓库 → *Actions* → *Update concerts* → *Run workflow*。

完成后网站地址是 `https://<你的用户名>.github.io/<仓库名>/`。之后每天自动更新两次（UTC 11:17 和 23:17），每次 push 到 `main` 也会更新一次。

> 第一次运行会开一个「开始追踪」Issue，列出关注歌手目前已公布的全部场次。之后只有**新增**场次才会开 Issue。
> 收不到邮件的话，检查仓库右上角 *Watch* 是否为 *All Activity* 或 *Participating and @mentions*，以及 GitHub 的通知邮箱设置。

## 修改关注的歌手

编辑 `config.json` 里的 `watchlist`，提交后 Action 会自动重新抓取：

```json
"watchlist": [
  "Ed Sheeran",
  "Coldplay",
  { "name": "Florence + the Machine", "aliases": ["Florence and the Machine"] },
  { "name": "某歌手", "tm_id": "K8vZ9171xxx" }
]
```

- 直接写英文名即可，大小写、重音、开头的 “The” 都会忽略
- 网页上提示「Ticketmaster 上未找到该歌手」时，可以加 `aliases`（别名），或者直接填 `tm_id`：用浏览器打开
  `https://app.ticketmaster.com/discovery/v2/attractions.json?keyword=歌手名&apikey=你的key`，
  在结果里找到正确的歌手，复制它的 `id`（形如 `K8vZ9171xxx`；注意不是 Ticketmaster 网页网址里的数字）

## 其他设置（`config.json`）

| 字段 | 作用 | 默认 |
|---|---|---|
| `radius_miles` | 以纳什维尔市中心为圆心，多少英里内算「纳什维尔」（覆盖 Franklin 的 FirstBank Amphitheater 等） | 30 |
| `nearby_radius_miles` | 关注歌手的「周边城市」场次范围 | 350 |
| `months_ahead` | 抓取未来多少个月 | 12 |
| `major_venues` | 「主要场馆」视图包含的场馆（名称包含即可） | 见文件 |
| `exclude_name_patterns` | 名称含这些词的条目会被过滤（停车券、VIP 套餐等） | 见文件 |
| `manual_events` | 手动添加的演出，见下 | `[]` |

### 手动添加演出

有些演出官宣了但还没上架 Ticketmaster，或者在 AXS 等其他平台售票，可以手动加进去：

```json
"manual_events": [
  {
    "name": "Some Artist: Tour 2027",
    "artists": ["Some Artist"],
    "date": "2027-05-15",
    "time": "19:30",
    "venue": "Nissan Stadium",
    "url": "https://example.com/tickets",
    "note": "官宣，11 月 7 日开售"
  }
]
```

只有 `name` 和 `date` 是必填的。等 Ticketmaster 上架了同一天、同一位歌手的场次，手动条目会自动隐藏。

## 本地运行

```bash
# 抓数据（需要 API key）
TICKETMASTER_API_KEY=你的key python3 scripts/fetch_events.py

# 预览网页，然后打开 http://localhost:8000
python3 -m http.server 8000

# 跑测试
python3 -m unittest discover -s tests
```

只用到 Python 标准库，不需要安装任何依赖。

## 项目结构

```
├── index.html                    网页
├── assets/app.js, style.css      前端（无框架、无构建）
├── config.json                   关注歌手和各项设置
├── data/
│   ├── events.json               抓取结果（Action 自动提交）
│   ├── watchlist.ics             关注歌手的日历订阅
│   └── state.json                记录哪些演出已见过、已通知
├── scripts/fetch_events.py       抓取脚本
├── tests/                        单元测试（模拟 Ticketmaster 响应）
└── .github/workflows/update.yml  定时抓取 + 开 Issue + 部署 Pages
```

## 已知限制

- 只覆盖 Ticketmaster 售票的演出；AXS、Eventbrite 和小场馆自售的演出可能缺失，可以用 `manual_events` 补
- Ticketmaster 的价格区间不一定准确，也不含转售价
- 公开仓库超过 60 天没有任何提交，GitHub 会暂停定时任务。数据有变化时 Action 会自动提交，一般不会触发；万一被暂停，到 *Actions* 页面重新启用即可
- 免费 API key 每天限 5000 次请求，这个项目每次运行大约用 50–80 次
