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

## 실행

```bash
pip install -e '.[dev]'
export ANTHROPIC_API_KEY=sk-ant-...
jarvis                       # 대화형
jarvis "이번 주 목표 정리해줘"   # 원샷
pytest                       # 테스트 (네트워크 불필요)
```

환경 변수: `JARVIS_MODEL`(기본 `claude-opus-5-5`), `JARVIS_EFFORT`(`low`~`max`, 기본 `high`), `JARVIS_HOME`(기본 `~/.jarvis`), `JARVIS_WEB_SEARCH=0`(웹 검색 끄기).

CLI 명령: `/profile`, `/memories [검색어]`, `/goals`, `/new`, `/think`(추론 요약 표시), `/quit`.

## 로드맵

| 단계 | 내용 |
|---|---|
| **v0.1 (현재)** | 에이전트 루프, 장기 기억/프로필, 목표·계획 추적, 정확한 계산, 웹 검색, 승인 게이트, CLI |
| v0.2 연결 | MCP 클라이언트로 캘린더·메일·노션·GitHub 등 외부 서비스 연결 → "지금 상황"을 실제 일정/할 일로 판단 |
| v0.3 능동성 | 스케줄러(아침 브리핑, 마감 임박 알림, 주간 회고), 대화 종료 시 자동 기억 정리·요약 |
| v0.4 기억 고도화 | 임베딩 기반 의미 검색, 기억 중요도 감쇠/병합, 대화 compaction으로 긴 세션 유지 |
| v0.5 인터페이스 | 음성 입출력(STT/TTS), 웹/모바일 UI, 알림 채널(텔레그램·슬랙) |
| v0.6 다중 에이전트 | 리서치·코딩·일정 담당 하위 에이전트로 분업, 결과를 메인 에이전트가 검증 |

## 구조

```
jarvis/
  agent.py      에이전트 루프 (도구 실행, 승인, pause/refusal/max_steps 처리, 롤백)
  memory.py     SQLite 기억 저장소 (profile / memories / goals / plan_steps)
  prompts.py    시스템 프롬프트, 매 턴 상황 블록
  tools/        도구 레지스트리와 기본 도구
  cli.py        터미널 UI
tests/          가짜 클라이언트로 루프 전체를 검증하는 단위 테스트
```
