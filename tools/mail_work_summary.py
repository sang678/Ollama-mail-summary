"""
================================================================================
메일 파싱 기반 일일 업무 내역 정리 LangGraph 에이전트 도구 (mail_work_summary.py)
================================================================================

[목적]
- Windows 사내망(폐쇄망) 환경에서 수신/발신 메일 원문(.mht)을 파싱하여
  특정 일자의 수행 업무 및 수신 요청 내역을 구조화된 일일 업무 일지(Markdown)로 자동 생성합니다.
- 소형 LLM(Ollama gemma4:e2b, VRAM 6GB 환경)의 한계를 고려하여, 결정적 규칙(Rule)으로 
  대부분을 처리하고 모호한 케이스만 최소한으로 LLM에 위임합니다.

[실행법]
1. 단독 CLI 실행 (테스트 및 단독 점검용):
   python mail_work_summary.py --target-date 2026-10-06 --no-llm
   python mail_work_summary.py --inbox-dir ./sample_mails/inbox --sent-dir ./sample_mails/sent

2. 챗봇 에이전트 도구로 활용:
   from impact_chat.tools.mail_work_summary import summarize_daily_work_mail
   # register()를 통해 챗봇 레지스트리에 자동 등록되어 사용됨

[설계 메모 및 주의사항]
- .mht 파일은 MIME 규격이므로 파이썬 내장 `email` 모듈로 외부 라이브러리 없이 안전하게 파싱합니다.
- RFC 2047 인코딩(=?UTF-8?B?...?= 또는 EUC-KR) 헤더를 필수적으로 디코딩해야 한글 깨짐이 없습니다.
- 소형 모델(2B)은 Tool Calling과 다중 질문에 매우 취약하므로 with_structured_output(json_schema)으로
  '단일 질문'만 던지고, 긴 본문은 핵심 요약과 `>>>` 마커로 한정합니다.
- 누락은 오탐보다 위험하므로 판단 실패 시 EXCLUDED가 아닌 UNCERTAIN(확인 필요)으로 보수적 배정합니다.
================================================================================
"""

import os
import sys
import re
import json
import email
import argparse
from email import policy
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, date
from pathlib import Path
from typing import TypedDict, List, Dict, Any, Optional

# 윈도우 콘솔 환경에서 한글 출력이 깨지지 않도록 UTF-8 인코딩 강제
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 상위 디렉토리(impact_chat) 임포트 경로 추가 (단독 실행 지원)
CURRENT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = CURRENT_DIR.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

CONFIG_FILE_PATH = "기본 fallback 설정 (config.py 미연결)"
try:
    import config
    CONFIG_FILE_PATH = str(getattr(config, "__file__", "config.py"))
    OLLAMA_HOST = getattr(config, "OLLAMA_HOST", "http://localhost:11434")
    JUDGE_MODEL = getattr(config, "JUDGE_MODEL", "gemma4:e2b")
    USE_LLM = getattr(config, "USE_LLM", True)
    MY_EMAIL = getattr(config, "MY_EMAIL", "developer@company.com")
    MAIL_INBOX_DIR = getattr(config, "MAIL_INBOX_DIR", str(PACKAGE_ROOT / "sample_mails" / "inbox"))
    MAIL_SENT_DIR = getattr(config, "MAIL_SENT_DIR", str(PACKAGE_ROOT / "sample_mails" / "sent"))
    CACHE_DIR = getattr(config, "CACHE_DIR", str(PACKAGE_ROOT / ".cache"))
    MAIL_EXTENSIONS = getattr(config, "MAIL_EXTENSIONS", [".eml", ".mht", ".mhtml", ".txt", ".mail"])
except ImportError as err:
    # config.py를 못 찾거나 임포트 실패 시 상세 원인 출력 (조용한 실패 방지)
    print(f"⚠️ [주의] config.py 임포트 실패 ({err}). 기본 sample_mails 폴백 설정을 사용합니다.")
    OLLAMA_HOST = "http://localhost:11434"
    JUDGE_MODEL = "gemma4:e2b"
    USE_LLM = True
    MY_EMAIL = "developer@company.com"
    MAIL_INBOX_DIR = str(PACKAGE_ROOT / "sample_mails" / "inbox")
    MAIL_SENT_DIR = str(PACKAGE_ROOT / "sample_mails" / "sent")
    CACHE_DIR = str(PACKAGE_ROOT / ".cache")
    MAIL_EXTENSIONS = [".eml", ".mht", ".mhtml", ".txt", ".mail"]

try:
    from tools import register
except ImportError:
    # tools 패키지 외 단독 실행 시 더미 등록 데코레이터
    def register(func, **kwargs):
        return func

# LangChain / LangGraph 및 Pydantic 의존성
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END


# ==============================================================================
# 1. Pydantic 모델 & 상태(State) 정의
# ==============================================================================

class WorkJudgement(BaseModel):
    """소형 LLM 구조화 출력을 위한 스키마 (단일 질문만 질의)"""
    is_work_related: bool = Field(
        description="해당 메일이 실제 업무 수행, 업무 진행 보고, 업무 협업 요청과 관련된 내용이면 true, 단순 안부/광고/사적대화면 false"
    )
    reason: str = Field(
        description="판단 이유를 1문장으로 간결하게 설명"
    )


