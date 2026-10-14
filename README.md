# JARVIS — 개인 맞춤 AI 에이전트

Claude API(Opus 5.5) 위에 **장기 기억 · 목표/계획 · 도구 실행 · 상황 인식**을 얹은 개인 비서 에이전트입니다.

> "ASI급"은 모델을 직접 학습시켜 얻는 게 현실적이지 않습니다. 대신 이미 최상위 추론 능력을 가진 프런티어 모델을 두뇌로 쓰고,
> 그 주변에 **나를 아는 기억**, **목표를 끝까지 추적하는 계획**, **실제로 행동하는 도구**를 붙이는 것이 개인이 만들 수 있는 가장 강한 구조입니다.
> 성능의 상한은 모델이, *개인화와 실행력*은 이 저장소가 담당합니다.

## 아키텍처

```
 사용자 ──► CLI (jarvis/cli.py)
              │
              ▼
        Agent loop (jarvis/agent.py) ◄────────── 승인 콜백 (위험 도구는 사용자 확인)
          │   ▲           │
          │   │           ├─► Claude API: adaptive thinking, effort=high,
          │   │           │   web_search(서버 도구), 거절 시 서버측 fallback
          │   │           │
          │   └── tool_result ◄── Tool registry (jarvis/tools/)
          │                         remember / recall / forget / update_profile
          │                         add_goal / save_plan / update_status / list_goals
          │                         calculate (정확한 계산) / run_python (승인 필요)
          ▼
   <situation> 블록 (jarvis/prompts.py)
     현재 시각 · 프로필 · 약점/강점 · 진행 중 목표와 다음 단계
          ▲
          └── MemoryStore (jarvis/memory.py, SQLite: ~/.jarvis/memory.db)
```

| 요구사항 | 구현 방식 |
|---|---|
| 정확한 분석 · 판단 | Opus 5.5 + adaptive thinking(effort `high`), 확신도 구분을 시스템 프롬프트로 요구, 최신 사실은 웹 검색 |
| 광범위한 계획 · 추론 · 가능성 판단 | 목표 → 단계별 계획을 DB에 저장, 매 턴 "다음 단계"를 컨텍스트로 주입해 세션을 넘어 추적 |
| 부족한 능력 보조 | `weakness` 기억을 매 턴 주입 → 해당 영역은 더 꼼꼼히 처리하거나 놓칠 부분을 먼저 짚음 |
| 계산 | `calculate`(AST 기반 안전 평가), 복잡한 건 `run_python`(서브프로세스, 매번 승인) |
| 상황 맞춤 서비스 추천 · 작업 | 상황 블록(시간·프로필·목표) 기반 다음 행동 제안, 도구로 직접 수행. 외부 부작용이 있는 도구는 승인 게이트 통과 필수 |

설계 포인트
- **프롬프트 캐싱**: 시스템 프롬프트와 도구 목록은 바이트 단위로 고정, 변하는 상황 정보는 사용자 턴에 넣어 캐시가 깨지지 않게 했습니다.
- **append-only 대화 기록**: thinking 블록은 만들어진 대화 이력에 묶여 있어서 이전 턴을 수정하지 않습니다. 중간에 실패한 턴만 통째로 롤백합니다.
- **도구 추가가 쉬움**: `registry.tool(...)` 데코레이터 한 줄로 새 기능(캘린더, 메일, 파일 등)을 붙이고 `risk="confirm"`으로 승인 대상을 지정합니다.

## 상시 음성 모드 (전원 ON)

`jarvis listen`을 켜두면 마이크를 계속 듣습니다.

```
마이크 ─► VAD(발화 구간 분리) ─► 화자 인증(ECAPA 성문 vs 내 목소리)
                                   ├─ 다른 사람: 개수만 세고 버림 (전사·저장 안 함)
                                   └─ 나: Whisper로 전사
                                         ├─ "자비스, …"로 시작: 명령으로 실행하고 음성으로 답함
                                         └─ 그 외: 음성 메모로 저장 (비슷한 말을 또 하면 언급 횟수 +1)
15분마다 또는 메모 20개가 쌓이면 ─► 자비스가 메모 판단 (Claude Haiku 5.5 구조화 출력)
   · 메모별 중요도 1~5, 장기 기억으로 승격 / 메모로 유지 / 삭제
   · 말 속에서 새 목표와 단계별 계획을 만들고, 기존 계획 진행 상황을 갱신
   · 유지된 메모는 중요도별 보관 기간(1일·3일·7일·30일·영구)이 지나면 자동 삭제.
     다시 언급하면 기간이 늘어나고 재판단을 받음
매일 07:30 ─► 아침 브리핑 (마감 임박 목표, 오늘 할 일 3가지, 어제 한 말 중 챙길 것) 음성 출력
```

