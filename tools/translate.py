#!/usr/bin/env python3
"""오케스트레이터(Orchestrator) — 원문 변경분을 받아 docs/index.html 을 **페이즈 단위로** 최신화한다.

CI(GitHub Actions)에서 호출된다. state/guide/ 스냅샷이 바뀌었을 때만 실행된다.
흐름:  감지(phase_detect) → 바뀐 페이즈만 순차 번역·교체(phase_apply) → 전체 검증 → 쓰기.

전체 페이지를 매번 재생성하던 옛 방식과 달리, 바뀐 섹션만 다시 만들어 끼워넣는다.
→ 회당 출력 토큰이 페이지 전체가 아니라 섹션 1개라 비용↓·64K 잘림 위험 제거,
  섹션 순서는 order.json 위치에 splice 되므로 코드가 강제(오배치 불가).

구성:
    tools/phase_detect.py  감지   — 어느 페이즈를 update/add/remove 할지
    tools/phase_apply.py   적용   — 페이즈 1개 번역 + section/nav splice
    tools/htmlsplice.py    공용   — 섹션/목차 줄단위 조작(순수)
    tools/llm.py           공용   — Anthropic 스트리밍 클라이언트·비용·경보

환경변수:
    ANTHROPIC_API_KEY  (필수)
    MODEL              (선택, 기본 claude-sonnet-4-6)
    DISCORD_WEBHOOK    (선택, 경보용)
    TRANSLATE_MAX_FAILS        (선택, 기본 3)  같은 본문 N회 연속 실패 시 서킷 오픈
    TRANSLATE_COOLDOWN_HOURS   (선택, 기본 6)  서킷 오픈 후 프로브 간격(시간)
    TRANSLATE_COST_ALERT_USD   (선택, 기본 3.0) 1회 실행 추정비용 초과 시 경보

인자:
    sys.argv[1]        state/guide diff 가 담긴 파일 경로

종료 코드:
    0  성공(docs/index.html 갱신, 가드 리셋) 또는 변경 없음
    1  실패(번역/검증 실패 → state/guide 롤백 후 재시도 대상). index.html 미변경.
    2  서킷 오픈 — 같은 본문이 연속 실패해 API 호출 자체를 차단(토큰 0)
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import htmlsplice
import llm
import phase_apply
import phase_detect

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "docs" / "index.html"
GLOSSARY = ROOT / "TRANSLATION.md"
GUIDE_DIR = ROOT / "state" / "guide"

GUARD_PATH = ROOT / "state" / "translate_guard.json"
TOKENLOG_PATH = ROOT / "state" / "token_log.jsonl"
TOKENLOG_KEEP = 500

MAX_FAILS = int(os.environ.get("TRANSLATE_MAX_FAILS", "3"))
COOLDOWN_HOURS = float(os.environ.get("TRANSLATE_COOLDOWN_HOURS", "6"))
COST_ALERT_USD = float(os.environ.get("TRANSLATE_COST_ALERT_USD", "3.0"))

STYLE_REF_MAX = 8000  # 신규 페이즈용 형식 참고 섹션 길이 상한(토큰 절약)


# ─────────────────────────── 서킷브레이커 가드 ───────────────────────────
def source_hash() -> str:
    """현재 state/guide 본문(번역 입력)의 안정적 해시. 본문이 바뀌면 해시도 바뀐다."""
    h = hashlib.sha256()
    for p in sorted(GUIDE_DIR.glob("phase*.html")):
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def load_guard() -> dict:
    try:
        return json.loads(GUARD_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"hash": None, "fail_count": 0, "tripped": False, "tripped_until": None}


def save_guard(g: dict) -> None:
    GUARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUARD_PATH.write_text(json.dumps(g, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record_failure(g: dict, src: str, now: float, reason: str) -> None:
    """실패를 가드에 기록. 같은 본문 연속 MAX_FAILS 회 → 서킷 오픈(본문 바뀌면 카운터 리셋)."""
    if g.get("hash") != src:
        g.update({"hash": src, "fail_count": 1, "tripped": False, "tripped_until": None})
    else:
        g["fail_count"] = int(g.get("fail_count", 0)) + 1
    if g["fail_count"] >= MAX_FAILS:
        newly_tripped = not g.get("tripped")
        g["tripped"] = True
        g["tripped_until"] = now + COOLDOWN_HOURS * 3600
        if newly_tripped:
            llm.alert(
                f"⚠️ yan-flash 번역 {g['fail_count']}회 연속 실패 → 서킷 오픈. "
                f"이후 같은 본문은 {COOLDOWN_HOURS:.0f}h 간격 프로브만(토큰 절약).\n사유: {reason}"
            )
    save_guard(g)
    print(f"[guard] 실패 기록: count={g['fail_count']} tripped={g['tripped']} ({reason})",
          file=sys.stderr)


# ─────────────────────────── 토큰/비용 로깅 ───────────────────────────
def add_usage(agg: dict, u: dict) -> None:
    for k in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens"):
        agg[k] = agg.get(k, 0) + (u.get(k, 0) or 0)


def log_tokens(usage: dict, status: str, cost: float, phases: int) -> None:
    """1회 실행(여러 페이즈 호출 합산)의 토큰/비용을 stdout + token_log.jsonl 에 남긴다."""
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": llm.MODEL,
        "status": status,            # ok | fail
        "phases": phases,
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "cache_write": usage.get("cache_creation_input_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "est_usd": round(cost, 4),
    }
    print(f"[tokens] phases={phases} in={rec['input']} out={rec['output']} "
          f"→ ~${rec['est_usd']:.4f} (model={llm.MODEL}, {status})")
    try:
        TOKENLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if TOKENLOG_PATH.exists():
            lines = TOKENLOG_PATH.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(rec, ensure_ascii=False))
        TOKENLOG_PATH.write_text("\n".join(lines[-TOKENLOG_KEEP:]) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[tokens] 기록 실패: {e}", file=sys.stderr)


# ─────────────────────────── 보조 ───────────────────────────
def load_order() -> list:
    try:
        return [str(x) for x in json.loads((GUIDE_DIR / "order.json").read_text(encoding="utf-8"))]
    except Exception:
        # order.json 없으면 phaseN.html 을 번호순으로
        return sorted((p.name[5:-5] for p in GUIDE_DIR.glob("phase*.html")), key=int)


def existing_phases(order: list) -> list:
    """현재 원문에 실제 존재하는(=섹션이 있어야 하는) 페이즈 번호."""
    return [n for n in order if (GUIDE_DIR / f"phase{n}.html").exists()]


def extract_section(lines, n):
    b = htmlsplice.find_section(lines, n)
    return "\n".join(lines[b[0]:b[1] + 1]) if b else None


def extract_nav(lines, n):
    b = htmlsplice.find_section(lines, n)
    if not b:
        return None
    label = htmlsplice.section_label(lines, b)
    nb = htmlsplice.find_nav_group(lines, label) if label else None
    return "\n".join(lines[nb[0]:nb[1] + 1]) if nb else None


def style_reference(lines, order):
    """신규 페이즈 작성 시 형식 참고용으로 줄 가장 가까운 기존 섹션(앞쪽 우선)."""
    for n in order:
        b = htmlsplice.find_section(lines, n)
        if b:
            return "\n".join(lines[b[0]:b[1] + 1])[:STYLE_REF_MAX]
    return ""


# ─────────────────────────── 오케스트레이터 ───────────────────────────
def main() -> None:
    diff_path = sys.argv[1] if len(sys.argv) > 1 else None
    diff = Path(diff_path).read_text(encoding="utf-8") if diff_path else ""
    if not diff.strip():
        print("diff 없음 — 번역 스킵")
        return

    src = source_hash()
    g = load_guard()
    now = time.time()

    # ── 서킷브레이커: 같은 본문이 연속 실패해 트립된 상태면 API 호출 자체를 차단(토큰 0) ──
    if g.get("tripped") and g.get("hash") == src:
        until = g.get("tripped_until") or 0
        if now < until:
            mins = int((until - now) / 60)
            print(f"[guard] 서킷 오픈: 같은 본문이 {g.get('fail_count')}회 연속 실패. "
                  f"API 호출 차단(쿨다운 {mins}분 남음, 토큰 0).", file=sys.stderr)
            sys.exit(2)
        print("[guard] 쿨다운 경과 — 1회만 프로브 시도.", file=sys.stderr)

    current = INDEX.read_text(encoding="utf-8")
    glossary = GLOSSARY.read_text(encoding="utf-8")
    order = load_order()

    # ── 1) 감지: 어느 페이즈를 어떻게 ──
    items = phase_detect.plan(diff, current, order)
    if not items:
        print("페이즈 단위 변경 없음 — 스킵(메타/순서 변경뿐).")
        return
    print("감지된 작업: " + ", ".join(f"phase-{it['phase']}({it['action']})" for it in items))

    # ── 2) 적용: 순차로 페이즈별 번역 + splice ──
    lines = current.split("\n")
    agg = {}
    for it in items:
        n, action = it["phase"], it["action"]
        section = nav = None
        if action != "remove":
            src_html = (GUIDE_DIR / f"phase{n}.html").read_text(encoding="utf-8")
            cur_section = extract_section(lines, n)
            cur_nav = extract_nav(lines, n)
            ref = "" if action == "update" else style_reference(lines, order)
            images = llm.changed_images(phase_detect.subdiff(diff, n))
            try:
                section, nav, usage, _ = phase_apply.translate_phase(
                    n, action, src_html, cur_section, cur_nav, glossary, images, ref)
            except Exception as e:  # noqa: BLE001
                record_failure(g, src, now, f"phase-{n} 번역 실패: {e}")
                print(f"ERROR: phase-{n} 번역 실패 — index.html 미변경. ({e})", file=sys.stderr)
                sys.exit(1)
            add_usage(agg, usage)
            print(f"  [phase-{n} {action}] in={usage.get('input_tokens',0)} "
                  f"out={usage.get('output_tokens',0)} img={len(images)}")
        try:
            lines = phase_apply.apply_to_lines(lines, it, section, nav, order)
        except Exception as e:  # noqa: BLE001
            record_failure(g, src, now, f"phase-{n} splice 실패: {e}")
            print(f"ERROR: phase-{n} splice 실패 — index.html 미변경. ({e})", file=sys.stderr)
            sys.exit(1)

    out = "\n".join(lines)

    # ── 3) 전체 검증(잘림·섹션 누락·목차 불일치 차단) ──
    reason = htmlsplice.validate_page(out, existing_phases(order))
    if reason:
        cost = llm.est_cost(agg)
        log_tokens(agg, "fail", cost, len(items))
        llm.alert(f"⚠️ yan-flash splice 결과 검증 실패: {reason} → index.html 미적용.")
        record_failure(g, src, now, f"splice 검증 실패: {reason}")
        print(f"ERROR: splice 검증 실패({reason}). index.html 변경 안 함.", file=sys.stderr)
        sys.exit(1)

    # ── 4) 성공: 토큰 로깅 + (임계 초과 시 경보) + 쓰기 + 가드 리셋 ──
    cost = llm.est_cost(agg)
    log_tokens(agg, "ok", cost, len(items))
    if cost >= COST_ALERT_USD:
        llm.alert(f"💸 yan-flash 번역 1회 실행 추정비용 ~${cost:.2f} "
                  f"(임계 ${COST_ALERT_USD:.2f} 초과, {len(items)}개 페이즈).")

    INDEX.write_text(out + ("\n" if not out.endswith("\n") else ""), encoding="utf-8")
    save_guard({"hash": src, "fail_count": 0, "tripped": False, "tripped_until": None})
    print(f"docs/index.html 갱신 완료 ({len(out)} bytes, {len(items)}개 페이즈, "
          f"model={llm.MODEL}, ~${cost:.4f})")


if __name__ == "__main__":
    main()