class EmailItem(TypedDict):
    """개별 파싱된 메일의 표준 데이터 구조"""
    id: str                 # 파일명 또는 메시지 식별자
    file_path: str          # 실제 파일 경로
    folder_type: str        # INBOX (수신) | SENT (발신)
    subject: str            # 디코딩된 제목
    sender: str             # 발신자
    receiver: str           # 수신자
    mail_date: str          # 메일 발송/수신 일시 (YYYY-MM-DD HH:MM:SS)
    date_str: str           # 비교용 일자 (YYYY-MM-DD)
    body_clean: str         # 정제된 텍스트 본문 (최대 1500자)
    is_my_sent: bool        # 본인 발신 여부 (MY_EMAIL 기준)
    status: str             # INCLUDED (업무 포함) | EXCLUDED (제외) | UNCERTAIN (확인 필요) | PENDING_LLM (판정 대기)
    category: str           # MY_REPORT (내 발신/보고) | RECEIVED_REQUEST (수신 협업/요청) | UNCERTAIN_ITEM (확인 필요) | EXCLUDED_ITEM (제외)
    decided_by: str         # static | rule | llm | fallback
    rule_name: str          # 적용된 규칙명 또는 모델명
    evidence: str           # 판단 근거 실제 텍스트 조각


class DailyWorkState(TypedDict):
    """LangGraph 전체 파이프라인에서 공유하는 상태"""
    target_date: str                # 대상 일자 (YYYY-MM-DD)
    inbox_dir: str                  # 수신 메일 폴더 경로
    sent_dir: str                   # 발신 메일 폴더 경로
    extensions: List[str]           # 스캔 대상 메일 확장자 목록
    my_email: str                   # 작성자 이메일
    use_llm: bool                   # LLM 활성화 여부
    detail_level: str               # full | summary | minimal
    parsed_items: List[EmailItem]   # 파싱 및 분류된 전체 메일 리스트
    rule_stats: Dict[str, int]      # 규칙별 처리 통계 카운터
    summary: Dict[str, Any]         # 최종 집계 요약
    report: str                     # 최종 Markdown 보고서
    error_message: Optional[str]    # 에러 발생 시 기록


# ==============================================================================
# 2. 캐시 관리 모듈 (파일 스캔 및 파싱 비용 절감)
# ==============================================================================

def _get_cache_file_path() -> Path:
    cache_path = Path(CACHE_DIR)
    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path / "mail_parsing_cache.json"


def _load_parsing_cache() -> Dict[str, Any]:
    """디스크에서 파싱 캐시를 로드합니다."""
    cache_file = _get_cache_file_path()
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_parsing_cache(cache: Dict[str, Any]) -> None:
    """파싱 결과를 디스크 캐시에 저장합니다."""
    cache_file = _get_cache_file_path()
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[경고] 캐시 저장 실패: {e}")


def clear_mail_cache() -> bool:
    """저장된 메일 파싱 캐시를 초기화합니다."""
    cache_file = _get_cache_file_path()
    if cache_file.exists():
        try:
            cache_file.unlink()
            print("[캐시] 메일 파싱 캐시가 성공적으로 삭제되었습니다.")
            return True
        except Exception as e:
            print(f"[오류] 캐시 삭제 실패: {e}")
            return False
    print("[캐시] 삭제할 캐시 파일이 없습니다.")
    return True


# ==============================================================================
# 3. .mht (MIME) 파일 파싱 유틸리티 함수
# ==============================================================================

def _decode_mime_header(header_value: Optional[str]) -> str:
    """
    RFC 2047로 인코딩된 이메일 헤더(=?UTF-8?B?...?= 또는 EUC-KR)를 안전하게 디코딩합니다.
    디코딩 실패 시 원본 문자열을 보존하여 누락을 방지합니다.
    """
    if not header_value:
        return ""
    
    decoded_fragments = []
    try:
        fragments = decode_header(header_value)
        for fragment, encoding in fragments:
            if isinstance(fragment, bytes):
                enc = encoding or "utf-8"
                try:
                    decoded_fragments.append(fragment.decode(enc, errors="replace"))
                except LookupError:
                    # 잘못된 인코딩 이름일 경우 cp949 또는 utf-8 fallback
                    decoded_fragments.append(fragment.decode("utf-8", errors="replace"))
            else:
                decoded_fragments.append(str(fragment))
        return "".join(decoded_fragments).strip()
    except Exception:
        return str(header_value).strip()


def _clean_html_text(html_content: str) -> str:
    """
    HTML 태그 및 스타일/스크립트를 제거하고 일반 텍스트만 추출합니다.
    사내 폐쇄망 환경에서 BeautifulSoup 미설치 상황을 고려하여 표준 정규식 기반으로 구현합니다.
    """
    if not html_content:
        return ""
    
    # 1. 스크립트 및 스타일 블록 전체 제거
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
    # 2. HTML 태그 제거
    text = re.sub(r"<[^>]+>", " ", text)
    # 3. HTML 특수문자 엔티티 치환
    html_entities = {
        "&nbsp;": " ",
        "&lt;": "<",
        "&gt;": ">",
        "&amp;": "&",
        "&quot;": '"',
        "&#39;": "'",
    }
    for entity, char in html_entities.items():
        text = text.replace(entity, char)
    
    # 4. 공백 및 줄바꿈 정규화
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _extract_date_regex(text: str) -> Optional[datetime]:
    """텍스트에서 다양한 형식의 날짜(ISO, 한글, 점구분, 8자리숫자)를 정규식으로 안전하게 추출합니다."""
    if not text:
        return None
    
    # 1. YYYY-MM-DD, YYYY.MM.DD, YYYY/MM/DD
    m1 = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", text)
    if m1:
        try:
            return datetime(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)))
        except ValueError:
            pass

    # 2. YYYY년 MM월 DD일
    m2 = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if m2:
        try:
            return datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        except ValueError:
            pass

    # 3. YYYYMMDD (파일명 등 8자리 숫자)
    m3 = re.search(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])", text)
    if m3:
        try:
            return datetime(int(m3.group(1)), int(m3.group(2)), int(m3.group(3)))
        except ValueError:
            pass

    return None


