#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热点雷达 (hotspot-radar)
========================
抓深圳与世界的实时热点：三个热榜（微博/头条/百度）+ 新闻流（中新网/新华网/深圳新闻网）。

设计要点：
  · 热榜类源给「热度排名」，新闻流类源给「内容」——两者分开处理，不混排序
  · 同一条热点会在多个榜单同时出现（"12306迎来重大更新"三榜都在），必须聚类
  · 用 SQLite 记 first_seen，才能回答"这条是不是刚上榜"

用法：
    python radar.py sync      # 抓取 + 聚类 + 生成页面
    python radar.py show      # 命令行看当前榜
"""

from __future__ import annotations

import hashlib
import html as ht
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
DB_PATH = DATA_DIR / "hotspot.db"
CONF_PATH = ROOT / "sources.json"
TEMPLATE = ROOT / "template.html"

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CURL = "curl.exe" if os.name == "nt" else "curl"


def now_cst() -> datetime:
    return datetime.now(CST)


def today() -> str:
    return now_cst().strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════
# 传输
# ══════════════════════════════════════════════════════════════

def http_get(url: str, referer: str | None = None, timeout: int = 20,
             retries: int = 3) -> str:
    cmd = [CURL, "-s", "-L", "--compressed", "--max-time", str(timeout),
           "-A", UA, "-H", "Accept-Language: zh-CN,zh;q=0.9"]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    last = ""
    for i in range(retries):
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        raw = p.stdout or b""
        if raw:
            for enc in ("utf-8", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "ignore")
        last = f"curl exit {p.returncode}"
        if i < retries - 1:
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"取不到（{last}）{url}")


def clean(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = ht.unescape(s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def strip_tag(s: str) -> str:
    """去掉微博话题的 # #、书名号等，便于聚类比对。"""
    s = re.sub(r"[#＃].*?[#＃]", "", s) if s.count("#") >= 2 else s
    return re.sub(r"[\s#＃《》〈〉“”\"'（）()【】\[\]·,，、。：:；;！!？?…—\-~]", "", s)


# ══════════════════════════════════════════════════════════════
# 适配器：热榜类（带热度）
# ══════════════════════════════════════════════════════════════

def adapt_weibo(src, conf):
    """微博热搜，data.realtime[]，字段 note/num/realpos。"""
    d = json.loads(http_get(src["url"], referer="https://weibo.com/"))
    out = []
    for it in (d.get("data") or {}).get("realtime") or []:
        title = clean(it.get("note") or it.get("word") or "")
        if not title:
            continue
        out.append({
            "title": title,
            "url": f"https://s.weibo.com/weibo?q={title}",
            "hot": int(it.get("num") or 0),
            "rank": int(it.get("realpos") or 0),
            "source": src["name"],
            "tag": clean(it.get("icon_desc") or ""),
        })
    return out


def adapt_toutiao(src, conf):
    """今日头条热榜，data[]，字段 Title/HotValue/Url。"""
    d = json.loads(http_get(src["url"], referer="https://www.toutiao.com/"))
    out = []
    for i, it in enumerate(d.get("data") or [], 1):
        title = clean(it.get("Title") or "")
        if not title:
            continue
        out.append({
            "title": title,
            "url": it.get("Url") or "",
            "hot": int(float(it.get("HotValue") or 0)),
            "rank": i,
            "source": src["name"],
            "tag": clean(it.get("Label") or ""),
        })
    return out


def adapt_baidu(src, conf):
    """百度热搜：标题取 c-single-text-ellipsis，热度用 hotScore 按顺序配对。
    不能用 '"word":"(.*?)"[^}]*?"hotScore"' —— 会把 '","isTop":true},{' 吃进标题。"""
    h = http_get(src["url"], referer="https://www.baidu.com/")
    titles = re.findall(r'class="c-single-text-ellipsis">\s*(.*?)\s*<', h, re.S)
    scores = re.findall(r'"hotScore":"(\d+)"', h)

    out, seen = [], set()
    for i, raw in enumerate(titles, 1):
        title = clean(raw)
        if not title or title in seen or len(title) < 4:
            continue
        seen.add(title)
        hot = int(scores[i - 1]) if i - 1 < len(scores) else 0
        out.append({
            "title": title,
            "url": f"https://www.baidu.com/s?wd={title}",
            "hot": hot,
            "rank": i,
            "source": src["name"],
            "tag": "",
        })
    return out


