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
 assert(await owner.until("S.lastCount===3&&LAMP.snapshot.mode==='standby'"));
 assert(await observer.until("LAMP.snapshot.mode==='standby'"));
 await sleep(500);
 assert.equal(await owner.ev('LAMP.snapshot.device.status'),'connected');
 await owner.screenshot(path.join(output('lighting'),'multi-standby.png'));
 await owner.ev("$('btnMain').click();lightSessionQueue");
 assert(await observer.until('LAMP.snapshot.owner===null'));
 assert.equal(owner.errors.length+observer.errors.length,0);
 console.log('PASS shared backend color, observer reload, ownership conflict, multi-person standby, USB connected, stop release');
}
main().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{await owner?.close();await observer?.close()});
