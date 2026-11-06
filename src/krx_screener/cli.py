"""krx-screener 명령줄 진입점."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.table import Table

from krx_screener import universe as uni

app = typer.Typer(help="코스피·코스닥 전종목에서 분석 후보를 규칙으로 고르는 도구", no_args_is_help=True)
console = Console()

# 제안서의 기본 가정. 개수를 확인하기 위한 "가정 시 남는 수" 계산에만 쓰고 확정 규칙이 아니다.
_ASSUMED_EXCLUDE = ["is_preferred", "is_spac", "is_managed", "is_zero_volume", "is_new_listing"]


@app.callback()
def main() -> None:
    """krx-screener."""


def _today_kst() -> date:
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


@app.command()
def universe(
    as_of: str = typer.Option(None, "--date", help="신규상장 판정 기준일 (YYYY-MM-DD, 기본값: 오늘)"),
) -> None:
    """전종목 목록을 받아 종류별 개수를 보여준다. 제외 규칙은 정하지 않는다."""
    try:
        base = date.fromisoformat(as_of) if as_of else _today_kst()
    except ValueError:
        console.print(f"[red]날짜 형식이 올바르지 않습니다: {as_of} (예: 2026-11-06)[/red]")
        raise typer.Exit(2) from None

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
