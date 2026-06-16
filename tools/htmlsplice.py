#!/usr/bin/env python3
"""docs/index.html 의 페이즈 <section> 과 좌측 목차(nav.toc) 그룹을 **줄 단위**로
찾고 교체/삽입/삭제한다.

외부 의존이 없는 순수 함수 모음이라 API 키 없이 로컬에서 단위 테스트가 가능하다.
페이지 구조 가정(현재 index.html 기준):
  - 페이즈 본문:  <section class="phase" id="phase-{N}"> … </section>   (중첩 없음)
  - 페이즈 제목:  <h2 class="phase-title">{라벨}</h2>                    (섹션 내부)
  - 목차 그룹:    <div class="grp">{라벨}</div> 다음 <a> 링크들           (다음 grp/`</nav>` 전까지)
  - 섹션 라벨 == 목차 그룹 라벨 로 1:1 매칭(예: phase-7 ↔ "PHASE 3-2")
"""
import re

_TITLE_RE = re.compile(r'<h2 class="phase-title">(.*?)</h2>', re.S)


def _section_pat(n):
    return re.compile(r'<section\b[^>]*\bid="phase-%s"' % re.escape(str(n)))


def find_section(lines, n):
    """phase-n 섹션의 줄 범위 [i, j] (양끝 포함). 없으면 None."""
    pat = _section_pat(n)
    start = next((i for i, l in enumerate(lines) if pat.search(l)), None)
    if start is None:
        return None
    end = next((j for j in range(start, len(lines)) if "</section>" in lines[j]), None)
    if end is None:
        return None
    return (start, end)


def block_start(lines, section_start):
    """섹션 시작 줄 바로 위에 <!-- … --> 구분 주석이 있으면 그 줄까지 포함한 시작 인덱스.

    페이지는 각 섹션 위에 `<!-- ===== PHASE N ===== -->` 주석을 둔다. 삽입/삭제 시
    주석이 섹션과 함께 움직여야 고아 주석이 안 생긴다(제자리 교체 update 는 무관)."""
    i = section_start
    if i - 1 >= 0 and lines[i - 1].lstrip().startswith("<!--") and "-->" in lines[i - 1]:
        i -= 1
    return i


def section_block(lines, n):
    """phase-n 의 (구분 주석 포함) 블록 줄 범위 [i, j]. 없으면 None."""
    sb = find_section(lines, n)
    if sb is None:
        return None
    return (block_start(lines, sb[0]), sb[1])


def section_label(lines, bounds):
    """섹션의 <h2 class="phase-title"> 라벨 텍스트. 없으면 None."""
    text = "\n".join(lines[bounds[0]:bounds[1] + 1])
    m = _TITLE_RE.search(text)
    return m.group(1).strip() if m else None


def find_nav_group(lines, label):
    """라벨이 일치하는 <div class="grp"> 목차 그룹의 줄 범위 [i, j] (양끝 포함).

    j 는 다음 grp 또는 </nav> 직전 줄. 없으면 None."""
    target = '<div class="grp">%s</div>' % label
    start = next((i for i, l in enumerate(lines) if target in l), None)
    if start is None:
        return None
    j = start
    for k in range(start + 1, len(lines)):
        if '<div class="grp">' in lines[k] or "</nav>" in lines[k]:
            break
        j = k
    while j > start and lines[j].strip() == "":  # 그룹 뒤 빈 줄(구분선)은 그룹에서 제외
        j -= 1
    return (start, j)


def replace_lines(lines, bounds, new_text):
    """bounds[0..1] 줄을 new_text(여러 줄)로 교체한 새 리스트."""
    new = new_text.rstrip("\n").split("\n")
    return lines[:bounds[0]] + new + lines[bounds[1] + 1:]


def remove_lines(lines, bounds):
    """bounds[0..1] 줄(+바로 뒤 공백 1줄까지)을 삭제한 새 리스트."""
    end = bounds[1]
    if end + 1 < len(lines) and lines[end + 1].strip() == "":
        end += 1  # 섹션 사이 빈 줄도 같이 정리
    return lines[:bounds[0]] + lines[end + 1:]


def insert_before(lines, idx, new_text, blank_after=False):
    """idx 줄 앞에 new_text(여러 줄)를 삽입한 새 리스트. blank_after=True면 뒤에 빈 줄 1개."""
    new = new_text.rstrip("\n").split("\n")
    if blank_after:
        new = new + [""]
    return lines[:idx] + new + lines[idx:]


def footer_index(lines):
    return next((i for i, l in enumerate(lines) if "<footer" in l), len(lines))


def nav_end_index(lines):
    return next((i for i, l in enumerate(lines) if "</nav>" in l), len(lines))


def section_count(lines):
    return sum(1 for l in lines if '<section class="phase"' in l)


def navgrp_count(lines):
    return sum(1 for l in lines if '<div class="grp">' in l)


def validate_page(text, required_phases):
    """splice 후 전체 페이지 무결성 검사. 문제 있으면 사유 문자열, 정상이면 None.

    required_phases: 현재 원문에 존재하는(=섹션이 있어야 하는) 페이즈 번호 리스트."""
    low = text.lower()
    if not low.startswith("<!doctype html"):
        return "<!doctype html> 로 시작하지 않음"
    if not low.rstrip().endswith("</html>"):
        return "</html> 로 끝나지 않음(잘림 추정)"
    if len(text) < 5000:
        return f"본문이 너무 짧음({len(text)} bytes)"
    lines = text.split("\n")
    for n in required_phases:
        if find_section(lines, n) is None:
            return f"phase-{n} 섹션 누락"
    sc, nc = section_count(lines), navgrp_count(lines)
    if sc != nc:
        return f"섹션 수({sc}) != 목차 그룹 수({nc})"
    return None