- 성문 등록: `jarvis enroll` (예문 5개를 4초씩 읽음, `~/.jarvis/voiceprint.npy`)
- 모든 음성 처리는 로컬에서 합니다(STT faster-whisper, 화자 인증 SpeechBrain). 외부로 나가는 건 판단·대화를 위한 텍스트뿐입니다.
- 사람이 키보드 앞에 없는 모드라서 승인이 필요한 도구(`run_python`)는 자동으로 거절됩니다.
- 화자 인증 임계값은 `JARVIS_OWNER_THRESHOLD`(기본 0.5)로 조정합니다. 다른 사람이 내 메모로 들어오면 올리고, 내 말이 빠지면 내립니다.

## 지식 수집 (매일)

매일 06:30에 관심 분야의 새 글과 논문을 모아 로컬 지식 DB(`~/.jarvis/knowledge.db`)에 쌓습니다. 자비스는 대화 중 `search_knowledge`로 검색해 근거(링크)와 함께 답하고, 아침 브리핑에서 "볼 만한 새 자료"를 골라 알려줍니다.

| 분야 | 기본 수집처 |
|---|---|
| IT·AI | GeekNews(본문 포함), Hacker News, arXiv cs.AI·cs.LG |
| 수학 | Quanta Magazine(본문 포함), arXiv math.HO |
| 기계공학 | arXiv cs.RO(로보틱스)·eess.SY(제어), IEEE Spectrum |
| 일상 | 연합뉴스 최신 |

- 수집처는 처음 실행 때 생기는 `~/.jarvis/sources.json`을 고쳐서 추가하거나 뺍니다. RSS/Atom 피드면 됩니다.
- **예의 바른 수집**: 피드 위주로 읽고, 본문을 받을 때는 robots.txt를 지키며, 같은 사이트에 1초 간격을 두고, 이미 받은 글은 건너뜁니다. 90일 지난 글은 지웁니다(`JARVIS_KNOWLEDGE_KEEP_DAYS`).
- **안전한 수집**: 공개 인터넷 주소의 http(s)만 가져옵니다(리다이렉트 포함). 피드 링크가 `file://`이거나 내 PC·공유기 같은 사설망을 가리키면 가져오지도 저장하지도 않습니다. 그래서 로컬·사설망 피드는 수집처로 쓸 수 없습니다.
- 아침 브리핑은 수집한 글 제목을 읽으므로 **읽기 전용 도구만** 씁니다. 글 속 문장이 기억이나 일정을 바꾸게 할 수 없습니다.
- 검색은 SQLite FTS5 trigram이라 한국어 단어 안쪽까지 찾습니다. 의미 기반 검색(임베딩)은 로드맵 v0.4에 있습니다.
- 수집한 글은 신뢰할 수 없는 데이터로 취급하도록 시스템 프롬프트에 명시해, 글 속 지시문에 자비스가 휘둘리지 않게 했습니다.
- 지금 바로 받으려면 `jarvis collect`. 본문 추출 품질을 높이려면 `pip install trafilatura`(없으면 기본 추출기 사용).

## 일정과 알림

외부 캘린더 없이 자비스가 일정을 직접 관리합니다.
- 채팅이나 음성으로 "목요일 3시 치과"라고 하면 자비스가 알아서 일정에 넣고, 날짜·요일·시간을 한 줄로 확인해 줍니다. 옮기기, 취소, 완료도 말로 합니다.
- 웹 앱의 📅 일정 화면에서 직접 추가하고, 완료·취소할 수 있습니다.
- 알림 시각(기본 10분 전)이 되면 알려 줍니다.
  - 상시 모드(`jarvis listen` 또는 `serve --listen`): PC가 소리로 말합니다.
  - 열려 있는 웹 앱: 화면 알림과 브라우저 알림을 띄웁니다. "알림 켜기"를 한 번 눌러야 하고, 앱이 열려 있을 때만 받습니다.