def _parse_single_mht_file(file_path: Path, folder_type: str) -> Optional[EmailItem]:
    """
    단일 메일 파일(.eml, .mht 등)을 읽어 분석합니다.
    [원칙] 파싱 중 어떤 에러가 발생하더라도 절대 파일을 버리지 않고, 
    텍스트 복원 모드로 살려내어 '확인 필요(UNCERTAIN)'로 보존합니다 (누락 원천 차단).
    """
    try:
        stat = file_path.stat()
        mtime = stat.st_mtime
    except Exception:
        mtime = datetime.now().timestamp()

    raw_bytes = b""
    try:
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
    except Exception as e:
        print(f"[파일 읽기 실패] {file_path.name}: {e}")
        return None

    # 1차 시도: 표준 MIME 파싱
    try:
        msg = email.message_from_bytes(raw_bytes, policy=policy.default)
        subject = _decode_mime_header(msg.get("Subject", ""))
        sender = _decode_mime_header(msg.get("From", ""))
        receiver = _decode_mime_header(msg.get("To", ""))

        # 날짜 파싱
        parsed_dt = None
        date_header = msg.get("Date")
        if date_header:
            try:
                parsed_dt = parsedate_to_datetime(date_header)
            except Exception:
                pass
            if not parsed_dt:
                parsed_dt = _extract_date_regex(str(date_header))

        if not parsed_dt:
            for alt_header in ("Delivery-date", "Resent-Date", "Received"):
                h_val = msg.get(alt_header)
                if h_val:
                    try:
                        ts = str(h_val).split(";")[-1].strip() if ";" in str(h_val) else str(h_val)
                        parsed_dt = parsedate_to_datetime(ts)
                    except Exception:
                        parsed_dt = _extract_date_regex(str(h_val))
                    if parsed_dt:
                        break

        if not parsed_dt:
            parsed_dt = _extract_date_regex(file_path.stem)

        if not parsed_dt:
            try:
                sample_header_text = raw_bytes[:1500].decode("utf-8", errors="ignore")
                parsed_dt = _extract_date_regex(sample_header_text)
            except Exception:
                pass

        if not parsed_dt:
            parsed_dt = datetime.fromtimestamp(mtime)

        date_str = parsed_dt.strftime("%Y-%m-%d")
        mail_date = parsed_dt.strftime("%Y-%m-%d %H:%M:%S")

        # 본문 텍스트 추출 (multipart 순회)
        body_text = ""
        html_fallback = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disp = str(part.get("Content-Disposition", ""))
                if "attachment" in content_disp:
                    continue

                try:
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    decoded_str = payload.decode(charset, errors="replace")

                    if content_type == "text/plain" and not body_text:
                        body_text = decoded_str
                    elif content_type == "text/html" and not html_fallback:
                        html_fallback = decoded_str
                except Exception:
                    continue
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                decoded_str = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    html_fallback = decoded_str
                else:
                    body_text = decoded_str

        final_body = body_text if body_text else _clean_html_text(html_fallback)
        final_body_clean = final_body[:1500].strip()

        # 만약 본문이 비어있다면 원문 텍스트 복원 시도
        if not final_body_clean:
            for enc in ("utf-8", "cp949", "euc-kr", "latin1"):
                try:
                    final_body_clean = _clean_html_text(raw_bytes[:3000].decode(enc, errors="ignore"))
                    break
                except Exception:
                    pass

        # 제목이 비어있다면 파일명으로 대체
        if not subject or subject == "(제목 없음)":
            subject = file_path.stem

        is_my_sent = (MY_EMAIL.lower() in sender.lower()) or (folder_type == "SENT")

        return {
            "id": file_path.name,
            "file_path": str(file_path),
            "folder_type": folder_type,
            "subject": subject,
            "sender": sender or "발신자 미상",
            "receiver": receiver or "수신자 미상",
            "mail_date": mail_date,
            "date_str": date_str,
            "body_clean": final_body_clean,
            "is_my_sent": is_my_sent,
            "status": "PENDING_LLM",
            "category": "OTHER",
            "decided_by": "static",
            "rule_name": "mime_parsed",
            "evidence": "표준 MIME 파싱 성공",
        }

    except Exception as mime_err:
        # [Fail-Safe] 표준 파싱 실패 시 텍스트 복원 모드로 구제 (절대 버리지 않음)
        print(f"⚠️ [파싱 경고] {file_path.name} MIME 파싱 오류 ({mime_err}) -> 텍스트 복원 모드로 안전 보존합니다.")
        recovered_text = ""
        for enc in ("utf-8", "cp949", "euc-kr", "latin1"):
            try:
                recovered_text = raw_bytes[:3000].decode(enc, errors="ignore")
                break
            except Exception:
                continue

        # 텍스트 내에서 제목/날짜 간이 추출
        subj_match = re.search(r"(?:Subject|제목)\s*[:：]\s*(.+)", recovered_text, re.IGNORECASE)
        fallback_subject = subj_match.group(1).strip() if subj_match else file_path.stem

        fallback_dt = _extract_date_regex(recovered_text) or _extract_date_regex(file_path.stem) or datetime.fromtimestamp(mtime)

        return {
            "id": file_path.name,
            "file_path": str(file_path),
            "folder_type": folder_type,
            "subject": fallback_subject[:100],
            "sender": "파싱 복원 (헤더 손상)",
            "receiver": "파싱 복원 (헤더 손상)",
            "mail_date": fallback_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "date_str": fallback_dt.strftime("%Y-%m-%d"),
            "body_clean": _clean_html_text(recovered_text[:1000]),
            "is_my_sent": folder_type == "SENT",
            "status": "UNCERTAIN",
            "category": "UNCERTAIN_ITEM",
            "decided_by": "fallback",
            "rule_name": "failsafe_recovery",
            "evidence": f"파싱 오류({mime_err}) 발생으로 텍스트 복원 후 확인 필요 보존",
        }


