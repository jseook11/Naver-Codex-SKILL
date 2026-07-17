# 설치와 업데이트

## 에이전트에게 설치 맡기기

Agent Skills를 지원하는 에이전트에게 저장소 주소와 설치할 스킬 이름을 알려줍니다.

> https://github.com/jseook11/naver-map-serp.git 저장소에서 `search-naver-map` 스킬을 설치해줘. Python 실행 환경을 스킬 폴더 안에 준비하고 `bin/naver-place capabilities --json`까지 확인해줘.

설치가 끝나면 아래 두 명령이 성공해야 합니다.

```bash
cd "<설치된 search-naver-map 폴더>"
python3 scripts/bootstrap.py
bin/naver-place capabilities --json
```

## 직접 설치하기

Python 3.10 이상이 필요합니다.

```bash
git clone https://github.com/jseook11/naver-map-serp.git
cd naver-map-serp/search-naver-map
python3 scripts/bootstrap.py
bin/naver-place capabilities --json
```

에이전트가 읽는 스킬 폴더에 `search-naver-map` 디렉터리를 복사하거나 심볼릭 링크로 연결할 수도 있습니다.

자주 쓰는 경로:

- Codex: `$CODEX_HOME/skills/search-naver-map` 또는 `~/.codex/skills/search-naver-map`
- Claude Code: `~/.claude/skills/search-naver-map`
- Agent Skills 공용 경로: `~/.agents/skills/search-naver-map`

에이전트마다 스킬 탐색 규칙이 다를 수 있습니다. 설치 후에는 새 세션에서 스킬이 목록에 나타나는지 확인하세요.

## 실행 환경

`scripts/bootstrap.py`는 다음 작업을 수행합니다.

- 스킬 폴더 안에 `.venv` 생성
- `requirements.txt`의 고정된 버전 설치
- 명령 목록을 읽을 수 있는지 확인
- 이미 같은 환경이 준비됐다면 불필요한 재설치 생략

시스템 Python에 `pip install`하지 마세요. 설치에 실패하면 출력된 명령과 오류를 그대로 확인하는 편이 원인 파악에 유리합니다.

개발용 테스트 도구가 필요하면 다음 명령을 사용합니다.

```bash
python3 scripts/bootstrap.py --dev
```

현재 환경이 파일과 일치하는지만 확인하려면:

```bash
python3 scripts/bootstrap.py --check
python3 scripts/bootstrap.py --dev --check
```

준비한 환경이 개발용이라면 `--dev --check`를 사용해야 합니다.

## 업데이트

Git으로 내려받았다면 저장소를 업데이트한 뒤 준비 스크립트를 다시 실행합니다.

```bash
git pull
cd search-naver-map
python3 scripts/bootstrap.py
bin/naver-place capabilities --json
```

스킬 폴더를 복사해 설치했다면 새 버전으로 다시 복사한 뒤 같은 확인 명령을 실행합니다.

## 문제 확인

- `dependency_missing`: `.venv`가 없거나 준비가 끝나지 않았습니다. `python3 scripts/bootstrap.py`를 실행합니다.
- `capabilities --json` 실패: Python 버전과 설치 경로를 확인합니다.
- 네트워크 오류: 설치 문제가 아니라 공개 네이버 요청이 거부되거나 연결되지 않은 상황일 수 있습니다.
- 스킬이 목록에 없음: 사용 중인 에이전트의 스킬 탐색 경로와 폴더 이름을 확인하고 새 세션을 시작합니다.
