# 📬 Ollama 기반 메일 일일 업무 정리 에이전트 (Ollama-mail-summary)

로컬 소형 LLM(Ollama `gemma4:e2b`, VRAM 6GB 환경) 및 사내 폐쇄망 환경에서 안정적으로 동작하는 **LangGraph 기반 메일 원문(.mht) 파싱 및 일일 업무 일지 자동 생성 에이전트 도구**입니다.

---

## 🌟 주요 특징 및 설계 원칙

1. **Java 고속 파서 + Python LangGraph 하이브리드 구조**:
   - 사내 폐쇄망 환경에서 인코딩/메일 포맷 이슈를 완벽하게 해결하기 위해 **순수 Java SE 기반 메일 파서(`MailParser.java`)**를 1순위로 연동합니다.
   - 외부 라이브러리(Maven/Gradle) 전혀 없이 JDK 11+ 내장 API만으로 단일 실행되며, Java가 없는 환경에서는 Python 내장 파서로 자동 폴백(Fallback)합니다.
2. **규칙 우선 및 LLM 최소 호출 (Cost & Token Efficiency)**:
   - 결정적인 수집, 파싱, 시스템 알림/식단표 제외, 명확한 업무 키워드 매칭은 **100% 코드로 처리**합니다.
   - 규칙으로 판단할 수 없는 모호한 메일만 **소형 LLM(gemma4:e2b)**에 구조화 출력(`with_structured_output`, `json_schema`)으로 1회 질의합니다.
3. **보수적 누락 방지 (Conservative Fallback)**:
   - LLM 오류, 네트워크 지연, 미판정 메일은 절대 버리지 않고 `UNCERTAIN`("확인 필요")으로 안전하게 보존합니다.
4. **판정 근거 보존**:
   - 모든 메일에 판정 주체(`decided_by`: static | rule | llm | fallback), 규칙명(`rule_name`), 판단 근거(`evidence`)를 함께 기록합니다.
5. **수신 / 발신 폴더 분리 및 본인 식별**:
   - 수신(`inbox`), 발신(`sent`) 폴더 스캔 및 본인 이메일(`MY_EMAIL`) 대조를 통해 "주요 발신/수행 업무"와 "수신 협업/요청"을 정확히 분류합니다.

---

## 📂 프로젝트 구조

```
impact_chat/
├── config.py                 # 공통 환경 설정 (Ollama 호스트, 모델, 폴더 경로 등)
├── requirements.txt          # 최소 의존성 패키지 명세
├── .gitignore                # 민감 정보 및 대용량 캐시 제외
├── README.md                 # 프로젝트 문서
└── tools/
    ├── __init__.py           # 챗봇 도구 레지스트리 (register)
    ├── MailParser.java       # ★ 순수 Java 고속 메일 파서 (외부 의존성 제로)
    └── mail_work_summary.py  # ★ 메일 파싱 및 일일 업무 정리 LangGraph 단일 파일
```

---

## 🚀 빠른 시작

### 1. 필수 패키지 설치
```bash
pip install -r requirements.txt
```

### 2. 환경 설정 (`config.py`)
사내망 환경 및 본인 계정에 맞게 환경 변수 또는 `config.py`를 설정합니다:
```python
# config.py
OLLAMA_HOST = "http://localhost:11434"
JUDGE_MODEL = "gemma4:e2b"
MY_EMAIL = "developer@company.com"
MAIL_INBOX_DIR = r"C:\mail_archive\inbox"
MAIL_SENT_DIR = r"C:\mail_archive\sent"
MAIL_EXTENSIONS = [".eml", ".mht", ".mhtml", ".txt", ".mail"]  # 대상 확장자 설정 (자유롭게 추가/변경 가능)
```

### 3. CLI 단독 실행 테스트

#### (1) 규칙 기반 단독 실행 (Ollama 불필요)
```bash
# 기본 설정된 확장자로 실행
python tools/mail_work_summary.py --target-date 2026-10-06 --no-llm

# 특정 확장자만 지정하여 실행 (예: .eml)
python tools/mail_work_summary.py --target-date 2026-10-06 --extensions .eml --no-llm
```

#### (2) 소형 LLM 연동 실행
```bash
python tools/mail_work_summary.py --target-date 2026-10-06
```

#### (3) 캐시 초기화
```bash
python tools/mail_work_summary.py --clear-cache
```

---

## 🤖 챗봇 도구 연동 방법

기존 챗봇 시스템(`app.py` 등)에서 아래와 같이 간단히 도구를 등록하여 활용할 수 있습니다:

```python
from tools.mail_work_summary import summarize_daily_work_mail

# 에이전트 바인딩 (LLM에는 단일 도구만 노출)
tools = [summarize_daily_work_mail]
```

---

## 📊 결과 리포트 예시

```markdown
# 📅 일일 업무 내역 정리 (2026-10-06)

## 📊 업무 처리 요약
- **전체 분석 메일**: 5건
- **업무 내역 반영**: 2건 (발신 보고 1건 / 수신 요청 1건)
- **확인 필요(모호/오류)**: 1건
- **제외 메일**: 2건

## 1. 오늘 주요 업무 핵심 요약
- **[발신]** [보고] API 서버 튜닝 및 Redis 캐시 적용 완료의 건 (근거: 업무 키워드 발견: '보고')
- **[수신]** [검토 요청] 4분기 서비스 배포 일정 및 리소스 점검 요청 (발신: 이팀장, 근거: 업무 키워드 발견: '검토 요청')

## 2. 주요 발신 및 수행 업무 (Sent)
### 2-1. [보고] API 서버 튜닝 및 Redis 캐시 적용 완료의 건
- **일시**: 2026-10-06 10:30:00
- **수신자**: teamleader@company.com
- **판정 방식**: `rule` (work_keyword_match)
- **내용 요약**: 팀장님, 요청하신 API 응답속도 개선을 위해 Redis 캐시 적용...

## 3. 수신 요청 및 협업 대응 내역 (Received)
### 3-1. [검토 요청] 4분기 서비스 배포 일정 및 리소스 점검 요청
- **일시**: 2026-10-06 09:15:00
- **발신자**: 이팀장 <teamleader@company.com>
- **판정 방식**: `rule` (work_keyword_match)
- **내용 요약**: 안녕하세요. 4분기 운영 배포 관련하여 스테이징 환경 리소스...

## 4. ⚠️ 확인 필요 항목 (UNCERTAIN)
- **[INBOX] 오늘 오후 3시에 커피 한잔 어떠세요?**
  - 발신: 동료개발자 | 일시: 2026-10-06 11:10:00
  - 근거: `LLM 판정 오류 발생 또는 LLM 미사용, 확인 필요 배정`
```
