import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
plt.rcParams['font.family'] = 'Malgun Gothic'
fig, ax = plt.subplots(figsize=(7.2, 4.6), dpi=200)
ax.set_xlim(0, 760); ax.set_ylim(452, 0); ax.axis('off')
def box(x, y, w, h, main=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle='round,pad=0,rounding_size=8',
                                fc='#e3edf8' if main else 'white', ec='#2f6fd0' if main else '#555', lw=1.6 if main else 1))
def arrow(x1, y1, x2, y2):
    ax.annotate('', xy=(x2, y2), xytext=(x1, y1), arrowprops=dict(arrowstyle='-|>', color='#555', lw=1))
box(24, 44, 712, 44)
ax.text(380, 66, '입력: 노트북 6개(.ipynb) · 비교 버전 배포본 · 근거 버전 배포본', ha='center', va='center', fontsize=9)
ax.text(24, 108, '처리 단계', fontsize=8, color='#666'); ax.text(390, 108, '단계 출력(실험 결과)', fontsize=8, color='#666')
stages = [('① 경로 추출', 'AST 분석 (코드 미실행)', '출현 188건 · 고유 경로 52개', '경로 미확정 24건'),
          ('② 현재 경로 확인', '비교 버전 소스 파싱', '기존 경로 52개 모두 미발견', '(존재 확인 EXISTS 0건)'),
          ('③ 과거 근거 탐색', '근거 버전의 재노출·폐기 선언', '재노출·폐기 선언 126건(38경로)', '패키지 분할 동일 정의 20건(10경로)'),
          ('④ 검증·판정', '신규 경로를 같은 종류로 재확인', '이동 확인 146건(48경로)', '미검증 42건 · 분석 오류 0건')]
ys = [120, 184, 248, 312]
arrow(189, 88, 189, 120)
for i, (n, l, o, o2) in enumerate(stages):
    y = ys[i]
    box(24, y, 330, 48); box(390, y, 346, 48)
    ax.text(40, y + 16, n, fontsize=9, fontweight='bold', va='center'); ax.text(40, y + 34, l, fontsize=8, color='#555', va='center')
    ax.text(406, y + 16, o, fontsize=9, va='center'); ax.text(406, y + 34, o2, fontsize=8.5, color='#555', va='center')
    arrow(354, y + 24, 390, y + 24)
    if i < 3: arrow(189, y + 48, 189, ys[i + 1])
arrow(189, 360, 189, 388)
box(24, 388, 712, 44, True)
ax.text(380, 410, '출력: 출현별 상태·현재 경로·근거(JSON)  |  import 검증: 기존 52개 실패, 현재 48개 성공', ha='center', va='center', fontsize=8.5, fontweight='bold')
fig.savefig('fig1.png', bbox_inches='tight')
