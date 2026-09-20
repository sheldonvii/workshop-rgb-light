"""Real API contract checks, compatible with a concurrent camera session."""
import json, os, time, urllib.request, urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
BASE=os.environ.get('BAROMETER_URL','http://127.0.0.1:8321')
checks=[]
def request(path,data=None,headers=None):
    req=urllib.request.Request(BASE+path,data=data,headers=headers or {})
    try:
        with urllib.request.urlopen(req,timeout=8) as r:return r.status,json.load(r)
    except urllib.error.HTTPError as e:
        return e.code,json.load(e)
def check(name,condition,detail=None):
    checks.append({'name':name,'ok':bool(condition),'detail':detail})
    print('PASS' if condition else 'FAIL',name,flush=True)
def infer(frame):
    for _ in range(30):
        status,j=request('/api/infer',(ROOT/'web/test-frames'/frame).read_bytes(),{'X-Run-Id':'contract','X-Seq':'42'})
        if status!=409:return status,j
        time.sleep(.15)
    raise RuntimeError('server stays busy')

status,h=request('/api/health')
check('loaded B0 with CPU provider',status==200 and h['emotion_key']=='hse-b0' and h['models']['emotion']['providers']==['CPUExecutionProvider'])
for name,count in [('none.jpg',0),('single.jpg',1),('multi.jpg',3)]:
    status,j=infer(name)
    check(name+' count',status==200 and j.get('count')==count,{'count':j.get('count'),'timing':j.get('timing')})
    check(name+' run and seq',j.get('run_id')=='contract' and j.get('seq')==42)
    if count==1:
        p=j['primary'];dist=p['distribution']
        check('8 real classes normalized',len(dist)==8 and abs(sum(x['score'] for x in dist)-1)<1e-5)
        check('highest class matches primary',max(dist,key=lambda x:x['score'])['label']==p['label'])
        check('bbox stays within input',0<=p['box'][0]<p['box'][2]<=j['frame']['w'] and 0<=p['box'][1]<p['box'][3]<=j['frame']['h'])
    else:check(name+' no false classification',j['primary'] is None)
status,j=request('/api/infer',b'not an image')
if status==409:
    for _ in range(20):
        time.sleep(.15);status,j=request('/api/infer',b'not an image')
        if status!=409:break
check('bad image rejected',status==400 and j['error']['code']=='bad_image')
status,j=request('/api/model',b'{"model":"not-a-model"}',{'Content-Type':'application/json'})
check('unknown model rejected',status==400 and j['error']['code']=='bad_model')
for p in ['/../server/app.py','/%2e%2e/server/app.py']:
    status,_=request(p);check('static path restricted '+p,status==404)
report={'date':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'checks':checks}
out=ROOT/'tests/output/api'
out.mkdir(parents=True,exist_ok=True)
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
raise SystemExit(0 if all(c['ok'] for c in checks) else 1)
