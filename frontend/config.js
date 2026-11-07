// 화면 설정. 백엔드와 같은 주소에서 서빙하면 apiBase 는 빈 문자열 그대로 둔다.
export const config = {
  // 예: 'http://127.0.0.1:8100' (다른 포트의 백엔드를 부를 때). 끝에 / 없이.
  apiBase: '',
  // 처리 중일 때 조회를 다시 부르는 간격(ms)
  pollMs: 10_000,
  // 목록을 만드는 중일 때 조회를 다시 부르는 간격(ms). 진행 숫자가 자주 바뀌어서 처리 중보다 짧다
  buildPollMs: 2_000,
  // TradingAgents-KR 보고서 화면 주소. {report_id} 가 보고서 번호(예: 028050_20261106_172435)로 바뀐다.
  // null 이면 보고서 번호를 링크 없이 글자로만 보여준다. 예:
  //   'http://localhost:4321/report?id={report_id}'                                   (TradingAgents-KR 웹 화면, `npm run dev`)
  //   'http://127.0.0.1:8000/api/reports/{report_id}/download?format=html'            (분석 서버만 켜 두고 HTML 보고서로 바로)
  reportUrlTemplate: null,
  // 종목당 예상 분석 시간(분). 설계 문서 기준 약 5분.
  minutesPerItem: 5,
  // 주소에 ?mock=1 을 붙이면 백엔드 없이 예시 데이터로 동작한다.
  mock: new URLSearchParams(location.search).has('mock'),
  // mock 모드에서 종목 하나를 처리하는 데 걸리는 시간(ms)과 조회 간격
  mockMsPerItem: 3_000,
  mockPollMs: 1_000,
  // mock 모드에서 새 목록을 만드는 데 걸리는 시간(ms)과 그동안의 조회 간격
  mockBuildMs: 6_000,
  mockBuildPollMs: 500,
};
