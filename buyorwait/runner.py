"""Resumable CLI orchestration, evaluation, and submission packaging."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .agent import Agent, MODELS, RateLimiter, safe_error
from .data import Dataset, rows
from .models import Decision, OUTPUT_COLUMNS, money
from .store import Store, atomic_json
from .verify import verify

CODE = Path(__file__).resolve().parents[1]
ROOT = CODE.parent


def code_fingerprint():
    digest=hashlib.sha256()
    for path in sorted((CODE/'buyorwait').glob('*.py'))+sorted((CODE/'prompts').glob('*.txt')):
        digest.update(path.name.encode());digest.update(path.read_bytes())
    return digest.hexdigest()


def export_csv(path, decisions):
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w',encoding='utf-8',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=OUTPUT_COLUMNS)
        writer.writeheader();writer.writerows(decisions)
    temporary.replace(path)


def usage_report(store, run, count, destination):
    report=store.report(run)
    calls=sum(x['calls'] for x in report)
    tokens=sum(x['total_tokens'] for x in report)
    lines=['# Model usage report', '', f'Run: `{run}`. Requests in this run: {count}.', '',
           'Provider: Google Gemini Developer API. Actual usage metadata is recorded in SQLite. Thinking tokens are shown separately; provider total tokens are not double-counted.', '',
           '| Model | Calls/attempts | Input tokens | Visible output tokens | Thinking tokens | Total tokens | Failed calls |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for x in report:
        lines.append('| '+' | '.join(str(x[k]) for k in ('model','calls','input_tokens','output_tokens','thinking_tokens','total_tokens','errors'))+' |')
    lines.extend(['',f'Total calls/attempts: {calls}. Total provider-reported tokens: {tokens}.',
                  f'Average tokens per request: {tokens/count:.2f}.' if count else 'No requests processed.',
                  '', 'Estimated charged model cost: USD 0.00 total and USD 0.00 per request under the participant-confirmed free tier. This is not a billing receipt. No paid-rate equivalent is asserted.',
                  '', 'Resuming this run retains and includes its original calls. Reused completed cases add no new calls. Other run IDs are excluded. Failed calls without usage metadata have unknown token usage, not proven zero usage.'])
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text('\n'.join(lines)+'\n',encoding='utf-8')


def evaluate_samples(dataset, decisions, destination):
    expected={x['request_id']:x for x in dataset.labels}
    metrics={k:0 for k in OUTPUT_COLUMNS if k not in ('request_id','decision_explanation')}
    differences=[]
    for actual in decisions:
        label=expected[actual['request_id']]
        delta={}
        for field in metrics:
            if field=='amount_safe_to_pay':
                match=abs(money(actual[field])-money(label[field]))<=money('.01')
            elif field=='payment_plan':
                def normalize(value):
                    return value if value=='none' else [(p.split(':')[0],money(p.split(':')[1])) for p in value.split('|')]
                match=normalize(actual[field])==normalize(label[field])
            else: match=actual[field]==label[field]
            metrics[field]+=int(match)
            if not match: delta[field]={'actual':actual[field],'expected':label[field]}
        if delta: differences.append({'request_id':actual['request_id'],'differences':delta})
    result={'count':len(decisions),'matches':metrics,'differences':differences}
    atomic_json(destination,result)
    print(json.dumps({'sample_count':len(decisions),'field_matches':metrics}),flush=True)


def run_cases(args):
    load_dotenv(CODE/'.env',override=False)
    load_dotenv(ROOT/'.env',override=False)
    if not (os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')):
        raise RuntimeError('No credential configured. Set GEMINI_API_KEY, or put it in code/.env. Do not paste credentials into chat.')
    dataset=Dataset(args.dataset,args.samples)
    requests=dataset.requests
    if args.ids:
        selected=set(args.ids.split(','));requests=[x for x in requests if x.request_id in selected]
        if {x.request_id for x in requests}!=selected: raise ValueError('Unknown requested IDs')
    if args.limit: requests=requests[:args.limit]
    run=args.run_id or ('samples' if args.samples else 'final')
    if not run or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in run):
        raise ValueError('Run ID must contain only letters, digits, hyphens, underscores')
    directory=CODE/'runs'/run;directory.mkdir(parents=True,exist_ok=True)
    store=Store(CODE/'runs/checkpoints.sqlite')
    engine_hash=code_fingerprint()
    manifest={'run_id':run,'created_at':datetime.now(timezone.utc).isoformat(),'code_fingerprint':engine_hash,
              'sample_mode':args.samples,'request_ids':[r.request_id for r in requests],
              'models':list(MODELS),'forecast_days':90,'assumptions':'See README.md and forecast.py'}
    atomic_json(directory/'manifest.json',manifest)
    agent=Agent(store,RateLimiter(),run)
    complete={};failures={}

    def process(index,request):
        case=dataset.case(request)
        fingerprint=case.fingerprint()+engine_hash
        cached=store.cached(run,request.request_id,fingerprint)
        if cached:
            verify(case,Decision.model_validate(cached['decision']),cached['audit'])
            return request.request_id,cached,True
        model=MODELS[index%len(MODELS)]
        try:
            result=agent.process(case,model)
            store.save(run,request.request_id,fingerprint,'complete',result)
            atomic_json(directory/(request.request_id+'.json'),result)
            return request.request_id,result,False
        except Exception as error:
            failure={'error':safe_error(error),'model':model,'trace':getattr(error,'trace',[])}
            atomic_json(directory/(request.request_id+'.error.json'),failure)
            store.save(run,request.request_id,fingerprint,'unresolved',failure)
            return request.request_id,failure,False

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(process,i,r) for i,r in enumerate(requests)]
        for future in as_completed(futures):
            rid,result,cached=future.result()
            if 'decision' in result: complete[rid]=result['decision']
            else: failures[rid]=result
            print(f'{len(complete)+len(failures)}/{len(requests)} {rid}: '+('cached' if cached else 'complete' if 'decision' in result else result['error']),flush=True)
            atomic_json(directory/'progress.json',{'complete':len(complete),'total':len(requests),'failures':failures})
    decisions=[complete[r.request_id] for r in requests if r.request_id in complete]
    usage_report(store,run,len(requests),directory/'usage_report.md')
    if args.samples: evaluate_samples(dataset,decisions,directory/'sample_report.json')
    if failures:
        atomic_json(directory/'resolved_results.json',decisions)
        raise RuntimeError(f'{len(failures)} unresolved cases; no submission CSV published. Correct and resume --run-id {run}.')
    export_csv(directory/'output.csv',decisions)
    full=not args.samples and len(requests)==len(dataset.requests)
    if full:
        validate_output(dataset,decisions,directory)
        export_csv(ROOT/'output.csv',decisions)
        shutil.copyfile(directory/'usage_report.md',CODE/'evaluation/usage_report.md')
        atomic_json(CODE/'evaluation/final_run.json',manifest)
    print(f'Wrote {len(decisions)} predictions to '+str(ROOT/'output.csv' if full else directory/'output.csv'),flush=True)


def validate_output(dataset, predictions, directory):
    if len(predictions)!=len(dataset.requests) or {r['request_id'] for r in predictions}!={r.request_id for r in dataset.requests}:
        raise ValueError('Output IDs do not exactly match inputs')
    indexed={r['request_id']:r for r in predictions}
    for request in dataset.requests:
        row=indexed[request.request_id]
        if list(row)!=OUTPUT_COLUMNS: raise ValueError('Output columns differ from contract')
        audit=json.loads((directory/(request.request_id+'.json')).read_text(encoding='utf-8'))
        if audit['decision']!=row: raise ValueError('CSV differs from saved decision')
        verify(dataset.case(request),Decision.model_validate(row),audit['audit'])


def replay_cases(args):
    """Recalculate from saved model evidence without charging for another interpretation."""
    from .models import Interpretation
    from .planner import select_plan
    dataset=Dataset(args.dataset,args.samples)
    requests=dataset.requests
    if args.ids: requests=[r for r in requests if r.request_id in set(args.ids.split(','))]
    if args.limit: requests=requests[:args.limit]
    run=args.run_id or ('samples' if args.samples else 'final')
    directory=CODE/'runs'/run
    store=Store(CODE/'runs/checkpoints.sqlite')
    decisions=[];failures={}
    for request in requests:
        try:
            path=directory/(request.request_id+'.json')
            old=json.loads(path.read_text(encoding='utf-8'))
            facts=Interpretation.model_validate(old['audit']['interpretation'])
            case=dataset.case(request)
            decision,audit=select_plan(case,facts)
            verify(case,decision,audit)
            old['decision']=decision.csv_row();old['audit']=audit
            old['replayed_with_code']=code_fingerprint()
            old['certificate_id']=hashlib.sha256(json.dumps(old['decision'],sort_keys=True).encode()).hexdigest()[:24]
            atomic_json(path,old)
            store.save(run,request.request_id,case.fingerprint()+code_fingerprint(),'complete',old)
            decisions.append(decision.csv_row())
        except Exception as error: failures[request.request_id]=safe_error(error)
    if args.samples: evaluate_samples(dataset,decisions,directory/'sample_report.json')
    atomic_json(directory/'replay_errors.json',failures)
    if len(requests)==len(dataset.requests):
        atomic_json(directory/'progress.json',{'complete':len(decisions),'total':len(requests),'failures':failures})
    if failures: raise RuntimeError(f'{len(failures)} cases lack reusable valid evidence; see replay_errors.json')
    export_csv(directory/'output.csv',decisions)
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    manifest['code_fingerprint']=code_fingerprint();manifest['replayed_at']=datetime.now(timezone.utc).isoformat()
    atomic_json(directory/'manifest.json',manifest)
    if not args.samples and len(requests)==len(dataset.requests):
        validate_output(dataset,decisions,directory)
        export_csv(ROOT/'output.csv',decisions)
        usage_report(store,run,len(requests),CODE/'evaluation/usage_report.md')
        atomic_json(CODE/'evaluation/final_run.json',manifest)
    print(f'Replayed {len(decisions)} decisions without new model calls.')


def package(args):
    dataset=Dataset(args.dataset)
    validate_output(dataset,rows(ROOT/'output.csv'),CODE/'runs'/args.run_id)
    manifest=json.loads((CODE/'evaluation/final_run.json').read_text(encoding='utf-8'))
    if manifest['run_id']!=args.run_id or manifest['code_fingerprint']!=code_fingerprint():
        raise ValueError('Final results are stale for this source/run; replay and validate before packaging')
    if not (CODE/'evaluation/usage_report.md').stat().st_size:
        raise ValueError('Usage report is empty')
    target=ROOT/'code.zip'
    temporary=target.with_suffix('.zip.tmp')
    allowed={'.py','.txt','.md','.json','.toml','.ini','.png'}
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(CODE.rglob('*')):
            relative=path.relative_to(CODE)
            if (path.is_file() and (path.suffix in allowed or path.name=='.env.example') and not any(p in {'runs','__pycache__','.pytest_cache','.venv'} for p in relative.parts)
                    and path.name not in ('.env','package_check.json')):
                archive.write(path,relative.as_posix())
    temporary.replace(target)
    if (ROOT/'log.txt').exists(): shutil.copyfile(ROOT/'log.txt',ROOT/'chat_transcript.txt')
    print(f'Packaged {target}',flush=True)


def main():
    parser=argparse.ArgumentParser(description='Buy or Wait: evidence-grounded 90-day affordability')
    parser.add_argument('command',nargs='?',default='run',choices=['run','replay','validate','package','check'])
    parser.add_argument('--dataset',type=Path,default=ROOT/'dataset')
    parser.add_argument('--samples',action='store_true')
    parser.add_argument('--run-id')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--ids')
    parser.add_argument('--workers',type=int,default=4,choices=range(1,9))
    args=parser.parse_args()
    try:
        if args.command=='run': run_cases(args)
        elif args.command=='replay': replay_cases(args)
        elif args.command=='check':
            dataset=Dataset(args.dataset,args.samples)
            for request in dataset.requests:
                case=dataset.case(request)
                for image in case.images: case.image_path(image['image_id'])
            print(f'Input validation passed: {len(dataset.requests)} requests; no model calls.')
        elif args.command=='validate':
            validate_output(Dataset(args.dataset),rows(ROOT/'output.csv'),CODE/'runs'/(args.run_id or 'final'))
            print('All output rows and saved schedules validated.')
        else:
            args.run_id=args.run_id or 'final';package(args)
    except Exception as error:
        print('ERROR: '+safe_error(error),file=sys.stderr)
        return 1
    return 0