# ==============================================================================
# 4. LangGraph 파이프라인 노드 정의
# ==============================================================================

def node_scan_and_parse_mht(state: DailyWorkState) -> dict:
    """
    [1단계 노드] 지정된 수신/발신 폴더의 .mht 파일을 스캔하고 파싱합니다.
    캐시를 활용하여 이미 파싱된 파일은 디스크 I/O를 생략합니다.
    """
    target_date = state.get("target_date", "").strip()
    is_all_dates = target_date.lower() in ("all", "*", "")
    inbox_dir = Path(state["inbox_dir"])
    sent_dir = Path(state["sent_dir"])
    extensions = state.get("extensions") or MAIL_EXTENSIONS

    # 확장자 목록 정규화 (점 포함, 소문자)
    active_exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions if e}
    allow_no_ext = ("" in extensions) or ("no_ext" in extensions)
    allow_all = ("*" in extensions) or ("all" in extensions)

    cache = _load_parsing_cache()
    new_cache = dict(cache)
    parsed_items: List[EmailItem] = []

    # 스캔 대상 폴더 정의 (수신, 발신)
    scan_targets = [
        (inbox_dir, "INBOX"),
        (sent_dir, "SENT"),
    ]

    print("\n[📁 경로 진단]")
    for p, label in scan_targets:
        exists_str = "존재함 (O)" if p.exists() else "❌ 존재하지 않음 (X)"
        print(f" - [{label}] 경로: {p.resolve()} -> {exists_str}")

    total_scanned = 0
    date_matched = 0
    all_found_dates = set()
    found_ext_stats: Dict[str, int] = {}

    for folder_path, folder_type in scan_targets:
        if not folder_path.exists():
            print(f"[경고] {folder_type} 폴더가 디스크에 존재하지 않습니다: {folder_path.resolve()}")
            continue

        # 대소문자 및 하위 폴더까지 중복 없이 안전하게 탐색
        scanned_files = set()
        try:
            for file_path in folder_path.rglob("*"):
                if not file_path.is_file():
                    continue
                
                suffix = file_path.suffix.lower()
                found_ext_stats[suffix] = found_ext_stats.get(suffix, 0) + 1

                if allow_all or (suffix in active_exts) or (not suffix and allow_no_ext):
                    scanned_files.add(file_path.resolve())
        except Exception as e:
            print(f"[경고] 폴더 스캔 중 오류 ({folder_path}): {e}")

        for file_path in sorted(scanned_files):
            total_scanned += 1
            try:
                mtime = file_path.stat().st_mtime
                cache_key = f"{file_path}_{mtime}"
            except Exception:
                continue

            item: Optional[EmailItem] = None
            if cache_key in new_cache:
                item = new_cache[cache_key]
            else:
                item = _parse_single_mht_file(file_path, folder_type)
                if item:
                    new_cache[cache_key] = item

            if item:
                all_found_dates.add(item["date_str"])
                # 대상 일자가 all이거나 지정일과 일치하는 경우
                if is_all_dates or item["date_str"] == target_date:
                    parsed_items.append(item)
                    date_matched += 1

    _save_parsing_cache(new_cache)

    if found_ext_stats:
        ext_summary = ", ".join([f"'{k or '(확장자없음)'}': {v}개" for k, v in found_ext_stats.items()])
        print(f" - 폴더 내 실제 파일 확장자 분포: {ext_summary}")
    else:
        print(" - ⚠️ 지정된 폴더에 파일이 하나도 없습니다.")

    # 스캔된 파일의 실제 날짜 샘플 출력 (상위 5건)
    if all_found_dates:
        print(f" - 📅 파일들에서 감지된 날짜 목록: {', '.join(sorted(list(all_found_dates))[:8])}")

    target_desc = "전체 일자(all)" if is_all_dates else target_date
    print(f"[1] 메일 파싱 완료: 총 {total_scanned}개 파일 스캔(허용 확장자: {','.join(extensions)}) 중 대상일({target_desc}) {date_matched}건 추출")
    
    if total_scanned > 0 and date_matched == 0 and not is_all_dates:
        dates_preview = ", ".join(sorted(list(all_found_dates))[:5])
        print(f" ⚠️ [알림] 파일은 {total_scanned}개 발견되었으나, 대상일({target_date})과 일치하지 않아 0건 추출되었습니다.")
        print(f"    👉 감지된 메일 날짜 중 하나로 실행하려면: python tools/mail_work_summary.py --target-date {list(all_found_dates)[0]} --no-llm")
        print("    👉 날짜 제한 없이 전부 정리하려면: python tools/mail_work_summary.py --target-date all --no-llm")

    return {
        "parsed_items": parsed_items,
        "rule_stats": {
            "total_parsed": len(parsed_items),
            "rule_excluded": 0,
            "rule_included": 0,
            "llm_evaluated": 0,
            "uncertain_fallback": 0,
        }
    }


