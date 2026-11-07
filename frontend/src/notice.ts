// 오류 알림. 오류가 어디서 났는지(출처)를 기억해서, 이어지는 조회가 성공했을 때 지워도 되는 오류만 지운다.
//
//  - 'action'  : 사용자가 누른 동작(처리 시작·정리·새 목록)이 실패해서 띄운 오류. 동작은 끝나면 곧바로 조회를 다시 부르는데,
//                그 조회가 성공했다고 이 오류를 지우면 사용자가 읽기도 전에 사라진다 (실제 브라우저에서 약 20ms).
//                다음 동작을 시작하거나 다른 세션을 고를 때까지 둔다.
//  - 'refresh' : 화면을 다시 불러오다(처음·주기적 갱신) 실패해서 띄운 오류. 다음 조회가 성공하면 사라진다.
//
// `el` 은 `hidden`·`textContent` 가 있는 요소다 (DOM 없이도 확인할 수 있게 요소를 받는다).
export function createNotice(el) {
  let source = null;
  return {
    show(message, from = 'refresh') {
      source = from;
      el.textContent = message;
      el.hidden = false;
    },
    clear() {
      source = null;
      el.textContent = '';
      el.hidden = true;
    },
    // 조회가 성공했을 때: 조회에서 난 오류만 지운다 (동작 오류는 둔다)
    clearIfFromRefresh() {
      if (source !== 'action') this.clear();
    },
  };
}
