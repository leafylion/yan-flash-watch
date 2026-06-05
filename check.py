#!/usr/bin/env python3
"""yan-flash 妖精乱舞 攻略ページの「更新履歴」を監視し、変化があれば Discord に通知する。

ページの更新履歴は Next.js がレンダリングした素の <time>/<p> タグとして HTML に
含まれているため、ヘッドレスブラウザは不要。curl/urllib で取得してパースできる。
"""
import html as htmlmod
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

URL = "https://yan-flash.com/ultimate/yosei-ranbu"
STATE = Path(__file__).parent / "state" / "latest.json"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# <time ...>2026/06/05 22:24</time><p ...>本文...</p> のペアを拾う
ENTRY_RE = re.compile(
    r'<time class="flex-shrink-0[^"]*">([^<]+)</time>'
    r'<p class="whitespace-pre-wrap[^"]*">(.*?)</p>',
    re.S,
)


def fetch() -> str:
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse(html: str) -> list[dict]:
    out, seen = [], set()
    for date, text in ENTRY_RE.findall(html):
        text = htmlmod.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        date = date.strip()
        key = (date, text)
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": date, "text": text})
    return out


def notify(new_entries: list[dict]) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        print("DISCORD_WEBHOOK 未設定 — 通知をスキップ", file=sys.stderr)
        return
    fields = []
    for e in new_entries[:25]:  # Discord embed は最大 25 fields
        val = e["text"] or "（本文なし）"
        if len(val) > 1024:
            val = val[:1021] + "..."
        fields.append({"name": e["date"], "value": val})
    payload = {
        # username / avatar_url はあえて指定しない。
        # → Discord の webhook 設定（名前・アイコン）がそのまま使われる。
        # コード側で固定したい場合は下記を有効化:
        #   "username": "好きな名前",
        #   "avatar_url": "https://example.com/icon.png",
        "embeds": [
            {
                "title": "妖精乱舞 攻略ページが更新されました",
                "url": URL,
                "color": 15158332,
                "fields": fields,
            }
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"Discord 通知送信: HTTP {r.status}")


def main() -> None:
    entries = parse(fetch())
    if not entries:
        print(
            "ERROR: 0 件しかパースできませんでした。サイト構造が変わった可能性。"
            "state を更新せず終了します。",
            file=sys.stderr,
        )
        sys.exit(1)

    old = json.loads(STATE.read_text(encoding="utf-8"))["entries"] if STATE.exists() else []
    old_keys = {(e["date"], e["text"]) for e in old}
    new_entries = [e for e in entries if (e["date"], e["text"]) not in old_keys]

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps({"entries": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if not old:
        print(f"ベースライン {len(entries)} 件を記録。初回は通知しません。")
        return
    if new_entries:
        print(f"新規/変更 {len(new_entries)} 件を検出 → 通知")
        for e in new_entries:
            print(f"  + {e['date']} | {e['text'][:50]}")
        notify(new_entries)
    else:
        print("変化なし。")


if __name__ == "__main__":
    main()