- 오늘·내일 일정은 매 답변의 상황 정보와 아침 브리핑에 들어갑니다. 그래서 "오늘 뭐 할까?"에 일정을 고려해 답하고, 겹치거나 빠듯한 일정을 짚어 줍니다.
- 시간은 PC의 현지 시각으로 저장합니다(`data/memory.db`의 `schedule` 테이블).

## 능동 사고 (스스로 챙기기)

하루 몇 번(기본 12:30, 18:00, 21:30) 자비스가 아무도 묻지 않아도 상황을 돌아봅니다. 목표와 계획 진척, 앞으로 7일 일정, 메모, 새 자료, 최근에 한 알림을 함께 봅니다. 그다음 지금 알려 줄 가치가 있는 것만 최대 3개 고릅니다.
- 마감 위험, 일정 겹침이나 빠듯한 이동, 시작해야 할 계획 단계, 말해 놓고 놓치고 있는 일, 목표와 직결된 새 자료 등을 고릅니다.
- 할 말이 없으면 아무것도 남기지 않고, 이미 한 말은 상황이 바뀌지 않는 한 되풀이하지 않습니다.
- 결과는 웹 앱의 🔔 자비스 알림에 쌓이고, 화면 알림과 브라우저 알림이 뜹니다. 상시 모드에서는 중요한 것(우선순위 2 이상)을 소리로 말합니다.
- PC가 꺼져 있어 놓친 시각은 3시간 안이면 켜졌을 때 실행합니다.
- 웹 앱의 "지금 생각해 보기" 버튼이나 `jarvis reflect`로 바로 실행할 수 있습니다. 시각은 `JARVIS_REFLECT_TIMES`로 바꾸고, 빈 값이면 끕니다.
- 비용: 실행 1번에 Claude Sonnet 5.5 구조화 출력 호출 1번(effort medium)이 듭니다. 네트워크 오류로 실패하면 10분 뒤 다시 시도합니다.

## 시각 (객체탐지)

카메라와 객체탐지 모델을 자비스의 "눈"으로 연결합니다. `JARVIS_CAMERA=0`(웹캠 번호 또는 RTSP/HTTP 주소)을 설정하면 켜집니다.

- **탐지기 교체 가능**: 기본은 Ultralytics YOLO(`yolo11n.pt`)입니다. CCTV 프로젝트에서 학습한 가중치는 `JARVIS_DETECTOR_WEIGHTS=/path/best.pt`로 바로 쓸 수 있습니다. YOLO가 아닌 모델은 `jarvis/vision/base.py`의 `Detector`(`detect(frame) -> list[Detection]`)만 구현하면 됩니다.
- **장면 추적**: 프레임마다 흔들리는 탐지 결과를 디바운스해서 "사람 등장/사라짐" 같은 안정된 사건으로 바꿉니다(`SceneTracker`). 사건은 기록되고, 현재 장면과 함께 매 턴 상황 블록에 들어갑니다.
- **`look` 도구**: 대화 중 자비스가 직접 카메라를 봅니다. 탐지 라벨로 부족하면(글자, 표정, 행동) 프레임 이미지를 Claude가 직접 확인합니다. 이미지는 이때만 외부로 나갑니다.
- **상시 모드 연동**: `jarvis listen`이 2초마다 장면을 갱신하고, 아침 브리핑은 카메라 앞에 사람이 확인될 때까지 기다렸다가 읽어줍니다.

## 실행

```bash
pip install -e '.[dev]'           # 음성·시각까지: '.[dev,voice,vision]'
export ANTHROPIC_API_KEY=sk-ant-...
jarvis                            # 대화형
jarvis "이번 주 목표 정리해줘"        # 원샷
jarvis enroll                     # 내 목소리 등록
jarvis listen                     # 상시 음성 모드 (--no-voice: 브리핑·정리만)
jarvis brief                      # 지금 아침 브리핑
jarvis consolidate                # 지금 메모 정리
jarvis collect                    # 지금 자료 수집
jarvis reflect                    # 지금 스스로 상황 점검
jarvis serve                      # 웹 앱 서버 (pip install -e '.[web]')
pytest                            # 테스트 (네트워크·마이크 불필요)
```

