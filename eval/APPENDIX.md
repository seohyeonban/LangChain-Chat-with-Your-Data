# 부록 (외부 저장소 이전용)

본문 3쪽 제한 때문에 상세 자료는 GitHub 저장소 `https://github.com/seohyeonban/LangChain-Chat-with-Your-Data`에 올려 본문에서 참조한다. **현재 이 저장소(브랜치 `seohyeonban/Papers`)에는 v2 분석기와 v2 결과 JSON이 없다.** 업로드할 파일은 다음이다 (Desktop `LAB/LangChain-Chat-with-Your-Data/` 기준).

| 파일 | 용도 | 본문 연결 |
|---|---|---|
| `api_migration_analyzer_v2.py` (SHA256 `6a439146…d011`) | 분석기 | 표 1 |
| `ground_truth/api_migration_v2_L1_L6.json` | 결과 JSON(표 2·3의 근거) | 표 2, 표 3 |
| `L1~L6` 노트북 6개 | 분석 대상(저장소에 이미 있으며 해시가 결과 JSON과 일치) | 2.2절 |
| `test_api_migration_split_v2.py` | 패키지 분할 규칙 합성 테스트(7개 통과 확인) | 3.2절 |

## A. 실행 명령

```powershell
python api_migration_analyzer_v2.py L1-Document_loading.ipynb L2-Document_splitting.ipynb `
  L3-Vectorstores_and_Embeddings.ipynb L4-Retrieval.ipynb L5-Question_answering.ipynb L6-Chat.ipynb `
  --source-version langchain=0.3.27 --package-version langchain=1.3.13 `
  --package-version langchain-community=0.4.2 --package-version langchain-core=1.6.9 `
  --package-version langchain-openai=1.7.0 --package-version langchain-text-splitters=1.1.3 `
  -o api_migration_v2_L1_L6.json
python -m unittest test_api_migration_split_v2 -v
```
저장 JSON을 만든 당시의 명령 기록은 없다. 위 명령은 `README_api_migration.md`의 명령이며, 재실행 결과의 (파일, 셀, 줄, 열, 경로, 상태, 대체 경로, 근거 유형)이 저장 JSON과 모두 일치함을 확인했다. 네트워크 오류가 나면 분석 오류(ANALYSIS_ERROR)가 늘어나므로 재시도한다.

## B. 핵심 판정 코드 (`MigrationAnalyzer.check`, 763~849행 요약)

```python
current = self.comparison.resolve(path)            # ② 비교 버전에서 경로 확인
if current.state == 'found':   status = 'EXISTS'
elif current.state == 'error': status = 'ANALYSIS_ERROR'
else:                                              # ③ 과거 근거 버전에서 간선 탐색
    for history in self.histories:
        old = history.resolve(path)
        for edge in old.evidence:                  # 실제 소스의 재노출/폐기 선언 간선만 인정
            destination = edge['new_path'] + suffix
            target = self.comparison.resolve(destination)       # ④ 신규 경로 검증
            if target.state != 'found' or target.symbol.kind != old.symbol.kind: continue
    split = None if alternatives else self.package_split(path)  # langchain -> langchain_classic
    # 간선 1개: MOVED / 간선 충돌: 대체 경로 미선택 / 없음: UNVERIFIED(이름 후보는 목록에만)
```

## C. 사례별 판정 근거 (JSON `evidence`에서)

| 기존 경로 | 근거 항목(종류: 배포본 파일:줄) |
|---|---|
| `langchain.document_loaders.PyPDFLoader` | 폐기 선언: langchain 0.3.27 `langchain/document_loaders/__init__.py`:374; 지연 재노출: community 0.3.27 `…/document_loaders/__init__.py`:739; 정의: `pdf.py`:186; 비교: community 0.4.2 :739, `pdf.py` |
| `langchain.text_splitter.RecursiveCharacterTextSplitter` | 재노출: langchain 0.3.27 `text_splitter.py`:3; splitters 0.3.9 `__init__.py`:28; 정의 `character.py`:79; 비교 1.1.3 `__init__.py`:21, `character.py`:91 |
| `langchain.vectorstores.Chroma` | 폐기 선언: langchain 0.3.27 `vectorstores/__init__.py`:181; community 0.3.27 :528, `chroma.py`:54; 비교 0.4.2 :449 |
| `langchain.prompts.PromptTemplate` | 재노출: langchain 0.3.27 `prompts/__init__.py`:38; core 0.3.72 :117, `prompts/prompt.py`:25; 비교 core 1.6.9 :93 |
| `langchain.chains.ConversationalRetrievalChain` | 정의: langchain 0.3.27 `chains/conversational_retrieval/base.py`:260; 분할 규칙(정규화 AST 동일); classic 1.0.8 같은 파일:263 |
| 미검증 4개 경로 | `langchain.chat_models.ChatOpenAI`(6), `langchain.chains.RetrievalQA.from_chain_type`(7), `langchain.llms.OpenAI`(3), `langchain.retrievers.ContextualCompressionRetriever.get_relevant_documents`(2) — 근거 버전에서 관계 간선을 확인하지 못함 |

## D. 이전 분석기와의 차이 (본문 수치에 사용하지 않음)

저장소의 `api_analyzer.py`와 `L1~L6_result.json`은 최신 소스 색인에 경로가 없으면 `OUTDATED`로 두고 마지막 이름이 같은 항목을 대체 경로로 채운다. 188건이 모두 `OUTDATED`, `EXISTS` 0건이며 첫 후보 예: `langchain.vectorstores.Chroma.similarity_search` → `langchain_community.graph_vectorstores.base.GraphVectorStore.similarity_search`.
