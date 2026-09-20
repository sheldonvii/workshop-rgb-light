/* Camera soak plus real single-face classifier pressure. Defaults to 30 minutes. */
const fs=require('node:fs'),path=require('node:path');
const {ROOT,BASE,output,openBrowser,serverPid,sleep}=require('./browser');
const fixtureMode=process.argv.includes('--frames');
const OUT=output('soak'),duration=Number(process.env.SOAK_SECONDS||1800)*1000;
if(!Number.isFinite(duration)||duration<10000)throw Error('SOAK_SECONDS must be at least 10');
const report={date:new Date().toISOString(),source:fixtureMode?'test images with real inference; no camera validation':'real USB camera',requestedSeconds:duration/1000,samples:[],classification:{count:0,busy:0,errors:[],first:null,last:null}};
let browser,pressureTask,pressureStop=false;
async function pressure(){
  const image=fs.readFileSync(path.join(ROOT,'web/test-frames/single.jpg'));
  let seq=0;
  while(!pressureStop){
    const start=Date.now();
    try{
      const r=await fetch(BASE+'/api/infer',{method:'POST',body:image,headers:{'X-Run-Id':'soak','X-Seq':String(++seq)},signal:AbortSignal.timeout(8000)});
      if(r.status===409)report.classification.busy++;
      else{
        if(!r.ok)throw Error(`Inference HTTP ${r.status}`);
        const j=await r.json(),p=j.primary;
        if(j.count!==1 || p?.model!=='hse-b0' || p.distribution?.length!==8 || Math.abs(p.distribution.reduce((s,v)=>s+v.score,0)-1)>1e-5)throw Error('Invalid B0 single-face result');
        const row={label:p.label,score:p.score,ms:j.timing.total_ms};
        report.classification.count++;report.classification.first??=row;report.classification.last=row;
      }
    }catch(e){report.classification.errors.push(String(e));}
    if(!pressureStop)await sleep(Math.max(10,1000-(Date.now()-start)));
  }
}
async function main(){
  const pid=serverPid(),health=await(await fetch(BASE+'/api/health')).json();
  if(health.emotion_key!=='hse-b0'||health.state!=='ready')throw Error('Start the service with B0 before running the soak');
  browser=await openBrowser({camera:!fixtureMode});report.browser=browser.version;
  const {ev,until,navigate}=browser;
  await navigate(BASE+(fixtureMode?'/?frames=test':''));await ev("$('btnMain').click()");
  if(!await until("S.phase==='running' && S.lastAcceptedSeq>0",20000))throw Error(await ev("$('bannerText').textContent")||'Input did not start');
  pressureTask=pressure();
  const start=Date.now(),interval=Math.min(30000,duration/4);
  while(Date.now()-start<duration){
    const s=await ev(`(()=>{const v=$('video');return {t:Date.now(),phase:S.phase,stale:S.stale,failCount:S.failCount,frames:FRAME_TEST?null:v.getVideoPlaybackQuality().totalVideoFrames,seq:S.lastAcceptedSeq,track:FRAME_TEST?{width:S.trackInfo.w,height:S.trackInfo.h}:S.stream?.getVideoTracks()[0]?.getSettings(),heapBytes:performance.memory?.usedJSHeapSize}})()`);
    s.serverRssKiB=Number(fs.readFileSync(`/proc/${pid}/status`,'utf8').match(/VmRSS:\s+(\d+)/)[1]);
    report.samples.push(s);console.log('SAMPLE',report.samples.length,'frames',s.frames,'seq',s.seq,'RSS KiB',s.serverRssKiB);
    await sleep(Math.min(interval,Math.max(0,duration-(Date.now()-start))));
  }
  report.elapsedSeconds=(Date.now()-start)/1000;
  pressureStop=true;await pressureTask;
  await ev("window.qaTracks=S.stream?.getTracks()||[];$('btnMain').click()");
  report.stopped=await until("S.phase==='idle' && !$('video').srcObject && !S.inFlight && !S.submitTimer && qaTracks.every(t=>t.readyState==='ended')");
  report.passed=report.samples.length>=3&&report.samples.every((s,i)=>s.phase==='running'&&!s.stale&&s.failCount===0&&s.track?.width>=1280&&s.track?.height>=720&&(i===0||((fixtureMode||s.frames>report.samples[i-1].frames)&&s.seq>report.samples[i-1].seq)))&&report.stopped&&report.classification.count>=Math.max(3,Math.floor(duration/1000*.25))&&!report.classification.errors.length&&!browser.errors.length;
  report.javascriptErrors=browser.errors;
}
main().catch(e=>{report.passed=false;report.error=e.stack;console.error(e)}).finally(async()=>{
  pressureStop=true;await pressureTask;await browser?.close();
  fs.writeFileSync(path.join(OUT,'report.json'),JSON.stringify(report,null,2));console.log(report.passed?'PASS':'FAIL',report.source,'and classifier pressure',report.requestedSeconds,'seconds');process.exitCode=report.passed?0:1;
});