환경 변수: `JARVIS_MODEL`(대화·음성 명령·브리핑, 기본 `claude-opus-5-5`), `JARVIS_MEMO_MODEL`(메모 판단, 기본 `claude-haiku-5-5`), `JARVIS_REFLECT_MODEL`(능동 사고, 기본 `claude-sonnet-5-5`; 두 값 모두 빈 값이면 `JARVIS_MODEL`), `JARVIS_COMPACT_AT_TOKENS`(대화가 이 토큰 수에 이르면 서버가 요약으로 압축, 기본 `80000`, `0`이면 끔), `JARVIS_EFFORT`(`low`~`max`, 기본 `high`), `JARVIS_HOME`(기본 `~/.jarvis`), `JARVIS_WEB_SEARCH=0`(웹 검색 끄기), `JARVIS_BRIEFING_TIME`(기본 `07:30`, 빈 값이면 끔), `JARVIS_CONSOLIDATE_MIN`, `JARVIS_CONSOLIDATE_AFTER`, `JARVIS_OWNER_THRESHOLD`, `JARVIS_WHISPER_MODEL`(기본 `small`), `JARVIS_LANGUAGE`(기본 `ko`), `JARVIS_CAMERA`(빈 값이면 끔), `JARVIS_DETECTOR_WEIGHTS`(기본 `yolo11n.pt`), `JARVIS_VISION_EVERY_S`(기본 2), `JARVIS_COLLECT_TIME`(기본 `06:30`, 빈 값이면 끔), `JARVIS_KNOWLEDGE_KEEP_DAYS`(기본 90).

CLI 명령: `/profile`, `/memories [검색어]`, `/goals`, `/memos`, `/new`, `/think`(추론 요약 표시), `/quit`.

## USB 휴대형 + 웹 앱 (PWA)

JARVIS를 USB에 담아 어느 PC에서든 실행하고, 폰·태블릿·다른 PC에서는 브라우저로 접속해 **앱처럼 설치**해서 씁니다. 폰은 JARVIS를 직접 돌리지 않고, USB를 꽂은 PC(서버)에 붙는 화면입니다.

**1. USB 만들기** (인터넷 되는 PC에서 한 번)
```bash
python portable/make_usb.py E:\                       # Windows
python portable/make_usb.py /Volumes/USB --download-uv  # uv 동봉: Python 없는 PC에서도 실행
python portable/make_usb.py /media/me/USB --extras web,voice   # 음성 모드까지
```
`<USB>/JARVIS/`에 `app/`(코드), `data/`(기억·지식·토큰, 다시 만들어도 보존), `start-windows.bat` / `start-mac.command` / `start-linux.sh`가 생깁니다.

**2. 아무 PC에서 실행**: 런처를 더블클릭하면 브라우저에 채팅 화면이 자동으로 열립니다. 첫 실행 때 Python과 패키지를 그 PC의 캐시에 설치합니다. USB 파일 시스템은 가상환경을 담지 못하기 때문입니다. 그다음 서버를 띄우고 `http://127.0.0.1:8765/#token=...` 주소를 보여 줍니다. API 키를 물을 때 USB에 저장하면 다음부터 묻지 않지만, USB를 잃어버리면 키도 노출되니 선택입니다.

