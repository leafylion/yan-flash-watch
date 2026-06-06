#!/usr/bin/env python3
"""원문 본문 변경분을 받아 docs/index.html(한글 번역본)을 최신화한다.

CI(GitHub Actions)에서 호출된다. state/guide/ 스냅샷이 바뀌었을 때만 실행되며,
변경 diff + 현재 번역본 + 용어집(TRANSLATION.md)을 Anthropic API에 넘겨
**바뀐 부분만** 반영한 완성된 HTML을 돌려받아 덮어쓴다.

환경변수:
    ANTHROPIC_API_KEY  (필수)
    MODEL              (선택, 기본 claude-opus-4-8)

인자:
    sys.argv[1]        state/guide diff 가 담긴 파일 경로
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "docs" / "index.html"
GLOSSARY = ROOT / "TRANSLATION.md"
GUIDE_DIR = ROOT / "state" / "guide"

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("MODEL", "claude-opus-4-8")

SYSTEM = """\
너는 FFXIV 절 「妖精乱舞(요성난무)」 공략의 한국어 번역본 docs/index.html을 유지보수한다.
원문(일본어)이 바뀌면, 바뀐 부분만 번역본에 반영한다.

반드시 지킬 것:
1. 변경 diff가 요구하는 부분만 수정한다. 그 외 기존 HTML(구조·스타일·스크립트·문구)은 그대로 둔다.
2. 첨부된 용어집(TRANSLATION.md)의 로컬라이징 용어를 따른다.
   (頭割り→쉐어, 탱 대상 強攻撃→탱버스터, 半面→반갈, 真偽→진짜/가짜, フェーズ移行→페이즈 전환 등)
3. 원문에서 "삭제 예정(削除予定)"으로 명시된 처리법(旧散会, ②ミッシング ぴ 등)은 번역본에서 제외한다.
   신규 "최신" 처리법은 포함한다.
4. 이미지 src는 https://yan-flash.com + /api/uploads/... 형식을 유지한다.
5. 섹션이 추가/삭제되면 좌측 목차(nav.toc)의 링크와 id도 같이 맞춘다.
6. 라이트박스/목차 활성화 <script>, CSS는 절대 건드리지 않는다.

출력: 완성된 index.html 전체를 그대로 출력한다. 코드펜스(```)나 설명 문장 없이 <!DOCTYPE html>로 시작하는 HTML만 출력한다.\
"""


def call_api(prompt: str) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    payload = {
        "model": MODEL,
        "max_tokens": 32000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode("utf-8"))
    return "".join(block.get("text", "") for block in data.get("content", []))


def main() -> None:
    diff_path = sys.argv[1] if len(sys.argv) > 1 else None
    diff = Path(diff_path).read_text(encoding="utf-8") if diff_path else ""
    if not diff.strip():
        print("diff 없음 — 번역 스킵")
        return

    current = INDEX.read_text(encoding="utf-8")
    glossary = GLOSSARY.read_text(encoding="utf-8")
    guide = "\n\n".join(
        f"===== {p.name} =====\n{p.read_text(encoding='utf-8')}"
        for p in sorted(GUIDE_DIR.glob("phase*.html"))
    )

    prompt = f"""\
# 용어집 (TRANSLATION.md)
{glossary}

# 원문 본문 변경분 (state/guide/ 의 git diff)
```diff
{diff}
```

# 변경 반영된 현재 원문 본문 전체 (참고용)
{guide}

# 현재 번역본 docs/index.html (이것을 수정 대상으로 삼아라)
{current}

위 변경분을 번역본에 반영한 완성된 index.html 전체를 출력하라."""

    out = call_api(prompt)
    out = re.sub(r'^\s*```(?:html)?\s*', '', out)
    out = re.sub(r'\s*```\s*$', '', out).strip()

    if not out.lower().startswith("<!doctype html") or len(out) < 5000:
        print("ERROR: 응답이 올바른 HTML이 아님. index.html 을 변경하지 않음.", file=sys.stderr)
        print(out[:500], file=sys.stderr)
        sys.exit(1)

    INDEX.write_text(out + ("\n" if not out.endswith("\n") else ""), encoding="utf-8")
    print(f"docs/index.html 갱신 완료 ({len(out)} bytes, model={MODEL})")


if __name__ == "__main__":
    main()
