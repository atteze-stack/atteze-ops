# ATTEZE OPS — 온라인판 (GitHub Pages)

주소 하나로 어디서나 열리는 아테즈 조직·오피스·진행 현황 대시보드.
NAS도, 서버도, 켜둘 컴퓨터도 필요 없습니다.

```
슬랙  ──(GitHub Actions · 15분마다)──▶  data/ops.json  ──▶  GitHub Pages
                                                              │
브라우저 ──(30초마다 다시 읽음)────────────────────────────────┘
```

---

## 설치 — 순서대로 10분

### 1단계 · 저장소 만들기 (2분)

1. https://github.com/new
2. Repository name: `atteze-ops`
3. **Public** 선택 (Private 으로 하면 Pages 가 무료 플랜에서 안 됩니다)
4. **Create repository**
5. 다음 화면의 `uploading an existing file` 클릭 → **이 폴더 안의 파일을 전부 드래그** → Commit

> `.github` 폴더와 `.nojekyll` 파일도 빠짐없이 올라가야 합니다.
> 드래그가 숨김 파일을 빼먹으면, 저장소에서 **Add file → Create new file** 로
> `.github/workflows/refresh.yml` 을 직접 만들고 내용을 붙여넣으세요.

### 2단계 · 슬랙 토큰 넣기 (5분)

**토큰 발급**

1. https://api.slack.com/apps → **Create New App** → **From scratch**
   (이름 `ATTEZE OPS Reader`, 워크스페이스 `테즈`)
2. 왼쪽 **OAuth & Permissions** → **Bot Token Scopes** 에 5개 추가

   `channels:read` · `channels:history` · `groups:read` · `groups:history` · `users:read`

3. 위로 올라가 **Install to Workspace** → 허용
4. **Bot User OAuth Token** (`xoxb-` 로 시작) 복사
5. 슬랙에서 읽고 싶은 채널마다 `/invite @ATTEZE OPS Reader`
   (비공개 채널은 초대해야만 읽힙니다)

**GitHub 에 등록**

저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

- Name: `SLACK_TOKEN`
- Secret: 복사한 `xoxb-...`

> 토큰은 GitHub 비밀값으로만 저장됩니다. 페이지 소스에도, 저장소에도 절대 안 실립니다.
> 읽기 전용 권한이라 이 토큰으로는 슬랙에 글을 쓰거나 지울 수 없습니다.

### 3단계 · Pages 켜기 (2분)

저장소 → **Settings** → **Pages**

- Source: `Deploy from a branch`
- Branch: `main` / `/ (root)` → **Save**

1~2분 뒤 이 주소로 열립니다:

```
https://<GitHub계정>.github.io/atteze-ops/
```

### 4단계 · 첫 수집 (1분)

저장소 → **Actions** → 왼쪽 `refresh` → **Run workflow**

로그에 `ops.json 갱신 완료 — 메시지 N건` 이 뜨면 끝입니다.
이후로는 15분마다 알아서 돕니다.

---

## 내 도메인 붙이기 (선택)

1. 도메인 DNS 에 CNAME 추가: `ops` → `<GitHub계정>.github.io`
2. 저장소 → Settings → Pages → **Custom domain** 에 `ops.내도메인.com` 입력 → Save
3. **Enforce HTTPS** 체크 (인증서는 GitHub 이 무료로 자동 발급)

---

## ⚠️ 공개 저장소입니다 — 이것만 지켜주세요

이 저장소와 사이트는 **주소를 아는 누구나 볼 수 있습니다.**
(접근 제한이 걸린 Pages 는 GitHub Enterprise 에서만 됩니다.)

그래서 이 패키지는 이미 안전하게 정리해서 보냅니다:

- 「노출된 비밀키」 건은 **「보안 점검 진행 중」으로 문구를 바꿔** 넣었습니다. 원문은 이 저장소에 없습니다.
- **슬랙 대화 원문(최근 대화 피드)은 아예 싣지 않습니다.** 워크플로의 `PUBLIC_MODE: "1"` 이 매번 걸러냅니다.
- 미수금은 전액 수금 완료라 금액 노출 이슈가 없습니다.

**앞으로 지키실 것 하나:** `data/ops.base.json` 에 **남이 보면 안 되는 내용을 적지 마세요.**
공개 저장소라 적는 즉시 그대로 노출됩니다. 민감한 항목은 NAS/로컬 사본에서만 관리하세요.

---

## 고치기

| 고치고 싶은 것 | 어디를 |
|---|---|
| 사람 · 조직 · 칸반 · 막힌 것 · 빈 자리 | `data/ops.base.json` |
| 화면 디자인 | `index.html` |
| 수집 주기 | `.github/workflows/refresh.yml` 의 `cron` |
| 수집 로직 | `refresh.py` |

`data/ops.base.json` 을 고쳐서 저장(commit)하면 그때 바로 한 번 수집이 돌고 반영됩니다.

**자리 상태는 손대지 마세요 — 자동입니다.** 최근 72시간 안에 슬랙에서 발언한 계정은
초록 배지(연결됨), 앱만 있고 발언이 없으면 회색 점선(미연결)으로 그려집니다.
지금 하위 부서 5종이 회색인 건 아직 distribute 안 된 실제 상태입니다.

---

## 안 될 때

| 증상 | 확인할 것 |
|---|---|
| Actions 로그 `invalid_auth` | `SLACK_TOKEN` 비밀값이 틀렸거나 만료 → 재발급 |
| Actions 로그 `not_in_channel` | 그 채널에 앱을 `/invite` 안 함 |
| 특정 채널만 0건 | 비공개 채널인데 초대 누락, 또는 `groups:history` 권한 빠짐 |
| 페이지는 뜨는데 데이터가 그대로 | Actions 가 한 번도 안 돌았음 → 수동 실행 |
| 좌상단이 `연결 끊김` | `data/ops.json` 을 못 읽음 → Pages 경로/`.nojekyll` 확인 |
| 404 | Pages 설정에서 Branch 가 `main` / `root` 인지 확인 |
