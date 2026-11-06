"""krx-screener 명령줄 진입점."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import pandas as pd
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from krx_screener import universe as uni
from krx_screener.analysis import (
    DONE,
    FAILED,
    SKIPPED,
    TIMEOUT,
    AnalysisClient,
    AnalysisError,
    AnalysisOutcome,
    rating_label,
    run_one,
)
from krx_screener.asof import AsOfError, resolve_as_of
from krx_screener.config import ConfigError, load_config
from krx_screener.flow import REASON_LABELS, STATUS_LABELS, apply_flow, naver_trend
from krx_screener.prices import PriceStore, fdr_fetcher
from krx_screener.results import load_done_analysis, write_analysis, write_results
from krx_screener.screen import condition_names, lookback_days, run_screen
from krx_screener.store import HISTORY_RANK, PENDING, QueueEntry, QueueRow, QueueStore

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
    db: Annotated[Path, typer.Option("--db", help="분석 대기열 DB 파일")] = Path("krx-screener.db"),
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

    lookback = lookback_days(config.filters)
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

    # 1.5단계: 후보에만 수급 조회. 읽지 못한 종목은 항상 "미확인" 으로 남고 탈락하지 않는다.
    with Progress(
        TextColumn("수급 조회"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as flow_bar:
        flow_task = flow_bar.add_task("flow", total=None)

        def on_flow_progress(done: int, total: int) -> None:
            flow_bar.update(flow_task, completed=done, total=total)

        result.candidates, result.flow = apply_flow(
            result.candidates,
            config.flow.mode,
            config.flow.window_days,
            base,
            naver_trend,
            workers,
            on_flow_progress,
        )

    out_dir = write_results(result, config, out, datetime.now(ZoneInfo("Asia/Seoul")))
    _print_summary(result, config, out_dir)

    with QueueStore(db) as store:
        inserted = _register(store, base.isoformat(), result.candidates, out_dir)
    console.print(
        f"분석 대기열에 후보 {len(result.candidates):,}개를 등록했습니다 (새로 {inserted:,}개, DB: {db})."
    )


def _print_summary(result, config, out_dir: Path) -> None:
    funnel = Table(title=f"단계별 종목 수 (기준일 {result.as_of})")
    funnel.add_column("단계")
    funnel.add_column("종목 수", justify="right")
    funnel.add_row("대상 목록", f"{result.listing_total:,}")
    funnel.add_row("종류 제외 후", f"{result.after_static:,}")
    funnel.add_row("거래정지 의심 제외", f"-{result.zero_volume_excluded:,}")
    funnel.add_row("판정 불가 (시세 조회 실패·부족)", f"-{len(result.undetermined):,}")
    funnel.add_row("필터 판정 대상", f"{result.evaluated:,}")
    if result.flow is not None and result.flow.mode == "filter":
        # 표시만 모드에서는 1단계 후보와 최종 후보가 같아서 줄을 늘리지 않는다.
        funnel.add_row("1단계 후보", f"{result.flow.stage1_candidates:,}")
        funnel.add_row("수급 약함 탈락", f"-{result.flow.dropped_by_filter:,}")
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
    if result.flow is not None:
        flow = result.flow
        counts = " · ".join(f"{STATUS_LABELS[k]} {v:,}" for k, v in flow.status_counts.items())
        mode = "표시만" if flow.mode == "mark" else "수급 약함 탈락"
        console.print(f"수급(최근 {flow.window_days}거래일, {mode}): {counts}")
        if flow.unconfirmed_by_reason:
            reasons = " · ".join(
                f"{REASON_LABELS.get(k, k)} {v:,}" for k, v in flow.unconfirmed_by_reason.items()
            )
            console.print(f"[yellow]수급 미확인 사유: {reasons} (run.json 에 종목별 사유)[/yellow]")
    console.print("[dim]거래대금은 종가 × 거래량 근사값입니다. 후보는 등급이 아니라 분석해 볼 만한 종목입니다.[/dim]")
    console.print(f"저장: {out_dir}/candidates.csv, run.json")


_STATUS_NAMES = {
    PENDING: "대기",
    DONE: "완료",
    "error": "오류",
    "timeout": "시간 초과",
    SKIPPED: "건너뜀",
}


def _latest_result_dir(out: Path) -> Path | None:
    """results/ 아래에서 candidates.csv 가 있는 가장 최근 기준일 폴더."""
    if not out.is_dir():
        return None
    dirs = [
        d for d in out.iterdir()
        if d.is_dir() and (d / "candidates.csv").exists() and _is_date(d.name)
    ]
    return max(dirs, key=lambda d: d.name) if dirs else None


def _is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


DEFAULT_DB = Path("krx-screener.db")

# analyze 가 보내는 종목의 상태 (대기, 이전 시도 실패)
_NOT_FINISHED = {PENDING, FAILED, TIMEOUT}


def _candidate_entries(candidates: pd.DataFrame) -> list[QueueEntry]:
    """후보 표(거래대금 큰 순서)를 대기열 항목으로. 순번은 1부터."""
    has_flow = "flow_status" in candidates.columns
    entries = []
    for rank, (_, r) in enumerate(candidates.iterrows(), start=1):
        flow = str(r["flow_status"]) if has_flow and pd.notna(r["flow_status"]) else ""
        entries.append(QueueEntry(str(r["code"]), str(r["name"]), rank, flow))
    return entries


def _register(
    store: QueueStore, trade_date: str, candidates: pd.DataFrame, out_dir: Path
) -> int:
    """후보를 대기열에 넣는다 (여러 번 넣어도 안전). 새로 들어간 종목 수를 돌려준다.

    이 기준일을 처음 넣는 경우에만, DB 도입 전 방식(analysis.csv)으로 끝난 결과를 가져온다.
    """
    entries = _candidate_entries(candidates)
    inserted = store.enqueue(trade_date, entries)
    if entries and inserted == len(entries):
        store.import_done(trade_date, load_done_analysis(out_dir))
    return inserted


def _waiting(store: QueueStore, trade_date: str) -> list[QueueRow]:
    """아직 안 끝난 종목 (대기 + 이전 시도 실패), 거래대금 큰 순서."""
    return [r for r in store.rows(trade_date, HISTORY_RANK - 1) if r.state in _NOT_FINISHED]


def _plan(rows: list[QueueRow], redo: bool) -> list[tuple[QueueRow, str]]:
    """각 후보를 reuse(끝난 결과 재사용) / skip(보내지 않음) / send(보냄) 로 나눈다.

    ``redo`` 면 끝난 종목도 다시 보내지만, 새 결과가 나오기 전까지 기존 등급은 DB 에 그대로 있다.
    """
    plan = []
    for r in rows:
        if r.state == SKIPPED:
            action = "skip"
        elif r.state == DONE and not redo:
            action = "reuse"
        else:
            action = "send"
        plan.append((r, action))
    return plan


@app.command()
def analyze(
    count: Annotated[
        int,
        typer.Option("--count", min=1, help="이번에 처리할 종목 수 (필수)"),
    ],
    offset: Annotated[
        int,
        typer.Option("--offset", min=0, help="목록 앞에서 건너뛸 개수 (거래대금 큰 순서, 기본 0)"),
    ] = 0,
    as_of: Annotated[
        str | None, typer.Option("--date", help="기준일 (기본값: 가장 최근 결과 폴더)")
    ] = None,
    config_path: Annotated[Path, typer.Option("--config", help="설정 파일")] = Path("config.yaml"),
    out: Annotated[Path, typer.Option("--out", help="결과 폴더")] = Path("results"),
    db: Annotated[Path, typer.Option("--db", help="분석 대기열 DB 파일")] = DEFAULT_DB,
    yes: Annotated[bool, typer.Option("--yes", help="확인 질문 없이 진행")] = False,
    redo: Annotated[
        bool, typer.Option("--redo", help="이전에 끝난 분석도 다시 보낸다 (쿼터를 다시 씀)")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="아무것도 보내지 않고 대상 목록과 예상 시간만 보여준다"),
    ] = False,
) -> None:
    """후보를 TradingAgents-KR 로 보내 전체 분석을 돌리고 등급을 모은다.

    종목마다 몇 분이 걸리고 Max 플랜 쿼터를 쓴다. 한 번에 하나씩 순서대로 보내고, 끝나는 즉시
    대기열 DB 에 적는다. `--offset 0 --count 20` 은 목록의 1~20번, `--offset 20 --count 20` 은 21~40번이다.
    이미 끝난 종목은 다시 보내지 않는다.
    `--dry-run` 이면 서버에 연결하지도 않고 쿼터도 쓰지 않는다 (대기열에 목록만 등록).
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None

    requested = _parse_date(as_of)
    out_dir = out / requested.isoformat() if requested else _latest_result_dir(out)
    if out_dir is None or not (out_dir / "candidates.csv").exists():
        console.print("[red]후보 파일이 없습니다. 먼저 `krx-screener run` 을 실행하세요.[/red]")
        raise typer.Exit(1)

    try:
        candidates = pd.read_csv(out_dir / "candidates.csv", dtype={"code": str})
        _candidate_entries(candidates)  # code·name 컬럼이 있는지 미리 확인
    except (OSError, KeyError, ValueError, pd.errors.ParserError):
        console.print(f"[red]후보 파일을 읽지 못했습니다: {out_dir / 'candidates.csv'}[/red]")
        raise typer.Exit(1) from None

    trade_date = out_dir.name
    with QueueStore(db) as store:
        _register(store, trade_date, candidates, out_dir)
        rows = store.rows(trade_date, count, offset)
        if not rows:
            console.print(
                f"{offset + 1}번부터는 종목이 없습니다 (후보 목록은 {len(candidates):,}개). "
                "아무것도 보내지 않았습니다."
            )
            return
        plan = _plan(rows, redo)
        n_send = sum(1 for _, a in plan if a == "send")
        n_reuse = sum(1 for _, a in plan if a == "reuse")
        n_skip = sum(1 for _, a in plan if a == "skip")
        console.print(
            f"기준일 {trade_date} · 후보 {len(candidates):,}개 중 {rows[0].rank}~{rows[-1].rank}번 "
            f"{len(rows)}개 — 보낼 것 {n_send}개 · 이전 결과 재사용 {n_reuse}개 · "
            f"건너뜀(영숫자 코드) {n_skip}개"
        )
        if dry_run:
            _print_dry_run(store, plan)
            return

        client = AnalysisClient(config.analysis.base_url)
        if n_send:
            console.print(
                f"[yellow]종목마다 몇 분이 걸리고 Max 플랜 쿼터를 씁니다 "
                f"(effort: {config.analysis.effort}, 서버: {config.analysis.base_url}).[/yellow]"
            )
            if not yes and not typer.confirm("계속할까요?"):
                console.print("중단했습니다. 아무것도 보내지 않았습니다.")
                raise typer.Exit(1)
            try:
                client.check_health()
            except AnalysisError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from None

        interrupted = False
        for index, (row, action) in enumerate(plan, start=1):
            if action == "send":
                try:
                    outcome = run_one(
                        client,
                        row.code,
                        row.name,
                        trade_date,
                        config.analysis.effort,
                        config.analysis.timeout_seconds,
                        config.analysis.poll_interval_seconds,
                    )
                except KeyboardInterrupt:
                    interrupted = True
                    break
                if row.state == DONE and outcome.status != DONE:
                    # 다시 분석하다 실패했다고 이미 있는 등급을 지우지 않는다.
                    note = f"재분석 실패, 기존 등급 유지 ({outcome.elapsed_seconds:.0f}초)"
                else:
                    store.mark(trade_date, outcome, config.analysis.effort)  # 끝나는 즉시 DB 에 적는다
                    write_analysis(out_dir, store.all_outcomes(trade_date))
                    note = f"{outcome.elapsed_seconds:.0f}초"
            else:
                outcome = row.to_outcome()
                note = "재사용" if action == "reuse" else "보내지 않음"
            result_text = (
                rating_label(outcome.decision) if outcome.status == DONE
                else f"{_STATUS_NAMES.get(outcome.status, outcome.status)}: {outcome.message}"
            )
            console.print(
                f"[{index}/{len(plan)}] {row.code} {row.name} → {result_text} ({note})",
                highlight=False,
            )

        if interrupted:
            console.print("[yellow]중단했습니다. 지금까지의 결과는 DB 에 저장했습니다. "
                          "다시 실행하면 이어서 합니다.[/yellow]")
        flow = {r.code: r.flow_status for r in rows}
        codes = {r.code for r in rows}
        outcomes = [o for o in store.all_outcomes(trade_date) if o.code in codes]
        _print_analysis_summary(outcomes, flow, out_dir)
        left = len(_waiting(store, trade_date))
        next_offset = offset + count
        if next_offset < len(candidates):
            console.print(
                f"목록 전체에서 안 끝난 종목 {left}개. 다음 구간: "
                f"krx-screener analyze --offset {next_offset} --count {count}"
            )
        else:
            console.print(f"목록 끝까지 왔습니다. 안 끝난 종목 {left}개.")
    if interrupted:
        raise typer.Exit(130)


