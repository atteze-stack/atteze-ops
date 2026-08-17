#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ATTEZE OPS — 슬랙 데이터 수집기

하는 일
  1) data/ops.base.json (사람·조직·칸반 등 손으로 정리한 내용) 을 읽고
  2) 슬랙 API 에서 채널/메시지/발신자 활동을 긁어와
  3) 둘을 합쳐서 data/ops.json 으로 씁니다.

대시보드는 data/ops.json 만 봅니다. 이 스크립트가 주기적으로 돌면
웹 화면이 알아서 최신 상태가 됩니다.

필요한 것
  SLACK_TOKEN     슬랙 봇 토큰 (xoxb-...)
                  권한: channels:history, groups:history, channels:read,
                        groups:read, users:read
선택 (없으면 그 부분만 건너뜁니다)
  ICAL_URL        구글 캘린더 '비공개 주소 iCal' — 일정이 대시보드로 바로 들어옵니다.
                  쉼표로 여러 개 넣을 수 있습니다. 슬랙 채널 필요 없음.
  TG_MEMO_TOKEN   메모 전용 텔레그램 봇 토큰 — 아이디어가 대시보드로 바로 들어옵니다.
                  ※ 자비스 봇 토큰을 넣으면 안 됩니다. 자비스가 메시지를 못 받게 됩니다.