def node_rule_filter(state: DailyWorkState) -> dict:
    """
    [2단계 노드] 정적 조건 및 정규식 규칙으로 명확한 대상을 먼저 선별합니다.
    LLM을 호출하지 않고 코드로 결정적 처리를 하여 비용과 환각을 원천 차단합니다.
    """
    items = state["parsed_items"]
    updated_items: List[EmailItem] = []
    stats = dict(state.get("rule_stats", {}))

    # 1. 자동 제외 규칙 (시스템 알림, 사내 공지/식단표, 스팸 등)
    EXCLUDE_SENDER_PATTERNS = [
        r"no-?reply", r"mailer-daemon", r"postmaster",
        r"jira-notification", r"gitlab-notification", r"github-notification",
        r"alert@.*", r"monitoring@.*", r"newsletter",
    ]
    EXCLUDE_SUBJECT_PATTERNS = [
        r"\[식단\]", r"\[식당\]", r"구내식당", r"\[동호회\]", r"\[경조사\]",
        r"\[설문조사\]", r"\[홍보\]", r"\(광고\)", r"사내 전산 정기점검 완료 안내",
    ]

    # 2. 확실한 업무 확정 규칙 (명시적 키워드)
    WORK_KEYWORD_PATTERNS = [
        r"보고", r"회의록", r"검토\s*요청", r"배포\s*(완료|계획|공지)",
        r"장애\s*(공유|조치|발생)", r"이슈\s*(분석|해결)", r"API\s*(개발|연동|변경)",
        r"릴리즈", r"DB\s*(튜닝|변경|마이그레이션)", r"결재\s*(상신|승인|요청)",
        r"일정\s*(공유|협의)", r"피드백\s*요청", r"개발\s*(진척|현황)",
    ]

    sender_exclude_re = re.compile("|".join(EXCLUDE_SENDER_PATTERNS), re.IGNORECASE)
    subject_exclude_re = re.compile("|".join(EXCLUDE_SUBJECT_PATTERNS), re.IGNORECASE)
    work_keyword_re = re.compile("|".join(WORK_KEYWORD_PATTERNS), re.IGNORECASE)

    for item in items:
        subject = item["subject"]
        sender = item["sender"]
        body = item["body_clean"]

        # 규칙 1: 시스템 알림 또는 제외 발신자
        if sender_exclude_re.search(sender):
            item["status"] = "EXCLUDED"
            item["category"] = "EXCLUDED_ITEM"
            item["decided_by"] = "rule"
            item["rule_name"] = "system_sender_filter"
            item["evidence"] = f"발신자 제외 패턴 감지 ({sender})"
            stats["rule_excluded"] = stats.get("rule_excluded", 0) + 1
            updated_items.append(item)
            continue

        # 규칙 2: 제외 제목 패턴 (식단표, 광고 등)
        if subject_exclude_re.search(subject):
            item["status"] = "EXCLUDED"
            item["category"] = "EXCLUDED_ITEM"
            item["decided_by"] = "rule"
            item["rule_name"] = "subject_exclusion_filter"
            item["evidence"] = f"제목 제외 패턴 감지 ({subject})"
            stats["rule_excluded"] = stats.get("rule_excluded", 0) + 1
            updated_items.append(item)
            continue

        # 규칙 3: 명확한 업무 키워드 매칭
        work_match = work_keyword_re.search(subject) or work_keyword_re.search(body[:300])
        if work_match:
            item["status"] = "INCLUDED"
            item["decided_by"] = "rule"
            item["rule_name"] = "work_keyword_match"
            item["evidence"] = f"업무 키워드 발견: '{work_match.group()}'"
            
            # 발신/수신 카테고리 확정
            if item["is_my_sent"] or item["folder_type"] == "SENT":
                item["category"] = "MY_REPORT"
            else:
                item["category"] = "RECEIVED_REQUEST"
                
            stats["rule_included"] = stats.get("rule_included", 0) + 1
            updated_items.append(item)
            continue

        # 규칙에 걸리지 않은 애매한 메일은 LLM 판정 대기로 유지
        item["status"] = "PENDING_LLM"
        item["decided_by"] = "fallback"
        item["rule_name"] = "rule_unmatched"
        item["evidence"] = "정적 규칙 미매칭으로 LLM 판정 대기"
        updated_items.append(item)

    pending_count = sum(1 for x in updated_items if x["status"] == "PENDING_LLM")
    print(f"[2] 규칙 필터링 완료: 업무 확정 {stats.get('rule_included', 0)}건, 제외 {stats.get('rule_excluded', 0)}건, LLM 판정 대상 {pending_count}건")

    return {
        "parsed_items": updated_items,
        "rule_stats": stats,
    }


