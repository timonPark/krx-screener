"""화면의 응답 타입(``frontend/src/types.ts``)이 백엔드 응답 모델과 어긋나지 않는다 (#65).

TypeScript 는 화면 안에서만 타입을 지켜 주므로, 백엔드 모델이 바뀌었는데 ``types.ts`` 가 그대로면 컴파일은 통과하고 실제 응답과 달라진다.
그래서 인터페이스의 키와 "값이 null 일 수 있는가" 를 백엔드 모델(``routes_sessions.py``)과 대조한다. Node 없이 도는 테스트다.
"""

import re
import types
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from krx_screener import routes_sessions as models

TYPES_TS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "types.ts"

# types.ts 의 인터페이스 → 백엔드 응답 모델
PAIRS = {
    "Build": models.BuildOut,
    "Filters": models.FiltersOut,
    "FlowCriteria": models.FlowCriteriaOut,
    "Criteria": models.CriteriaOut,
    "Session": models.SessionOut,
    "Item": models.ItemOut,
    "Counts": models.CountsOut,
    "SessionDetail": models.SessionDetailOut,
}


def interfaces() -> dict[str, dict[str, str]]:
    """``export interface X { 키: 타입; }`` → {X: {키: 타입}} (줄 끝 주석은 뗀다)."""
    text = TYPES_TS.read_text(encoding="utf-8")
    found: dict[str, dict[str, str]] = {}
    for name, body in re.findall(r"export interface (\w+) \{\n(.*?)\n\}", text, flags=re.S):
        fields: dict[str, str] = {}
        for line in body.splitlines():
            line = line.split("//")[0].strip()
            if not line:
                continue
            key, _, kind = line.partition(":")
            fields[key.strip()] = kind.strip().rstrip(";").strip()
        found[name] = fields
    return found


def allows_none(annotation: object) -> bool:
    return type(None) in typing.get_args(annotation) and (
        typing.get_origin(annotation) in (typing.Union, types.UnionType)
    )


@pytest.mark.parametrize(("name", "model"), PAIRS.items())
def test_the_interface_has_exactly_the_fields_of_the_backend_model(name, model: type[BaseModel]):
    assert set(interfaces()[name]) == set(model.model_fields), name


@pytest.mark.parametrize(("name", "model"), PAIRS.items())
def test_the_interface_allows_null_exactly_where_the_backend_does(name, model: type[BaseModel]):
    fields = interfaces()[name]
    for key, info in model.model_fields.items():
        in_ts = "| null" in fields[key]
        assert in_ts == allows_none(info.annotation), f"{name}.{key}: types.ts 는 {fields[key]!r}, 백엔드는 {info.annotation}"


def test_every_interface_that_mirrors_a_backend_model_is_checked():
    """types.ts 에 응답 모양 인터페이스를 새로 더하면 여기 PAIRS 에도 올려서 대조하게 한다."""
    not_models = {"ProcessResult", "OrganizeResult", "ApiErrorBody", "Api"}  # 응답 모델이 따로 없는 것 (process·organize 는 딕셔너리로 답한다)
    assert set(interfaces()) - set(PAIRS) == not_models
