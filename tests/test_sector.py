"""네이버 종목 상세에서 업종 정보를 읽는 코드 (#16). 실제 요청 없이 응답을 바꿔 끼워서 검증한다."""

import pytest
import requests

from krx_screener import sector
from krx_screener.externals import SectorInfo

SAMSUNG = {
    "itemcode": "005930",
    "upjongCode": "27",
    "upJongName": "반도체와반도체장비",
    "comment1": "동사는 1969년 설립된 글로벌 전자 기업으로, 309개의 종속기업을 보유하고 있음.",
    "comment2": "DX 부문은 TV, 냉장고, 스마트폰을 생산·판매하고 있음.",
    "comment3": "동사는 AI 기술 도입과 친환경·고효율 기술 혁신에 주력하고 있음.",
}


# --- 응답 해석 ---


def test_the_sector_code_name_and_the_three_comment_lines_are_read():
    info = sector.parse_detail(SAMSUNG)
    assert (info.code, info.name) == ("27", "반도체와반도체장비")
    assert info.description == f"{SAMSUNG['comment1']} {SAMSUNG['comment2']} {SAMSUNG['comment3']}"


def test_missing_and_blank_comments_are_skipped_and_spaces_are_trimmed():
    payload = {"upjongCode": " 26 ", "upJongName": " 건설 ", "comment1": "  첫 줄  ", "comment2": "", "comment3": None}
    assert sector.parse_detail(payload) == SectorInfo("26", "건설", "첫 줄")


def test_a_value_that_is_not_text_is_treated_as_empty():
    assert sector.parse_detail({"upjongCode": 27, "upJongName": "건설"}) == SectorInfo("", "건설", "")


@pytest.mark.parametrize("payload", [{}, {"upjongCode": "", "upJongName": " ", "comment1": ""}, [], "text", None, 5])
def test_a_response_with_nothing_to_read_is_none(payload):
    assert sector.parse_detail(payload) is None


# --- 조회 ---


class _Resp:
    def __init__(self, status=200, payload=None, json_error=False):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    """requests.get 을 가짜로 바꾼다. ``responses`` 에 응답(또는 예외)을 순서대로 쌓는다."""

    class Calls(list):
        responses: list = []

    recorded = Calls()

    def fake_get(url, **kwargs):
        recorded.append((url, kwargs))
        item = recorded.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "get", fake_get)
    return recorded


def test_a_stock_is_looked_up_by_its_code(calls):
    calls.responses = [_Resp(payload=SAMSUNG)]
    info = sector.naver_sector("005930")
    assert info.name == "반도체와반도체장비"
    assert calls[0][0] == "https://stock.naver.com/api/domestic/detail/005930/detail?codeType=KRX"
    assert calls[0][1]["headers"]["Referer"] == "https://stock.naver.com/"
    assert calls[0][1]["timeout"] > 0


def test_an_alphanumeric_code_is_looked_up_too(calls):
    calls.responses = [_Resp(payload={"upjongCode": "21", "upJongName": "제약"})]
    assert sector.naver_sector("0126Z0").name == "제약"
    assert "/0126Z0/detail" in calls[0][0]


@pytest.mark.parametrize("code", ["", "5930", "../x", "00593O!", "abcdef"])
def test_a_malformed_code_is_none_without_a_request(calls, code):
    assert sector.naver_sector(code) is None
    assert calls == []


@pytest.mark.parametrize(
    "response",
    [_Resp(status=404), _Resp(status=500), _Resp(json_error=True), _Resp(payload=["x"]), _Resp(payload={})],
)
def test_an_unreadable_response_is_none_and_is_not_retried(calls, response):
    calls.responses = [response, _Resp(payload=SAMSUNG)]  # 두 번째 응답은 쓰이면 안 된다
    assert sector.naver_sector("005930", retry_wait=0) is None
    assert len(calls) == 1


def test_a_network_error_is_retried_once(calls):
    calls.responses = [requests.ConnectionError("끊김"), _Resp(payload=SAMSUNG)]
    assert sector.naver_sector("005930", retry_wait=0).code == "27"
    assert len(calls) == 2


def test_two_network_errors_in_a_row_give_none(calls):
    calls.responses = [requests.ConnectionError("끊김"), requests.Timeout("느림")]
    assert sector.naver_sector("005930", retry_wait=0) is None
    assert len(calls) == 2


def test_retries_can_be_turned_off(calls):
    calls.responses = [requests.ConnectionError("끊김"), _Resp(payload=SAMSUNG)]
    assert sector.naver_sector("005930", retries=0) is None
    assert len(calls) == 1
