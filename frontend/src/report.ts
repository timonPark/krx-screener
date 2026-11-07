// 보고서 링크 주소. 화면(app.ts)에서 나눠 둔 이유: DOM 이 없어서 단독으로 확인할 수 있다.
// 보고서 번호(report_id, 예: 028050_20261106_172435)는 TradingAgents-KR 가 저장한 보고서 폴더 이름이다.
// 서버의 실행 번호는 백엔드가 내려주지 않고 화면에도 나오지 않는다.

// 주소 틀(template)의 {report_id} 를 번호로 바꾼 주소. 틀이 없거나 번호가 없으면 null (링크 없이 글자로만 보인다).
export function reportHref(template: string | null | undefined, reportId: string | null | undefined): string | null {
  if (!template || !reportId) return null;
  return template.replaceAll('{report_id}', encodeURIComponent(reportId));
}
