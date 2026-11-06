"""krx-screener 명령줄 진입점."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from krx_screener import universe as uni
from krx_screener.asof import AsOfError, resolve_as_of
from krx_screener.config import ConfigError, load_config
from krx_screener.prices import PriceStore, fdr_fetcher
from krx_screener.results import write_results
from krx_screener.screen import condition_names, run_screen

app = typer.Typer(help="코스피·코스닥 전종목에서 분석 후보를 규칙으로 고르는 도구", no_args_is_help=True)
console = Console()

# 제안서의 기본 가정. `universe` 명령이 "가정 시 남는 수" 를 계산할 때만 쓰며 확정 규칙이 아니다.
_ASSUMED_EXCLUDE = ["is_preferred", "is_spac", "is_managed", "is_zero_volume", "is_new_listing"]

_KIND_NAMES = {
    "preferred": "우선주",
    "spac": "스팩",
    "managed": "관리종목",
    "new_listing": "신규상장",
    "reit": "리츠",
    "caution": "투자주의환기",
    "foreign": "외국기업",
}

_CONDITION_LABELS = {
    "avg_trading_value": "평균 거래대금(근사) 하한 미달",
    "rsi": "RSI 과열",
}

# 시세 조회 구간: 필요한 거래일 수의 약 1.7배 + 여유 (주말·휴장 반영).
_LOOKBACK_FACTOR = 1.7
_LOOKBACK_MARGIN_DAYS = 20


@app.callback()
def main() -> None:
    """krx-screener."""


def _today_kst() -> date:
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def _parse_date(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        console.print(f"[red]날짜 형식이 올바르지 않습니다: {text} (예: 2026-11-06)[/red]")
        raise typer.Exit(2) from None


@app.command()
def universe(
    as_of: Annotated[
        str | None, typer.Option("--date", help="신규상장 판정 기준일 (YYYY-MM-DD, 기본값: 오늘)")
    ] = None,
) -> None:
    """전종목 목록을 받아 종류별 개수를 보여준다. 제외 규칙은 정하지 않는다."""
    base = _parse_date(as_of) or _today_kst()

    try:
        classified = uni.classify(uni.fetch_listing(), base)
    except uni.UniverseFetchError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None

    console.print(f"기준일 {base} · 코스피·코스닥 전종목 {len(classified):,}개 (KONEX 제외)")
    markets = uni.count_by_market(classified)
    console.print("시장별: " + " · ".join(f"{k} {v:,}" for k, v in sorted(markets.items())))

    table = Table(title="종류별 개수 (한 종목이 여러 종류에 속할 수 있음)")
    table.add_column("종류")
    table.add_column("개수", justify="right")
    for item in uni.count_kinds(classified):
        table.add_row(item.label, f"{item.count:,}")
    console.print(table)

    left = uni.remaining_after(classified, _ASSUMED_EXCLUDE)
    labels = ", ".join(uni.KIND_LABELS[k] for k in _ASSUMED_EXCLUDE)
    console.print(f"가정(미확정): {labels} 제외 시 남는 종목 {left:,}개 (겹침 반영)")


@app.command()
def run(
    as_of: Annotated[
        str | None, typer.Option("--date", help="기준일 (YYYY-MM-DD, 기본값: 직전 영업일)")
    ] = None,
    config_path: Annotated[Path, typer.Option("--config", help="설정 파일")] = Path("config.yaml"),
    out: Annotated[Path, typer.Option("--out", help="결과 폴더")] = Path("results"),
    cache: Annotated[Path, typer.Option("--cache", help="시세 캐시 폴더")] = Path("cache"),
    refresh: Annotated[
        bool, typer.Option("--refresh", help="캐시를 읽지 않고 시세를 다시 받아 덮어쓴다")
    ] = False,
    workers: Annotated[int, typer.Option("--workers", min=1, max=16, help="동시 조회 수")] = 8,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="앞에서 N개 종목만 처리 (점검용)")
    ] = None,
) -> None:
    """1단계 필터를 실행하고 결과를 results/<기준일>/ 에 저장한다."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None

    today = _today_kst()
    try:
        base = resolve_as_of(today, _parse_date(as_of), fdr_fetcher)
    except AsOfError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    if base == today:
        console.print("[yellow]기준일이 오늘입니다. 장 마감 전이면 종가가 확정되지 않았고 캐시도 쓰지 않습니다.[/yellow]")

    try:
        classified = uni.classify(uni.fetch_listing(), base)
    except uni.UniverseFetchError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    if limit:
        classified = classified.head(limit).reset_index(drop=True)

    lookback = int(config.filters.required_rows * _LOOKBACK_FACTOR) + _LOOKBACK_MARGIN_DAYS
    store = PriceStore(fdr_fetcher, base, lookback, cache, today=today, refresh=refresh)

    console.print(f"기준일 {base} · 대상 목록 {len(classified):,}개")
    with Progress(
        TextColumn("시세 조회·판정"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as bar:
        task = bar.add_task("run", total=None)

        def on_progress(done: int, total: int) -> None:
            bar.update(task, completed=done, total=total)

        result = run_screen(classified, config, base, store, workers, on_progress)

    out_dir = write_results(result, config, out, datetime.now(ZoneInfo("Asia/Seoul")))
    _print_summary(result, config, out_dir)


def _print_summary(result, config, out_dir: Path) -> None:
    funnel = Table(title=f"단계별 종목 수 (기준일 {result.as_of})")
    funnel.add_column("단계")
    funnel.add_column("종목 수", justify="right")
    funnel.add_row("대상 목록", f"{result.listing_total:,}")
    funnel.add_row("종류 제외 후", f"{result.after_static:,}")
    funnel.add_row("거래정지 의심 제외", f"-{result.zero_volume_excluded:,}")
    funnel.add_row("판정 불가 (시세 조회 실패·부족)", f"-{len(result.undetermined):,}")
    funnel.add_row("필터 판정 대상", f"{result.evaluated:,}")
    funnel.add_row("[bold]후보[/bold]", f"[bold]{len(result.candidates):,}[/bold]")
    console.print(funnel)

    labels = dict(_CONDITION_LABELS)
    for name in condition_names(config.filters):
        if name.startswith("above_ma"):
            labels[name] = f"종가가 {name.removeprefix('above_ma')}일선 이하"
    drops = Table(title="탈락 사유 (첫 번째로 만족하지 못한 조건 / 조건별 독립 집계)")
    drops.add_column("조건")
    drops.add_column("첫 탈락", justify="right")
    drops.add_column("독립 집계", justify="right")
    for name in condition_names(config.filters):
        drops.add_row(labels[name], f"{result.drop_first[name]:,}", f"{result.fail_each[name]:,}")
    console.print(drops)

    if result.static_excluded_by_kind:
        kinds = " · ".join(
            f"{_KIND_NAMES.get(k, k)} {v:,}" for k, v in result.static_excluded_by_kind.items()
        )
        console.print(f"종류 제외(겹침 포함): {kinds}")
    console.print("[dim]거래대금은 종가 × 거래량 근사값입니다. 후보는 등급이 아니라 분석해 볼 만한 종목입니다.[/dim]")
    console.print(f"저장: {out_dir}/candidates.csv, run.json")
