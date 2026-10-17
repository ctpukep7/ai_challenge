"""Explicit paid live evaluation; fixtures never enter the model prompt or search."""
import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

def check_result(case, result):
    """Contract checks only: semantic usefulness still requires human review."""
    trace=result.get('trace',[])
    actual=[t['tool'] for t in trace if t.get('method')=='tools/call']
    answer=result.get('answer','')
    sources=result.get('sources',[])
    ledger={s['id']:s for t in trace if t.get('method')=='tools/call'
            for s in t.get('result',{}).get('evidence',[]) if isinstance(s,dict) and 'id' in s}
    citations=all(isinstance(s.get('quote'),str) and bool(s['quote'].strip())
                  and s['quote'] in ledger.get(s.get('id'),{}).get('text','')
                  and all(s.get(k)==ledger.get(s.get('id'),{}).get(k)
                          for k in ('text','kind','path','start_line','end_line')) for s in sources)
    expected=case.get('expected_status','answered')
    checks={'status':result.get('status')==expected,
            'complete':not result.get('partial',False) if expected=='answered' else True,
            'answer':isinstance(answer,str) and bool(answer.strip()),
            'tools':all(t in actual for t in case['expected_tools']),
            'facts':all(f.casefold() in answer.casefold() for f in case['expected_facts']),
            'citations':citations and (bool(sources) if expected=='answered' else not sources)}
    if 'max_words' in case:
        checks['concise']=len(answer.split())<=case['max_words']
    if case.get('expect_suggestions'):
        suggestions=result.get('suggestions',[])
        checks['suggestions']=2<=len(suggestions)<=3 and all(isinstance(s,str) and s.strip() for s in suggestions)
    if case.get('expected_source_prefixes'):
        checks['source_coverage']=all(any((s.get('path') or '').startswith(prefix) for s in sources)
                                      for prefix in case['expected_source_prefixes'])
    if case.get('expected_kind'):
        checks['source_kind']=any(s.get('kind')==case['expected_kind'] for s in sources)
    if case['id']=='private-refusal':
        checks['no_private_read']=not any(t.get('tool')=='read_project_file' and
            '.env' in t.get('arguments',{}).get('path','') for t in trace if t.get('method')=='tools/call')
    return checks, actual

def main():
    parser=argparse.ArgumentParser(description='Day31 live API evaluation (makes paid DeepSeek requests)')
    parser.add_argument('--run-live',action='store_true',required=True)
    parser.add_argument('--base-url',default='http://127.0.0.1:5030')
    parser.add_argument('--ids',nargs='+',required=True,help='Select case IDs explicitly; no automatic full paid suite')
    parser.add_argument('--output',type=Path,help='Save complete responses as JSONL to a new local file for manual review')
    parser.add_argument('--conversation',action='store_true',help='Send real preceding answers as history, in the order of --ids')
    args=parser.parse_args()
    cases=json.loads(Path(__file__).with_name('cases.json').read_text())
    by_id={case['id']:case for case in cases}
    if set(args.ids)-by_id.keys(): parser.error('Unknown case ID')
    if len(set(args.ids))!=len(args.ids): parser.error('Duplicate case IDs')
    chosen=[by_id[cid] for cid in args.ids]
    if args.conversation and any(case.get('history') for case in chosen):
        parser.error('--conversation cannot mix actual history with fixture history')
    failed=False
    history=[]
    output=None
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        output=args.output.open('x',encoding='utf-8')
    for case in chosen:
        # Only question/history go to product API, never expected tools/facts/note.
        request_data={k:case[k] for k in ('question','history') if k in case}
        if args.conversation: request_data['history']=history[-8:]
        payload=json.dumps(request_data,ensure_ascii=False).encode()
        request=urllib.request.Request(args.base_url.rstrip('/')+'/api/help',data=payload,headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=190) as response: result=json.load(response)
            checks,actual=check_result(case,result)
            failed |= not all(checks.values())
            if output:
                output.write(json.dumps({'id':case['id'],'question':case['question'],'request':request_data,'checks':checks,'result':result},ensure_ascii=False)+'\n')
                output.flush()
            print(json.dumps({'id':case['id'],'checks':checks,'tools':actual,'status':result['status'],'answer':result.get('answer'),'sources':[{'kind':s.get('kind'),'path':s.get('path')} for s in result.get('sources',[])],'metrics':result.get('metrics')},ensure_ascii=False))
            if args.conversation and result.get('status')!='error' and not result.get('partial',False):
                history=(history+[{'role':'user','content':case['question']},{'role':'assistant','content':result['answer']}])[-8:]
        except Exception as exc:
            failed=True
            failure={'id':case['id'],'error':type(exc).__name__}
            if isinstance(exc,urllib.error.HTTPError): failure['http_status']=exc.code
            print(json.dumps(failure))
            if output:
                output.write(json.dumps(failure)+'\n'); output.flush()
    if output: output.close()
    return 1 if failed else 0

if __name__=='__main__': sys.exit(main())
