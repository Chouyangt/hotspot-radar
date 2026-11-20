# 热点雷达

深圳与世界实时热点，每 15 分钟自动抓取、聚类、发布。

- **热榜**：https://chouyangt.github.io/hotspot-radar/
- 手机浏览器打开即可，无需电脑开机

## 数据源

| 源 | 类型 | 说明 |
|---|---|---|
| 微博热搜 | 热榜 | 55 条实时榜，带热度值与排名 |
| 头条热榜 | 热榜 | 50 条，带 HotValue |
| 百度热搜 | 热榜 | 51 条，带 hotScore |
| 深圳新闻网 | 新闻流 | 首页要闻，日期从 URL 提取 |
| 中新网 · 国际 | 新闻流 | RSS |
| 新华网 · 国际 | 新闻流 | RSS（历史堆积多，按 pubDate 截断）|

## 怎么做的

**聚类是关键。** 同一条热点会同时出现在三个榜上（"普京：中俄美元首会晤前所未有"
在头条和百度都在榜）。程序把标题归一化后合并，一条热点显示为：
热度取各榜最高、来源列出全部平台、榜位取最优。

归一化规则：去掉 `#话题#`、书名号、标点、空格；一方是另一方的前缀且长度差 ≤6 也算同一条。

**分类**按标题里的主体词：
- 深圳：深圳/鹏城/前海/南山/福田/宝安/龙岗/龙华/光明/坪山/盐田/大鹏/罗湖/深汕
- 世界：具体国名或外国领导人（特朗普/普京/泽连斯基…）、国际组织
- 国内：其余

不用「全球」「国际」这种泛词判世界 —— 否则「"全球最大医院"原党委书记被双开」会被误判。

**新上榜判断**靠 SQLite 的 `first_seen`：本轮有、库里没有 → 新上榜，页面打红标，同时推送。

## 用法

```bat
打开热榜.bat          :: 抓取 + 打开
推送更新.bat          :: 改了代码同步到 GitHub
python radar.py show  :: 命令行看当前榜
```

## 推送

飞书卡片，只推**新上榜且属于深圳或世界**的热点，一次最多 8 条。

配置（本地 `config.json`，线上用 GitHub Secret `CONFIG_JSON`）：

```json
{
  "notify": {
    "feishu": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
    "site_url": "https://chouyangt.github.io/hotspot-radar/"
  }
}
```

## 目录

```
hotspot-radar\
├─ radar.py          抓取 / 聚类 / 分类 / 渲染
├─ notify.py         飞书卡片推送
├─ sources.json      数据源表
├─ template.html     热榜页（自包含，无 CDN）
├─ data\hotspot.db   SQLite，记 first_seen 用
├─ out\index.html    热榜页
└─ .github/workflows/radar.yml
```

## 已知限制

- **境外 runner 可能抓不到国内热榜**：微博/头条/百度对境外 IP 可能限流。
  workflow 跑完后看页面底部的数据源状态，○ 表示该源在本轮失败。
- 新华网国际 RSS 里混着 2022 年的旧闻，已按 pubDate 截断最近 4 天。
- 微博热搜偶有错别字（如「APCE」），照原样显示，不做纠正。
