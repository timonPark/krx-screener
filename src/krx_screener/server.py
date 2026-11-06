"""웹 서버 조립 (이슈 #26): 실제 외부 경계를 이어 붙여 ``api.create_app`` 에 넘길 ``Dependencies`` 를 만든다.

테스트는 같은 ``Dependencies`` 를 가짜 경계로 만든다. 여기는 실제 구현체를 고르는 곳이다:

| 경계 | 실제 구현 |
|---|---|
| 전종목 목록 | ``universe.fetch_listing`` (FinanceDataReader) |
| 종목별 시세 | ``prices.fdr_fetcher`` |
| 수급 | ``flow.naver_trend`` (네이버 증권, 비공식) |
| 업종·기업 설명 | ``sector.naver_sector`` (네이버, 비공식) |
| 분석 서버 | ``analysis.AnalysisClient`` (``config.yaml`` 의 ``analysis.base_url``) |
| 분석 상태 로그 | TradingAgents-KR 서버의 로그 폴더 (아래) |
| DB | ``db.connect`` — 요청마다 새 연결. 주소는 환경변수 ``KRX_SCREENER_DATABASE_URL`` |

만들 때는 DB 나 네트워크에 연결하지 않는다 (연결은 요청이 올 때 한다). 그래서 DB 가 아직 없어도 서버는 뜨고, 화면이 한국어 오류로 알려준다.

분석 상태 로그 폴더는 TradingAgents-KR 서버와 같은 규칙으로 정한다: 환경변수 ``TRADINGAGENTS_RESULTS_DIR``, 없으면
``~/.tradingagents/logs``. 서버와 같은 컴퓨터에서 실행한다는 전제다 (에이전트별 등급 판독기, #21).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from krx_screener import db
from krx_screener.analysis import AnalysisClient
from krx_screener.api import Dependencies
from krx_screener.config import load_config
from krx_screener.externals import Externals
from krx_screener.flow import naver_trend
from krx_screener.prices import fdr_fetcher
from krx_screener.sector import naver_sector
from krx_screener.universe import fetch_listing

DEFAULT_PORT = 8100  # TradingAgents-KR 는 8000, 개발용 DB 는 5433


def default_frontend_dir() -> Path:
    """저장소의 ``frontend/`` (이 파일은 ``src/krx_screener/`` 에 있다)."""
    return Path(__file__).resolve().parents[2] / "frontend"


def default_state_log_root(env: Mapping[str, str] | None = None, *, home: Path | None = None) -> Path:
    """분석 상태 로그 폴더. TradingAgents-KR 서버가 결과를 쌓는 곳과 같은 규칙이다."""
    env = os.environ if env is None else env
    configured = env.get("TRADINGAGENTS_RESULTS_DIR")
    if configured:
        return Path(configured).expanduser()
    return (home or Path.home()) / ".tradingagents" / "logs"


def build_dependencies(
    config_path: Path,
    *,
    cache_dir: Path | None,
    frontend_dir: Path | None,
    env: Mapping[str, str] | None = None,
    workers: int = 8,
) -> Dependencies:
    """실제 경계로 ``Dependencies`` 를 만든다. 설정 파일을 읽지 못하면 한국어 ``ConfigError``."""
    config = load_config(config_path)
    externals = Externals(
        listing=fetch_listing,
        prices=fdr_fetcher,
        trend=naver_trend,
        sector=naver_sector,
        analysis=AnalysisClient(config.analysis.base_url),
        state_log_root=default_state_log_root(env),
    )
    return Dependencies(
        connect=db.connect,
        externals=externals,
        config=config,
        cache_dir=cache_dir,
        workers=workers,
        frontend_dir=frontend_dir,
    )