**3. 폰에 설치**
- 같은 와이파이에서 쓰려면 `start-* --host 0.0.0.0`으로 띄우고, 표시된 LAN 주소를 폰에서 엽니다.
- 홈 화면 설치와 마이크는 브라우저가 **HTTPS에서만** 허용합니다. 가장 쉬운 방법은 PC와 폰에 [Tailscale](https://tailscale.com)을 깔고 PC에서 `tailscale serve 8765`를 실행하는 것입니다. 그러면 `https://<pc이름>.<tailnet>.ts.net/#token=...`으로 집 밖에서도 접속됩니다.
- 열린 페이지에서 "홈 화면에 추가"(iOS Safari) 또는 "앱 설치"(Android Chrome)를 누르면 됩니다.

웹 앱은 챗봇처럼 씁니다.
- 답변이 생성되는 대로 실시간으로 표시됩니다(스트리밍). 표·코드 블록 같은 마크다운도 보이고, 코드는 복사 버튼으로 복사합니다.
- 대화 목록은 오늘/어제/지난 7일로 묶입니다. 대화는 PC의 `data/memory.db`에 저장되어 새로고침·재시작 후에도, 다른 기기에서도 이어서 할 수 있습니다.
- 자비스가 쓴 도구(웹 검색, 기억, 계산 등)는 답변 위에 표시됩니다.
- 🎤 음성 입력(브라우저 음성 인식), 답변 읽어 주기
- "메모로 저장": 음성 모드 메모와 같은 판단·정리 대상이 됩니다.
- 목표·계획·메모 보기

모든 API는 첫 실행 때 만든 토큰(`data/web_token`)이 있어야 합니다. 토큰을 바꾸려면 파일을 지우고 다시 실행하세요.

`jarvis serve [--host 0.0.0.0] [--port 8765] [--listen] [--open]`: `--open`은 브라우저를 엽니다. `--listen`을 붙이면 웹 서버와 상시 음성 모드를 함께 돌립니다.

## 로드맵

| 단계 | 내용 |
|---|---|
| **v0.1** | 에이전트 루프, 장기 기억/프로필, 목표·계획 추적, 정확한 계산, 웹 검색, 승인 게이트, CLI |
| **v0.1.1** | 상시 음성 모드: 화자 인증, 음성 메모, 중요도 기반 자동 정리·삭제, 대화 기반 자동 계획, 음성 명령, 아침 브리핑 |
| **v0.1.2** | 시각: 교체 가능한 객체탐지기(YOLO/CCTV 모델), 장면 추적, `look` 도구, 사람 감지 기반 브리핑 |
| v0.1.3 | 매일 지식 수집(IT·수학·기계공학·일상), 전문 검색 도구, 브리핑에 새 자료 반영 |
| v0.1.4 | USB 휴대형 실행(Windows/macOS/Linux 런처), 설치형 웹 앱(PWA), 챗봇형 채팅(실시간 스트리밍, 대화 기록 저장·이어하기), 토큰 인증 |
| v0.2 | 내장 일정: 대화에서 자동 등록, 음성·브라우저 알림, 상황·브리핑 반영, 웹 일정 화면 |
| v0.3 | 능동 사고: 하루 몇 번 스스로 상황을 돌아보고 챙길 것만 알림(음성·웹), 즉시 실행 버튼 |
| **v0.3.1 (현재)** | 보안·안정성 수정(수집기 SSRF 차단, 브리핑 읽기 전용, 알림·능동 사고 버그), 작업별 모델 분리(메모 판단 Haiku, 능동 사고 Sonnet), 긴 대화 자동 압축(compaction) |
| v0.3.x 연결 | 필요해지면 MCP로 외부 서비스(메일·노션·GitHub, 원하면 구글 캘린더 동기화) 연결 |
| v0.3.x 회고 | 주간 회고, 대화 종료 시 자동 기억 정리·요약 |
| v0.4 기억 고도화 | 임베딩 기반 의미 검색, 기억 중요도 감쇠/병합 |
| v0.5 인터페이스 | 자연스러운 TTS, 끼어들기(barge-in), 웹/모바일 UI, 알림 채널(텔레그램·슬랙) |
| v0.6 다중 에이전트 | 리서치·코딩·일정 담당 하위 에이전트로 분업, 결과를 메인 에이전트가 검증 |

## 구조

```
jarvis/
  agent.py      에이전트 루프 (도구 실행, 승인, pause/refusal/max_steps 처리, 롤백)
  memory.py     SQLite 기억 저장소 (profile / memories / goals / plan_steps / memos / schedule / chats)
  prompts.py    시스템 프롬프트, 매 턴 상황 블록
  tools/        도구 레지스트리와 기본 도구
  consolidate.py 음성 메모 판단 → 기억 승격·삭제, 목표·계획 생성
  daemon.py     상시 모드 (듣기, 명령, 주기적 정리, 아침 브리핑)
  briefing.py   아침 브리핑 프롬프트와 스케줄
  voice/        VAD, 화자 인증(성문), STT/TTS/마이크 백엔드
  vision/       카메라, 객체탐지기, 장면 추적, look 도구
  knowledge/    수집처 설정, 피드 수집기, 지식 DB와 검색 도구
  web/          웹 API(FastAPI)와 설치형 웹 앱(PWA)
  cli.py        터미널 UI와 하위 명령
portable/       USB 빌더(make_usb.py)와 OS별 런처
tests/          가짜 클라이언트로 루프 전체를 검증하는 단위 테스트
```
