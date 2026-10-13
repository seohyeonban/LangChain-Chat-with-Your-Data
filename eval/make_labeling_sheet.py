"""노트북 코드 셀의 비어 있지 않은 줄을 라벨링 시트(CSV)로 만든다.
분석기 결과(JSON)는 읽지 않는다: 정답표를 분석기와 독립으로 만들기 위해서다.
사용: python make_labeling_sheet.py <노트북 폴더> <출력 CSV>
"""
import csv, json, sys
from pathlib import Path

NOTEBOOKS = ['L1-Document_loading', 'L2-Document_splitting', 'L3-Vectorstores_and_Embeddings',
             'L4-Retrieval', 'L5-Question_answering', 'L6-Chat']

def main(folder, out):
    rows = []
    for name in NOTEBOOKS:
        nb = json.loads((Path(folder) / f'{name}.ipynb').read_text(encoding='utf-8'))
        for ci, cell in enumerate(nb['cells']):          # cell: 0부터, 분석기와 같은 규칙
            if cell['cell_type'] != 'code':
                continue
            for li, text in enumerate(''.join(cell['source']).split('\n'), 1):   # line: 셀 안 1부터
                if text.strip() and not text.strip().startswith('#'):
                    rows.append([name + '.ipynb', ci, li, text, '', ''])
    with open(out, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['notebook', 'cell', 'line', 'code', 'api_paths', 'note'])
        w.writerows(rows)
    print(len(rows), 'lines ->', out)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
