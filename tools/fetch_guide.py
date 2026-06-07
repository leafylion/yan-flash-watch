#!/usr/bin/env python3
"""妖精乱舞 攻略ページの本文を取得し、フェーズ別の素の HTML を state/guide/ に書き出す。

watch.py(check.py)が「更新履歴」リストを監視するのに対し、こちらは攻略の
**本文そのもの**をスナップショットする。これを git 管理しておくと、更新が来た
ときに `git diff state/guide/` でどの部分が変わったかが一目で分かり、翻訳
(docs/index.html)を差分だけ直して最新を保てる。

使い方:
    python3 tools/fetch_guide.py          # state/guide/phaseN.html を更新
    git diff state/guide/                 # 前回からの変更を確認
"""
import codecs
import re
import sys
import urllib.request
from pathlib import Path

URL = "https://yan-flash.com/ultimate/yosei-ranbu"
OUT = Path(__file__).resolve().parent.parent / "state" / "guide"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch() -> str:
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def decode_next_chunks(raw: str) -> str:
    """Next.js が self.__next_f.push([1,"..."]) で流す本文を結合してデコードする。"""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', raw, re.S)
    out = []
    for c in chunks:
        try:
            out.append(codecs.decode(c, "unicode_escape").encode("latin1").decode("utf-8"))
        except Exception:
            try:
                out.append(codecs.decode(c, "unicode_escape"))
            except Exception:
                out.append(c)
    return "".join(out)


def extract_phases(full: str) -> dict[str, str]:
    """phases 配列の各エントリ {"phase":N, ... ,"contentHtml":"$XX"} から chunk 本文を取り出す。

    サイト側でキーが増減しても壊れにくいよう、phase 番号と contentHtml の間の平文キー
    （badge, label, tocLabel 等）は任意に許容する。contentHtml が null のフェーズ
    （未公開）は "$xx" 形式に一致しないため自動的に除外される。
    """
    mapping = re.findall(
        r'"phase":(\d+),[^{}]*?"contentHtml":"\$([0-9a-f]+)"', full
    )
    result = {}
    for phase, chunk_id in mapping:
        m = re.search(rf'\b{chunk_id}:T[0-9a-f]+,', full)
        if not m:
            continue
        start = m.end()
        nxt = re.search(r'\b[0-9a-f]{2,3}:(T[0-9a-f]+,|\[)', full[start:])
        body = full[start:start + nxt.start()] if nxt else full[start:]
        result[phase] = body.strip()
    return result


def pretty(body: str) -> str:
    """diff を読みやすくするため、ブロック要素ごとに改行を入れるだけの軽整形。"""
    body = re.sub(r'(</(?:h2|h3|p|ol|ul|li|div)>)', r'\1\n', body)
    body = re.sub(r'(<(?:h2|h3|p|ol|ul|img|div)\b)', r'\n\1', body)
    return re.sub(r'\n{3,}', '\n\n', body).strip() + "\n"


def main() -> None:
    full = decode_next_chunks(fetch())
    phases = extract_phases(full)
    if not phases:
        print("ERROR: フェーズ本文を抽出できませんでした。サイト構造が変わった可能性。",
              file=sys.stderr)
        sys.exit(1)
    OUT.mkdir(parents=True, exist_ok=True)
    for phase, body in sorted(phases.items(), key=lambda kv: int(kv[0])):
        (OUT / f"phase{phase}.html").write_text(pretty(body), encoding="utf-8")
        imgs = len(re.findall(r'/api/uploads/', body))
        print(f"phase{phase}: {len(body)} bytes, {imgs} images")


if __name__ == "__main__":
    main()
