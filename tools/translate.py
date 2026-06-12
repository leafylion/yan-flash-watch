#!/usr/bin/env python3
"""원문 본문 변경분을 받아 docs/index.html(한글 번역본)을 최신화한다.

CI(GitHub Actions)에서 호출된다. state/guide/ 스냅샷이 바뀌었을 때만 실행되며,
변경 diff + 현재 번역본 + 용어집(TRANSLATION.md)을 Anthropic API에 넘겨
**바뀐 부분만** 반영한 완성된 HTML을 돌려받아 덮어쓴다.

환경변수:
    ANTHROPIC_API_KEY  (필수)
    MODEL              (선택, 기본 claude-sonnet-4-6)
    DISCORD_WEBHOOK    (선택, 경보용)
    TRANSLATE_MAX_FAILS        (선택, 기본 3)  같은 본문 N회 연속 실패 시 서킷 오픈
    TRANSLATE_COOLDOWN_HOURS   (선택, 기본 6)  서킷 오픈 후 프로브 간격(시간)
    TRANSLATE_COST_ALERT_USD   (선택, 기본 1.0) 1회 추정비용 초과 시 경보

인자:
    sys.argv[1]        state/guide diff 가 담긴 파일 경로

종료 코드:
    0  성공(docs/index.html 갱신, 가드 리셋)
    1  실패(타임아웃/오류/잘림/형식불량 → state/guide 롤백 후 재시도 대상)
    2  서킷 오픈 — 같은 본문이 연속 실패해 API 호출 자체를 차단(토큰 0)
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from urllib.error import HTTPError
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "docs" / "index.html"
GLOSSARY = ROOT / "TRANSLATION.md"
GUIDE_DIR = ROOT / "state" / "guide"

# ── 가드/로깅 상태 파일(저장소에 커밋되어 CI 회차 간 유지된다) ──────────────
GUARD_PATH = ROOT / "state" / "translate_guard.json"
TOKENLOG_PATH = ROOT / "state" / "token_log.jsonl"
TOKENLOG_KEEP = 500  # 최근 N줄만 유지

MAX_FAILS = int(os.environ.get("TRANSLATE_MAX_FAILS", "3"))
COOLDOWN_HOURS = float(os.environ.get("TRANSLATE_COOLDOWN_HOURS", "6"))
COST_ALERT_USD = float(os.environ.get("TRANSLATE_COST_ALERT_USD", "1.0"))
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 64000  # Sonnet 4.6 출력 상한. 스트리밍이라 타임아웃 위험 없음. 잘림 방지.
UA = "yan-flash-watch/1.0 (+https://github.com/leafylion/yan-flash-watch)"

# 1M 토큰당 단가 (input, output, cache_write_5m, cache_read) USD
PRICES = {
    "opus":   (5.0, 25.0, 6.25, 0.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku":  (1.0,  5.0, 1.25, 0.10),
}

SYSTEM = """\
너는 FFXIV 절 「妖精乱舞(요성난무)」 공략의 한국어 번역본 docs/index.html을 유지보수한다.
원문(일본어)이 바뀌면, 바뀐 부분만 번역본에 반영한다.

대원칙(다른 모든 규칙에 우선):
이 페이지는 원문 페이지의 **번역본**이다. 전체 섹션(페이즈)의 **순서**, **이미지와 그 순서**,
**공략 절차·구조**는 반드시 원문을 그대로 따른다. 임의로 순서를 바꾸거나, 섹션/이미지를 빼거나,
없는 처리법을 지어내지 않는다.
- 번역 품질을 위해 자연스러운 한국어로 의역하거나, 이해를 돕는 보충 설명을 더하는 것은 허용된다.
  단 그것이 원문의 순서·이미지·공략 내용 자체를 바꾸어선 안 된다.
- 섹션(페이즈) 배치 순서는 사용자 메시지의 "원문 섹션 표시 순서"와 **정확히 일치**해야 한다
  (phaseN 번호순이 아니라 그 순서. 번호와 표시 순서가 다를 수 있다).
