# HWP-MCP (헤드리스 `rhwp` 기반 한글/HWPX MCP 서버)

[![GitHub](https://img.shields.io/github/license/jkf87/hwp-mcp)](https://github.com/jkf87/hwp-mcp)

**HWP-MCP**는 한컴오피스(한글) GUI나 COM(`pywin32`)을 전혀 실행하지 않고, Rust 기반 [`rhwp`](https://github.com/edwardkim/rhwp) 엔진으로 `.hwp` 및 `.hwpx` 문서를 **헤드리스(Headless)로 읽고 편집**하는 Model Context Protocol(MCP) 서버입니다.

사용자의 키보드·마우스·포커스·클립보드를 빼앗지 않으며, 사용자가 편집기(HOP 등)에 문서를 띄워둔 상태에서도 백그라운드에서 안전하게 파일을 수정합니다.

---

## 주요 특징

- **한컴오피스 불필요**: 한글 프로그램 설치나 COM 호출 없이 `.hwp`(HWP 5.0) / `.hwpx`(OWPML) 원본 포맷을 그대로 유지하며 읽고 씁니다.
- **SafeWriter 원자적 쓰기**: 모든 쓰기 작업은 다음 6단계를 거칩니다.
  1. 현재 파일 SHA-256 계산 및 `expected_sha256` / 서버 최종 확인 해시 대조 (외부 변경 충돌 감지)
  2. `<문서폴더>/.hwp-mcp-history/<파일명>/<YYYYmmdd-HHMMSS>_<sha8>.<ext>` 자동 스냅샷 백업 (최근 50개 유지)
  3. 같은 폴더의 임시 파일에 `rhwp` 결과 출력 (원본 확장자 유지)
  4. 임시 파일 재열기 및 데이터 손실(`losses == 0`) 검증
  5. `os.replace` 원자적 교체 (`PermissionError` 시 3회 재시도 후 잠금 오류 안내)
  6. 새 SHA-256 및 변경 요약 반환
- **플랜 B 보존**: 기존 COM 기반 구현은 삭제하지 않고 [`legacy/`](legacy/) 폴더에 보존되어 있습니다.

---

## 설치 및 `rhwp` 배치

### 1. Python 의존성 설치
```bash
git clone https://github.com/jkf87/hwp-mcp.git
cd hwp-mcp
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. `rhwp` 프리빌트 바이너리 배치
1. [rhwp v0.9.0 릴리스](https://github.com/edwardkim/rhwp/releases/tag/v0.9.0)에서 `rhwp-v0.9.0-windows-x86_64.zip` 과 `SHA256SUMS.txt` 를 내려받아 해시를 검증합니다.
2. `D:/40_Tools/rhwp/` 아래에 압축을 해제합니다 (`D:/40_Tools/rhwp/rhwp/rhwp.exe`).
   - 다른 경로에 배치한 경우 환경변수 `RHWP_EXE` 로 `rhwp.exe` 경로를 지정할 수 있습니다.

### 3. MCP 클라이언트 등록 (예: Claude Code)
```json
{
  "mcpServers": {
    "hwp": {
      "command": "D:/50_AI/MCP/hwp-mcp/.venv/Scripts/python.exe",
      "args": ["D:/50_AI/MCP/hwp-mcp/hwp_mcp_stdio_server.py"]
    }
  }
}
```

---

## 제공 도구 목록 (총 14개, 모두 `file_path` 필수)

| 도구명 | 분류 | 설명 |
|---|---|---|
| `hwp_info` | 조회 | 문서 형식, 페이지 수, 문단 수, 글꼴, SHA-256 해시 조회 |
| `hwp_get_text` | 조회 | 본문 추출 (`format="text"` 또는 `"markdown"`). `rhwp` 실패 시 내장 `olefile` 리더 폴백 |
| `hwp_search` | 조회 | 문자열 검색 및 페이지·문맥·표 셀 좌표 반환 |
| `hwp_table_view` | 조회 | 표(Table)의 행/열 구조, 병합, 셀 좌표 및 텍스트 반환 |
| `hwp_fill_cells` | 쓰기 | 좌표(`row`, `col`) 또는 라벨(`label`, `direction="right"\|"below"`) 기준 셀 일괄 채우기 |
| `hwp_replace_text` | 쓰기 | 본문 및 표 셀 내 텍스트 일괄 치환 |
| `hwp_fill_fields` | 쓰기 | 누름틀(필드) 이름-값 매핑 채우기 |
| `hwp_insert_text` | 쓰기 | 지정 섹션/문단/오프셋에 텍스트 삽입 |
| `hwp_run_plan` | 쓰기 | `rhwp` 선언적 편집 계획서(JSON) 일괄 실행 |
| `hwp_render_page` | 렌더 | 특정 페이지(또는 전체)를 PNG/SVG 파일로 렌더링해 경로 반환 (시각 확인용) |
| `hwp_history` | 이력 | `.hwp-mcp-history` 스냅샷 목록 조회 |
| `hwp_restore` | 쓰기 | 지정 스냅샷(`snapshot_id`)으로 복원 (현재 상태도 백업 후 원자 교체) |
| `hwp_diff` | 조회 | 두 문서 또는 현재 문서와 스냅샷 간 텍스트 unified diff |
| `hwp_rhwp_call` | 탈출구 | `rhwp` 의 180여 개 전체 도구 직접 호출 (쓰기 계열은 반드시 `SafeWriter` 경유) |

---

## 히스토리·복원 및 HOP(편집기) 연동 흐름

1. **읽기**: 에이전트가 `hwp_info` / `hwp_table_view` / `hwp_get_text` 로 문서를 읽으면 서버가 현재 파일의 SHA-256 을 기록합니다.
2. **편집**: `hwp_fill_cells` 나 `hwp_replace_text` 호출 시 `SafeWriter` 가 원본을 `.hwp-mcp-history/<파일명>/` 에 스냅샷으로 저장한 뒤, 임시 파일 검증을 거쳐 원자적으로 교체합니다.
3. **충돌 방지**: 사용자가 HOP 등 편집기에서 파일을 수정·저장해 해시가 바뀌면 `HashConflictError` 로 덮어쓰기를 차단합니다. (필요 시 다시 읽거나 `force=True` 지정)
4. **파일 잠금 안내**: 편집기가 파일을 배타적으로 잠그고 있으면 3회 재시도 후 명확한 잠금 오류를 반환하며, 원본 스냅샷은 그대로 보존됩니다.
5. **확인 및 복원**: `hwp_diff` 로 변경 내역을 비교하거나 `hwp_render_page` 로 시각 확인을 수행하고, 문제 시 `hwp_history` → `hwp_restore` 로 즉시 되돌릴 수 있습니다.

---

## 프로젝트 구조

```
hwp-mcp/
├── hwp_mcp_stdio_server.py      # 얇은 stdio 진입점
├── requirements.txt             # 의존성 (pywin32/comtypes 는 legacy 전용 주석)
├── src/
│   ├── server.py                # FastMCP 서버 및 14개 도구 구현
│   ├── core/
│   │   ├── rhwp_backend.py      # rhwp.exe mcp-serve 상주 프록시 + CLI 보조
│   │   └── safe_writer.py       # 해시 검증, 스냅샷(50개), 재열기 검증, 원자 교체
│   ├── tools/
│   │   └── hwp_reader.py        # olefile 기반 폴백 텍스트 리더
│   ├── utils/
│   │   └── command_parser.py
│   └── __tests__/               # 기존 파서/리더 단위 테스트
├── tests/                       # SafeWriter 단위, rhwp 통합, MCP E2E 테스트
└── legacy/                      # 기존 한컴 COM 기반 코드 보존 (플랜 B)
```

## 테스트 실행

```bash
.venv\Scripts\python.exe -m pytest src/__tests__ tests -v
```
