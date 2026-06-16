#!/usr/bin/env python3
"""감지(Detection) — state/guide 의 git diff 로부터 '어느 페이즈를 어떻게 갱신할지' 결정한다.

오케스트레이터(translate.py)가 이걸 호출해 작업 목록을 받고, 항목마다 phase_apply 로 적용한다.
순수 함수(LLM 호출 없음)라 로컬 테스트 가능.
"""
import re

import htmlsplice


def parse_diff_phases(diff: str) -> dict:
    """diff 를 파일 블록으로 쪼개, 바뀐 phaseN.html 의 상태를 매핑한다.

    반환: {"3": "modified", "5": "added", "9": "deleted", ...}
    (order.json 등 phase 파일이 아닌 변경은 무시)"""
    res = {}
    for block in re.split(r"(?=^diff --git )", diff, flags=re.M):
        m = re.search(r"b/state/guide/phase(\d+)\.html", block)
        if not m:
            m = re.search(r"a/state/guide/phase(\d+)\.html", block)  # 삭제(+++ /dev/null) 대비
        if not m:
            continue
        n = m.group(1)
        if "deleted file mode" in block or re.search(r"^\+\+\+ /dev/null", block, re.M):
            res[n] = "deleted"
        elif "new file mode" in block or re.search(r"^--- /dev/null", block, re.M):
            res[n] = "added"
        else:
            res[n] = "modified"
    return res


def subdiff(diff: str, n: str) -> str:
    """phaseN.html 한 파일에 해당하는 diff 블록만 추출(이미지 수집용)."""
    for block in re.split(r"(?=^diff --git )", diff, flags=re.M):
        if re.search(r"[ab]/state/guide/phase%s\.html" % re.escape(str(n)), block):
            return block
    return ""


def plan(diff: str, html: str, order: list) -> list:
    """작업 목록을 만든다. 각 항목: {"phase": n, "action": "update"|"add"|"remove"}.

    - 원문 phaseN 이 바뀜 + 섹션 존재  → update
    - 원문 phaseN 이 바뀜/신설 + 섹션 없음 → add (원문 순서상 올바른 위치에 삽입)
    - 원문 phaseN 삭제 + 섹션 존재     → remove
    항목은 order(원문 표시 순서) 기준으로 정렬해 반환(삽입 위치 일관성 위해)."""
    changed = parse_diff_phases(diff)
    lines = html.split("\n")
    rank = {n: i for i, n in enumerate(order)}
    items = []
    for n in sorted(changed, key=lambda x: rank.get(x, 10_000)):
        st = changed[n]
        exists = htmlsplice.find_section(lines, n) is not None
        if st == "deleted":
            if exists:
                items.append({"phase": n, "action": "remove"})
            # 섹션도 없으면 할 일 없음
        elif exists:
            items.append({"phase": n, "action": "update"})
        else:
            items.append({"phase": n, "action": "add"})
    return items