- 새 페이즈가 추가되면 그 순서상의 **올바른 위치에 삽입**한다(끝에 무조건 붙이지 않는다).

반드시 지킬 것:
1. 변경 diff가 요구하는 부분만 수정한다. 그 외 기존 HTML(구조·스타일·스크립트·문구)은 그대로 둔다.
2. 첨부된 용어집(TRANSLATION.md)의 로컬라이징 용어를 따른다.
   (頭割り→쉐어, 탱 대상 強攻撃→탱버스터, 半面→반갈, 真偽→진짜/가짜, フェーズ移行→페이즈 전환 등)
3. 원문에서 "삭제 예정(削除予定)"으로 명시된 처리법(旧散会, ②ミッシング ぴ 등)은 번역본에서 제외한다.
   신규 "최신" 처리법은 포함한다.
   - 같은 섹션에 謝罪文(사과/수정 안내)이 여러 개 있어도 번역본에서는 하나의 callout으로 자연스럽게 합쳐 유지한다(중복 나열 금지).
   - 삭제 예정 콘텐츠(旧散会 등)만 가리키는 안내 문장은 빼도 된다.
4. 이미지 src는 https://yan-flash.com + /api/uploads/... 형식을 유지한다.
5. 섹션이 추가/삭제되면 좌측 목차(nav.toc)의 링크와 id도 같이 맞춘다.
6. 라이트박스/목차 활성화 <script>, CSS는 절대 건드리지 않는다.
7. 각 이미지(또는 이미지 그룹) 바로 아래에 `<div class="cap">…</div>` 캡션을 둔다.
   - 이미지 안에 일본어 문장/라벨이 있으면 그 내용을 한국어로 번역해 캡션에 적는다.
   - 아이콘·숫자·방위뿐이면 표기 안내만 간단히 적는다(예: 직업 아이콘=담당자, 숫자=순번, A·B·C·1~4=방위).
   - 첨부된 이미지를 직접 보고 캡션을 작성/갱신한다. 이미지가 교체됐으면 새 이미지에 맞게 캡션을 다시 쓴다.
   - 캡션 형식: `<div class="cap"><span class="h">🖼 이미지 안 표기</span>…</div>` (긴 설명은 <ul><li> 사용).