def _print_analysis_summary(
    outcomes: list[AnalysisOutcome], flow: dict[str, str], out_dir: Path
) -> None:
    counts: dict[str, int] = {}
    for o in outcomes:
        label = _STATUS_NAMES.get(o.status, o.status)
        counts[label] = counts.get(label, 0) + 1
    console.print("분석 결과: " + (" · ".join(f"{k} {v}" for k, v in counts.items()) or "없음"))

    ratings: dict[str, int] = {}
    for o in outcomes:
        if o.status == DONE:
            label = rating_label(o.decision)
            ratings[label] = ratings.get(label, 0) + 1
    if ratings:
        console.print("등급 분포: " + " · ".join(f"{k} {v}" for k, v in ratings.items()))

    targets = [o for o in outcomes if o.is_target]
    show_flow = any(flow.values())
    table = Table(title="최종 등급이 매수·비중 확대인 종목")
    table.add_column("종목코드")
    table.add_column("종목명")
    table.add_column("등급")
    if show_flow:
        table.add_column("수급")
    for o in targets:
        cells = [o.code, o.name, rating_label(o.decision)]
        if show_flow:
            cells.append(flow.get(o.code, ""))
        table.add_row(*cells)
    if targets:
        console.print(table)
    else:
        console.print("최종 등급이 매수·비중 확대인 종목은 없습니다.")
    console.print("[dim]등급은 모델 분석의 결론이며 투자 성과를 보증하지 않습니다. 후보 목록으로 읽으세요.[/dim]")
    console.print(f"저장: {out_dir}/analysis.csv (DB 에서 내보낸 사본)")


