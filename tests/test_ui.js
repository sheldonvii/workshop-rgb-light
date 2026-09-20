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
main().catch(e=>{report.failed=true;report.error=e.stack;console.error(e)}).finally(async()=>{
 report.javascriptErrors=browser?.errors||[];if(report.javascriptErrors.length)report.failed=true;
 await browser?.close();fs.writeFileSync(path.join(OUT,'report.json'),JSON.stringify(report,null,2));process.exitCode=report.failed?1:0;
});