def node_llm_classify(state: DailyWorkState) -> dict:
    """
    [3단계 노드] 규칙으로 풀지 못한 모호한 메일만 소형 LLM(gemma4:e2b)으로 판정합니다.
    - 단일 질문 + json_schema 스키마 강제
    - 대상 줄 `>>>` 마커 표시
    - 실패 시 누락 방지를 위해 UNCERTAIN(확인 필요) 배정
    """
    use_llm = state.get("use_llm", USE_LLM)
    items = state["parsed_items"]
    stats = dict(state.get("rule_stats", {}))

    pending_indices = [i for i, x in enumerate(items) if x["status"] == "PENDING_LLM"]
    if not pending_indices:
        print("[3] LLM 판정 단계: 판정 대상 메일이 없어 건너뜁니다.")
        return {"parsed_items": items, "rule_stats": stats}

    # LLM 미사용 모드이거나 폐쇄망에서 LLM 비활성화 시 보수적 폴백 처리
    if not use_llm:
        print(f"[3] LLM 비활성화(--no-llm) 모드: 모호한 메일 {len(pending_indices)}건을 '확인 필요(UNCERTAIN)'로 배정합니다.")
        for idx in pending_indices:
            items[idx]["status"] = "UNCERTAIN"
            items[idx]["category"] = "UNCERTAIN_ITEM"
            items[idx]["decided_by"] = "fallback"
            items[idx]["rule_name"] = "no_llm_conservative_fallback"
            items[idx]["evidence"] = "LLM 미사용으로 인한 보수적 확인 필요 배정"
            stats["uncertain_fallback"] = stats.get("uncertain_fallback", 0) + 1
        return {"parsed_items": items, "rule_stats": stats}

    # ChatOllama 소형 모델 초기화 (규격 필수 인자 준수)
    try:
        from langchain_ollama import ChatOllama
        base_llm = ChatOllama(
            base_url=OLLAMA_HOST,
            model=JUDGE_MODEL,
            temperature=0,
            num_ctx=8192,       # 기본값 2048의 프롬프트 조용한 잘림 방지
            keep_alive="10m",   # 루프 도는 동안 언로드 방지
        )
        # tool calling 대신 안정적인 스키마 강제 디코딩
        structured_llm = base_llm.with_structured_output(WorkJudgement, method="json_schema")
    except Exception as e:
        print(f"[경고] ChatOllama 초기화 실패 ({e}). 모호한 항목을 모두 '확인 필요'로 전환합니다.")
        for idx in pending_indices:
            items[idx]["status"] = "UNCERTAIN"
            items[idx]["category"] = "UNCERTAIN_ITEM"
            items[idx]["decided_by"] = "fallback"
            items[idx]["rule_name"] = "ollama_init_failure"
            items[idx]["evidence"] = f"LLM 연결 실패로 인한 보수적 배정 ({e})"
            stats["uncertain_fallback"] = stats.get("uncertain_fallback", 0) + 1
        return {"parsed_items": items, "rule_stats": stats}

    print(f"[3] LLM 판정 시작: {len(pending_indices)}건의 애매한 메일을 {JUDGE_MODEL} 모델로 판정합니다.")

    for idx in pending_indices:
        item = items[idx]
        snippet = item["body_clean"][:400].replace("\n", " ").strip()

        # 프롬프트 구성: 한 프롬프트에 질문 하나, 짧고 명확한 규칙, >>> 마커 활용
        prompt = f"""당신은 사내 이메일이 실제 업무와 관련된 것인지 판정하는 어시스턴트입니다.
규칙:
1. 실제 프로젝트, 시스템 개발, 업무 협의, 일정 조율, 업무 요청이면 is_work_related=true
2. 사적인 대화, 단순 안부 인사, 외부 스팸성 홍보 메일이면 is_work_related=false
3. 애매하면 누락 방지를 위해 true로 판정하세요.

판정 대상 메일:
>>> 제목: {item['subject']}
>>> 발신자: {item['sender']}
>>> 본문 내용: {snippet}
"""
        try:
            result: WorkJudgement = structured_llm.invoke(prompt)
            stats["llm_evaluated"] = stats.get("llm_evaluated", 0) + 1

            if result.is_work_related:
                item["status"] = "INCLUDED"
                item["decided_by"] = "llm"
                item["rule_name"] = f"llm_{JUDGE_MODEL}"
                item["evidence"] = result.reason or "LLM 업무 판정"
                item["category"] = "MY_REPORT" if item["is_my_sent"] else "RECEIVED_REQUEST"
            else:
                item["status"] = "EXCLUDED"
                item["category"] = "EXCLUDED_ITEM"
                item["decided_by"] = "llm"
                item["rule_name"] = f"llm_{JUDGE_MODEL}"
                item["evidence"] = result.reason or "LLM 비업무 판정"
        except Exception as e:
            # LLM 호출 실패 시 조용히 넘기지 않고 보수적으로 '확인 필요' 표시
            item["status"] = "UNCERTAIN"
            item["category"] = "UNCERTAIN_ITEM"
            item["decided_by"] = "fallback"
            item["rule_name"] = "llm_invoke_error"
            item["evidence"] = f"LLM 판정 오류 발생 ({e}), 확인 필요 배정"
            stats["uncertain_fallback"] = stats.get("uncertain_fallback", 0) + 1

    print(f"[3] LLM 판정 완료: 성공 {stats.get('llm_evaluated', 0)}건, 폴백(확인필요) {stats.get('uncertain_fallback', 0)}건")

    return {
        "parsed_items": items,
        "rule_stats": stats,
    }