@app.command("list")
def list_queue(
    as_of: Annotated[
        str | None, typer.Option("--date", help="기준일 (기본값: 대기열에 있는 가장 최근 기준일)")
    ] = None,
    db: Annotated[Path, typer.Option("--db", help="분석 대기열 DB 파일")] = DEFAULT_DB,
    state: Annotated[
        str | None,
        typer.Option("--state", help="이 상태만 보기: pending, done, error, timeout, skipped"),
    ] = None,
    offset: Annotated[int, typer.Option("--offset", min=0, help="앞에서 건너뛸 개수")] = 0,
    count: Annotated[int | None, typer.Option("--count", min=1, help="보여줄 개수 (기본: 전부)")] = None,
) -> None:
    """분석 대기열의 전체 목록과 처리 여부를 보여준다.

    서버에 연결하지 않고 아무것도 바꾸지 않는다. 분석이 돌아가는 중에도 볼 수 있다.
    """
    if state is not None and state not in _STATUS_NAMES:
        console.print(f"[red]--state 는 {', '.join(_STATUS_NAMES)} 중 하나여야 합니다.[/red]")
        raise typer.Exit(2)
    requested = _parse_date(as_of)
    if not db.exists():
        console.print(f"[red]분석 대기열 DB 가 없습니다: {db}. 먼저 `krx-screener run` 을 실행하세요.[/red]")
        raise typer.Exit(1)

    with QueueStore(db) as store:
        trade_date = requested.isoformat() if requested else store.latest_date()
        if trade_date is None:
            console.print("[red]대기열이 비어 있습니다. 먼저 `krx-screener run` 을 실행하세요.[/red]")
            raise typer.Exit(1)
        # 후보에서 빠져 기록으로만 남은 종목(순번이 매우 큼)은 목록에서 뺀다
        everyone = [r for r in store.rows(trade_date, HISTORY_RANK - 1) if r.rank < HISTORY_RANK]

    if not everyone:
        console.print(f"[red]{trade_date} 기준의 목록이 없습니다.[/red]")
        raise typer.Exit(1)

    shown = [r for r in everyone if state is None or r.state == state]
    shown = shown[offset:] if count is None else shown[offset : offset + count]
    show_flow = any(r.flow_status for r in shown)

    table = Table(title=f"분석 목록 (기준일 {trade_date})")
    table.add_column("순번", justify="right")
    table.add_column("종목코드")
    table.add_column("종목명")
    if show_flow:
        table.add_column("수급")
    table.add_column("처리 여부")
    table.add_column("등급")
    table.add_column("소요", justify="right")
    for r in shown:
        cells = [str(r.rank), r.code, r.name]
        if show_flow:
            cells.append(r.flow_status)
        cells.append(_STATUS_NAMES.get(r.state, r.state))
        cells.append(rating_label(r.decision or None))
        cells.append(f"{r.elapsed_seconds:.0f}초" if r.elapsed_seconds else "")
        table.add_row(*cells)
    console.print(table)

    counts: dict[str, int] = {}
    for r in everyone:
        counts[r.state] = counts.get(r.state, 0) + 1
    ordered = " · ".join(
        f"{_STATUS_NAMES[s]} {counts[s]}" for s in _STATUS_NAMES if s in counts
    )
    console.print(f"전체 {len(everyone)}개 — {ordered}")
    sendable = len(everyone) - counts.get(SKIPPED, 0)
    console.print(f"진행: 완료 {counts.get(DONE, 0)} / 분석 가능 {sendable}")
    ratings: dict[str, int] = {}
    for r in everyone:
        if r.state == DONE:
            label = rating_label(r.decision or None) or "-"
            ratings[label] = ratings.get(label, 0) + 1
    if ratings:
        console.print("등급 분포: " + " · ".join(f"{k} {v}" for k, v in ratings.items()))
    if state is not None or offset or count is not None:
        console.print(f"[dim]표에는 {len(shown)}개만 보입니다 (필터·구간 적용). 요약은 전체 기준입니다.[/dim]")


