"""
impact_chat 공통 환경 설정 모듈 (config.py)
모든 환경 설정은 이 파일에서 관리하며, 도구 파일에서 직접 os.getenv를 호출하지 않습니다.
"""

import os
from pathlib import Path

# 기본 디렉토리 기준점 설정
BASE_DIR = Path(__file__).resolve().parent

# Ollama 로컬 LLM 설정 (폐쇄망 사내망 환경)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gemma4:e2b")
USE_LLM = os.getenv("USE_LLM", "true").lower() == "true"

# 사용자 식별용 이메일 (발신/수신 식별 및 업무 분류용)
MY_EMAIL = os.getenv("MY_EMAIL", "developer@company.com")

# ==============================================================================
# 메일 아카이브 실제 폴더 경로 설정 (★ 여기에 실제 폴더 경로를 입력하세요)
# 윈도우 역슬래시(\) 오류 방지를 위해 경로 앞에 r을 붙여주세요.
# 예: MAIL_INBOX_DIR = r"C:\Users\username\Desktop\Mail\Inbox"
# ==============================================================================
MAIL_INBOX_DIR = str(BASE_DIR / "sample_mails" / "inbox")
MAIL_SENT_DIR = str(BASE_DIR / "sample_mails" / "sent")

# (선택) 시스템 환경 변수가 설정되어 있으면 환경 변수 값으로 우선 덮어씀
if os.getenv("MAIL_INBOX_DIR"):
    MAIL_INBOX_DIR = os.getenv("MAIL_INBOX_DIR")
if os.getenv("MAIL_SENT_DIR"):
    MAIL_SENT_DIR = os.getenv("MAIL_SENT_DIR")

# 스캔 대상 메일 파일 확장자 (콤마 구분 환경변수 지원, 기본값: .eml, .mht, .mhtml, .txt, .mail)
_raw_exts = os.getenv("MAIL_EXTENSIONS", ".eml,.mht,.mhtml,.txt,.mail")
MAIL_EXTENSIONS = [ext.strip().lower() for ext in _raw_exts.split(",") if ext.strip()]

# 캐시 저장 경로
CACHE_DIR = os.getenv("CACHE_DIR", str(BASE_DIR / ".cache"))