def node_aggregate_and_report(state: DailyWorkState) -> dict:
    """
    [4단계 노드] 분류 결과를 집계하고 구조화된 Markdown 보고서를 생성합니다.
    소형 LLM의 서식 붕괴를 막기 위해 보고서 뼈대 및 섹션 분류는 코드로 100% 결정적으로 조립합니다.
    """
    raw_date = state.get("target_date", "")
    target_date = "전체 기간(All)" if raw_date.lower() in ("all", "*") else (raw_date or datetime.now().strftime("%Y-%m-%d"))
    items = state["parsed_items"]
    stats = state.get("rule_stats", {})

    my_reports = [x for x in items if x["status"] == "INCLUDED" and x["category"] == "MY_REPORT"]
    received_requests = [x for x in items if x["status"] == "INCLUDED" and x["category"] == "RECEIVED_REQUEST"]
    uncertain_items = [x for x in items if x["status"] == "UNCERTAIN"]
    excluded_items = [x for x in items if x["status"] == "EXCLUDED"]

    # 1. 요약 통계 딕셔너리 생성
    summary = {
        "target_date": target_date,
        "total_emails": len(items),
        "included_work_count": len(my_reports) + len(received_requests),
        "my_reports_count": len(my_reports),
        "received_requests_count": len(received_requests),
        "uncertain_count": len(uncertain_items),
        "excluded_count": len(excluded_items),
        "rule_stats": stats,
    }

    # 2. Markdown 리포트 조립
    lines = []
    lines.append(f"# 📅 일일 업무 내역 정리 ({target_date})")
    lines.append("")
    lines.append("## 📊 업무 처리 요약")
    lines.append(f"- **전체 분석 메일**: {len(items)}건")
    lines.append(f"- **업무 내역 반영**: {len(my_reports) + len(received_requests)}건 (발신 보고 {len(my_reports)}건 / 수신 요청 {len(received_requests)}건)")
    lines.append(f"- **확인 필요(모호/오류)**: {len(uncertain_items)}건")
    lines.append(f"- **제외 메일**: {len(excluded_items)}건")
    lines.append("")

    # 핵심 요약 불릿
    lines.append("## 1. 오늘 주요 업무 핵심 요약")
    if not my_reports and not received_requests:
        lines.append("- 해당 일자에 추출된 업무 메일이 없습니다.")
    else:
        for item in my_reports:
            lines.append(f"- **[발신]** {item['subject']} (근거: {item['evidence']})")
        for item in received_requests:
            lines.append(f"- **[수신]** {item['subject']} (발신: {item['sender']}, 근거: {item['evidence']})")
    lines.append("")

    # 주요 발신 및 수행 업무
    lines.append("## 2. 주요 발신 및 수행 업무 (Sent)")
    if my_reports:
        for idx, item in enumerate(my_reports, 1):
            lines.append(f"### 2-{idx}. {item['subject']}")
            lines.append(f"- **일시**: {item['mail_date']}")
            lines.append(f"- **수신자**: {item['receiver']}")
            lines.append(f"- **판정 방식**: `{item['decided_by']}` ({item['rule_name']})")
            snippet = item['body_clean'][:200].replace("\n", " ").strip()
            lines.append(f"- **내용 요약**: {snippet}...")
            lines.append("")
    else:
        lines.append("- 당일 발신/보고 메일 내역이 없습니다.\n")

    # 수신 요청 및 협업 대응 내역
    lines.append("## 3. 수신 요청 및 협업 대응 내역 (Received)")
    if received_requests:
        for idx, item in enumerate(received_requests, 1):
            lines.append(f"### 3-{idx}. {item['subject']}")
            lines.append(f"- **일시**: {item['mail_date']}")
            lines.append(f"- **발신자**: {item['sender']}")
            lines.append(f"- **판정 방식**: `{item['decided_by']}` ({item['rule_name']})")
            snippet = item['body_clean'][:200].replace("\n", " ").strip()
            lines.append(f"- **내용 요약**: {snippet}...")
            lines.append("")
    else:
        lines.append("- 당일 수신된 요청 메일 내역이 없습니다.\n")

    # 확인 필요 항목 (보수적 원칙에 따른 보존)
    if uncertain_items:
        lines.append("## 4. ⚠️ 확인 필요 항목 (UNCERTAIN)")
        lines.append("> 규칙 미매칭 또는 LLM 판단 불가로 누락 방지를 위해 사람이 직접 검토해야 하는 메일입니다.")
        for idx, item in enumerate(uncertain_items, 1):
            lines.append(f"- **[{item['folder_type']}] {item['subject']}**")
            lines.append(f"  - 발신: {item['sender']} | 일시: {item['mail_date']}")
            lines.append(f"  - 근거: `{item['evidence']}`")
        lines.append("")

    lines.append("---")
    lines.append(f"*작성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 작성 기준: {MY_EMAIL}*")

    report_markdown = "\n".join(lines)

    print(f"[4] 일일 업무 일지 생성 완료 (총 업무 {len(my_reports) + len(received_requests)}건 반영)")

    return {
        "summary": summary,
        "report": report_markdown,
    }


# ==============================================================================
# 5. LangGraph 컴파일 함수
# ==============================================================================

def build_mail_summary_graph():
    """LangGraph 워크플로우를 생성하고 컴파일하여 반환합니다."""
    g = StateGraph(DailyWorkState)
    
    g.add_node("scan_and_parse", node_scan_and_parse_mht)
    g.add_node("rule_filter", node_rule_filter)
    g.add_node("llm_classify", node_llm_classify)
    g.add_node("aggregate_and_report", node_aggregate_and_report)

    g.add_edge(START, "scan_and_parse")
    g.add_edge("scan_and_parse", "rule_filter")
    g.add_edge("rule_filter", "llm_classify")
    g.add_edge("llm_classify", "aggregate_and_report")
    g.add_edge("aggregate_and_report", END)

    return g.compile()


# ==============================================================================
# 6. 실행 함수 및 에이전트 도구(@tool) 래핑
# ==============================================================================