def _format_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분" if hours else f"{minutes}분"


def _print_dry_run(store: QueueStore, plan: list[tuple[QueueRow, str]]) -> None:
    """보낼 목록과 예상 시간을 보여준다. 서버에 연결하지 않고 분석 상태도 바꾸지 않는다."""
    queue = [r for r, a in plan if a == "send"]
    show_flow = any(r.flow_status for r in queue)
    table = Table(title=f"분석에 보낼 목록 {len(queue)}개 (거래대금 큰 순서)")
    table.add_column("순번", justify="right")
    table.add_column("종목코드")
    table.add_column("종목명")
    if show_flow:
        table.add_column("수급")
    for r in queue:
        table.add_row(str(r.rank), r.code, r.name, *([r.flow_status] if show_flow else []))
    console.print(table)

    if show_flow:
        counts: dict[str, int] = {}
        for r in queue:
            counts[r.flow_status or "-"] = counts.get(r.flow_status or "-", 0) + 1
        console.print("수급 구성: " + " · ".join(f"{k} {v}" for k, v in counts.items()))

    known = store.done_durations()
    if known and queue:
        avg = sum(known) / len(known)
        console.print(
            f"예상 소요: 약 {_format_duration(avg * len(queue))} "
            f"(끝난 {len(known)}개의 평균 {avg:.0f}초 기준, 종목마다 다름)"
        )
    elif queue:
        console.print("예상 소요: 알 수 없음 (끝난 분석이 없어 시간을 잴 수 없음)")
    console.print("[dim]--dry-run: 서버에 연결하지 않았고 쿼터를 쓰지 않았습니다.[/dim]")
