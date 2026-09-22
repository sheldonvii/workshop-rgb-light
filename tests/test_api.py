"""API and composite-face inference checks. Stop other experiences before running."""
import json, os, time, urllib.request, urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
BASE=os.environ.get('BAROMETER_URL','http://127.0.0.1:8321')
import sys
import uuid
import cv2
import numpy as np
sys.path.insert(0, str(ROOT/'server'))
from lighting import Lighting, load_config
OUTPUT = Path(os.environ.get('TEST_OUTPUT_DIR', ROOT/'tests/output/latest'))
GROUP_OUT = OUTPUT/'multi-face'

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

def contracts():
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
        else:check(name+' primary reserved for single face',j['primary'] is None)
        if count>1:
            check('every face classified',all(f.get('emotion',{}).get('distribution') for f in j['faces']))
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


def call(path,data=None,headers=None):
    status, value = request(path,data,headers)
    if status != 200:
        raise RuntimeError(f'{path}: HTTP {status}: {value}')
    return value

def groups():
    GROUP_OUT.mkdir(parents=True,exist_ok=True)
    cfg=load_config(ROOT/'configs/light.json')
    original=(ROOT/'web/test-frames/single.jpg').read_bytes()
    result=call('/api/infer',original)
    img=cv2.imdecode(np.frombuffer(original,np.uint8),1)
    x1,y1,x2,y2=map(int,result['primary']['box'])
    pad=round((x2-x1)*.2)
    crop=img[max(0,y1-pad):min(img.shape[0],y2+pad),max(0,x1-pad):min(img.shape[1],x2+pad)]
    run='multi-test-'+uuid.uuid4().hex
    def session(action):
        return call('/api/light/session',json.dumps(dict(action=action,run_id=run)).encode(),{'Content-Type':'application/json'})
    session('start');seq=0;report=[]
    try:
        for count,tier in [(3,1),(6,2)]:
            session('stop');session('start')
            tile=cv2.resize(crop,(300,360));canvas=np.full((800,1020,3),100,np.uint8)
            for i in range(count):
                y=20+(i//3)*400;x=20+(i%3)*340;canvas[y:y+360,x:x+300]=tile
            data=cv2.imencode('.jpg',canvas)[1].tobytes()
            (GROUP_OUT/f'same-{count}.jpg').write_bytes(data)
            timings=[]
            for _ in range(4):
                seq+=1
                response=call('/api/infer',data,{'X-Run-Id':run,'X-Seq':str(seq)})
                assert response['count']==count,response
                assert all(f['emotion'] and f.get('track_id') for f in response['faces'])
                # Inference itself consumes the interval; do not add 350 ms again.
                timings.append(response['timing']['total_ms']);time.sleep(.05)
            group=response['light']['group']
            assert group['agreed'] and group['confirmed_count']==count,group
            if cfg.get('current_alignment'):
                assert response['light']['current_target_ma']==cfg['current_alignment']['targets_ma'][tier],response['light']
            else:
                assert response['light']['transition']['to_power']==cfg['group_brightness'][tier-1],response['light']
            power=response['light']['transition']['to_power']
            if os.environ.get('TEST_REQUIRE_LIGHT')=='1':
                assert response['light']['device']['status']=='connected',response['light']['device']
            report.append(dict(count=count,power=power,timings_ms=timings,group=group))
            print('PASS real repeated-person composite',count,'faces',power,timings,flush=True)
    finally:session('stop')
    # Deterministic Lighting snapshots test presentation, not model accuracy.
    cfg=load_config(ROOT/'configs/light.json');cfg['enabled']=False;cfg['group_mode']='spatial'
    now=[0.];light=Lighting(cfg,clock=lambda:now[0]);light.session('start','ui-fixture')
    faces=[]
    for i,label in enumerate(['happy','happy','sad']):
        zh='开心' if label=='happy' else '难过'
        emotion=dict(label=label,label_zh=zh,uncertain=False,score=.9,score_type='softmax',
                     distribution=[dict(label=k,score=.9 if k==label else .1) for k in ['happy','sad']])
        faces.append(dict(x1=30+i*340,y1=80,x2=260+i*340,y2=360,emotion=emotion))
    payload=dict(count=3,frame=dict(w=1020,h=800),faces=faces,mirror=True)
    for seq,t in enumerate([0,.35,.8],1):
        now[0]=t;light.accept(light.ticket('ui-fixture',seq),payload)
    fixture=dict(light=light.snapshot(),faces=faces)
    (GROUP_OUT/'mixed-ui.json').write_text(json.dumps(fixture,ensure_ascii=False),encoding='utf-8')
    (GROUP_OUT/'report.json').write_text(json.dumps(dict(scope='Repeated source face tests inference and control, not group accuracy',runs=report),ensure_ascii=False,indent=2),encoding='utf-8')



def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    try:
        contracts()
        groups()
        check('3/6 face inference, tiers and UI fixtures', True)
    except Exception:
        import traceback
        check('API/group exception', False, traceback.format_exc())
    finally:
        (OUTPUT/'api-report.json').write_text(json.dumps({'checks':checks},ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if checks and all(c['ok'] for c in checks) else 1

if __name__ == '__main__': raise SystemExit(main())