표준 라이브러리만 씁니다. pip install 필요 없음.
"""

import json, os, re, sys, time, urllib.parse, urllib.request, urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

KST        = timezone(timedelta(hours=9))
HERE       = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.environ.get("DATA_DIR", os.path.join(HERE, "site", "data"))
BASE_PATH  = os.path.join(DATA_DIR, "ops.base.json")
OUT_PATH   = os.path.join(DATA_DIR, "ops.json")
INBOX_PATH = os.path.join(DATA_DIR, "inbox.json")   # 텔레그램 메모 누적 보관함
# atteze-agents 의 vault-sync 가 직접 밀어넣는 작업 로그 (슬랙을 거치지 않음).
# 원래 설계는 atteze_log.py → 슬랙 → parse_worklog 였는데, 다리를 하나 줄인 경로다.
# 형식은 parse_worklog() 반환값과 동일 — renderLog() 를 그대로 쓴다.
VAULT_WORKLOG_PATH = os.path.join(DATA_DIR, "vault-worklog.json")
USAGE_DIR         = os.path.join(DATA_DIR, "usage")
HERMES_USAGE_PATH = os.path.join(USAGE_DIR, "hermes.json")   # Hermes 프로필별 토큰·비용 (컨테이너가 밀어줌)
SLACK_USAGE_PATH  = os.path.join(USAGE_DIR, "slack.json")    # 슬랙 페르소나(성재경 등) 토큰 (비용은 여기서 계산)

TOKEN      = os.environ.get("SLACK_TOKEN", "").strip()
DAYS       = int(os.environ.get("DAYS", "30"))
INTERVAL   = int(os.environ.get("INTERVAL_SEC", "900"))       # 기본 15분
ONCE       = os.environ.get("ONCE", "").lower() in ("1", "true", "yes")
LIVE_HOURS = int(os.environ.get("LIVE_HOURS", "72"))          # 최근 N시간 발언 = 연결됨
TOKEN_STATS = os.environ.get("TOKEN_STATS_FILE", "").strip()  # 선택: 토큰/비용 JSON
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "").lower() in ("1", "true", "yes")
# 더 이상 안 쓰는 채널 — 수집에서 뺍니다 (쉼표로 여러 개)
SKIP_CHANNELS = [c.strip().lstrip("#") for c in
                 os.environ.get("SKIP_CHANNELS", "아테즈-업무단체방").split(",") if c.strip()]

API = "https://slack.com/api/"

WORKLOG_MARKER = "📋 작업 로그"     # atteze_log.py 가 붙이는 표시

# ── 슬랙을 안 거치는 두 갈래 ────────────────────────────────────────────
# 일정   : 구글 캘린더 비공개 iCal 주소를 그대로 읽습니다.
# 아이디어: 메모 전용 텔레그램 봇의 메시지를 그대로 읽습니다.
# 둘 다 슬랙 채널이 필요 없습니다.
ICAL_URLS  = [u.strip() for u in os.environ.get("ICAL_URL", "").split(",") if u.strip()]
TG_TOKEN   = os.environ.get("TG_MEMO_TOKEN", "").strip()
PLAN_DAYS  = int(os.environ.get("PLAN_DAYS", "21"))    # 앞으로 N일치 일정만 표시
# 이름에 이 말이 들어간 캘린더는 '아이디어 보관함'으로 봅니다.
# 자비스가 아이디어를 이 캘린더에 넣으면 대시보드 아이디어 칸에 뜹니다.
IDEA_CAL   = os.environ.get("IDEA_CAL", "아이디어").strip()
IDEA_DAYS  = int(os.environ.get("IDEA_DAYS", "60"))    # 최근 N일치 아이디어 표시
MEMO_KEEP  = int(os.environ.get("MEMO_KEEP", "200"))   # 보관함에 남길 메모 개수
# 메모 첫 줄에 이 말이 있으면 대시보드에 안 올립니다 (공개 배포본 대비)
MEMO_HIDE_RE = re.compile(r"^\s*(비공개|비밀|숨김|private)\b", re.I)

# ── 작업 상태 신호 ────────────────────────────────────────────────────
# 직원들이 채널에 남기는 상태 메시지를 읽어 '지금 일하는 중/멈춤'을 판정합니다.
#   ▶ 시작 · t_xxx · 요약   ⏳ 진행 · t_xxx · 내용
#   ✅ 완료 · t_xxx · 결과   ⛔ 중단 · t_xxx · 사유
# 완화판(2026-08-16): 구분자(·/:) 도 t_ID 도 없이 이모지(유니코드 또는 슬랙
# :코드: 표기) + 자유 텍스트만 있어도 인식하도록 완화. 기존 엄격 형식도
# 계속 정상 인식됩니다 (하위호환 — 아래 테스트 참조).
_STATUS_CODE2MARK = {
    "arrow_forward": "▶", "hourglass_flowing_sand": "⏳",
    "white_check_mark": "✅", "no_entry": "⛔",
}
STATUS_RE = re.compile(
    r"^\s*(?:"
      r"(?P<mark_u>[▶⏳✅⛔])"
      r"|:(?P<mark_c>arrow_forward|hourglass_flowing_sand|white_check_mark|no_entry):"
    r")\s*"
    r"(?:(?:시작|진행|완료|중단)\s*[·:]\s*)?"
    r"(?:(?P<task>t_[A-Za-z0-9_-]+)\s*[·:]\s*)?"
    r"(?P<note>.*)$")


def _status_match(raw):
    """STATUS_RE.match 를 감싸서 mark 를 항상 유니코드 기호로 정규화합니다
    (슬랙이 :arrow_forward: 코드로 보내든 ▶ 유니코드로 보내든 다운스트림은
    항상 ▶/⏳/✅/⛔ 만 봄)."""
    m = STATUS_RE.match(raw)
    if not m:
        return None
    mark = m.group("mark_u") or _STATUS_CODE2MARK.get(m.group("mark_c"))
    return mark, m.group("task"), m.group("note")
WARN_MIN      = int(os.environ.get("WARN_MIN", "10"))      # N분 무신호 = 신호 지연 (재촉)
STALL_MIN     = int(os.environ.get("STALL_MIN", "20"))     # N분 무신호 = 멈춤 경보
ALERT_CHANNEL = os.environ.get("ALERT_CHANNEL", "아테즈-신규").lstrip("#")

# 슬랙 채널로도 받고 싶으면 이름을 넣으세요. 비워두면 안 씁니다.
IDEA_CHANNEL = os.environ.get("IDEA_CHANNEL", "").lstrip("#")
PLAN_CHANNEL = os.environ.get("PLAN_CHANNEL", "").lstrip("#")
# 실수로 로그에 섞여 들어온 비밀값은 대시보드에 올리기 전에 지웁니다
SECRET_RE = re.compile(
    r"(xox[baprs]-[A-Za-z0-9-]+|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")


def parse_worklog(text, ts):
    """atteze_log.py 가 올린 메시지를 대시보드용 항목으로 바꿉니다."""
    if WORKLOG_MARKER not in text:
        return None
    text = SECRET_RE.sub("[비밀값 제거됨]", text)
    def field(label):
        m = re.search(r"\*" + label + r"\*\s*·\s*(.+)", text)
        return m.group(1).strip() if m else ""
    meta = dict(re.findall(r"`(\w+):([^`]+)`", text))
    head = re.search(re.escape(WORKLOG_MARKER) + r"\s*·\s*(\S+)", text)
    dt = datetime.fromtimestamp(ts, KST)
    return {
        "tool":    meta.get("tool") or (head.group(1) if head else "unknown"),
        "summary": field("한 일") or "(요약 없음)",
        "detail":  field("상세"),
        "where":   field("위치"),
        "result":  field("결과"),
        "tag":     meta.get("tag", ""),
        "session": meta.get("session", ""),
        "when":    dt.strftime("%m-%d %H:%M"),
        "day":     dt.strftime("%Y-%m-%d"),
        "ts":      ts,
    }


def parse_memo(text, ts, kind):
    """#아이디어 / #일정 채널의 메시지를 그대로 항목으로 만듭니다. 형식 요구 없음."""
    text = SECRET_RE.sub("[비밀값 제거됨]", text).strip()
    if not text:
        return None
    # 자비스가 인용부호(>)나 이모지를 붙여도 알아서 벗겨냅니다
    lines = [re.sub(r"^[>*\-•\s💡📅📋🗒️📝]*", "", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l and not re.fullmatch(r"[💡📅📋\s]*", l)]
    if not lines:
        return None
    meta = dict(re.findall(r"`(\w+):([^`]+)`", text))
    lines = [re.sub(r"`\w+:[^`]+`", "", l).strip() for l in lines]
    lines = [l for l in lines if l]
    dt = datetime.fromtimestamp(ts, KST)
    item = {"kind": kind, "title": lines[0], "lines": lines[1:4],
            "tag": meta.get("tag", ""), "from": meta.get("from", "telegram"),
            "when": dt.strftime("%m-%d %H:%M"), "day": dt.strftime("%Y-%m-%d"), "ts": ts}
    if kind == "plan":                       # 앞에 날짜가 적혀 있으면 떼어서 따로 보여줍니다
        m = re.match(r"([\d]{1,2}[-/\.][\d]{1,2}(?:\s+[\d]{1,2}:[\d]{2})?)\s*[·|,]?\s*(.+)", item["title"])
        if m:
            item["at"], item["title"] = m.group(1), m.group(2)
    return item


def log(*a):
    print(datetime.now(KST).strftime("[%m-%d %H:%M:%S]"), *a, flush=True)


# ══════════════════════════════════════════════════════════════════════
#  일정 — 구글 캘린더 iCal 을 그대로 읽습니다 (슬랙 안 거침)
# ══════════════════════════════════════════════════════════════════════

def _ics_unfold(text):
    """iCal 은 긴 줄을 접어서 보냅니다. 이어붙여 원래 줄로 되돌립니다."""
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _ics_text(v):
    return (v.replace("\\n", " ").replace("\\N", " ")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")).strip()


def _ics_dt(val, params):
    """DTSTART 값을 KST datetime 으로. (날짜만인 종일 일정은 all_day=True)"""
    val = val.strip()
    if re.fullmatch(r"\d{8}", val):                       # 20260820 — 종일
        return datetime.strptime(val, "%Y%m%d").replace(tzinfo=KST), True
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", val)
    if not m:
        return None, False
    dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    if m.group(2) == "Z":                                 # UTC 표기
        return dt.replace(tzinfo=timezone.utc).astimezone(KST), False
    tzid = params.get("TZID", "")
    if tzid in ("Asia/Seoul", "Asia/Tokyo", ""):          # 구글 한국 캘린더는 거의 여기
        return dt.replace(tzinfo=KST), False
    return dt.replace(tzinfo=KST), False                  # 다른 지역은 근사 — 표시용이라 충분


def _rrule_dates(start, rule, win_start, win_end):
    """반복 일정을 창(win) 안에서만 펼칩니다. 흔한 형태만 다룹니다."""
    r = dict(kv.split("=", 1) for kv in rule.split(";") if "=" in kv)
    freq = r.get("FREQ", "")
    step = max(1, int(r.get("INTERVAL", "1") or 1))
    until = None
    if r.get("UNTIL"):
        until, _ = _ics_dt(r["UNTIL"], {})
    count = int(r["COUNT"]) if r.get("COUNT", "").isdigit() else None
    days = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    byday = [days[d[-2:]] for d in r.get("BYDAY", "").split(",") if d[-2:] in days]

    out, cur, n = [], start, 0
    for _ in range(2000):                                  # 폭주 방지
        if until and cur > until:
            break
        if count is not None and n >= count:
            break
        if cur > win_end:
            break
        if not byday or cur.weekday() in byday:   # 진짜 발생일만 COUNT 에 셉니다
            if cur >= win_start:
                out.append(cur)
            n += 1
        if freq == "DAILY":
            cur += timedelta(days=step)
        elif freq == "WEEKLY":
            cur += timedelta(days=7 * step) if not byday else timedelta(days=1)
        elif freq == "MONTHLY":
            y, mo = cur.year, cur.month + step
            y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
            try:
                cur = cur.replace(year=y, month=mo)
            except ValueError:
                break
        elif freq == "YEARLY":
            try:
                cur = cur.replace(year=cur.year + step)
            except ValueError:
                break
        else:
            break
    return out


def fetch_calendar():
    """구글 캘린더 iCal → 일정 + 아이디어 목록.
       이름에 IDEA_CAL('아이디어')이 들어간 캘린더의 항목은 전부 아이디어로 봅니다."""
    if not ICAL_URLS:
        return []
    now = datetime.now(KST)
    today0    = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win_start = today0
    win_end   = today0 + timedelta(days=PLAN_DAYS)
    idea_start = today0 - timedelta(days=IDEA_DAYS)      # 아이디어는 과거 것도 보여줍니다
    items = []

    for url in ICAL_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "atteze-ops/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as e:
            log("  캘린더 읽기 실패:", repr(e))
            continue

        ev, cal_name, is_idea = None, "", False
        for line in _ics_unfold(body):
            if line == "BEGIN:VEVENT":
                ev = {}
                continue
            if line == "END:VEVENT":
                if ev and ev.get("title") and ev.get("start"):
                    if is_idea:
                        s = ev["start"]
                        if idea_start <= s <= win_end:
                            memo = _ics_text(ev.get("desc", ""))
                            items.append({
                                "kind": "idea", "title": ev["title"],
                                "lines": [memo[:100]] if memo else [],
                                "tag": "", "from": "jarvis",
                                "when": s.strftime("%m-%d %H:%M"),
                                "day": s.strftime("%Y-%m-%d"),
                                "ts": s.timestamp(),
                            })
                    else:
                        starts = ([ev["start"]] if not ev.get("rrule")
                                  else _rrule_dates(ev["start"], ev["rrule"], win_start, win_end))
                        for s in starts:
                            if not (win_start <= s <= win_end):
                                continue
                            items.append({
                                "kind": "plan",
                                "at": s.strftime("%m-%d") + ("" if ev["all_day"] else s.strftime(" %H:%M")),
                                "title": ev["title"],
                                "lines": [x for x in [ev.get("where", "")] if x][:1],
                                "tag": "", "from": cal_name or "google",
                                "when": s.strftime("%m-%d %H:%M"),
                                "day": s.strftime("%Y-%m-%d"),
                                "ts": s.timestamp(),
                            })
                ev = None
                continue
            if ":" not in line:
                continue
            head, val = line.split(":", 1)
            parts = head.split(";")
            key = parts[0].upper()
            params = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            if key == "X-WR-CALNAME":
                cal_name = _ics_text(val)[:20]
                is_idea = bool(IDEA_CAL) and IDEA_CAL in cal_name
                continue
            if ev is None:
                continue
            if key == "SUMMARY":
                ev["title"] = SECRET_RE.sub("[비밀값 제거됨]", _ics_text(val))[:80]
            elif key == "DESCRIPTION":
                ev["desc"] = SECRET_RE.sub("[비밀값 제거됨]", _ics_text(val))[:200]
            elif key == "LOCATION":
                ev["where"] = _ics_text(val)[:40]
            elif key == "DTSTART":
                ev["start"], ev["all_day"] = _ics_dt(val, params)
            elif key == "RRULE":
                ev["rrule"] = val.strip()

    n_idea = sum(1 for i in items if i["kind"] == "idea")
    items.sort(key=lambda x: x["ts"])
    log(f"  구글 캘린더: 일정 {len(items)-n_idea}건 · 아이디어 {n_idea}건")
    return items[:80]


# ══════════════════════════════════════════════════════════════════════
#  아이디어 — 메모 전용 텔레그램 봇을 그대로 읽습니다 (슬랙 안 거침)
#  ※ 자비스 봇이 아니라 '메모 전용' 봇이어야 합니다. 텔레그램은 한 봇의
#     메시지를 한 곳에서만 받아가기 때문입니다.
# ══════════════════════════════════════════════════════════════════════

def load_vault_worklog():
    """atteze-agents 의 vault-sync 가 밀어넣은 작업 로그를 읽습니다.

    Claude Code 웹·터미널·Codex 세션이 `/log` 로 남긴 것이 여기로 들어옵니다.
    슬랙을 거치지 않으므로 슬랙 토큰과 무관하게 동작합니다.

    ⚠️ 파일이 없거나 깨져도 **절대 예외를 올리지 않습니다** — 이게 없다고 대시보드 갱신
       전체가 죽으면 안 됩니다. 못 읽으면 빈 목록이고, 슬랙 쪽 worklog 는 그대로 나옵니다.
    """
    try:
        items = json.load(open(VAULT_WORKLOG_PATH, encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        log(f"⚠️ vault-worklog.json 을 못 읽었습니다 (무시하고 계속): {e}")
        return []
    if not isinstance(items, list):
        log("⚠️ vault-worklog.json 이 배열이 아닙니다 — 무시합니다.")
        return []
    out = []
    for w in items:
        if not isinstance(w, dict) or not w.get("summary"):
            continue
        # 공개 배포본에도 나가므로 여기서도 한 번 더 비밀값을 지웁니다(밀어넣는 쪽에서도 하지만).
        for k in ("summary", "detail", "where", "result"):
            if w.get(k):
                w[k] = SECRET_RE.sub("[비밀값 제거됨]", str(w[k]))
        w.setdefault("ts", 0)
        out.append(w)
    return out


def _inbox_load():
    try:
        return json.load(open(INBOX_PATH, encoding="utf-8"))
    except Exception:
        return {"tg_offset": 0, "items": []}


def _inbox_save(box):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = INBOX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(box, f, ensure_ascii=False, indent=2)
    os.replace(tmp, INBOX_PATH)


def fetch_telegram():
    """메모 봇에 새로 온 메시지를 받아 보관함에 쌓고, 전체 메모 목록을 돌려줍니다."""
    box = _inbox_load()
    if not TG_TOKEN:
        return box.get("items", [])

    url = (f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
           f"?timeout=0&limit=100&allowed_updates=%5B%22message%22%2C%22channel_post%22%5D"
           f"&offset={int(box.get('tg_offset', 0)) + 1}")
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "atteze-ops/1.0"}), timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log("  텔레그램 읽기 실패:", repr(e))
        return box.get("items", [])
    if not body.get("ok"):
        log("  텔레그램 오류:", body.get("description", "?"))
        return box.get("items", [])

    seen = {i["ts"] for i in box.get("items", [])}
    added = 0
    for up in body.get("result", []):
        box["tg_offset"] = max(int(box.get("tg_offset", 0)), int(up.get("update_id", 0)))
        msg = up.get("message") or up.get("channel_post") or {}
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text or text.startswith("/"):        # /start 같은 명령은 무시
            continue
        ts = float(msg.get("date", 0)) or time.time()
        if MEMO_HIDE_RE.match(text):
            continue                                 # '비공개'로 시작하면 대시보드에 안 올림
        # 첫 줄이 날짜로 시작하면 일정, 아니면 아이디어
        kind = "plan" if re.match(r"\s*\d{1,2}[-/\.]\d{1,2}", text) else "idea"
        item = parse_memo(text, ts, kind)
        if item and ts not in seen:
            item["from"] = "telegram"
            box.setdefault("items", []).append(item)
            seen.add(ts)
            added += 1

    box["items"] = sorted(box.get("items", []), key=lambda m: -m["ts"])[:MEMO_KEEP]
    _inbox_save(box)
    log(f"  텔레그램 메모: 새로 {added}건 · 보관 {len(box['items'])}건")
    return box["items"]


def call(method, **params):
    """슬랙 API 호출. rate limit(429) 은 기다렸다 재시도."""
    for attempt in range(6):
        url = API + method + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + TOKEN,
            "User-Agent": "atteze-ops/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "5"))
                log(f"  rate limit — {wait}초 대기")
                time.sleep(wait + 1)
                continue
            raise
        if body.get("ok"):
            return body
        err = body.get("error", "unknown")
        if err == "ratelimited":
            time.sleep(5)
            continue
        raise RuntimeError(f"{method} 실패: {err}")
    raise RuntimeError(f"{method}: 재시도 초과")


def paged(method, key, **params):
    cur, out = None, []
    while True:
        p = dict(params, limit=200)
        if cur:
            p["cursor"] = cur
        b = call(method, **p)
        out.extend(b.get(key, []))
        cur = (b.get("response_metadata") or {}).get("next_cursor") or ""
        if not cur:
            return out


def display_name(u):
    p = u.get("profile") or {}
    return (p.get("display_name") or p.get("real_name")
            or u.get("real_name") or u.get("name") or u.get("id"))


def collect():
    oldest = (datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp()

    log("사용자 목록 조회")
    users = paged("users.list", "members")
    uname = {u["id"]: display_name(u) for u in users}
    ubot  = {u["id"]: bool(u.get("is_bot")) for u in users}
    # 봇 프로필의 app_id → 슬랙 user_id 매핑 (연결 상태 판정에 사용)
    app2uid = {}
    for u in users:
        aid = (u.get("profile") or {}).get("api_app_id")
        if aid:
            app2uid[aid] = u["id"]

    log("채널 목록 조회")
    chans = [c for c in paged("conversations.list", "channels",
                              types="public_channel,private_channel",
                              exclude_archived="true")
             if c.get("is_member") or c.get("is_channel") or c.get("is_group")]

    per_channel, per_user, per_day = [], Counter(), Counter()
    last_seen, feed, worklog, memos = {}, [], [], []
    status_ev, alert_cid = {}, None      # uid → 최신 상태 신호
    total = 0

    for c in chans:
        cid, cname = c["id"], "#" + c["name"]
        if c["name"] == ALERT_CHANNEL:
            alert_cid = cid
        if c["name"] in SKIP_CHANNELS:
            log(f"  {cname}: 제외 (더 이상 사용 안 함)")
            continue
        memo_kind = ("idea" if IDEA_CHANNEL and c["name"] == IDEA_CHANNEL else
                     "plan" if PLAN_CHANNEL and c["name"] == PLAN_CHANNEL else None)
        msgs, cur = [], None
        while True:
            p = dict(channel=cid, limit=200, oldest=f"{oldest:.6f}")
            if cur:
                p["cursor"] = cur
            try:
                b = call("conversations.history", **p)
            except RuntimeError as e:
                log(f"  {cname}: 건너뜀 ({e})")
                break
            msgs.extend(b.get("messages", []))
            cur = (b.get("response_metadata") or {}).get("next_cursor") or ""
            if not cur or not b.get("has_more"):
                break

        real = [m for m in msgs if m.get("subtype") in (None, "bot_message", "thread_broadcast")]
        per_channel.append({"n": cname, "v": len(real),
                            "sub": (c.get("purpose") or {}).get("value", "") or
                                   (c.get("topic") or {}).get("value", "")})
        total += len(real)

        for m in real:
            ts  = float(m.get("ts", "0"))
            dt  = datetime.fromtimestamp(ts, KST)
            uid = m.get("user") or m.get("bot_id") or "?"
            per_user[uid] += 1
            per_day[dt.strftime("%m-%d")] += 1
            if uid not in last_seen or ts > last_seen[uid]:
                last_seen[uid] = ts
            raw = m.get("text") or ""
            # 작업 상태 신호 — 각 사람의 가장 최근 신호만 기억합니다
            sm = _status_match(raw.strip().splitlines()[0] if raw.strip() else "")
            if sm and (uid not in status_ev or ts > status_ev[uid]["ts"]):
                mark, task, note = sm
                status_ev[uid] = {"mark": mark, "task": task or "",
                                  "note": SECRET_RE.sub("[비밀값 제거됨]", note or "").strip()[:60],
                                  "ts": ts}
            wl = parse_worklog(raw, ts)
            if wl:
                worklog.append(wl)
                continue                      # 작업 로그는 일반 대화 피드에 넣지 않습니다
            if memo_kind:
                mm = parse_memo(raw, ts, memo_kind)
                if mm:
                    memos.append(mm)
                continue                      # 아이디어·일정 채널은 피드에 안 넣습니다
            txt = raw.replace("\n", " ").strip()
            if txt:
                feed.append({"uid": uid, "text": txt[:180], "ts": ts,
                             "when": dt.strftime("%m-%d %H:%M")})
        log(f"  {cname}: {len(real)}건")

    per_channel.sort(key=lambda x: -x["v"])
    feed.sort(key=lambda f: -f["ts"])
    daily = [{"d": d, "v": v} for d, v in sorted(per_day.items())]

    return {
        "uname": uname, "ubot": ubot, "app2uid": app2uid,
        "channels": per_channel, "per_user": per_user, "daily": daily,
        "last_seen": last_seen, "feed": feed[:12], "total": total,
        "worklog": sorted(worklog, key=lambda w: -w["ts"])[:60],
        "memos":   sorted(memos,   key=lambda m: -m["ts"])[:80],
        "status_ev": status_ev, "alert_cid": alert_cid,
    }


def _usage_load(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


def _usage_zero():
    return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0,
            "cost_usd": 0.0, "unpriced": False}


def merge_usage(base):
    """직원별 토큰 사용량 — data/usage/hermes.json(비용 그대로 사용) +
       data/usage/slack.json(토큰만 있음 → model_prices 로 비용 계산).
       둘 중 하나가 없어도(집계기 미가동) 조용히 건너뛰고 있는 것만 반영합니다.
       ⚠️ 단가표(ops.base.json:model_prices)에 없는 모델은 0원이 아니라
       unpriced 로 표시합니다 — 실제 지출이 작아 보이는 왜곡을 막기 위함."""
    prices = base.get("model_prices", {})

    def price_cost(model, tok):
        p = prices.get(model)
        if not p:
            return None                            # 단가 미등록 — 0 아님
        c = (tok.get("in", 0) / 1_000_000) * (p.get("in") or 0)
        c += (tok.get("out", 0) / 1_000_000) * (p.get("out") or 0)
        if p.get("cache_read") is not None:
            c += (tok.get("cache_read", 0) / 1_000_000) * p["cache_read"]
        if p.get("cache_write") is not None:
            c += (tok.get("cache_write", 0) / 1_000_000) * p["cache_write"]
        return c

    today = datetime.now(KST).strftime("%Y-%m-%d")
    since7 = (datetime.now(KST) - timedelta(days=6)).strftime("%Y-%m-%d")

    people_out, unpriced_models, daily_tokens = {}, set(), Counter()

    def add(pid, day, tok, cost, priced):
        daily_tokens[day] += int(tok.get("in", 0)) + int(tok.get("out", 0))
        slot = people_out.setdefault(pid, {"today": _usage_zero(), "d7": _usage_zero()})
        for bucket, cond in (("today", day == today), ("d7", day >= since7)):
            if not cond:
                continue
            b = slot[bucket]
            for k in ("in", "out", "cache_read", "cache_write"):
                b[k] += int(tok.get(k, 0) or 0)
            if priced and cost is not None:
                b["cost_usd"] += cost
            else:
                b["unpriced"] = True

    hermes_u = _usage_load(HERMES_USAGE_PATH)
    if hermes_u:
        for pid, slot in (hermes_u.get("people") or {}).items():
            for day, d in (slot.get("days") or {}).items():
                add(pid, day, d, d.get("cost_usd", 0.0), True)   # 재계산 안 함

    slack_u = _usage_load(SLACK_USAGE_PATH)
    if slack_u:
        for pid, slot in (slack_u.get("people") or {}).items():
            for day, d in (slot.get("days") or {}).items():
                models = d.get("models") or {}
                if not models:
                    add(pid, day, d, None, False)   # 모델 구분 없으면 단가 계산 불가
                    continue
                for model, tok in models.items():
                    cost = price_cost(model, tok)
                    if cost is None:
                        unpriced_models.add(model)
                    add(pid, day, tok, cost, cost is not None)

    total = {"today": _usage_zero(), "d7": _usage_zero()}
    for slot in people_out.values():
        for bucket in ("today", "d7"):
            b, tb = slot[bucket], total[bucket]
            for k in ("in", "out", "cache_read", "cache_write", "cost_usd"):
                tb[k] += b[k]
            tb["unpriced"] = tb["unpriced"] or b["unpriced"]

    have_source = bool(hermes_u or slack_u)
    base["usage"] = {
        "people": people_out,
        "total": total,
        "unpriced_models": sorted(unpriced_models),
        "sources": {"hermes": bool(hermes_u), "slack": bool(slack_u)},
        "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
    }

    # 사이드바 — 회사 전체 오늘 토큰·비용 (usage 소스가 있을 때만 덮어씀)
    if have_source:
        base["tokens"]["today"] = total["today"]["in"] + total["today"]["out"]
        base["tokens"]["cost_usd"] = round(total["today"]["cost_usd"], 4)
        last7 = sorted(daily_tokens)[-7:]
        base["tokens"]["spark7"] = [daily_tokens[d] for d in last7]
        base["tokens"]["note"] = ("일부 모델 단가 미등록 — 실비용보다 낮게 표시될 수 있습니다."
                                   if total["today"]["unpriced"] else "")
    log(f"  사용량: hermes={'있음' if hermes_u else '없음'} · slack={'있음' if slack_u else '없음'}"
        + (f" · 단가 미등록 모델: {', '.join(sorted(unpriced_models))}" if unpriced_models else ""))


def merge(base, s, extra_memos=None):
    people = base["people"]
    now = time.time()
    live_cut = now - LIVE_HOURS * 3600

    # 슬랙 앱이 실제로 붙었는지 판정
    for p in people:
        if p.get("engine") == "사람" or p.get("state") == "stopped":
            continue
        # 앱이 재설치되면 봇 user_id 가 바뀝니다 — app_id 매핑을 우선합니다.
        uid = s["app2uid"].get(p.get("app_id") or "") or p.get("slack_id")
        if uid:
            p["slack_id"] = uid
        if not uid:
            continue                       # 슬랙에 존재하지 않는 슬롯은 손으로 정한 값 유지
        # 슬랙에서 표시명을 바꾸면 대시보드 이름도 따라갑니다.
        # 손으로 고정하고 싶으면 그 사람에게 "name_lock": true 를 넣으세요.
        sname = (s["uname"].get(uid) or "").strip()
        if sname and not p.get("name_lock") and sname != p.get("name"):
            log(f'  이름 갱신: {p.get("name")} → {sname}')
            p["name_was"] = p.get("name")
            p["name"] = sname

        seen = s["last_seen"].get(uid, 0)
        if seen >= live_cut:
            p["state"] = "live"
            if p.get("status") in ("미연결", "대기"):
                p["status"] = "근무중"
        elif seen:
            p["state"] = "idle"            # 붙어는 있는데 최근 발언이 없음 — 미연결과 다릅니다
            p["status"] = "대기"
            p["last_seen_note"] = datetime.fromtimestamp(seen, KST).strftime("%Y-%m-%d")
        else:
            p["state"] = "pending"
            p["status"] = "미연결"         # 슬랙에 흔적 자체가 없음

    # 작업 상태 신호 → 지금 일하는 중 / 멈춤 판정
    now_ts = time.time()
    for p in people:
        ev = s.get("status_ev", {}).get(p.get("slack_id") or "")
        if not ev or now_ts - ev["ts"] > 24 * 3600:      # 하루 지난 신호는 무시
            p.pop("work", None)
            continue
        age_min = (now_ts - ev["ts"]) / 60
        sig_kst = datetime.fromtimestamp(ev["ts"], KST)
        dt = sig_kst.strftime("%H:%M")
        if ev["mark"] in ("▶", "⏳"):
            state = ("working" if age_min <= WARN_MIN else
                     "quiet"   if age_min <= STALL_MIN else "stalled")
        elif ev["mark"] == "✅":
            state = "done"
        else:
            state = "stopped"
        # day/ts 는 화면이 "어제/오늘"을 KST 기준으로 가르기 위한 값입니다.
        # last("HH:MM")만으로는 어느 날 신호인지 알 수 없어, 오늘 완료한 일이
        # "어제 한 일"에 섞여 나오던 문제가 있었습니다(2026-08-17).
        p["work"] = {"state": state, "task": ev["task"], "note": ev["note"],
                     "last": dt, "age_min": int(age_min),
                     "day": sig_kst.strftime("%Y-%m-%d"), "ts": ev["ts"]}

    # 파이프라인 마지막 단계 — '부서'는 슬랙 앱을 가진 실무층만 셉니다
    floor_live = [p for p in people
                  if p.get("desk") == "floor" and p.get("app_id")
                  and p["state"] in ("live", "idle")]
    for st in base["pipeline"]["steps"]:
        if st["id"] == "dept":
            st["state"] = ("live" if any(p["state"] == "live" for p in floor_live)
                           else "idle" if floor_live else "pending")

    by_slack = {p["slack_id"]: p for p in people if p.get("slack_id")}

    # 발신자별 집계 — 명단에 있는 사람 우선, 없으면 슬랙 표시명
    by_person = []
    for p in people:
        uid = p.get("slack_id")
        by_person.append({"id": p["id"], "n": p["name"],
                          "v": int(s["per_user"].get(uid, 0)) if uid else 0})
    known = {p.get("slack_id") for p in people}
    for uid, v in s["per_user"].items():
        if uid not in known:
            by_person.append({"id": uid, "n": s["uname"].get(uid, uid), "v": v})
    by_person.sort(key=lambda x: -x["v"])

    days = s["daily"]
    rng = f"{days[0]['d']} ~ {days[-1]['d']}" if days else "데이터 없음"

    base["activity"] = {
        "range": rng, "total": s["total"], "sampled": False,
        "channels": s["channels"], "by_person": by_person, "daily": days,
    }
    # 슬랙에서 파싱한 것 + vault 가 직접 밀어넣은 것을 합쳐 최신순.
    # 어느 한쪽이 비어도 나머지는 그대로 보인다.
    base["worklog"] = sorted(
        list(s.get("worklog", [])) + load_vault_worklog(),
        key=lambda w: -w.get("ts", 0),
    )[:60]
    memos = list(s.get("memos", [])) + list(extra_memos or [])
    today0 = datetime.now(KST).replace(hour=0, minute=0, second=0,
                                       microsecond=0).timestamp()
    a = base.setdefault("assistant", {})
    a["ideas"] = sorted([m for m in memos if m["kind"] == "idea"],
                        key=lambda m: -m["ts"])[:30]
    a["schedule"] = sorted([m for m in memos if m["kind"] == "plan"
                            and m["ts"] >= today0], key=lambda m: m["ts"])[:30]
    srcs = sorted({m.get("from", "") for m in memos} - {""})
    a["sources"] = srcs
    base["feed"] = [{
        "who": (by_slack.get(f["uid"]) or {}).get("id") or s["uname"].get(f["uid"], f["uid"]),
        "text": f["text"], "when": f["when"],
    } for f in s["feed"]]

    if TOKEN_STATS and os.path.exists(TOKEN_STATS):
        try:
            t = json.load(open(TOKEN_STATS, encoding="utf-8"))
            base["tokens"].update({k: v for k, v in t.items() if k in
                                   ("today", "cost_usd", "sessions", "spark7", "fx_krw")})
            base["tokens"]["note"] = ""
        except Exception as e:
            log("토큰 파일 읽기 실패:", e)

    merge_usage(base)   # 직원별 토큰 사용량(data/usage/*.json) — 사이드바·상세패널용

    base["meta"]["generated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    base["meta"]["source"] = f"슬랙 API 자동 수집 · 최근 {DAYS}일 · ops.base.json 병합"
    base["meta"]["notes"] = (f"최근 {LIVE_HOURS}시간 안에 발언이 있으면 '연결됨'으로 표시합니다. "
                             f"조직·칸반 내용은 ops.base.json 에서 수정하세요.")
    return base


def redact(d):
    """PUBLIC_MODE 일 때 private 표시된 항목의 문구를 가립니다.
       항목 자체는 남겨서 개수·진행률은 정확하게 유지합니다."""
    n = 0
    for b in d.get("boards", []):
        for c in b.get("cards", []):
            if c.pop("private", False):
                c["t"] = c.pop("public_t", "비공개 항목")
                c["note"] = c.pop("public_note", "")
                n += 1
            c.pop("public_t", None); c.pop("public_note", None)
    for bl in d.get("blockers", []):
        if bl.pop("private", False):
            bl["t"] = bl.pop("public_t", "비공개 항목")
            bl["d"] = bl.pop("public_d", "")
            n += 1
        bl.pop("public_t", None); bl.pop("public_d", None)
    # 작업 로그는 공개 모드에서도 보여줍니다 (이게 이 기능의 목적) — 비밀값만 제거
    for w in d.get("worklog", []):
        for k in ("summary", "detail", "where", "result"):
            if w.get(k):
                w[k] = SECRET_RE.sub("[비밀값 제거됨]", w[k])
    if d.get("feed"):
        d["feed"] = []
        d.setdefault("feed_note", "공개 배포본에서는 슬랙 원문을 표시하지 않습니다.")
    log(f"공개 모드 — {n}개 항목 문구를 가리고 대화 원문을 제외했습니다.")
    return d


def alert_stalls(out, prev, alert_cid):
    """상태가 나빠진 사람이 있으면 슬랙 채널에 알립니다.
       10분 무신호(quiet) → 재촉 한 번, 20분(stalled)·중단(stopped) → 경보 한 번.

    반환값: 이번 실행에서 발송을 '시도'했는지·성공/실패 몇 건인지 기록한 dict.
    (관측성 개선, 2026-08-16: 전엔 실패해도 로그 한 줄만 남고 Actions는 초록불
    이라 아무도 몰랐음. 이제 대시보드 사이드바 + Actions 어노테이션으로 눈에
    띄게 만듦. checked=False 면 이번 실행엔 보낼 신호가 없었다는 뜻 — 이 경우
    성공/실패를 판단할 수 없으므로 대시보드에서도 '해당 없음'으로 구분 표시.)"""
    status = {"checked_at": datetime.now(KST).isoformat(timespec="seconds"),
              "attempted": 0, "sent": 0, "failed": 0, "last_error": None,
              "token_present": bool(TOKEN), "channel_configured": bool(alert_cid)}
    if not (TOKEN and alert_cid):
        return status
    RANK = {"quiet": 1, "stalled": 2, "stopped": 2}
    prev_rank = {p["id"]: RANK.get((p.get("work") or {}).get("state"), 0)
                 for p in (prev or {}).get("people", [])}
    for p in out.get("people", []):
        w = p.get("work") or {}
        rank = RANK.get(w.get("state"), 0)
        if rank == 0 or rank <= prev_rank.get(p["id"], 0):
            continue                       # 그대로거나 좋아졌으면 조용히
        what = w.get("task") or w.get("note") or "작업"
        if w["state"] == "quiet":
            msg = (f"🟡 확인 — *{p['name']}* 신호 지연: {what} "
                   f"(마지막 신호 {w.get('last','?')} · {w.get('age_min','?')}분 경과). "
                   f"진행 중이면 ⏳ 를 남겨라. 10분 더 무신호면 경보로 올라간다.")
        else:
            msg = (f"⚠️ 경보 — *{p['name']}* {'응답 없음' if w['state']=='stalled' else '작업 중단'}: "
                   f"{what} (마지막 신호 {w.get('last','?')} · {w.get('age_min','?')}분 경과). "
                   f"진행 중이면 ⏳ 신호를, 막혔으면 ⛔ 와 사유를 남겨라.")
        status["attempted"] += 1
        try:
            call("chat.postMessage", channel=alert_cid, text=msg)
            status["sent"] += 1
            log(f"  알림 발송: {p['name']} ({w['state']})")
        except Exception as e:
            status["failed"] += 1
            status["last_error"] = repr(e)
            log(f"  알림 발송 실패({p['name']}):", repr(e))   # chat:write 권한 없으면 여기로
            # GitHub Actions 워크플로 요약에 노란 경고로 뜨게 함 — 로그 한 줄로
            # 묻혀서 아무도 모르는 상태를 막기 위함 (Actions 자체는 계속 초록불
            # 이지만 Summary 탭에 어노테이션이 남아 눈에 띔).
            print(f"::warning::슬랙 알림 발송 실패 ({p['name']}, {w['state']}): {e!r}")
    return status


def run_once():
    base = json.load(open(BASE_PATH, encoding="utf-8"))
    try:
        prev = json.load(open(OUT_PATH, encoding="utf-8"))
    except Exception:
        prev = None

    # 슬랙을 안 거치는 두 갈래 — 토큰/주소가 없으면 조용히 건너뜁니다
    extra = fetch_calendar() + fetch_telegram()

    if not TOKEN:
        log("SLACK_TOKEN 이 없습니다. 슬랙 수집은 건너뜁니다.")
        s = {"uname": {}, "ubot": {}, "app2uid": {}, "channels": [],
             "per_user": Counter(), "daily": [], "last_seen": {}, "feed": [],
             "total": 0, "worklog": [], "memos": [], "status_ev": {}, "alert_cid": None}
    else:
        s = collect()
    out = merge(base, s, extra)
    alert_status = alert_stalls(out, prev, s.get("alert_cid"))
    # 이번 실행에 보낼 신호가 없었으면(attempted=0) 이전 실행의 마지막 실제
    # 시도 결과를 이어받아 대시보드에서 "마지막 알림 발송 상태"가 계속 보이게
    # 함 — 안 그러면 알림 없는 정상적인 5분마다 상태가 사라져 버림.
    prev_alert = (prev or {}).get("alert_status") or {}
    if alert_status["attempted"] == 0 and prev_alert.get("last_attempt_at"):
        alert_status["last_attempt_at"] = prev_alert.get("last_attempt_at")
        alert_status["last_attempt_ok"] = prev_alert.get("last_attempt_ok")
        alert_status["last_error"] = prev_alert.get("last_error")
    elif alert_status["attempted"] > 0:
        alert_status["last_attempt_at"] = alert_status["checked_at"]
        alert_status["last_attempt_ok"] = alert_status["failed"] == 0
    else:
        alert_status["last_attempt_at"] = None
        alert_status["last_attempt_ok"] = None
    out["alert_status"] = alert_status
    if PUBLIC_MODE:
        out = redact(out)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_PATH)          # 원자적 교체 — 반쯤 쓰인 파일을 읽는 일 없음
    log(f"ops.json 갱신 완료 — 메시지 {out['activity']['total']}건")
    return True


if __name__ == "__main__":
    log(f"ATTEZE OPS 수집기 시작 (주기 {INTERVAL}초, 최근 {DAYS}일)")
    while True:
        try:
            run_once()
        except Exception as e:
            log("오류:", repr(e))
        if ONCE:
            break
        time.sleep(INTERVAL)
