/* Real three-face fixture first, then marked synthetic composition UI tests. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {BASE,ROOT,openBrowser,output,sleep}=require('./browser');
let browser;
async function main(){
  browser=await openBrowser();await browser.navigate(BASE+'/?frames=test');
  await browser.ev("$('btnMain').click()");assert(await browser.until("S.phase==='running'"));
  const bytes=fs.readFileSync(path.join(ROOT,'tests/output/multi-face/same-3.jpg')).toString('base64');
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
  const fixture=JSON.parse(fs.readFileSync(path.join(ROOT,'tests/output/multi-face/mixed-ui.json'),'utf8'));
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
main().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{await browser?.close()});
