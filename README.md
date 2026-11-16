# hwp-mcp (rhwp 백엔드)

[jkf87/hwp-mcp](https://github.com/jkf87/hwp-mcp)를 포크해서, 한컴오피스 COM 자동화 대신 [rhwp](https://github.com/edwardkim/rhwp)로 `.hwp` / `.hwpx` 파일을 직접 읽고 고치도록 바꾼 버전입니다.

한글 프로그램을 띄우지 않기 때문에 작업 중에 키보드나 창 포커스를 가져가지 않고, 문서를 HOP 같은 편집기에 열어 둔 상태에서도 쓸 수 있습니다.

## 쓰기 방식

파일을 고치는 도구는 모두 같은 순서로 저장합니다.

1. 파일의 SHA-256을 마지막으로 읽었을 때 값과 비교합니다. 그사이 다른 프로그램이 파일을 바꿨으면 저장하지 않습니다.
2. 원본을 `<문서 폴더>/.hwp-mcp-history/<파일명>/`에 백업합니다. 최근 50개까지 남깁니다.
3. 같은 폴더의 임시 파일에 결과를 쓰고, 다시 열어서 빠진 내용이 없는지 확인합니다.
4. `os.replace`로 원본과 바꿉니다. 파일이 잠겨 있으면 3번 다시 시도하고, 그래도 안 되면 오류를 돌려줍니다.

원래의 COM 기반 코드는 `legacy/`에 그대로 남겨 두었습니다.

## 설치

```bash
git clone -b feat/rhwp-headless https://github.com/myeongsu-dev/hwp-mcp.git
cd hwp-mcp
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

rhwp는 [v0.9.0 릴리스](https://github.com/edwardkim/rhwp/releases/tag/v0.9.0)에서 `rhwp-v0.9.0-windows-x86_64.zip`을 받아 압축을 풉니다. 서버는 `RHWP_EXE` 환경변수, `D:/40_Tools/rhwp/`, `PATH` 순서로 `rhwp.exe`를 찾습니다.

Claude Code 등록 예시:

```json
{
  "mcpServers": {
    "hwp": {
      "command": "<설치 경로>/.venv/Scripts/python.exe",
      "args": ["<설치 경로>/hwp_mcp_stdio_server.py"]
    }
  }
}
```

## 도구

모든 도구는 `file_path`를 받습니다.

| 도구 | 하는 일 |
|---|---|
| `hwp_info` | 형식, 페이지·문단 수, 글꼴, SHA-256 |
| `hwp_get_text` | 본문을 텍스트나 마크다운으로 추출. rhwp가 실패하면 olefile 리더로 읽음 |
| `hwp_search` | 문자열 검색. 페이지, 앞뒤 문맥, 표 셀 위치를 함께 반환 |
| `hwp_table_view` | 표의 행·열, 병합, 셀 내용 |
| `hwp_fill_cells` | 좌표나 라벨(오른쪽·아래 칸)로 셀 채우기 |
| `hwp_replace_text` | 본문과 표 안 텍스트 치환 |
| `hwp_fill_fields` | 누름틀 채우기 |
| `hwp_insert_text` | 지정한 위치에 텍스트 삽입 |
| `hwp_run_plan` | rhwp 편집 계획(JSON) 한 번에 실행 |
| `hwp_render_page` | 페이지를 PNG/SVG로 렌더링 |
| `hwp_history` | 백업 목록 |
| `hwp_restore` | 백업으로 되돌리기. 현재 상태도 먼저 백업함 |
| `hwp_diff` | 두 문서 또는 현재 문서와 백업 사이의 텍스트 diff |
| `hwp_rhwp_call` | 위에 없는 rhwp 도구를 직접 호출. 쓰기 도구도 같은 저장 순서를 거침 |

편집 중에 HOP에서 같은 파일을 저장하면 해시가 달라져서 `HashConflictError`가 납니다. 다시 읽고 고치거나, 덮어써도 되면 `force=True`를 줍니다.

## 구조

```
hwp_mcp_stdio_server.py   stdio 진입점
src/server.py             MCP 도구 정의
src/core/rhwp_backend.py  rhwp.exe mcp-serve 프로세스 관리, CLI 호출
src/core/safe_writer.py   해시 확인, 백업, 검증, 교체
src/tools/hwp_reader.py   olefile 텍스트 리더
tests/                    SafeWriter 단위 테스트, rhwp 통합, MCP E2E
legacy/                   원래 COM 기반 코드
```

## 테스트

```bash
.venv\Scripts\python.exe -m pytest src/__tests__ tests -v
```
