async function visual(){
const fs=require('node:fs'),path=require('node:path');
const {ROOT,BASE,sleep,output,openBrowser,serverPid}=require('./browser');
const OUT=output('ui');
const report={date:new Date().toISOString(),checks:[],screenshots:[]};
function check(name,ok,details){report.checks.push({name,ok:!!ok,details});console.log(ok?'PASS':'FAIL',name);if(!ok)report.failed=true;}
let browser,send,ev,until,viewport,navigate;
async function shot(name){await browser.screenshot(path.join(OUT,name+'.png'));report.screenshots.push(name+'.png');}
async function main(){
 browser=await openBrowser();({send,ev,until,viewport,navigate}=browser);report.browser=browser.version;
 await navigate(BASE);
 await viewport(1440,900);
 await ev("localStorage.setItem('mood-studio-theme','dark')");await navigate(BASE);
 for(const [w,h] of [[1440,900],[1280,720],[480,900],[320,740]]){
  await viewport(w,h);
  const layout=await ev(`(()=>{const b=$('btnMain').getBoundingClientRect(),c=$('stage').getBoundingClientRect(),l=$('barometer').getBoundingClientRect();return {overflow:document.documentElement.scrollWidth>innerWidth,buttonBottom:b.bottom,camera:c.width,lamp:l.width}})()`);
  check('responsive '+w,!layout.overflow&&layout.buttonBottom<h,layout);await shot('idle-'+w);
 }
 await viewport(1440,900);await ev("$('infoToggle').click()");
 check('drawer opens and focuses',await ev("$('settingsDrawer').open&&$('settingsDrawer').contains(document.activeElement)&&$('infoToggle').getAttribute('aria-expanded')==='true'"));
 await shot('settings-dark');
 await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
 await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
 check('escape restores focus',await ev("!$('settingsDrawer').open&&document.activeElement===$('infoToggle')&&!document.body.classList.contains('drawer-open')"));
 await ev("$('themeToggle').click();$('infoToggle').click()");await shot('settings-light');
 await ev("$('closeSettings').click();$('themeToggle').click()");
 await navigate(BASE+'/?frames=test');await ev("$('btnMain').click()");
 check('real B0 stable light',await until("LAMP.current?.label==='neutral'",18000));
 check('raw and stable separate',await ev("S.lastPrimary.distribution.length===8&&$('currentLabel').textContent==='平静'&&document.body.dataset.mood==='neutral'"));
 await sleep(550);await shot('neutral-real-dark');
 const dark=await ev("getComputedStyle(document.body).backgroundColor");
 check('whole background changes',dark==='rgb(23, 62, 57)',dark);
 await ev("$('btnMain').click()");
 check('stop resets output',await ev("!LAMP.current&&document.body.dataset.mood==='none'&&S.phase==='idle'"));
 // Explicit synthetic UI fixtures for color/transition/accessibility, not accuracy.
 // Freeze backend polling only for explicitly synthetic visual fixtures.
 await ev("window.qaFetch=window.fetch;window.fetch=(url,opts)=>url==='/api/light'?new Promise(()=>{}):qaFetch(url,opts);lightSessionQueue");await sleep(350);
 await ev(`S.phase='running';S.lastCount=1;S.stale=false;LAMP.snapshot=null;LAMP.current={label:'happy',label_zh:'开心',score:.8,score_type:'softmax'};LAMP.status='stable';renderCurrent();`);
 await sleep(550);await shot('happy-ui-fixture-dark');
 check('warm background',await ev("getComputedStyle(document.body).backgroundColor==='rgb(73, 59, 29)'"));
 await ev("$('themeToggle').click()");await sleep(550);await shot('happy-ui-fixture-light');
 await ev("LAMP.current={label:'neutral',label_zh:'平静',score:.6,score_type:'softmax'};renderCurrent()");await sleep(550);await shot('neutral-ui-fixture-light');
 await ev("S.phase='idle';LAMP.reset();renderCurrent();$('infoToggle').click()");
 await viewport(320,740);await shot('settings-mobile');
 check('mobile drawer fits',await ev("$('settingsDrawer').getBoundingClientRect().width<=innerWidth&&document.documentElement.scrollWidth<=innerWidth"));
 await ev("$('closeSettings').click()");
 await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
 check('reduced motion',await ev("getComputedStyle(document.body).transitionDuration==='0s'"));

 // Synthetic color fixtures verify every palette; they are not accuracy tests.
 await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
 const accents=await ev(`(()=>{
   const rows=[];
   const rgb=s=>s.startsWith('color(srgb')?s.match(/[\\d.]+/g).map(Number).map(v=>v*255):s.match(/[\\d.]+/g).map(Number);
   const lum=s=>rgb(s).slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
   for(const theme of ['dark','light']){
     document.documentElement.dataset.theme=theme;
     for(const label of Object.keys(MOOD_COLOR)){
       S.phase='running';S.stale=false;LAMP.current={label,label_zh:LABEL_ZH[label],score:.8,score_type:'softmax'};LAMP.status='stable';renderCurrent();
       for(const selector of ['.studio-sticker','#btnMain','.photo-tab']){
         const c=getComputedStyle(document.querySelector(selector)),a=lum(c.backgroundColor),b=lum(c.color);
         rows.push({theme,label,selector,background:c.backgroundColor,contrast:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)});
       }
     }
   }
   LAMP.reset();S.phase='idle';renderCurrent();return rows;
 })()`);
 check('all mood accents readable',accents.every(r=>r.contrast>=4.5),{minimumContrast:Math.min(...accents.map(r=>r.contrast))});
 check('each mood has its own accents',new Set(accents.filter(r=>r.theme==='dark'&&r.selector==='#btnMain').map(r=>r.background)).size===8);
 report.accents=accents;
 await ev("$('closeSettings').click()");
 await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
 await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
 check('keyboard focus visible',await ev("document.activeElement!==document.body && getComputedStyle(document.activeElement).outlineStyle!=='none'"));
 check('live result announced',await ev("$('currentLabel').getAttribute('aria-live')==='polite'"));
 check('no javascript errors',!browser.errors.length,browser.errors);
}
return main().catch(e=>{report.failed=true;report.error=e.stack;console.error(e)}).finally(async()=>{
 report.javascriptErrors=browser?.errors||[];if(report.javascriptErrors.length)report.failed=true;
 await browser?.close();fs.writeFileSync(path.join(OUT,'report.json'),JSON.stringify(report,null,2));process.exitCode=report.failed?1:0;
});

}
async function group(){
/* Real three-face fixture first, then marked synthetic composition UI tests. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {BASE,ROOT,openBrowser,output,sleep}=require('./browser');
let browser;
async function main(){
  browser=await openBrowser();await browser.navigate(BASE+'/?frames=test');
  await browser.ev("$('btnMain').click()");assert(await browser.until("S.phase==='running'"));
  const bytes=fs.readFileSync(path.join(output('multi-face'),'same-3.jpg')).toString('base64');
  await browser.ev(`testFrames.blobs.multi=new Blob([Uint8Array.from(atob(${JSON.stringify(bytes)}),c=>c.charCodeAt(0))],{type:'image/jpeg'});testFrames.idx=4;testFrames.lastSwitch=performance.now()+60000`);
  assert(await browser.until("LAMP.snapshot.group?.agreed&&LAMP.snapshot.group.confirmed_count===3"));
  assert.equal(await browser.ev("$('currentEn').textContent"),'3 人一致');
  assert.equal(await browser.ev("$('peopleList').children.length"),3);
  for(const mode of ['spatial','cycle']){
    await browser.ev(`$('groupMode').value=${JSON.stringify(mode)};$('groupMode').dispatchEvent(new Event('change'))`);
    assert(await browser.until(`!layoutBusy&&LAMP.snapshot.group_mode===${JSON.stringify(mode)}`));
  }
  await browser.viewport(1440,900);
  await browser.screenshot(path.join(output('multi-face'),'same-real.png'));
  await browser.ev("$('btnMain').click();lightSessionQueue");
  await browser.ev("window.qaFetch=fetch;window.fetch=(url,opts)=>url==='/api/light'?new Promise(()=>{}):qaFetch(url,opts)");
  await sleep(350);
  const fixture=JSON.parse(fs.readFileSync(path.join(output('multi-face'),'mixed-ui.json'),'utf8'));
  fixture.light.owner=null;
  await browser.ev(`S.phase='running';S.runId=null;S.lastCount=3;S.stale=false;S.lastOkTs=Date.now();S.lastFaces=${JSON.stringify(fixture.faces)};LAMP.reset();LAMP.apply(${JSON.stringify(fixture.light)});setBanner('warn','fixture','界面验证：合成情绪构成，非模型识别结果');renderCurrent();`);
  assert.equal(await browser.ev("$('currentLabel').textContent"),'多种情绪');
  assert(await browser.ev("$('groupSummary').textContent.includes('开心 2 人')&&$('groupSummary').textContent.includes('难过 1 人')"));
  for(const theme of ['dark','light']){
    for(const [w,h] of [[1440,900],[1280,720],[390,844],[320,740]]){
      await browser.viewport(w,h);
      await browser.ev(`document.documentElement.dataset.theme=${JSON.stringify(theme)};S.lastOkTs=Date.now();updateWeather()`);await sleep(650);
      assert(await browser.ev('document.documentElement.scrollWidth<=innerWidth'));
      assert(await browser.until("[...document.querySelectorAll('.group-scene')].some(el=>Number(getComputedStyle(el).opacity)>.999)",3000),JSON.stringify(await browser.ev("({theme:document.documentElement.dataset.theme,mood:document.body.dataset.mood,sceneKey,styles:[...document.querySelectorAll('.group-scene')].map(el=>({style:el.style.cssText,opacity:getComputedStyle(el).opacity}))})")));
      await browser.screenshot(path.join(output('multi-face'),`mixed-${theme}-${w}.png`));
    }
  }
  await browser.ev("$('peopleList').children[0].click()");
  assert(await browser.ev("$('settingsDrawer').open&&$('personSelect').value===String(selectedPerson)"));
  await browser.ev("$('personSelect').value=$('personSelect').options[2].value;$('personSelect').dispatchEvent(new Event('change'))");
  assert(await browser.ev("$('rawResult').textContent.includes('开心')"));
  await browser.screenshot(path.join(output('multi-face'),'person-settings.png'));
  await browser.ev("$('closeSettings').click();S.phase='idle';S.runId=null;LAMP.reset();renderCurrent()");
  assert.equal(browser.errors.length,0,JSON.stringify(browser.errors));
  console.log('PASS real 3-person brightness, curved group light/background, 8 viewports/themes, per-person scores; mixed UI uses marked synthetic fixture');
}
return main().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{await browser?.close()});

}
async function shared(){
/* Two real browser clients share one backend lamp, using labelled test images. */
const assert=require('node:assert/strict');
const path=require('node:path');
const {BASE,openBrowser,output,sleep}=require('./browser');
let owner,observer;
async function main(){
 owner=await openBrowser();observer=await openBrowser();
 await owner.navigate(BASE+'/?frames=test');
 await owner.ev("$('btnMain').click()");
 assert(await owner.until("S.phase==='running'"));
 await owner.ev("testFrames.idx=1;testFrames.lastSwitch=performance.now()+60000");
 assert(await owner.until("LAMP.current?.label==='neutral'"));
 const id=await owner.ev('S.runId');
 await observer.navigate(BASE+'/?frames=test');
 assert(await observer.until("S.phase==='idle'&&LAMP.current?.label==='neutral'"));
 assert.equal(await observer.ev('LAMP.snapshot.owner'),id);
 await observer.ev("$('btnMain').click()");
 assert(await observer.until("S.phase==='idle'&&S.bannerId==='light-owner'"));
 assert.equal(await owner.ev('LAMP.snapshot.owner'),id);
 await observer.navigate(BASE);
 assert(await observer.until("LAMP.current?.label==='neutral'"));
 await owner.screenshot(path.join(output('lighting'),'owner-neutral.png'));
 await observer.screenshot(path.join(output('lighting'),'observer-neutral.png'));
 await owner.ev("testFrames.idx=4;testFrames.lastSwitch=performance.now()+60000");
 assert(await owner.until("S.lastCount===3&&LAMP.snapshot.group.people.length===3"));
 assert(await observer.until("LAMP.snapshot.group.people.length===3"));
 assert(await owner.ev('S.lastFaces.every(f=>f.emotion?.distribution.length===8)'));
 await sleep(500);

 await owner.screenshot(path.join(output('lighting'),'multi-group.png'));
 await owner.ev("$('btnMain').click();lightSessionQueue");
 assert(await observer.until('LAMP.snapshot.owner===null'));
 assert.equal(owner.errors.length+observer.errors.length,0);
 console.log('PASS shared backend color, observer reload, ownership conflict, multi-person light, stop release');
}
return main().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{await owner?.close();await observer?.close()});

}

const cases={visual,group,shared};
async function run(){
 const index=process.argv.indexOf('--case');
 const selected=index<0?Object.keys(cases):[process.argv[index+1]];
 for(const name of selected){
  if(!cases[name])throw Error('Unknown UI case: '+name);
  console.log('CASE',name);await cases[name]();
  if(process.exitCode)throw Error('UI case failed: '+name);
 }
}
run().catch(e=>{console.error(e);process.exitCode=1});