# ══════════════════════════════════════════════════════════════
# 适配器：新闻流类（无热度）
# ══════════════════════════════════════════════════════════════

def _parse_rss(xml: str, keep_days: int = 3) -> list[dict]:
    out = []
    cutoff = now_cst() - timedelta(days=keep_days)
    for raw in re.findall(r"<item>(.*?)</item>", xml, re.S):
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", raw, re.S)
        l = re.search(r"<link>(.*?)</link>", raw, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", raw, re.S)
        if not t or not l:
            continue
        title = clean(t.group(1))
        if len(title) < 6:
            continue

        pub = None
        if d:
            for fmt in ("%a,%d-%b-%Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                        "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(d.group(1).strip(), fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=CST)
                    pub = dt.astimezone(CST)
                    break
                except Exception:
                    continue
        if pub and pub < cutoff:
            continue

        out.append({"title": title, "url": clean(l.group(1)),
                    "hot": 0, "rank": 0, "tag": "",
                    "time": pub.strftime("%Y-%m-%d %H:%M") if pub else ""})
    out.sort(key=lambda x: x.get("time") or "", reverse=True)
    return out


def adapt_rss(src, conf):
    rows = _parse_rss(http_get(src["url"]), int(src.get("keep_days", 3)))
    for r in rows:
        r["source"] = src["name"]
    return rows[: int(src.get("limit", 30))]


def adapt_sznews(src, conf):
    """深圳新闻网首页：链接里带 /news/content/YYYY-MM/DD/ 的可取日期。"""
    h = http_get(src["url"])
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]*sznews\.com/news/content/(\d{4})-(\d{2})/(\d{2})/[^"]+)"[^>]*>(.*?)</a>', h, re.S):
        url, y, mo, d, raw = m.groups()
        title = clean(raw)
        if len(title) < 8 or url in seen:
            continue
        seen.add(url)
        out.append({"title": title[:120], "url": url, "hot": 0, "rank": 0,
                    "tag": "", "time": f"{y}-{mo}-{d}", "source": src["name"]})
    # 同一日期内的按页面顺序保留
    return out[: int(src.get("limit", 40))]


ADAPTERS = {
    "weibo_hot": adapt_weibo,
    "toutiao_hot": adapt_toutiao,
    "baidu_hot": adapt_baidu,
    "rss": adapt_rss,
    "sznews": adapt_sznews,
}


# ══════════════════════════════════════════════════════════════
# 分类与聚类
# ══════════════════════════════════════════════════════════════

SZ_RE = re.compile(r"(深圳|鹏城|前海|河套|南山|福田|宝安|龙岗|龙华|光明|坪山|"
                   r"盐田|大鹏|罗湖|深汕|深中通道|深圳湾|华强北|大芬)")

# 国际主体：只认具体的国家/外国领导人/国际组织，不用「全球/国际」这种泛词，
# 否则「"全球最大医院"原党委书记被双开」会被误判成世界新闻
WORLD_RE = re.compile(
    r"(美国|特朗普|拜登|白宫|俄罗斯|普京|乌克兰|泽连斯基|欧洲|欧盟|北约|英国|法国|德国|"
    r"日本|韩国|朝鲜|印度|以色列|伊朗|加沙|巴勒斯坦|叙利亚|土耳其|沙特|埃及|南非|"
    r"巴西|阿根廷|墨西哥|加拿大|澳大利亚|新西兰|东盟|联合国|世贸|世卫|"
    r"G20|G7|金砖|美联储|纳斯达克|道琼斯|各国领导人)")
# 出现这些就按国内处理，即使标题里有外国名
DOMESTIC_RE = re.compile(r"(我国|中国|全国|国内|中央|国务院|省委|市委|全省|全市)")


def categorize(title: str, src_cat: str) -> str:
    if src_cat == "深圳" or SZ_RE.search(title):
        return "深圳"
    if src_cat == "国际":
        return "世界"
    if WORLD_RE.search(title) and not DOMESTIC_RE.search(title):
        return "世界"
    return "国内"


def cluster(items: list[dict]) -> list[dict]:
    """
    把多榜重复的同一条合并。规则：
      · 归一化标题完全相同 → 同簇
      · 一方是另一方的前缀且长度差 ≤ 6（"12306迎来重大更新" vs "12306迎来重大更新！"）
    """
    buckets: list[dict] = []
    for it in items:
        key = strip_tag(it["title"])
        if len(key) < 4:
            continue
        it["_key"] = key
        hit = None
        for b in buckets:
            k = b["_key"]
            if key == k:
                hit = b
                break
            short, long_ = (key, k) if len(key) <= len(k) else (k, key)
            if len(short) >= 6 and long_.startswith(short) and len(long_) - len(short) <= 6:
                hit = b
                break
        if hit is None:
            it["sources"] = [it["source"]]
            it["hot_max"] = it["hot"]
            it["rank_min"] = it["rank"] or 999
            buckets.append(it)
        else:
            hit["sources"].append(it["source"])
            hit["hot_max"] = max(hit["hot_max"], it["hot"])
            if it["rank"]:
                hit["rank_min"] = min(hit["rank_min"], it["rank"])
            # 没有链接的用有的补上
            if not hit.get("url") and it.get("url"):
                hit["url"] = it["url"]
            if not hit.get("time") and it.get("time"):
                hit["time"] = it["time"]
    return buckets


# ══════════════════════════════════════════════════════════════
# 存储
# ══════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, title TEXT, url TEXT, cat TEXT,
  hot INTEGER, rank INTEGER, sources TEXT,
  first_seen TEXT, last_seen TEXT, time TEXT
);
CREATE INDEX IF NOT EXISTS idx_last ON items(last_seen);
CREATE TABLE IF NOT EXISTS runs(ts TEXT, source TEXT, ok INTEGER, n INTEGER, note TEXT);
"""


def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def item_id(title: str) -> str:
    return hashlib.sha1(strip_tag(title).encode("utf-8")).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════

def load_conf():
    return json.loads(CONF_PATH.read_text(encoding="utf-8-sig"))


def sync(use_ai=False, test_push=False):
    conf = load_conf()
    conn = db()
    health, all_items = [], []

    print(f"\n=== 热点雷达 · {now_cst():%Y-%m-%d %H:%M} ===")
    for src in conf["sources"]:
        if not src.get("enabled", True):
            continue
        fn = ADAPTERS.get(src["adapter"])
        if not fn:
            print(f"  ? {src['name']}: 未知适配器")
            continue
        try:
            rows = fn(src, conf)
        except Exception as e:
            print(f"  x {src['name']}: {e}")
            health.append({"name": src["name"], "ok": False, "n": 0})
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?)",
                         (now_cst().isoformat(timespec="seconds"), src["id"], 0, 0, str(e)[:150]))
            continue

        for r in rows:
            r["cat"] = categorize(r["title"], src.get("cat", ""))
            r["_src_cat"] = src.get("cat", "")
        all_items.extend(rows)
        print(f"  ok {src['name']}: {len(rows)} 条")
        health.append({"name": src["name"], "ok": True, "n": len(rows)})
        conn.execute("INSERT INTO runs VALUES(?,?,?,?,?)",
                     (now_cst().isoformat(timespec="seconds"), src["id"], 1, len(rows), ""))
    conn.commit()

    # 聚类（只看热榜类；新闻流各自独立，不参与合并）
    ranked = [i for i in all_items if i["hot"] > 0 or i["rank"]]
    news = [i for i in all_items if not (i["hot"] > 0 or i["rank"])]
    merged = cluster(ranked)
    print(f"\n热榜 {len(ranked)} 条 → 聚类后 {len(merged)} 个话题；新闻流 {len(news)} 条")

    stamp = now_cst().isoformat(timespec="seconds")
    new_ids = []
    for it in merged + news:
        pid = item_id(it["title"])
        exists = conn.execute("SELECT 1 FROM items WHERE id=?", (pid,)).fetchone()
        if not exists:
            new_ids.append(pid)
        conn.execute("""
          INSERT INTO items(id,title,url,cat,hot,rank,sources,first_seen,last_seen,time)
          VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET
            last_seen=excluded.last_seen,
            hot=MAX(items.hot, excluded.hot),
            rank=MIN(CASE WHEN items.rank=0 THEN excluded.rank ELSE items.rank END,
                     CASE WHEN excluded.rank=0 THEN items.rank ELSE excluded.rank END),
            sources=excluded.sources
        """, (pid, it["title"], it.get("url", ""), it["cat"],
              int(it.get("hot") or 0), int(it.get("rank") or 0),
              "、".join(dict.fromkeys(it.get("sources") or [it["source"]])),
              stamp, stamp, it.get("time") or ""))
    conn.commit()

    print(f"本轮新上榜 {len(new_ids)} 条")
    render(conn, health, len(new_ids))

    # 推送：只推深圳/世界里的高热话题
    push_msg = ""
    if (conf.get("notify_min_hot") or 0) > 0 or new_ids:
        try:
            import notify
            rows = conn.execute(
                "SELECT * FROM items WHERE last_seen=? AND cat IN ('深圳','世界') "
                "ORDER BY hot DESC, rank ASC LIMIT 8", (stamp,)
            ).fetchall()
            hot = [dict(r) for r in rows]
            if test_push or hot:
                push_msg = notify.send(hot or [], conf)
                print(f"  · 推送：{push_msg}")
        except Exception as e:
            print(f"  ! 推送失败：{e}")

    conn.close()
    return len(new_ids)


# ── 渲染 ─────────────────────────────────────────────────────

def js_safe(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def load_view(conn) -> list[dict]:
    cut = (now_cst() - timedelta(days=2)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM items WHERE last_seen>=? ORDER BY "
        "CASE WHEN hot>0 THEN 0 ELSE 1 END, hot DESC, rank ASC, time DESC", (cut,)
    ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        out.append({
            "id": r["id"], "title": r["title"], "url": r["url"], "cat": r["cat"],
            "hot": r["hot"], "rank": r["rank"], "src": r["sources"],
            "time": r["time"] or "",
            "fresh": (r["first_seen"] or "") >= (now_cst() - timedelta(hours=3)).isoformat(timespec="seconds"),
        })
    return out


def render(conn, health=None, new_count=0):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load_view(conn)
    tpl = TEMPLATE.read_text(encoding="utf-8")
    meta = {
        "updated": now_cst().strftime("%Y-%m-%d %H:%M"),
        "today": today(),
        "sources": health or [],
        "new": new_count,
    }
    html = (tpl.replace("/*__DATA__*/[]", js_safe(data))
               .replace("/*__META__*/{}", js_safe(meta)))
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"  -> {OUT_DIR / 'index.html'}  ({len(data)} 条)")


def cmd_show():
    conn = db()
    rows = load_view(conn)
    for cat in ("深圳", "世界", "国内"):
        sub = [r for r in rows if r["cat"] == cat][:10]
        print(f"\n【{cat}】{len(sub)} 条")
        for r in sub:
            heat = f"{r['hot'] / 10000:.0f}万" if r["hot"] >= 10000 else (str(r["hot"]) if r["hot"] else "")
            print(f"  {r['title'][:44]:<46} {heat:<8} {r['src'][:24]}")
    conn.close()


def main():
    args = sys.argv[1:]
    cmd, test_push = "sync", False
    for a in args:
        if a in ("sync", "render", "show"):
            cmd = a
        elif a == "--test-push":
            test_push = True

    if cmd == "show":
        cmd_show()
    elif cmd == "render":
        conn = db()
        render(conn)
        conn.close()
    else:
        t0 = time.time()
        sync(test_push=test_push)
        print(f"耗时 {time.time() - t0:.1f}s\n")


if __name__ == "__main__":
    main()
