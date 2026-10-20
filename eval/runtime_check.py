"""경로 목록을 '현재 설치된 환경'에서 실제로 import해 본다 (분석기 판정과 무관한 실행 검증).
반드시 비교 버전을 설치한 새 가상환경의 python으로 실행한다.
사용: python runtime_check.py <paths.json> <out.json>   (paths.json: 문자열 배열)
"""
import importlib, json, sys, importlib.metadata as md

def resolve(path):
    parts = path.split('.')
    for i in range(len(parts), 0, -1):          # 가장 긴 모듈 접두사를 import
        mod = '.'.join(parts[:i])
        try:
            obj = importlib.import_module(mod)
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
            continue
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
            return {'ok': True, 'kind': type(obj).__name__}
        except AttributeError as e:
            return {'ok': False, 'error': f'AttributeError: {e}'}
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    return {'ok': False, 'error': err}

if __name__ == '__main__':
    paths = json.load(open(sys.argv[1], encoding='utf-8'))
    pk = ['langchain', 'langchain-community', 'langchain-core', 'langchain-openai',
          'langchain-text-splitters', 'langchain-classic']
    out = {'python': sys.version.split()[0],
           'versions': {p: md.version(p) for p in pk},
           'results': {p: resolve(p) for p in paths}}
    json.dump(out, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(sum(r['ok'] for r in out['results'].values()), '/', len(paths), 'importable')
