"""Read/validate engineering state; never starts product code or changes Git."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
STATES = {'TODO','READY','RUNNING','REVIEW','FIX_REQUIRED','INTEGRATING','DONE','BLOCKED_EXTERNAL','BLOCKED_USER_INPUT'}

def read(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))

def validate(specs, state):
    ids = {t['id'] for t in specs['tasks']}
    errors = []
    if len(ids) != len(specs['tasks']) or len(ids) != 30:
        errors.append('Task registry must contain exactly 30 unique IDs')
    if set(state['tasks']) != ids:
        errors.append('PROGRESS task IDs differ from TASKS.json')
    graph = {t['id']:t['depends_on'] for t in specs['tasks']}
    seen, visiting = set(), set()
    def visit(k):
        if k in visiting:
            errors.append('Dependency cycle at '+k)
            return
        if k in seen:
            return
        visiting.add(k)
        for dep in graph[k]:
            if dep not in ids:
                errors.append(k+' has unknown dependency '+dep)
            else:
                visit(dep)
        visiting.remove(k)
        seen.add(k)
    for key in ids:
        visit(key)
    for task in specs['tasks']:
        key=task['id']
        row=state['tasks'].get(key,{})
        if row.get('status') not in STATES:
            errors.append(key+' has invalid state')
        if not (ROOT/task['card']).is_file():
            errors.append(key+' missing task card')
        if row.get('status') in {'READY','RUNNING','REVIEW','FIX_REQUIRED','INTEGRATING','DONE'}:
            if any(state['tasks'].get(d,{}).get('status')!='DONE' for d in graph[key]):
                errors.append(key+' active with unfinished dependency')
        if row.get('status')=='DONE':
            for field in ['head_sha','source_digest','review_ref','gate_refs','integration_merge_sha']:
                if not row.get(field):
                    errors.append(key+' DONE missing '+field)
            ref=row.get('review_ref')
            if ref:
                try:
                    review=read(ref)
                    if review.get('decision')!='APPROVE':
                        errors.append(key+' missing APPROVE')
                    if review.get('source_digest')!=row.get('source_digest'):
                        errors.append(key+' review digest mismatch')
                except (OSError,ValueError):
                    errors.append(key+' review file unreadable')
        if row.get('status','').startswith('BLOCKED_') and not row.get('blocker_refs'):
            errors.append(key+' blocked without blocker ref')
    return errors

def eligible(specs,state):
    return [t['id'] for t in specs['tasks']
        if state['tasks'][t['id']]['status'] in {'TODO','READY'}
        and all(state['tasks'][d]['status']=='DONE' for d in t['depends_on'])]

def render(specs,state):
    done=sum(r['status']=='DONE' for r in state['tasks'].values())
    lines=['# 开发进度（从PROGRESS.json生成）','',
        f"更新时间：{state['updated_at']}；工程状态：{state['engineering_status']}。",
        f'任务完成：{done}/30。真实认证单列；计划与交接文档检查不计开发完成。','',
        '| Task | 标题 | 依赖 | 状态 | 作者 | 候选/合并 |',
        '|---|---|---|---|---|---|']
    for task in specs['tasks']:
        row=state['tasks'][task['id']]
        shas='/'.join(x[:10] for x in [row.get('head_sha'),row.get('integration_merge_sha')] if x) or '—'
        lines.append(f"| [{task['id']}]({task['card']}) | {task['title']} | {', '.join(task['depends_on']) or '无'} | {row['status']} | {row.get('owner') or '未派发'} | {shas} |")
    lines+=['','当前可派发（仍需文件锁/环境）：'+(', '.join(eligible(specs,state)) or '无'),
        '','更新方法：Supervisor修改PROGRESS.json后执行 `python docs/premium/tools/tracker.py validate` 和 `render`。']
    (ROOT/'PROGRESS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    milestones={'M0':range(0,4),'M1':list(range(4,14))+[19],
        'M2':range(0,24),'M3':range(24,30)}
    status=['# Roadmap实际进展（从PROGRESS.json生成）','',
        f"更新时间：{state['updated_at']}",'','计划依赖见ROADMAP.md；本文件只保存实际完成投影。','']
    for name, numbers in milestones.items():
        keys=[f'T{i:02d}' for i in numbers]
        count=sum(state['tasks'][k]['status']=='DONE' for k in keys)
        status.append(f'- {name}: {count}/{len(keys)} DONE')
    (ROOT/'ROADMAP_STATUS.md').write_text('\n'.join(status)+'\n',encoding='utf-8')

if __name__=='__main__':
    specs,state=read('TASKS.json'),read('PROGRESS.json')
    errors=validate(specs,state)
    if errors:
        print('\n'.join(errors))
        raise SystemExit(1)
    command=sys.argv[1] if len(sys.argv)>1 else 'status'
    if command=='render':
        render(specs,state)
        print('Rendered PROGRESS.md and ROADMAP_STATUS.md')
    elif command=='validate':
        print('PASS: 30 tasks, dependency DAG, state fields, task cards')
    elif command in {'status','ready'}:
        print(json.dumps({'engineering_status':state['engineering_status'],
            'done':sum(r['status']=='DONE' for r in state['tasks'].values()),
            'total':30,'eligible':eligible(specs,state)},ensure_ascii=False))
    else:
        raise SystemExit('Usage: tracker.py validate|render|status|ready')
