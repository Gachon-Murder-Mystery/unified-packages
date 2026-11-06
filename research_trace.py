"""Append-only decision/tool journal and separate, escaped HTML replay exports.

No hidden model reasoning is inferred. Public export contains public engine events
only. Table replay includes private exchanges and is operator-only.
"""
import contextvars
import html
import json
import re
import threading
import time
import uuid
from pathlib import Path

CONTEXT = contextvars.ContextVar('decision_context', default={})
_LOCK = threading.RLock()
ROOT = Path(__file__).resolve().parent / 'runs'
SECRETS = set()

def clean(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if any(w in k.lower() for w in ('api_key', 'token', 'password', 'secret', 'authorization')) and k not in ('output_tokens','input_tokens','total_tokens') else clean(v) for k,v in value.items()}
    if isinstance(value, list): return [clean(v) for v in value]
    if isinstance(value, str):
        for secret in tuple(SECRETS):
            if secret: value = value.replace(secret, '[REDACTED]')
        return re.sub(r'(?:sk-|oc_sk_)[A-Za-z0-9_-]{8,}', '[REDACTED]', value)
    return value

def begin(obs, kind, **extra):
    context = {k: obs.get(k) for k in ('game_id','cycle_id','phase','phase_epoch')}
    context.update(agent_id=obs.get('self',{}).get('id'), decision_id=uuid.uuid4().hex, kind=kind, **extra)
    CONTEXT.set(context)
    emit('observation', observation=obs)
    return context

def emit(kind, **data):
    context = CONTEXT.get()
    if not context.get('game_id'): return
    path = ROOT / context['game_id'] / 'detail.jsonl'
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = clean(dict(context, record_id=uuid.uuid4().hex, timestamp=time.time(), event=kind, **data))
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False)+'\n')
            f.flush()

class TraceClient:
    def __init__(self, client): self.client=client
    def __getattr__(self, key): return getattr(self.client,key)
    def bind(self, **credentials):
        for k,v in credentials.items():
            if 'token' in k and isinstance(v,str): SECRETS.add(v)
        return TraceClient(self.client.bind(**credentials))
    def call(self, route, **data):
        if data.get('api_key'): SECRETS.add(data['api_key'])
        active = route in ('agent/action','questions/respond','llm/invoke','gm/command')
        old = CONTEXT.get()
        if route=='gm/command':
            CONTEXT.set(dict(game_id=self.credentials.get('game_id'), decision_id=uuid.uuid4().hex, agent_id=self.credentials.get('controller_id'), kind='gm-code-policy'))
        call_id = data.get('call_id') or uuid.uuid4().hex
        if route=='llm/invoke':
            data['correlation'] = dict(data.get('correlation') or {}, **CONTEXT.get())
        if active: emit('tool_start', route=route, call_id=call_id, request=data)
        try:
            result=self.client.call(route, **data)
            if active: emit('tool_result', route=route, call_id=call_id, result=result)
            return result
        except Exception as exc:
            if active: emit('tool_error', route=route, call_id=call_id, error=str(exc), error_type=type(exc).__name__)
            raise
        finally:
            if route=='gm/command': CONTEXT.set(old)

def page(title, rows, detail=False):
    cards=[]
    for r in rows:
        label=' / '.join(str(r.get(k,'')) for k in ('cycle_id','phase','agent_id','speaker','visibility','event'))
        raw=html.escape(json.dumps(r,ensure_ascii=False,indent=2))
        content=('<details><summary>상세 기록 펼치기</summary><pre>'+raw+'</pre></details>') if detail else '<pre>'+html.escape(str(r.get('text','')))+'</pre>'
        cards.append('<article><small>'+html.escape(label)+'</small>'+content+'</article>')
    return '<!doctype html><meta charset="utf-8"><title>'+title+'</title><style>body{max-width:1100px;margin:32px auto;background:#101a21;color:#f7eedc;font:16px/1.6 system-ui}article{padding:16px;margin:12px 0;background:#20343e;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere}input{padding:12px;width:95%}small{color:#d3dce4}summary{cursor:pointer}</style><h1>'+title+'</h1><input placeholder="이름 · decision_id · 단계 검색" oninput="document.querySelectorAll(\'article\').forEach(x=>x.hidden=!x.textContent.includes(this.value))">'+''.join(cards)

def export_views(record, directory):
    directory=Path(directory)
    details=[]
    path=directory/'detail.jsonl'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try: details.append(json.loads(line))
            except ValueError: continue
    # Preserve native records including provider-returned reasoning summaries.
    details += [dict(event='provider_record', **x) for x in record.get('llm_records',[])]
    details += [dict(event='engine_event', **x) for x in record.get('events',[])]
    chat=[]; public=[]
    phases={'movement':'이동','interaction':'탐문·탐색','deduction':'추리'}
    actions={'move':'이동','stay':'머무르기','explore':'탐색','question':'질문','prepare':'준비','pass':'차례 넘기기','accuse':'최종 지목'}
    for e in record.get('events',[]):
        p=e.get('payload',{}); kind=e.get('type'); text=None
        row={'event':kind,'event_id':e.get('event_id'),'visibility':e.get('visibility'),'source':'engine-event-rendering'}
        if kind=='announcement' and p.get('kind')=='time_out': text='GM: '+p['text']
        elif kind=='phase_advanced': text=f"GM: {p.get('cycle_id')}라운드 · {phases.get(p.get('phase'),p.get('phase'))} 단계입니다."
        elif kind=='roles_assigned': text=f"GM: 플레이어 {p.get('player_count')}명이 참가합니다."
        elif kind=='question_pair': text=f"인물 {p.get('asker_id')} → 인물 {p.get('target_id')}: 질문을 진행합니다."
        elif kind=='game_finished': text=f"GM: 게임 종료 · {p.get('reason')}"
        if text:
            row['text']=text
            public.append(row); chat.append(row)
        elif kind=='action_completed':
            r=p.get('result',{})
            row.update(agent_id=r.get('agent_id'),cycle_id=r.get('cycle_id'),phase=r.get('phase'),action_id=r.get('action_id'))
            row['text']=f"{actions.get(r.get('action'),r.get('action'))} → {r.get('outcome')}"
            if r.get('roll'): row['text']+=' · 주사위 '+str(r['roll'].get('dice'))
            row['text']+='\n'+'\n'.join(i.get('text',str(i)) for i in r.get('acquired_information',[]))
            chat.append(row)
    live=[r for r in details if r.get('event')=='delivered_dialogue']
    # Live journal retains original delivery order; old events are never invented speech.
    if live: chat=live
    for name,rows,title,expand in [('public-log',public,'대외 공개 로그',False),('table-log',chat,'플레이 대화록 · 비공개 대화 포함 / 운영자용',False),('detail-log',clean(details),'상세 로그 · 운영자용 / 모델 출력·코드 판단 구분',True)]:
        (directory/(name+'.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
        (directory/(name+'.html')).write_text(page(title,rows,expand),encoding='utf-8')

if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Render existing full-record.json without inventing dialogue')
    parser.add_argument('record',type=Path)
    args=parser.parse_args()
    export_views(json.loads(args.record.read_text(encoding='utf-8')),args.record.parent)
