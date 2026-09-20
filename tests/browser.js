/* Shared CDP support. Node 22+ supplies WebSocket; no npm packages are required. */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const ROOT = path.resolve(__dirname, '..');
const BASE = process.env.BAROMETER_URL || 'http://127.0.0.1:8321';
const CHROMIUM = process.env.CHROMIUM || '/usr/lib/chromium/chromium';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function output(suite) {
  const folder = path.join(ROOT, 'tests/output', suite);
  fs.mkdirSync(folder, {recursive:true});
  return folder;
}

function serverPid() {
  const port = new URL(BASE).port || '80';
  const matches = [];
  for (const pid of fs.readdirSync('/proc').filter(x => /^\d+$/.test(x))) {
    try {
      const args = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').filter(Boolean);
      const cwd = fs.readlinkSync(`/proc/${pid}/cwd`);
      const entry = args.find(a => a.endsWith('server/app.py'));
      if (entry && path.resolve(cwd, entry) === path.join(ROOT,'server/app.py') &&
          args[args.indexOf('--port') + 1] === port) matches.push(Number(pid));
    } catch {}
  }
  if (matches.length !== 1) throw Error(`Expected one project server on port ${port}; found ${matches.length}`);
  return matches[0];
}

async function openBrowser({camera=false}={}) {
  if (!globalThis.WebSocket) throw Error('Browser tests require Node.js 22 or newer');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'barometer-browser-'));
  const browser = spawn(CHROMIUM, ['--headless=new','--no-sandbox','--disable-dev-shm-usage',
    '--remote-debugging-port=0',`--user-data-dir=${profile}`,'--window-size=1440,900',
    '--force-device-scale-factor=1','--lang=zh-CN',...(camera?['--use-fake-ui-for-media-stream']:[]),'about:blank'], {stdio:'ignore'});
  let spawnError, ws, sid, id=0;
  const pending = new Map(), errors=[];
  browser.on('error', e => {spawnError=e;});
  const terminate=()=>{if(browser.exitCode===null)browser.kill('SIGTERM');};
  process.once('exit',terminate);
  async function close() {
    for (const {timer,reject} of pending.values()) {clearTimeout(timer);reject(Error('Browser closed'));}
    pending.clear();ws?.close();
    if (browser.exitCode===null && browser.signalCode===null && browser.pid) {
      const exited=new Promise(resolve=>browser.once('exit',resolve));
      browser.kill('SIGTERM');
      await Promise.race([exited,sleep(2000)]);
      if(browser.exitCode===null && browser.signalCode===null){browser.kill('SIGKILL');await exited;}
    }
    process.removeListener('exit',terminate);
    fs.rmSync(profile,{recursive:true,force:true,maxRetries:3,retryDelay:100});
  }
  try {
    const started=Date.now();let version;
    while(Date.now()-started<20000){
      if(spawnError)throw spawnError;
      if(browser.exitCode!==null)throw Error('Chromium exited before CDP became ready');
      try {
        const port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
        version=await(await fetch(`http://127.0.0.1:${port}/json/version`,{signal:AbortSignal.timeout(1500)})).json();break;
      } catch {await sleep(150);}
    }
    if(!version)throw Error('Chromium CDP startup timed out');
    ws=new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('CDP WebSocket startup timed out')),10000);
      ws.addEventListener('open',()=>{clearTimeout(timer);resolve();},{once:true});
      ws.addEventListener('error',e=>{clearTimeout(timer);reject(e);},{once:true});
    });
    ws.addEventListener('message',event=>{
      const m=JSON.parse(event.data),p=pending.get(m.id);
      if(p){pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}
      if(m.method==='Runtime.exceptionThrown')errors.push(m.params);
    });
    const send=(method,params={},sessionId=sid)=>new Promise((resolve,reject)=>{
      const mid=++id;
      const timer=setTimeout(()=>{pending.delete(mid);reject(Error(`${method} timed out`));},20000);
      pending.set(mid,{resolve,reject,timer});
      ws.send(JSON.stringify({id:mid,method,params,...(sessionId?{sessionId}:{})}));
    });
    const target=await send('Target.createTarget',{url:'about:blank'},null);
    sid=(await send('Target.attachToTarget',{targetId:target.targetId,flatten:true},null)).sessionId;
    await send('Page.enable');await send('Runtime.enable');
    const ev=async expression=>{
      const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
      if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));
      return r.result.value;
    };
    const until=async(expression,limit=15000)=>{
      const start=Date.now();
      while(Date.now()-start<limit){if(await ev(expression))return true;await sleep(150);}
      return false;
    };
    return {send,ev,until,close,errors,version:version.Browser,
      navigate:async url=>{await send('Page.navigate',{url});if(!await until("document.readyState==='complete' && typeof S!=='undefined'"))throw Error('Page did not initialize');await sleep(300);},
      viewport:async(w,h)=>{await send('Emulation.setDeviceMetricsOverride',{width:w,height:h,deviceScaleFactor:1,mobile:false});await sleep(200);},
      screenshot:async file=>{const r=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(file,Buffer.from(r.data,'base64'));},
    };
  } catch(error){await close();throw error;}
}
module.exports={ROOT,BASE,CHROMIUM,sleep,output,serverPid,openBrowser};
