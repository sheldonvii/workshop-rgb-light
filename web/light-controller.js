/* The browser mirrors backend state; it never decides the physical lamp color. */
class LightController {
  constructor() { this.reset(); }
  reset() { this.current=null;this.status='waiting';this.snapshot=null; }
  apply(light) {
    if (!light) return;
    if (this.snapshot?.instance===light.instance && this.snapshot.sample_ms>light.sample_ms) return;
    this.snapshot=light;this.current=light.current;this.status=light.status;
  }
}

let lightSessionQueue=Promise.resolve();
function lightSession(action,runId,keepalive=false) {
  if(!runId)return Promise.resolve(null);
  const request=async()=>{
    const r=await fetch('/api/light/session',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,run_id:runId}),keepalive,signal:AbortSignal.timeout(3000)});
    const data=await r.json();
    if(!data.ok)throw Error(data.error?.message||'灯光会话连接失败');
    return data.light;
  };
  const next=lightSessionQueue.then(request,request);
  lightSessionQueue=next.catch(()=>null);
  return next;
}

function releaseLight(action='stop') {
  const id=S.runId;
  if(id)void lightSession(action,id,true).catch(()=>{});
}

async function pollLight() {
  try {
    if(!document.hidden){
      const r=await fetch('/api/light',{cache:'no-store',signal:AbortSignal.timeout(2000)});
      if(!r.ok)throw Error('灯光服务不可达');
      const data=await r.json();
      const previous=LAMP.current?.label;
      LAMP.apply(data.light);
      if(previous&&!LAMP.current)S.historyGap=true;
      renderCurrent();
    }
  } catch {
    if(LAMP.snapshot)LAMP.snapshot.device={status:'unavailable',message:'灯光服务不可达'};
    LAMP.current=null;LAMP.status='waiting';
    document.dispatchEvent(new Event('barometer-update'));
  } finally { setTimeout(pollLight,100); }
}

let lightHeartbeatBusy=false;
setInterval(async()=>{
  if(typeof S==='undefined'||S.phase!=='running'||document.hidden||lightHeartbeatBusy)return;
  const id=S.runId;lightHeartbeatBusy=true;
  try {
    const light=await lightSession('start',id);
    if(S.runId===id)LAMP.apply(light);
  } catch(err) {
    if(S.runId===id&&String(err.message).includes('另一页面')){
      stop();setBanner('warn','light-owner',err.message);
    }
  } finally {lightHeartbeatBusy=false;}
},2000);
window.addEventListener('DOMContentLoaded',()=>void pollLight());
