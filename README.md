# aoe-subscription-usage

[Agent of Empires](https://github.com/agent-of-empires/agent-of-empires)(AoE) 대시보드에서 Codex·Claude·Grok 같은 구독의 남은 사용량을 보는 플러그인입니다. 에이전트를 여러 개 띄워 두면 어느 구독이 먼저 바닥나는지 알기 어려워서, [OpenUsage](https://github.com/robinebers/openusage)와 [CodexBar](https://github.com/steipete/CodexBar)가 이미 모아 둔 값을 AoE 화면에 붙였습니다.

An AoE plugin that shows subscription usage (Codex, Claude, Grok, …) on the dashboard, using data from OpenUsage and the CodexBar CLI. UI text is Korean.

## 화면에 붙는 것

| 위치 | 내용 |
|---|---|
| 대시보드 첫 화면 패널 | 서비스별 요약, 한도(5시간 세션·주간·모델 전용 한도)마다 사용률 막대와 "10/4(일) 11:41 초기화 · 3일 4시간 남음", 지금 속도로 초기화 전에 소진되는지, 오늘·최근 30일 비용, 30일 사용 추이 |
| 상태줄 | `Codex 49% · Claude 21% · Grok 11%`, 마우스를 올리면 한도별 수치 |
| 세션 목록 | 각 세션 행에 그 에이전트의 사용률 배지 (`tool`이 codex·claude·grok·gemini/antigravity·opencode인 세션) |

한도에 가까워지거나 초기화 전에 소진될 것 같으면 패널 맨 위에 경고가 뜹니다.

## 필요한 것

- AoE 1.17 이상, `aoe serve`로 띄운 웹 대시보드
- Python 3.10 이상 (표준 라이브러리만 사용)
- 데이터 출처 중 하나 이상
  - **OpenUsage** macOS 앱: 로컬 API `http://127.0.0.1:6736/v1/usage`에서 플랜, 한도, 비용, 추이를 읽습니다. 서비스마다 계정 하나씩만 보입니다.
  - **CodexBar** CLI (`codexbar`): 계정별 한도와 소진 예상(pace)을 읽습니다. Codex는 `--all-accounts`로 여러 계정을 가져옵니다. Antigravity는 Antigravity 앱이 켜져 있어야 읽힙니다.

## 설치

```bash
aoe plugin install gh:hyunwook711/aoe-subscription-usage
# 또는 받아 둔 폴더에서
aoe plugin install ./aoe-subscription-usage
aoe serve --restart   # 이미 떠 있는 데몬은 새 플러그인을 다시 시작해야 읽습니다
```

요청하는 권한: `runtime.worker`, `net`(OpenUsage 로컬 API), `process.spawn`(CodexBar CLI 실행), `session.read`(세션 행 배지를 붙일 세션 목록).

## 설정

대시보드 설정 → Subscription Usage

| 키 | 기본값 | 설명 |
|---|---|---|
| `openusage_url` | `http://127.0.0.1:6736/v1/usage` | 비우면 OpenUsage를 쓰지 않음 |
| `codexbar_providers` | `codex,claude,grok` | CodexBar로 읽을 서비스 |
| `codexbar_refresh_secs` | 300 | CodexBar는 웹 조회가 섞여 느리므로 따로 주기를 둠 |
| `refresh_secs` | 30 | 화면 갱신 주기 |
| `warn_percent` / `danger_percent` | 75 / 90 | 색 기준 |
| `mask_accounts` | true | 계정 이메일 앞 세 글자만 표시 |
| `session_badges` | true | 세션 행 배지 표시 |

## 동작 방식

워커는 AoE 플러그인 규약대로 stdin/stdout의 줄 단위 JSON-RPC로 호스트와 이야기합니다. `config.get`으로 설정을 읽고, `sessions.list`로 세션을 받아 배지를 붙이고, `ui.state.set`으로 `home-pane`·`status-bar`·`row-badge`를 갱신합니다. CodexBar는 별도 스레드에서 느린 주기로 돌리고, 화면은 마지막 값을 씁니다.

같은 서비스가 두 출처에 모두 있으면 한도와 계정은 CodexBar 값을, 플랜·비용·추이는 OpenUsage 값을 씁니다.

## 한계

- 사용량 수치는 OpenUsage와 CodexBar가 각 서비스에서 읽어 온 값을 그대로 보여줍니다. 이 플러그인이 직접 서비스에 로그인하거나 토큰을 읽지는 않습니다.
- 한도가 차도 계정을 자동으로 바꾸지는 않습니다.
- 화면 문구는 한국어만 있습니다.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

네트워크 없이 표시 형식, 두 출처 병합, 경고, JSON-RPC 응답을 확인합니다.

## License

MIT
