"""
impact_chat 도구 레지스트리 모듈 (tools/__init__.py)
챗봇 에이전트에 바인딩할 도구들을 등록하고 관리합니다.
"""

from typing import Callable, Dict, Any, Optional

# 등록된 도구들의 메타데이터 저장소
TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register(
    tool_func: Callable,
    label: str,
    view: str = "text",
    detail_endpoint: str = "",
    hint: str = ""
) -> Callable:
    """
    챗봇 에이전트 도구 등록 데코레이터 및 함수.

    Args:
        tool_func: @tool로 래핑된 실행 함수
        label: UI 및 화면에 표시할 도구 이름
        view: 렌더러 종류 (text, markdown 등)
        detail_endpoint: 전체 상세 결과를 따로 받을 API 경로 (선택)
        hint: 시스템 프롬프트에 주입할 호출 주의사항 및 가이드

    Returns:
        원본 도구 함수
    """
    tool_name = getattr(tool_func, "name", None) or getattr(tool_func, "__name__", str(tool_func))
    TOOL_REGISTRY[tool_name] = {
        "function": tool_func,
        "label": label,
        "view": view,
        "detail_endpoint": detail_endpoint,
        "hint": hint,
    }
    return tool_func


def get_registered_tools() -> list:
    """등록된 모든 툴 함수 리스트 반환"""
    return [item["function"] for item in TOOL_REGISTRY.values()]
