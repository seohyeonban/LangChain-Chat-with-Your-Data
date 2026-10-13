# 정답표 A 라벨링 안내 (분석기 결과를 보지 않고 작성)

대상 파일: `labeling_sheet.csv` (노트북 6개, 코드 셀의 비어 있지 않은 줄 586개. `#` 주석 줄 제외).
Excel에서 열어 `api_paths` 열(와 필요하면 `note`)만 채운다. 저장은 CSV(UTF-8)로 한다.

## 1. 무엇을 적는가 (범위 — 라벨링 전에 고정, 도중에 바꾸지 않는다)
`langchain`, `langchain_community`, `langchain_core`, `langchain_openai`, `langchain_text_splitters`, `langchain_classic` 패키지에서 온 다음 세 가지의 **출현**을 적는다.
1. `import`/`from … import …`로 가져온 이름 (이름 하나가 출현 하나)
2. 그 이름으로 만든 호출: 클래스 생성, 함수 호출 (예: `PyPDFLoader(...)`)
3. 위 클래스로 만든 객체의 메서드 호출 (예: `loader.load()`는 `…PyPDFLoader.load`)

제외: 표준 라이브러리, `os`, `openai`, `pandas` 등 langchain 계열이 아닌 패키지, `print` 같은 내장 함수, 단순 변수 대입·속성 읽기(호출이 아닌 것).

## 2. 어떻게 적는가
- 한 줄에 출현이 여러 개면 `;`로 구분한다. 같은 경로가 한 줄에 두 번 나오면 두 번 적는다.
- 경로는 **별칭을 풀고 import한 원래 모듈 기준 전체 경로**로 쓴다.
  `from langchain.document_loaders import PyPDFLoader` → `langchain.document_loaders.PyPDFLoader`
  `loader = PyPDFLoader(f)` / `loader.load()` → 줄마다 `langchain.document_loaders.PyPDFLoader`, `langchain.document_loaders.PyPDFLoader.load`
- 메서드 경로는 "객체를 만든 클래스의 전체 경로 + 메서드 이름"이다.
- **객체의 클래스를 코드만으로 알 수 없는 경우**(예: 다른 메서드의 반환값 `vectordb._collection.count()`)는 경로 대신 `?`를 적는다. 이런 출현은 별도로 집계한다.
- 확신이 없으면 `note` 열에 이유를 적고 경로는 최선의 판단으로 적는다. 나중에 불일치 목록에서 기준이 모호했던 사례로 따로 보고한다.

## 3. 지켜야 할 것
- **분석기 결과 JSON·`L*_result.json`·논문 표를 열어 보지 않는다.** 보고 적으면 정밀도·재현율이 부풀려진다.
- 노트북 코드 셀을 직접 읽는다. 셀 번호는 0부터, 줄 번호는 셀 안에서 1부터이며 시트에 이미 들어 있다.
- 가능하면 다른 사람이 L4 한 개 정도를 독립적으로 적어 두 사람의 일치율을 본다(선택).
- 완성 후 `python compare_labels.py labeling_sheet.csv <분석기 JSON>`을 실행한다(분석기 JSON은 이때 처음 연다).

## 4. 정답표 B (실행 검증)
`runtime_check.py`는 비교 버전(langchain 1.3.13, community 0.4.2, core 1.6.9, openai 패키지 langchain-openai 1.7.0, splitters 1.1.3, classic 1.0.8)을 설치한 새 가상환경에서 경로를 실제로 `import`한다. 사람 판단이 필요 없다.
