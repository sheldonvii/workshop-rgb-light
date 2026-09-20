const fs=require('node:fs'),path=require('node:path');
const {BASE,output,openBrowser,sleep}=require('./browser');
const OUT=output('camera'),report={date:new Date().toISOString(),checks:[]};
let browser;
function check(name,ok,details){report.checks.push({name,ok:!!ok,details});console.log(ok?'PASS':'FAIL',name);if(!ok)report.failed=true;}
async function main(){
  browser=await openBrowser({camera:true});report.browser=browser.version;
  const {ev,until,navigate}=browser;
  await navigate(BASE);
  check('initial state',await ev("S.phase==='idle' && !$('video').srcObject"));
  await ev("$('btnMain').click()");
  const started=await until("S.phase==='running' && S.lastAcceptedSeq>0",20000);
  const cameraState=await ev("({phase:S.phase,banner:$('bannerText').textContent})");
  check('camera starts',started,cameraState);
  if(!started)throw Error(cameraState.banner||'Camera did not start');
  const before=await ev(`(()=>{const v=$('video'),t=S.stream?.getVideoTracks()[0],s=t?.getSettings();return {width:s?.width,height:s?.height,fps:s?.frameRate,device:t?.label,frames:v.getVideoPlaybackQuality().totalVideoFrames,seq:S.lastAcceptedSeq}})()`);
  check('camera at least 720p',before.width>=1280&&before.height>=720,before);
  await sleep(4000);
  check('video and inference progress',await ev(`$('video').getVideoPlaybackQuality().totalVideoFrames>${before.frames} && S.lastAcceptedSeq>${before.seq} && !S.stale`));
  await browser.screenshot(path.join(OUT,'running.png'));
  await ev("window.qaTracks=S.stream.getTracks();$('btnMain').click()");
  check('stop releases camera',await until("S.phase==='idle' && !$('video').srcObject && !S.inFlight && !S.submitTimer && qaTracks.every(t=>t.readyState==='ended')"));
  check('no javascript errors',!browser.errors.length,browser.errors);
}
main().catch(e=>{report.failed=true;report.error=e.stack;console.error(e)}).finally(async()=>{
  await browser?.close();fs.writeFileSync(path.join(OUT,'report.json'),JSON.stringify(report,null,2));process.exitCode=report.failed?1:0;
});
