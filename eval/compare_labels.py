"""수작업 정답표(A)와 분석기 결과(JSON)를 줄 단위로 비교해 정밀도·재현율을 계산한다.
정답표 CSV의 api_paths: 한 줄의 출현을 ';'로 구분. 경로를 확정할 수 없는 출현은 '?'.
사용: python compare_labels.py <labeling_sheet.csv> <analyzer.json>
정답표가 일부 노트북뿐이면 정답표에 있는 노트북의 분석기 결과만 비교한다(L4만 라벨링한 경우 등).
"""
import csv, json, sys
from collections import Counter, defaultdict
from pathlib import Path

def load_gold(path):
    gold = defaultdict(Counter)
    for row in csv.DictReader(open(path, encoding='utf-8-sig', newline='')):
        for p in filter(None, (x.strip() for x in row['api_paths'].split(';'))):
            gold[(row['notebook'], int(row['cell']), int(row['line']))][p] += 1
    return gold

def load_pred(path):
    pred = defaultdict(Counter)
    for r in json.load(open(path, encoding='utf-8'))['results']:
        pred[(Path(r['file']).name, r['cell'], r['line'])][r['old_path'] or '?'] += 1
    return pred

def score(gold, pred, keep=lambda p: True):
    tp = fp = fn = 0
    for k in set(gold) | set(pred):
        g = Counter({p: n for p, n in gold[k].items() if keep(p)})
        q = Counter({p: n for p, n in pred[k].items() if keep(p)})
        hit = sum((g & q).values())
        tp += hit; fp += sum(q.values()) - hit; fn += sum(g.values()) - hit
    pr = tp / (tp + fp) if tp + fp else float('nan')
    rc = tp / (tp + fn) if tp + fn else float('nan')
    return tp, fp, fn, pr, rc

if __name__ == '__main__':
    gold, pred = load_gold(sys.argv[1]), load_pred(sys.argv[2])
    labeled = {k[0] for k in gold} | {r['notebook'] for r in csv.DictReader(open(sys.argv[1], encoding='utf-8-sig', newline=''))}
    pred = defaultdict(Counter, {k: v for k, v in pred.items() if k[0] in labeled})
    print('gold', sum(sum(c.values()) for c in gold.values()), 'pred', sum(sum(c.values()) for c in pred.values()))
    for name, keep in [('전체(? 포함)', lambda p: True), ('경로 확정 항목만', lambda p: p != '?')]:
        tp, fp, fn, pr, rc = score(gold, pred, keep)
        print(f'{name}: TP={tp} FP={fp} FN={fn} 정밀도={pr:.3f} 재현율={rc:.3f}')
    for k in sorted(set(gold) | set(pred)):          # 불일치 줄 목록(오류 분석용)
        if gold[k] != pred[k]:
            print('DIFF', k, 'gold=', dict(gold[k]), 'pred=', dict(pred[k]))
