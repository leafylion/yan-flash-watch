#!/usr/bin/env python3
"""적용(Application) — 페이즈 **하나**를 번역해 <section> + 목차 그룹을 만들고,
docs/index.html(줄 리스트)에 splice/insert/remove 한다.

전체 페이지를 매번 다시 생성하지 않고 바뀐 페이즈 섹션만 교체한다(비용·잘림 방지).
"""
import re

import htmlsplice
import llm

SECTION_SYSTEM = """\
너는 FFXIV 절 「妖精乱舞(요성난무)」 공략 한국어 번역본에서 **딱 한 페이즈 섹션**을 만든다.

주어지는 것: (1) 그 페이즈의 일본어 원문, (2) 현재 번역본의 해당 <section>과 목차 그룹(있으면 — 형식·캡션 관례 참고용), (3) 용어집, (4) 이 페이즈에서 추가/교체된 이미지.

대원칙(다른 모든 규칙에 우선):
이건 원문 페이지의 **번역본**이다. 그 페이즈 안의 공략 순서·이미지와 그 순서·처리법을 원문 그대로 따른다.
표현은 자연스러운 한국어로 의역하거나 이해를 돕는 보충 설명을 더해도 되지만, 순서·이미지·내용 자체를 임의로 바꾸거나 빼지 않는다.
원문에서 "삭제 예정(削除予定)/旧…"으로 명시된 처리법은 제외하고, 신규 "최신" 처리법은 포함한다.

지킬 것:
1. 용어집(TRANSLATION.md)의 로컬라이징 용어를 따른다(頭割り→쉐어, 탱 대상 強攻撃→탱버스터, 半面→반갈, 真偽→진짜/가짜, フェーズ移行→페이즈 전환 등).
2. 이미지 src 는 https://yan-flash.com/api/uploads/...webp 형식을 유지한다.
3. 각 이미지(또는 이미지 그룹) 바로 아래에 `<div class="cap">…</div>` 캡션을 둔다.
   - 이미지 안에 일본어 문장/라벨이 있으면 한국어로 번역해 캡션에 적는다.
   - 아이콘·숫자·방위뿐이면 표기 안내만 간단히(예: 직업 아이콘=담당자, 숫자=순번, A·B·C·1~4=방위).
   - 첨부된 이미지를 직접 보고 작성/갱신한다.
   - 형식: `<div class="cap"><span class="h">🖼 이미지 안 표기</span>…</div>` (긴 설명은 <ul><li>).
4. HTML 구조·클래스(.shot/.cap/.tablewrap/.tl/.imgrow 등)·들여쓰기 관례를 현재 섹션과 동일하게 유지한다.
   표 셀에 들어가는 공략 도해 이미지는 `class="shot"` 를 붙인다(작은 인라인 아이콘만 클래스 없이).
5. 목차 그룹의 <a href="#앵커"> 앵커들은 섹션 안의 id 와 정확히 일치해야 한다. 라벨(예: "PHASE 3-2")은 현재 섹션의 phase-title 라벨을 유지한다(신규면 적절히 부여).

출력 형식 — 정확히 이대로, 마커 밖에는 아무 텍스트도 쓰지 않는다:
@@SECTION@@
<section class="phase" id="phase-{N}">
  <h2 class="phase-title">{라벨}</h2>
  …그 페이즈 전체…
</section>
@@NAV@@
  <div class="grp">{라벨}</div>
  <a href="#…">…</a>
  …(이 페이즈의 모든 mech/sub 앵커 링크만)…
@@END@@

- <section> 은 반드시 id="phase-{N}" 로 시작하고 </section> 로 끝낸다.
- @@NAV@@ 블록에는 이 페이즈의 목차 그룹(grp 1개 + 그 안의 <a> 링크들)만. 다른 페이즈는 절대 포함하지 않는다.
- 코드펜스(```)나 설명 문장 없이 위 마커 형식만 출력한다.\
"""

_MARK_SEC, _MARK_NAV, _MARK_END = "@@SECTION@@", "@@NAV@@", "@@END@@"


