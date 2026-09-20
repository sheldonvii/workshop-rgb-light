const fs=require('node:fs'),path=require('node:path');
const {ROOT,BASE,sleep,output,openBrowser,serverPid}=require('./browser');
const OUT=output('contract');
const report={date:new Date().toISOString(),checks:[],screenshots:[]};
function check(name,ok,details){report.checks.push({name,ok:!!ok,details});console.log(ok?'PASS':'FAIL',name);if(!ok)report.failed=true;}
let browser,send,ev,until,viewport,navigate;
async function shot(name){await browser.screenshot(path.join(OUT,name+'.png'));report.screenshots.push(name+'.png');}
const RESTART=process.argv.includes('--restart');
const LIFECYCLE_ONLY=process.argv.includes('--lifecycle')||RESTART;
let stoppedServer=false;
async function stopServer(){
 const pid=serverPid();process.kill(pid,'SIGTERM');
 for(let i=0;i<100;i++){try{process.kill(pid,0);}catch{stoppedServer=true;return;}await sleep(100);}
 throw Error('Server did not stop; refusing to start a duplicate');
}
async function startServer(){
 const {spawn}=require('node:child_process');
 const u=new URL(BASE);if(!['127.0.0.1','localhost'].includes(u.hostname))throw Error('Restart mode requires a local URL');
 const log=fs.openSync(path.join(output('runtime'),'server.log'),'a');
 const child=spawn(path.join(ROOT,'.venv/bin/python'),['-u',path.join(ROOT,'server/app.py'),'--host','127.0.0.1','--port',u.port||'80'],{cwd:ROOT,detached:true,stdio:['ignore',log,log]});
 fs.closeSync(log);let launchError;child.on('error',e=>{launchError=e;});child.unref();
 for(let i=0;i<150;i++){
  if(launchError)throw launchError;
  try{const h=await(await fetch(BASE+'/api/health',{signal:AbortSignal.timeout(1000)})).json();if(h.state==='ready'){stoppedServer=false;return;}}catch{}
  await sleep(200);
 }
 throw Error('Replacement server did not become ready; inspect tests/output/runtime/server.log');
}
async function switchModel(key){await ev(`if(!document.getElementById('settingsDrawer').open)document.getElementById('infoToggle').click();document.getElementById('modelSelect').value=${JSON.stringify(key)};document.getElementById('modelSelect').dispatchEvent(new Event('change'))`);const ready=await until(`!changingModel && currentModel===${JSON.stringify(key)}`,20000);await ev("document.getElementById('settingsDrawer').close()");check('switch '+key,ready,await ev("document.getElementById('modelStatus').textContent"));}
async function main(){
 browser=await openBrowser();({send,ev,until,viewport,navigate}=browser);report.browser=browser.version;
 await navigate(BASE);
 if(LIFECYCLE_ONLY){
   report.scope='Isolated synthetic media and delayed callbacks. Not camera hardware validation.';
   await ev(`window.qaMedia=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices); window.qaStopped=0;
     navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{window.qaResolve=()=>resolve({getTracks:()=>[{stop:()=>window.qaStopped++}]});});
     document.getElementById('btnMain').click();`);
   check('permission request begins',await until("typeof window.qaResolve==='function'"));
   check('starting can cancel',await ev("!document.getElementById('btnMain').disabled && document.getElementById('btnMainLabel').textContent==='取消开启'"));
   await ev("document.getElementById('btnMain').click();window.qaResolve()");await sleep(200);
   check('late media stream released',await ev("S.phase==='idle' && !S.stream && window.qaStopped===1"));
   await ev(`navigator.mediaDevices.getUserMedia=qaMedia;
     window.qaSource=document.createElement('canvas');qaSource.width=1280;qaSource.height=720;
     const q=qaSource.getContext('2d');q.fillStyle='#abc';q.fillRect(0,0,1280,720);
     window.qaStream=qaSource.captureStream(30);navigator.mediaDevices.getUserMedia=async()=>qaStream;
     window.qaToBlob=capCanvas.toBlob.bind(capCanvas);capCanvas.toBlob=callback=>{window.qaEncoded=callback;};
     window.qaFetch=window.fetch;window.qaInferRequests=0;window.fetch=(url,opts)=>{if(url==='/api/infer')qaInferRequests++;return qaFetch(url,opts)};
     document.getElementById('btnMain').click();`);
   check('frame encoding pending',await until("typeof window.qaEncoded==='function'"));
   await ev("document.getElementById('btnMain').click();qaEncoded(new Blob(['late frame']))");await sleep(250);
   check('late encoded frame discarded',await ev("S.phase==='idle' && !S.inFlight && qaInferRequests===0 && qaStream.getTracks().every(t=>t.readyState==='ended')"));
   await ev("capCanvas.toBlob=qaToBlob;window.fetch=qaFetch;navigator.mediaDevices.getUserMedia=qaMedia");
   await navigate(BASE+'/?frames=test');await ev("document.getElementById('btnMain').click()");
   check('real pipeline before failure injection',await until("LAMP.current && !S.stale"));
   if(RESTART)await stopServer();
   else await ev(`window.qaFetch=window.fetch;window.fetch=(url,opts)=>url==='/api/infer'?Promise.reject(new TypeError('QA simulated network outage')):qaFetch(url,opts);`);
   check('service failure invalidates',await until("S.stale && document.body.dataset.mood==='none' && document.getElementById('currentNote').textContent.includes('失效')",12000));
   const before=await ev('S.timeline.length');
   if(RESTART)await startServer();else await ev('window.fetch=qaFetch');
   check('service recovers real inference',await until("!S.stale && LAMP.current && !S.stale && S.bannerId!=='svc-live'",16000));
   check('outage separates timeline',await ev('S.timeline.length')>before);
   await ev("document.getElementById('btnMain').click()");
   check('final resources released',await ev("S.phase==='idle' && !S.inFlight && !S.stream && !S.submitTimer"));
   check('no javascript errors',!browser.errors.length,browser.errors);
   return;
 }
 await switchModel('hse-b2');await switchModel('official');await switchModel('hse-b0');
 await navigate(BASE+'/?frames=test');await viewport(1440,900);await ev("document.getElementById('btnMain').click()");
 check('test source starts',await until("S.phase==='running'"));
 check('eight class result',await until("S.lastPrimary?.distribution?.length===8 && !S.lastPrimary.uncertain"));
 const result=await ev("({p:S.lastPrimary,history:S.timeline.length,rows:document.getElementById('distribution').children.length})");
 check('probabilities normalize',Math.abs(result.p.distribution.reduce((s,x)=>s+x.score,0)-1)<.00001,result);
 await shot('recognizing-1440x900');
 await ev("testFrames.idx=4;testFrames.lastSwitch=performance.now()+60000");
 check('multiple faces detected',await until("S.lastCount===3 && $('stageStatusText').textContent.includes('多人入镜')"));
 check('multiple faces expire the lamp',await until("S.lastCount===3 && !LAMP.current && document.body.dataset.mood==='none'",5000));
 check('multiple faces have no raw classification',await ev('S.lastPrimary===null'));
 const history=await ev('S.timeline.length');await sleep(900);
 check('multiple faces add no emotion history',await ev('S.timeline.length')===history);
 await ev("testFrames.idx=0;testFrames.lastSwitch=performance.now()+60000");
 check('no face state',await until("S.lastCount===0 && !LAMP.current"));
 await ev("testFrames.idx=1;testFrames.lastSwitch=performance.now()+60000");
 check('single face recovers',await until("S.lastCount===1 && LAMP.current?.label==='neutral'"));
 await ev("document.getElementById('mirrorToggle').click()");check('mirror toggle',await ev("document.body.dataset.mirror==='false'"));
 await ev("document.getElementById('btnMain').click()");check('stop releases',await ev("S.phase==='idle' && !S.stream && !S.inFlight && !S.submitTimer && document.body.dataset.mood==='none'"));
 await switchModel('hse-b2');
 await ev("document.getElementById('btnMain').click()");
 check('uncertain result',await until("S.lastPrimary?.uncertain===true"));
 check('uncertainty no false history',await ev("S.timeline.length===0 && document.body.dataset.mood==='none' && document.getElementById('currentLabel').textContent==='观察中'"));await shot('uncertain-1440x900');
 await ev("document.getElementById('btnMain').click()");await switchModel('hse-b0');
 await navigate(BASE);
 await send('Browser.setPermission',{permission:{name:'camera'},setting:'denied',origin:BASE},null);
 await ev("document.getElementById('btnMain').click()");
 check('camera denied recovers',await until("S.phase==='idle' && !document.getElementById('banner').hidden"));
 await shot('camera-permission-denied');
 check('no javascript errors',!browser.errors.length,browser.errors);
}
main().catch(e=>{report.failed=true;report.error=e.stack;console.error(e)}).finally(async()=>{
 report.javascriptErrors=browser?.errors||[];if(report.javascriptErrors.length)report.failed=true;
 if(stoppedServer){try{await startServer();}catch(e){report.failed=true;report.restoreError=String(e);}}
 await browser?.close();fs.writeFileSync(path.join(OUT,LIFECYCLE_ONLY?(RESTART?'restart-report.json':'lifecycle-report.json'):'report.json'),JSON.stringify(report,null,2));process.exitCode=report.failed?1:0;
});
