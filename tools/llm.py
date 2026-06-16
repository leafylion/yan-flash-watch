#!/usr/bin/env python3
"""공용 LLM 인프라 — Anthropic 스트리밍 클라이언트 + 비용 계산 + Discord 경보.

페이즈 번역(phase_apply)과 오케스트레이터(translate)가 함께 쓴다.
앱 로직은 없다(순수 인프라). 환경변수로만 설정된다.

환경변수:
    ANTHROPIC_API_KEY  (필수)  — 호출 시점에 필요
    MODEL              (선택, 기본 claude-sonnet-4-6)
    DISCORD_WEBHOOK    (선택, 경보용)
"""
import json
import os
import re
import sys
import time
import urllib.request
from urllib.error import HTTPError

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 64000  # Sonnet 4.6 출력 상한. 섹션 1개는 한참 아래라 잘림 위험 없음.
UA = "yan-flash-watch/1.0 (+https://github.com/leafylion/yan-flash-watch)"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

HOST = "https://yan-flash.com"
IMG_RE = re.compile(r'/api/uploads/[0-9a-f-]+\.webp')

# 1M 토큰당 단가 (input, output, cache_write_5m, cache_read) USD
PRICES = {
    "opus":   (5.0, 25.0, 6.25, 0.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku":  (1.0,  5.0, 1.25, 0.10),
}


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


# ─────────────────────────── 토큰/비용 ───────────────────────────
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


def changed_images(diff: str, limit: int = 24) -> list[str]:
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


# ─────────────────────────── 스트리밍 호출 ───────────────────────────
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


def call_api(content, system: str, retries: int = 3):
    """Anthropic API 를 **스트리밍**으로 호출한다.

    스트리밍이면 토큰이 계속 흘러와 연결이 살아 있으므로 timeout 은 '스트림이 멈춘'
    경우에만 걸린다(비스트리밍은 생성 완료까지 데이터가 안 와 read timeout 으로 죽음).
    일시적 연결 오류/5xx/429 는 백오프로 재시도, 4xx(429 제외)는 즉시 중단.

    반환: (text, usage, stop_reason)"""
    key = os.environ["ANTHROPIC_API_KEY"]
    body = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "system": system,
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