def _strip_fences(s: str) -> str:
    s = re.sub(r"^\s*```(?:html)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def parse_output(out: str, n: str):
    """모델 출력에서 (section_html, nav_html) 추출 + 구조 검증."""
    if _MARK_SEC not in out or _MARK_NAV not in out:
        raise ValueError("출력에 @@SECTION@@/@@NAV@@ 마커가 없음")
    after = out.split(_MARK_SEC, 1)[1]
    sec_part, rest = after.split(_MARK_NAV, 1)
    nav_part = rest.split(_MARK_END, 1)[0]
    section = _strip_fences(sec_part)
    nav = _strip_fences(nav_part)
    if not section.lower().startswith("<section"):
        raise ValueError("섹션이 <section 으로 시작하지 않음")
    if ('id="phase-%s"' % n) not in section:
        raise ValueError('섹션에 id="phase-%s" 가 없음' % n)
    if not section.rstrip().endswith("</section>"):
        raise ValueError("섹션이 </section> 로 끝나지 않음(잘림 추정)")
    if '<div class="grp">' not in nav:
        raise ValueError("목차 그룹에 <div class=\"grp\"> 가 없음")
    return section, nav


def translate_phase(n, action, src_html, cur_section, cur_nav, glossary, images, style_ref):
    """페이즈 1개를 번역. 반환: (section_html, nav_html, usage, stop_reason).

    실패(마커 없음/잘림 등)는 예외로 던진다(오케스트레이터가 가드에 기록)."""
    text = (
        f"# 대상: phase-{n}  (작업: {action})\n\n"
        f"# 용어집 (TRANSLATION.md)\n{glossary}\n\n"
        f"# 이 페이즈의 일본어 원문 (state/guide/phase{n}.html)\n{src_html}\n\n"
    )
    if action == "update":
        text += (
            f"# 현재 번역본의 해당 섹션 (수정 대상 — 형식·캡션·들여쓰기 유지)\n{cur_section}\n\n"
            f"# 현재 목차 그룹 (형식·라벨·들여쓰기 유지)\n{cur_nav}\n\n"
        )
    else:  # add
        text += (
            f"# 형식 참고용 기존 섹션 (구조·클래스·캡션 스타일만 참고. 내용은 무시)\n{style_ref}\n\n"
            "이건 신규 페이즈다. 위 원문으로 새 섹션과 목차 그룹을 처음부터 작성한다.\n\n"
        )
    text += f"위 원문을 반영해 phase-{n} 섹션과 그 목차 그룹을 지정된 출력 형식으로 출력하라."

    content = [{"type": "text", "text": text}]
    for url in images:
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    if images:
        content.append({
            "type": "text",
            "text": f"위 {len(images)}장은 이 페이즈에서 추가/교체된 이미지다. "
                    "각 이미지를 보고 해당 <img> 아래 <div class=\"cap\"> 캡션을 작성/갱신하라.",
        })

    out, usage, stop = llm.call_api(content, SECTION_SYSTEM)
    if stop == "max_tokens":
        raise ValueError("출력이 max_tokens 로 잘림")
    section, nav = parse_output(out, n)
    return section, nav, usage, stop


# ─────────────────────────── splice/insert/remove ───────────────────────────
def _order_after_existing(lines, n, order):
    """order 상 n 다음에서 '섹션이 실제로 존재하는' 첫 페이즈 번호. 없으면 None."""
    if n not in order:
        return None
    for m in order[order.index(n) + 1:]:
        if htmlsplice.find_section(lines, m) is not None:
            return m
    return None


def apply_update(lines, n, section, nav):
    sb = htmlsplice.find_section(lines, n)
    label = htmlsplice.section_label(lines, sb)
    nb = htmlsplice.find_nav_group(lines, label) if label else None
    # 섹션(아래) 먼저 교체 → 위쪽 nav 인덱스가 안 밀린다. 그 다음 nav 교체.
    lines = htmlsplice.replace_lines(lines, sb, section)
    if nb:
        lines = htmlsplice.replace_lines(lines, nb, nav)
    return lines


def _section_with_comment(section, n):
    """신규 섹션 위에 기존 페이지 스타일의 구분 주석을 붙인다(고아/누락 방지)."""
    m = re.search(r'phase-title">(.*?)</h2>', section, re.S)
    label = m.group(1).strip() if m else f"PHASE {n}"
    bar = "=" * 21
    return f"<!-- {bar} {label} {bar} -->\n{section}"


def apply_add(lines, n, section, nav, order):
    nxt = _order_after_existing(lines, n, order)
    if nxt is not None:
        sb = htmlsplice.section_block(lines, nxt)   # 다음 페이즈의 '주석 포함' 블록 앞에 삽입
        sec_anchor = sb[0]
        nlabel = htmlsplice.section_label(lines, htmlsplice.find_section(lines, nxt))
        nb = htmlsplice.find_nav_group(lines, nlabel)
        nav_anchor = nb[0] if nb else htmlsplice.nav_end_index(lines)
    else:  # order 상 마지막 → 본문은 footer 앞, 목차는 </nav> 앞
        sec_anchor = htmlsplice.footer_index(lines)
        nav_anchor = htmlsplice.nav_end_index(lines)
    block = _section_with_comment(section, n)
    # 섹션(아래) 먼저 삽입 → 위쪽 nav_anchor 인덱스가 안 밀린다. 그 다음 nav 삽입.
    lines = htmlsplice.insert_before(lines, sec_anchor, block, blank_after=True)
    # nav: 다른 grp 앞에 넣을 땐 그룹 구분 빈 줄을 같이, </nav> 앞(맨끝)일 땐 빈 줄 없이.
    lines = htmlsplice.insert_before(lines, nav_anchor, nav, blank_after=(nxt is not None))
    return lines


def apply_remove(lines, n):
    blk = htmlsplice.section_block(lines, n)   # 구분 주석까지 포함해 삭제
    if blk is None:
        return lines
    label = htmlsplice.section_label(lines, htmlsplice.find_section(lines, n))
    nb = htmlsplice.find_nav_group(lines, label) if label else None
    lines = htmlsplice.remove_lines(lines, blk)  # 섹션 블록(아래) 먼저
    if nb:
        lines = htmlsplice.remove_lines(lines, nb)  # nav(위) — 인덱스 유효
    return lines


def apply_to_lines(lines, item, section, nav, order):
    """작업 1건을 줄 리스트에 적용. remove 는 section/nav 가 None 이어도 된다."""
    n, action = item["phase"], item["action"]
    if action == "update":
        return apply_update(lines, n, section, nav)
    if action == "add":
        return apply_add(lines, n, section, nav, order)
    if action == "remove":
        return apply_remove(lines, n)
    raise ValueError(f"알 수 없는 action: {action}")