출력: 완성된 index.html 전체를 그대로 출력한다. 코드펜스(```)나 설명 문장 없이 <!DOCTYPE html>로 시작하는 HTML만 출력한다.\
"""


HOST = "https://yan-flash.com"
IMG_RE = re.compile(r'/api/uploads/[0-9a-f-]+\.webp')


# ─────────────────────────── 경보(Discord) ───────────────────────────
def alert(msg: str) -> None:
    """stderr + (설정 시) Discord 로 경보를 보낸다. 실패해도 본 작업은 막지 않는다."""
    print(f"[alert] {msg}", file=sys.stderr)
    if not DISCORD_WEBHOOK:
        return
    try:
        data = json.dumps({"content": msg}, ensure_ascii=False).encode("utf-8")
        # Discord 는 urllib 기본 UA 를 403 으로 막으므로 UA 명시(check.py 와 동일)
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=data,
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001  경보 실패가 본 작업을 깨선 안 됨
        print(f"[alert] Discord 전송 실패: {e}", file=sys.stderr)


# ─────────────────────────── 서킷브레이커 ───────────────────────────
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
    """실패를 가드에 기록한다. 같은 본문 연속 MAX_FAILS 회 → 서킷 오픈.

    본문(해시)이 바뀌면 카운터를 리셋한다(새 변경엔 다시 기회를 준다).
    이미 트립된 상태에서의 프로브 실패는 쿨다운만 연장(경보 스팸 방지)."""
    if g.get("hash") != src:
        g.update({"hash": src, "fail_count": 1, "tripped": False, "tripped_until": None})
    else:
        g["fail_count"] = int(g.get("fail_count", 0)) + 1
    if g["fail_count"] >= MAX_FAILS:
        newly_tripped = not g.get("tripped")
        g["tripped"] = True
        g["tripped_until"] = now + COOLDOWN_HOURS * 3600
        if newly_tripped:
            alert(
                f"⚠️ yan-flash 번역 {g['fail_count']}회 연속 실패 → 서킷 오픈. "
                f"이후 같은 본문은 {COOLDOWN_HOURS:.0f}h 간격 프로브만(토큰 절약).\n사유: {reason}"
            )
    save_guard(g)
    print(
        f"[guard] 실패 기록: count={g['fail_count']} tripped={g['tripped']} ({reason})",
        file=sys.stderr,
    )


# ─────────────────────────── 토큰/비용 로깅 ───────────────────────────
def price_tier(model: str):
    m = (model or "").lower()
    for k, v in PRICES.items():
        if k in m:
            return v
    return PRICES["opus"]  # 모르는 모델이면 비싸게 잡아 경보가 잘 뜨도록(보수적)


def est_cost(usage: dict) -> float:
    pi, po, pw, pr = price_tier(MODEL)
    return (
        usage.get("input_tokens", 0) * pi
        + usage.get("output_tokens", 0) * po
        + usage.get("cache_creation_input_tokens", 0) * pw
        + usage.get("cache_read_input_tokens", 0) * pr
    ) / 1e6


def log_tokens(usage: dict, stop_reason, status: str, cost: float) -> None:
    """1회 호출의 토큰/비용을 stdout + state/token_log.jsonl(최근 N줄)에 남긴다."""
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": MODEL,
        "status": status,            # ok | truncated | bad_html
        "stop_reason": stop_reason,
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "cache_write": usage.get("cache_creation_input_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "est_usd": round(cost, 4),
    }
    print(
        f"[tokens] in={rec['input']} out={rec['output']} "
        f"cw={rec['cache_write']} cr={rec['cache_read']} "
        f"→ ~${rec['est_usd']:.4f} (model={MODEL}, stop={stop_reason}, {status})"
    )
    try:
        TOKENLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if TOKENLOG_PATH.exists():
            lines = TOKENLOG_PATH.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(rec, ensure_ascii=False))
        TOKENLOG_PATH.write_text("\n".join(lines[-TOKENLOG_KEEP:]) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001  로깅 실패가 본 작업을 깨선 안 됨
        print(f"[tokens] 기록 실패: {e}", file=sys.stderr)


def changed_images(diff: str, limit: int = 16) -> list[str]:
    """diff에서 추가(+)된 줄의 이미지 URL을 수집(중복 제거, 최대 limit장)."""
    urls, seen = [], set()
    for line in diff.splitlines():
        if not line.startswith("+"):
            continue
        for m in IMG_RE.findall(line):
            if m not in seen:
                seen.add(m)
                urls.append(HOST + m)
    return urls[:limit]


def parse_sse(resp):
    """Anthropic 스트리밍(SSE) 응답에서 text_delta·usage·stop_reason 을 모은다."""
    chunks, usage, stop_reason = [], {}, None
    for raw in resp:  # 줄 단위로 들어옴
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        try:
            evt = json.loads(payload)
        except Exception:
            continue
        t = evt.get("type")
        if t == "message_start":
            u = (evt.get("message") or {}).get("usage") or {}
            usage.update(u)  # input/cache 토큰은 여기서 옴
        elif t == "content_block_delta":
            d = evt.get("delta", {})
            if d.get("type") == "text_delta":
                chunks.append(d.get("text", ""))
        elif t == "message_delta":
            d = evt.get("delta", {})
            if d.get("stop_reason"):
                stop_reason = d["stop_reason"]
            for k, v in (evt.get("usage") or {}).items():
                if v is not None:
                    usage[k] = v  # 누적 output_tokens 등 갱신
        elif t == "error":
            raise RuntimeError(f"API error event: {evt.get('error')}")
        elif t == "message_stop":
            break
    return "".join(chunks), usage, stop_reason


def call_api(content, retries: int = 3):
    """Anthropic API 를 **스트리밍**으로 호출한다.

    큰 HTML 을 한 번에 생성하면 비스트리밍 응답은 생성 완료까지 아무 데이터도
    오지 않아 read timeout 으로 죽는다(이전 실패 원인). 스트리밍이면 토큰이 계속
    흘러와 연결이 살아 있으므로, timeout 은 '스트림이 멈춘' 경우에만 걸린다.
    일시적 연결 오류/5xx/429 는 백오프로 재시도, 4xx(429 제외)는 즉시 중단.

    반환: (text, usage, stop_reason)"""
    key = os.environ["ANTHROPIC_API_KEY"]
    body = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": content}],
    }).encode("utf-8")
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API_URL, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:  # per-read; 스트림이 흐르면 안 걸림
                text, usage, stop_reason = parse_sse(r)
            if not text.strip():
                raise RuntimeError("스트림에서 텍스트를 받지 못함(빈 응답)")
            return text, usage, stop_reason
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code != 429 and e.code < 500:
                print(f"API 오류 {e.code} (비재시도): {detail}", file=sys.stderr)
                raise
            last = e
            print(f"API {e.code} (시도 {attempt + 1}/{retries}): {detail}", file=sys.stderr)
        except Exception as e:  # RemoteDisconnected, timeout, URLError 등
            last = e
            print(f"API 연결 오류 (시도 {attempt + 1}/{retries}): {e}", file=sys.stderr)
        if attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
    raise last


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
            print(
                f"[guard] 서킷 오픈: 같은 본문이 {g.get('fail_count')}회 연속 실패. "
                f"API 호출 차단(쿨다운 {mins}분 남음, 토큰 0). 차분은 다음 회차로.",
                file=sys.stderr,
            )
            sys.exit(2)
        print("[guard] 쿨다운 경과 — 1회만 프로브 시도.", file=sys.stderr)

    current = INDEX.read_text(encoding="utf-8")
    glossary = GLOSSARY.read_text(encoding="utf-8")

    # 원문 배열 순서(=사이트 표시·전투 순서)대로 본문을 제시한다. 번역본의 섹션 순서가
    # 이 순서를 그대로 따라야 하기 때문(phaseN 번호순 정렬이 아니라 원문 배열 순서).
    files = {p.name: p for p in GUIDE_DIR.glob("phase*.html")}
    try:
        src_order = json.loads((GUIDE_DIR / "order.json").read_text(encoding="utf-8"))
    except Exception:
        src_order = []
    ordered = [f"phase{n}.html" for n in src_order if f"phase{n}.html" in files]
    for name in sorted(files):  # order.json 에 없는 파일은 안전망으로 뒤에 번호순 첨부
        if name not in ordered:
            ordered.append(name)
    order_hint = " → ".join(n[:-5] for n in ordered)  # 예: phase1 → phase2 → phase3 → phase7 → phase4
    guide = "\n\n".join(
        f"===== {name} =====\n{files[name].read_text(encoding='utf-8')}" for name in ordered
    )

    prompt = f"""\
# 용어집 (TRANSLATION.md)
{glossary}

# 원문 본문 변경분 (state/guide/ 의 git diff)
```diff
{diff}
```

# 변경 반영된 현재 원문 본문 전체 (참고용 · **아래 제시된 순서 = 원문 사이트 표시 순서**)
{guide}

# 원문 섹션(페이즈) 표시 순서 — 번역본의 섹션·목차도 반드시 이 순서를 따른다
{order_hint}

# 현재 번역본 docs/index.html (이것을 수정 대상으로 삼아라)
{current}

위 변경분을 번역본에 반영한 완성된 index.html 전체를 출력하라.
섹션과 좌측 목차(nav.toc)의 순서는 위 "원문 섹션 표시 순서"와 정확히 일치해야 한다."""

    # 변경/추가된 이미지를 비전으로 첨부 → 캡션을 보고 작성/갱신
    imgs = changed_images(diff)
    content = [{"type": "text", "text": prompt}]
    for url in imgs:
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    if imgs:
        content.append({
            "type": "text",
            "text": f"위 {len(imgs)}장은 이번에 추가/교체된 이미지다. "
                    "각 이미지를 보고 해당 <img> 아래 <div class=\"cap\"> 캡션을 작성/갱신하라.",
        })
    print(f"첨부 이미지 {len(imgs)}장")

    # ── API 호출(실패 시 가드에 기록하고 1로 종료 → watch.yml 이 state/guide 롤백) ──
    try:
        out, usage, stop_reason = call_api(content)
    except Exception as e:  # noqa: BLE001
        record_failure(g, src, now, f"API 호출 실패: {e}")
        print(f"번역 실패: {e}", file=sys.stderr)
        sys.exit(1)

    cost = est_cost(usage)

    # 잘림 방지: max_tokens 로 끊긴 응답은 불완전 HTML 이므로 적용하지 않는다.
    if stop_reason == "max_tokens":
        log_tokens(usage, stop_reason, "truncated", cost)
        alert(f"⚠️ yan-flash 번역 출력이 max_tokens({MAX_TOKENS})로 잘림 → index.html 미적용.")
        record_failure(g, src, now, "출력이 max_tokens 로 잘림")
        print("ERROR: 응답이 max_tokens 로 잘림. index.html 변경 안 함.", file=sys.stderr)
        sys.exit(1)

    out = re.sub(r'^\s*```(?:html)?\s*', '', out)
    out = re.sub(r'\s*```\s*$', '', out)
    # 모델이 HTML 앞에 설명/뒤에 군더더기를 다는 경우(Sonnet 특성) <!DOCTYPE~</html> 만 사용.
    i = out.lower().find("<!doctype html")
    if i > 0:
        out = out[i:]
    j = out.lower().rfind("</html>")
    if j != -1:
        out = out[:j + len("</html>")]
    out = out.strip()

    # 잘림 방지(핵심): </html> 로 끝나지 않으면 스트림이 중간에 끊긴 불완전 HTML이다.
    # stop_reason 이 max_tokens 가 아니어도(연결 드롭 등) 잘릴 수 있어 위 트림으로는 못 거른다.
    # 구조로 검증해 거부한다(과거 잘린 HTML 이 커밋되던 회귀의 근본 차단).
    if not out.lower().endswith("</html>"):
        log_tokens(usage, stop_reason, "truncated", cost)
        alert("⚠️ yan-flash 번역 출력이 </html> 로 끝나지 않음(스트림 잘림 추정) → index.html 미적용.")
        record_failure(g, src, now, "출력이 </html> 로 끝나지 않음(잘림)")
        print("ERROR: 응답이 </html>로 끝나지 않음(잘림). index.html 변경 안 함.", file=sys.stderr)
        sys.exit(1)

    if not out.lower().startswith("<!doctype html") or len(out) < 5000:
        log_tokens(usage, stop_reason, "bad_html", cost)
        record_failure(g, src, now, "응답이 올바른 HTML 이 아님")
        print("ERROR: 응답이 올바른 HTML이 아님. index.html 을 변경하지 않음.", file=sys.stderr)
        print(out[:500], file=sys.stderr)
        sys.exit(1)

    # ── 성공: 토큰 로깅 + (임계 초과 시 경보) + 가드 리셋 ──
    log_tokens(usage, stop_reason, "ok", cost)
    if cost >= COST_ALERT_USD:
        alert(
            f"💸 yan-flash 번역 1회 추정비용 ~${cost:.2f} (임계 ${COST_ALERT_USD:.2f} 초과). "
            f"model={MODEL}, in={usage.get('input_tokens', 0)}, out={usage.get('output_tokens', 0)}, "
            f"cache_read={usage.get('cache_read_input_tokens', 0)}."
        )

    INDEX.write_text(out + ("\n" if not out.endswith("\n") else ""), encoding="utf-8")
    save_guard({"hash": src, "fail_count": 0, "tripped": False, "tripped_until": None})
    print(f"docs/index.html 갱신 완료 ({len(out)} bytes, model={MODEL}, ~${cost:.4f})")


if __name__ == "__main__":
    main()
