// 타입 명시 강제 (#65): 모든 변수·함수(매개변수·반환값 포함)에 타입을 적게 한다. 추론에 맡기지 않는다.
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['frontend/dist/', 'node_modules/'] },
  ...tseslint.configs.recommended,
  {
    files: ['frontend/src/**/*.ts'],
    rules: {
      // 변수: const a: number = 1. 구조 분해(배열·객체)도 대상이다.
      // 함수를 담는 변수(const f = (x: number): string => …)는 아래 반환값 규칙과 매개변수 규칙이 타입을 요구하므로 변수 자체의 타입은 생략한다.
      '@typescript-eslint/typedef': ['error', {
        arrayDestructuring: true,
        arrowParameter: true,
        memberVariableDeclaration: true,
        objectDestructuring: true,
        parameter: true,
        propertyDeclaration: true,
        variableDeclaration: true,
        variableDeclarationIgnoreFunction: true,
      }],
      // 모든 함수의 반환 타입. 콜백·고차 함수·즉시 실행 함수도 예외 없이 적는다.
      '@typescript-eslint/explicit-function-return-type': ['error', {
        allowExpressions: false,
        allowTypedFunctionExpressions: false,
        allowHigherOrderFunctions: false,
        allowDirectConstAssertionInArrowFunctions: false,
      }],
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
);
