"""고유 API 경로 52개를 10개 평가 기준으로 채운 표(eval/api_criteria_10.csv)를 만든다.

입력: ground_truth/api_migration_v2_L1_L6.json (분석기 v2 결과), eval/runtime_old.json, eval/runtime_new.json
규칙은 모두 이 파일 안의 코드이며 새 판정을 추가하지 않는다. 구현되지 않은 검사(Git 변경 이력,
기능적 동등성)는 '미확인'으로 둔다. 사용: python eval/api_criteria_table.py (저장소 루트에서)
"""
import csv
import json
import collections

R = json.load(open('ground_truth/api_migration_v2_L1_L6.json', encoding='utf-8'))
OLD = json.load(open('eval/runtime_old.json', encoding='utf-8'))['results']
NEW = json.load(open('eval/runtime_new.json', encoding='utf-8'))['results']
REEXPORT_KINDS = {'official_deprecated_export', 'reexport', 'lazy_reexport',
                  'relative_lazy_reexport', 'getattr_forwarder'}

by_path = collections.OrderedDict()
for r in R['results']:
    if r.get('old_path'):
        by_path.setdefault(r['old_path'], []).append(r)

rows = []
for old, rs in by_path.items():
    r = rs[0]
    new = r.get('replacement')
    kinds = {e['kind'] for x in rs for e in x.get('evidence', [])}
    basis = r.get('basis') or ''
    o_parts, n_parts = old.split('.'), (new or '').split('.')
    sym = r.get('symbol_kind') or ''
    is_method = sym == 'method' or (len(o_parts) > 1 and o_parts[-2][:1].isupper())

    c1 = '존재' if new and 'comparison_destination' in kinds else '미존재(후보 없음)'
    c2 = '정확' if new and 'comparison_destination' in kinds else '해당 없음'
    c3 = ('일치' if o_parts[-1] == n_parts[-1] else '불일치') if new else '해당 없음'
    if not new:
        c4 = '해당 없음'
    elif is_method:
        c4 = '일치' if o_parts[-2] == n_parts[-2] else '불일치'
    else:
        c4 = '해당 없음(메서드 아님)'
    c5 = '미확인'
    c6 = '미확인'
    c7 = '확인' if kinds & REEXPORT_KINDS and basis == 'official_export_relationship' else '미확인'
    c8 = '확인' if 'inherited_member' in kinds else '미확인'
    if new and OLD.get(old, {}).get('ok') is False and NEW.get(new, {}).get('ok') is True:
        c9 = '성공(현재 경로) / 기존 경로 실패'
    elif new:
        c9 = '실패'
    else:
        c9 = '미검증'
    if new and c7 == '확인' and c9.startswith('성공'):
        c10 = '확정'
    elif new and c9.startswith('성공'):
        c10 = '검토 필요'
    else:
        c10 = '부적합'
    rows.append({
        '기존 API 경로': old, '현재 API 경로': new or '',
        '출현 수': len(rs), '분석 상태': r['status'], '근거 유형': basis,
        '1 API 존재': c1, '2 경로 일치': c2, '3 이름 일치': c3, '4 클래스 소속': c4,
        '5 기능 동등성': c5, '6 Git 변경 이력': c6, '7 REEXPORT': c7, '8 상속': c8,
        '9 실행 가능': c9, '10 정답 신뢰도': c10,
    })

with open('eval/api_criteria_10.csv', 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)

print('경로', len(rows), '출현', sum(r['출현 수'] for r in rows))
for k in ['1 API 존재', '3 이름 일치', '4 클래스 소속', '7 REEXPORT', '8 상속', '9 실행 가능', '10 정답 신뢰도']:
    c = collections.Counter(r[k] for r in rows)
    print(k, dict(c))
c = collections.Counter(r['10 정답 신뢰도'] for r in rows for _ in range(r['출현 수']))
print('10 (출현 기준)', dict(c))