def run_mail_work_summary(
    target_date: str = "",
    inbox_dir: str = "",
    sent_dir: str = "",
    my_email: str = "",
    extensions: Optional[List[str]] = None,
    use_llm: bool = True,
    detail: str = "full"
) -> dict:
    """
    메일 기반 일일 업무 정리 파이프라인 실행 진입 함수.

    Args:
        target_date: 대상 일자 (YYYY-MM-DD, 비어있으면 오늘 날짜)
        inbox_dir: 수신 메일 폴더 (.eml, .mht 등)
        sent_dir: 발신 메일 폴더 (.eml, .mht 등)
        my_email: 사용자 본인 식별 이메일 주소
        extensions: 탐색할 메일 확장자 리스트 (기본값: config.py의 MAIL_EXTENSIONS)
        use_llm: LLM 정밀 판정 사용 여부
        detail: 출력 상세 수준 (full, summary, minimal)

    Returns:
        dict: {"summary": dict, "items": list, "report": str}
    """
    raw_target = target_date.strip()
    if raw_target.lower() in ("all", "*"):
        effective_date = "all"
    elif raw_target:
        effective_date = raw_target
    else:
        effective_date = date.today().strftime("%Y-%m-%d")

    effective_inbox = inbox_dir.strip() if inbox_dir else MAIL_INBOX_DIR
    effective_sent = sent_dir.strip() if sent_dir else MAIL_SENT_DIR
    effective_email = my_email.strip() if my_email else MY_EMAIL
    effective_exts = extensions if extensions is not None else MAIL_EXTENSIONS

    initial_state: DailyWorkState = {
        "target_date": effective_date,
        "inbox_dir": effective_inbox,
        "sent_dir": effective_sent,
        "extensions": effective_exts,
        "my_email": effective_email,
        "use_llm": use_llm,
        "detail_level": detail,
        "parsed_items": [],
        "rule_stats": {},
        "summary": {},
        "report": "",
        "error_message": None,
    }

    graph = build_mail_summary_graph()
    final_state = graph.invoke(initial_state)

    return {
        "summary": final_state.get("summary", {}),
        "items": final_state.get("parsed_items", []),
        "report": final_state.get("report", ""),
    }


# LLM 에이전트에게 노출할 단일 도구 정의
# [원칙] 소형 모델이 여러 도구를 오판하여 호출하지 않도록 에이전트에는 이 단일 도구만 노출합니다.
@tool
def summarize_daily_work_mail(target_date: str = "") -> str:
    """수신/발신 메일 원문(.eml, .mht 등)을 분석하여 특정 일자의 업무 내역 일지(발신 보고, 수신 요청 등)를 마크다운 리포트로 생성합니다.

    Args:
        target_date: 정리할 대상 일자 (YYYY-MM-DD 형식, 예: '2026-10-06'). 비어있으면 오늘 날짜를 기준으로 합니다.

    Returns:
        일일 업무 정리 마크다운 보고서
    """
    result = run_mail_work_summary(target_date=target_date, detail="summary")
    return result["report"]


# impact_chat 챗봇 레지스트리에 도구 등록
register(
    summarize_daily_work_mail,
    label="일일 메일 업무 정리",
    view="markdown",
    detail_endpoint="/api/tools/mail-work-summary/detail",
    hint="메일 원문(.eml, .mht)을 기반으로 일별 업무 수행 내역을 취합할 때 사용하세요. target_date는 YYYY-MM-DD 형식입니다."
)


# ==============================================================================
# 7. 단독 CLI 실행 엔트리포인트 (테스트 및 독립 실행용)
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="수신/발신 메일 원문(.eml, .mht 등) 파싱 기반 일일 업무 정리 LangGraph 도구"
    )
    parser.add_argument(
        "--target-date",
        type=str,
        default="",
        help="정리 대상 일자 (YYYY-MM-DD, 기본값: 오늘)",
    )
    parser.add_argument(
        "--inbox-dir",
        type=str,
        default="",
        help="수신 메일 폴더 경로 (.eml, .mht 등)",
    )
    parser.add_argument(
        "--sent-dir",
        type=str,
        default="",
        help="발신 메일 폴더 경로 (.eml, .mht 등)",
    )
    parser.add_argument(
        "--extensions",
        type=str,
        default="",
        help="스캔 대상 메일 확장자 (콤마 구분, 예: .eml,.mht,.txt)",
    )
    parser.add_argument(
        "--my-email",
        type=str,
        default="",
        help="본인 식별 이메일 주소",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="LLM 호출 없이 결정적 규칙만으로 판정 (모호한 건은 확인 필요 배정)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="저장된 메일 파싱 캐시를 삭제하고 종료합니다.",
    )
    parser.add_argument(
        "--detail",
        type=str,
        default="full",
        choices=["full", "summary", "minimal"],
        help="결과 상세 수준",
    )

    args = parser.parse_args()

    if args.clear_cache:
        clear_mail_cache()
        sys.exit(0)

    print("=================================================================")
    print(" [일일 메일 업무 정리 LangGraph 에이전트 CLI 실행]")
    print(f" - 설정 파일 출처: {CONFIG_FILE_PATH}")
    print(f" - 기준 일자: {args.target_date or date.today().strftime('%Y-%m-%d')}")
    print(f" - LLM 사용: {not args.no_llm}")
    print("=================================================================")

    ext_list = [e.strip() for e in args.extensions.split(",") if e.strip()] if args.extensions else None

    output = run_mail_work_summary(
        target_date=args.target_date,
        inbox_dir=args.inbox_dir,
        sent_dir=args.sent_dir,
        my_email=args.my_email,
        extensions=ext_list,
        use_llm=not args.no_llm,
        detail=args.detail,
    )

    print("\n[생성된 일일 업무 리포트]")
    print("-----------------------------------------------------------------")
    print(output["report"])
    print("-----------------------------------------------------------------")
    print("\n[통계 요약 JSON]")
    print(json.dumps(output["summary"], ensure_ascii=False, indent=2))
