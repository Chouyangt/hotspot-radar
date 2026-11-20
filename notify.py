# -*- coding: utf-8 -*-
"""
热点推送。配置写在 config.json 的 notify 段，支持飞书/企微/钉钉/Telegram/Bark。
飞书用卡片，一条热点一个区块。

  "notify": {
      "feishu": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
      "feishu_secret": "开了签名校验才填",
      "site_url": "https://<user>.github.io/hotspot-radar/"
  }
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CURL = "curl.exe" if os.name == "nt" else "curl"


def _post(url: str, payload: dict, timeout: int = 20) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    p = subprocess.run(
        [CURL, "-sS", "-X", "POST",
         "-H", "Content-Type: application/json; charset=utf-8",
         "--max-time", str(timeout), "--data-binary", "@-", url],
        input=body, capture_output=True, timeout=timeout + 10)
    return (p.stdout or b"").decode("utf-8", "ignore")


def _ok(channel: str, raw: str) -> str:
    t = (raw or "").strip()
    try:
        d = json.loads(t)
    except Exception:
        return f"⚠ {channel} 返回无法解析：{t[:100]}"
    if "code" in d:
        return "ok" if d.get("code") == 0 else f"⚠ 飞书 {d.get('code')}: {d.get('msg')}"
    if "StatusCode" in d:
        return "ok" if d.get("StatusCode") == 0 else f"⚠ 飞书 {d.get('StatusCode')}"
    if "errcode" in d:
        return "ok" if d.get("errcode") == 0 else f"⚠ {channel} {d.get('errcode')}: {d.get('errmsg')}"
    if "ok" in d:
        return "ok" if d["ok"] else f"⚠ Telegram：{t[:100]}"
    return "ok"


def load_cfg() -> dict:
    cfg = {}
    p = ROOT / "config.json"
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            cfg = {}
    raw = os.environ.get("CONFIG_JSON")
    if raw:
        try:
            env = json.loads(raw)
            if isinstance(env, dict):
                for k, v in env.items():
                    if k == "notify" and isinstance(v, dict):
                        cfg.setdefault("notify", {}).update(v)
                    else:
                        cfg[k] = v
        except Exception:
            pass
    return cfg


def heat(n) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return ""
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{round(n / 10000)}万"
    return str(n) if n else ""


def feishu_card(items: list[dict], site_url: str = "") -> dict:
    els = []
    for i, it in enumerate(items):
        lines = [f"**{it['title'][:58]}**"]
        meta = f"{it.get('cat', '')} · {heat(it.get('hot')) or '无热度'}"
        if it.get("rank"):
            meta += f" · 榜位 {it['rank']}"
        lines.append(meta)
        if it.get("sources"):
            lines.append(f"来源 {it['sources']}")
        if it.get("url"):
            lines.append(f"[看详情]({it['url']})")
        els.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        if i < len(items) - 1:
            els.append({"tag": "hr"})
    if site_url:
        els.append({"tag": "action", "actions": [{
            "tag": "button", "text": {"tag": "plain_text", "content": "打开热榜"},
            "type": "primary", "url": site_url}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": f"🔥 热点雷达 · 新上榜 {len(items)} 条"},
                       "template": "red"},
            "elements": els}


def build_text(items: list[dict], site_url: str = "") -> str:
    lines = [f"【热点雷达】新上榜 {len(items)} 条"]
    for it in items:
        lines.append("")
        lines.append(f"{it['title']}")
        m = f"{it.get('cat','')} · {heat(it.get('hot'))} · {it.get('sources','')}"
        lines.append(m)
        if it.get("url"):
            lines.append(it["url"])
    if site_url:
        lines.append(f"\n热榜：{site_url}")
    return "\n".join(lines)


def feishu_sign(secret: str):
    ts = str(int(time.time()))
    h = hmac.new(f"{ts}\n{secret}".encode(), b"", digestmod=hashlib.sha256).digest()
    return ts, base64.b64encode(h).decode()


def send(items: list[dict], conf: dict | None = None) -> str:
    cfg = conf or load_cfg()
    n = cfg.get("notify") or {}
    if not items:
        return "本轮无新上榜"
    if not n:
        return "未配置推送通道"

    site = n.get("site_url", "")
    text = build_text(items, site)
    results = []

    def try_send(name, fn):
        try:
            s = _ok(name, fn())
            results.append(f"{name}：{s}")
            print(f"    · {name}：{s}")
        except Exception as e:
            results.append(f"{name}：异常 {e}")
            print(f"    ! {name} 失败：{e}")

    if n.get("feishu"):
        payload = {"msg_type": "interactive", "card": feishu_card(items, site)}
        if n.get("feishu_secret"):
            ts, sign = feishu_sign(n["feishu_secret"])
            payload.update({"timestamp": ts, "sign": sign})
        try_send("飞书", lambda: _post(n["feishu"], payload))

    if n.get("wecom"):
        try_send("企业微信", lambda: _post(n["wecom"], {"msgtype": "text",
                                                   "text": {"content": text}}))
    if n.get("dingtalk"):
        try_send("钉钉", lambda: _post(n["dingtalk"], {"msgtype": "text",
                                                  "text": {"content": text}}))
    if n.get("bark"):
        base = n["bark"].rstrip("/")
        try_send("Bark", lambda: _post(base, {"title": "热点雷达", "body": text[:200]}))

    return "；".join(results) if results else "无可用通道"
