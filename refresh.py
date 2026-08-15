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
  SLACK_TOKEN   슬랙 봇 토큰 (xoxb-...)
                권한: channels:history, groups:history, channels:read,
                      groups:read, users:read
표준 라이브러리만 씁니다. pip install 필요 없음.
"""

import json, os, sys, time, urllib.parse, urllib.request, urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

KST        = timezone(timedelta(hours=9))
HERE       = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.environ.get("DATA_DIR", os.path.join(HERE, "site", "data"))
BASE_PATH  = os.path.join(DATA_DIR, "ops.base.json")
OUT_PATH   = os.path.join(DATA_DIR, "ops.json")

TOKEN      = os.environ.get("SLACK_TOKEN", "").strip()
DAYS       = int(os.environ.get("DAYS", "30"))
INTERVAL   = int(os.environ.get("INTERVAL_SEC", "900"))       # 기본 15분
ONCE       = os.environ.get("ONCE", "").lower() in ("1", "true", "yes")
LIVE_HOURS = int(os.environ.get("LIVE_HOURS", "72"))          # 최근 N시간 발언 = 연결됨
TOKEN_STATS = os.environ.get("TOKEN_STATS_FILE", "").strip()  # 선택: 토큰/비용 JSON
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "").lower() in ("1", "true", "yes")

API = "https://slack.com/api/"


def log(*a):
    print(datetime.now(KST).strftime("[%m-%d %H:%M:%S]"), *a, flush=True)


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
    last_seen, feed = {}, []
    total = 0

    for c in chans:
        cid, cname = c["id"], "#" + c["name"]
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
            txt = (m.get("text") or "").replace("\n", " ").strip()
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
    }


def merge(base, s):
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
        seen = s["last_seen"].get(uid, 0)
        if seen >= live_cut:
            p["state"] = "live"
            if p.get("status") in ("미연결", "대기"):
                p["status"] = "근무중"
        elif seen:
            p["state"] = "pending"
            p["status"] = "대기"           # 붙어는 있는데 최근 발언이 없음
        else:
            p["state"] = "pending"
            p["status"] = "미연결"         # 슬랙에 흔적 자체가 없음

    # 파이프라인 마지막 단계 — '부서'는 슬랙 앱을 가진 실무층만 셉니다
    floor_live = [p for p in people
                  if p.get("desk") == "floor" and p.get("app_id") and p["state"] == "live"]
    for st in base["pipeline"]["steps"]:
        if st["id"] == "dept":
            st["state"] = "live" if floor_live else "pending"

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
    for f in list(d.get("feed", [])):
        pass  # 피드는 슬랙 원문이라 공개 모드에서 통째로 뺍니다
    if d.get("feed"):
        d["feed"] = []
        d.setdefault("feed_note", "공개 배포본에서는 슬랙 원문을 표시하지 않습니다.")
    log(f"공개 모드 — {n}개 항목 문구를 가리고 대화 원문을 제외했습니다.")
    return d


def run_once():
    if not TOKEN:
        log("SLACK_TOKEN 이 없습니다. 수집을 건너뜁니다 (기존 ops.json 유지).")
        return False
    base = json.load(open(BASE_PATH, encoding="utf-8"))
    out = merge(base, collect())
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
