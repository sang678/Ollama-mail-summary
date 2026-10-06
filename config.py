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

# 메일 아카이브 파일 경로 및 대상 확장자 설정
DEFAULT_DATA_DIR = BASE_DIR / "sample_mails"
MAIL_INBOX_DIR = os.getenv("MAIL_INBOX_DIR", str(DEFAULT_DATA_DIR / "inbox"))
MAIL_SENT_DIR = os.getenv("MAIL_SENT_DIR", str(DEFAULT_DATA_DIR / "sent"))

# 스캔 대상 메일 파일 확장자 (콤마 구분 환경변수 지원, 기본값: .eml, .mht, .mhtml, .txt, .mail)
_raw_exts = os.getenv("MAIL_EXTENSIONS", ".eml,.mht,.mhtml,.txt,.mail")
MAIL_EXTENSIONS = [ext.strip().lower() for ext in _raw_exts.split(",") if ext.strip()]

# 캐시 저장 경로
CACHE_DIR = os.getenv("CACHE_DIR", str(BASE_DIR / ".cache"))
