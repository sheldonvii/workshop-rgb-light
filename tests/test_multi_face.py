"""Real composite-image inference plus explicitly synthetic group UI fixtures."""
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import uuid
import cv2
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from lighting import Lighting, load_config

BASE=os.environ.get('BAROMETER_URL','http://127.0.0.1:8321')
OUT=ROOT/'tests/output/multi-face'


def request(path,data=None,headers=None):
    with urllib.request.urlopen(urllib.request.Request(BASE+path,data=data,headers=headers or {}),timeout=8) as response:
        return json.load(response)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    original=(ROOT/'web/test-frames/single.jpg').read_bytes()
    result=request('/api/infer',original)
    img=cv2.imdecode(np.frombuffer(original,np.uint8),1)
    x1,y1,x2,y2=map(int,result['primary']['box'])
    pad=round((x2-x1)*.2)
    crop=img[max(0,y1-pad):min(img.shape[0],y2+pad),max(0,x1-pad):min(img.shape[1],x2+pad)]
    run='multi-test-'+uuid.uuid4().hex
    def session(action):
        return request('/api/light/session',json.dumps(dict(action=action,run_id=run)).encode(),{'Content-Type':'application/json'})
    session('start');seq=0;report=[]
    try:
        for count,power in [(3,.78),(6,.95)]:
            session('stop');session('start')
            tile=cv2.resize(crop,(300,360));canvas=np.full((800,1020,3),100,np.uint8)
            for i in range(count):
                y=20+(i//3)*400;x=20+(i%3)*340;canvas[y:y+360,x:x+300]=tile
            data=cv2.imencode('.jpg',canvas)[1].tobytes()
            (OUT/f'same-{count}.jpg').write_bytes(data)
            timings=[]
            for _ in range(4):
                seq+=1
                response=request('/api/infer',data,{'X-Run-Id':run,'X-Seq':str(seq)})
                assert response['count']==count,response
                assert all(f['emotion'] and f.get('track_id') for f in response['faces'])
                # Inference itself consumes the interval; do not add 350 ms again.
                timings.append(response['timing']['total_ms']);time.sleep(.05)
            group=response['light']['group']
            assert group['agreed'] and group['confirmed_count']==count,group
            assert response['light']['transition']['to_power']==power,response['light']
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
    (OUT/'mixed-ui.json').write_text(json.dumps(fixture,ensure_ascii=False),encoding='utf-8')
    (OUT/'report.json').write_text(json.dumps(dict(scope='Repeated source face tests inference and control, not group accuracy',runs=report),ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
