"use strict";
const LABEL_ZH = {happy:'开心',neutral:'平静',surprise:'惊讶',sad:'难过',angry:'生气',fear:'害怕',disgust:'厌恶',contempt:'轻蔑'};
const WEATHER_ICON = {happy:'sun',neutral:'moon',surprise:'sparkles',sad:'cloud-rain',angry:'flame',fear:'cloud-fog',disgust:'leaf',contempt:'cloud'};
const LIGHT_NAME = {happy:'日光黄',neutral:'薄荷青',surprise:'琥珀橙',sad:'雨夜蓝',angry:'朱红',fear:'紫罗兰',disgust:'青柠绿',contempt:'玫瑰粉'};
let activeLabels = Object.keys(LABEL_ZH);
let currentModel = 'hse-b0';
let changingModel = false;
let savedTheme = null;
try { savedTheme = localStorage.getItem('mood-studio-theme'); } catch {}
document.documentElement.dataset.theme = savedTheme || 'dark';
$('themeToggle').addEventListener('click', () => {
  savedTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = savedTheme;
  try { localStorage.setItem('mood-studio-theme',savedTheme); } catch {}
  updateWeather();
});
const drawer=$('settingsDrawer');
$('infoToggle').addEventListener('click',()=>{drawer.showModal();document.body.classList.add('drawer-open');$('infoToggle').setAttribute('aria-expanded','true');});
$('closeSettings').addEventListener('click',()=>drawer.close());
drawer.addEventListener('close',()=>{document.body.classList.remove('drawer-open');$('infoToggle').setAttribute('aria-expanded','false');$('infoToggle').focus();});
drawer.addEventListener('click',e=>{const r=drawer.getBoundingClientRect();if(e.target===drawer&&(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom))drawer.close();});
$('mirrorToggle').addEventListener('change', e => { document.body.dataset.mirror=String(e.target.checked); if(!S.stale)drawOverlay(S.lastFaces,S.lastPrimary); });
$('clearHistory').addEventListener('click', () => { S.timeline=[]; S.historyGap=true; renderTimeline(false); updateWeather(); });

function createDistribution() {
  $('colorLegend').replaceChildren(...activeLabels.map(label => {
    const chip=document.createElement('span');chip.className='color-chip';chip.dataset.label=label;
    chip.style.setProperty('--c',MOOD_COLOR[label]);
    const swatch=document.createElement('i');swatch.setAttribute('aria-hidden','true');
    const name=document.createElement('span');name.textContent=LABEL_ZH[label];chip.append(swatch,name);return chip;
  }));
  $('distribution').replaceChildren(...activeLabels.map(label => {
    const row=document.createElement('div'); row.className='dist-item'; row.dataset.label=label;
    row.style.setProperty('--c',MOOD_COLOR[label] || '#779ba7');
    const name=document.createElement('span'); name.textContent=LABEL_ZH[label] || label;
    const score=document.createElement('span'); score.className='dist-value'; score.textContent='待识别';
    const line=document.createElement('span'); line.className='dist-line'; line.setAttribute('aria-hidden','true'); line.append(document.createElement('i'));
    row.append(name,score,line); return row;
  }));
}
function updateWeather() {
  updateGroupView();
  const raw=selectedRaw();
  const p=LAMP.current;
  const valid=!!p;
  $('clearHistory').disabled=!S.timeline.length;
  $('modelSelect').disabled=S.phase!=='idle'||changingModel||!!LAMP.snapshot?.owner;
  $('distributionNote').textContent=raw?.distribution ? '原始分数' : raw ? '此模型只返回首选类别' : S.stale ? '结果已失效' : '等待识别';
  for(const row of $('distribution').children){
    const v=raw?.distribution?.find(x=>x.label===row.dataset.label);
    row.style.setProperty('--score',v?v.score:0);
    row.classList.toggle('is-leading',!!v&&raw.label===v.label);
    row.querySelector('.dist-value').textContent=v?`${(v.score*100).toFixed(1)}%`:raw?'未提供':'待识别';
  }
  $('rawResult').textContent=raw ? `${raw.uncertain?'不确定，较接近':'首选类别'}${raw.label_zh}${raw.score_type==='softmax'?` · ${(raw.score*100).toFixed(1)}%`:''}` : S.stale?'结果已失效':'等待入镜';
  const name=valid?WEATHER_ICON[p.label]:S.phase==='idle'?'power':'scan-face';
  document.body.dataset.light=valid?'on':'standby';
  document.body.dataset.mood=p?.label||'none';
  setLightTarget(valid?(MOOD_COLOR[p.label]||MOOD_COLOR[LAMP.snapshot?.group?.composition[0]?.label]||'#eab7cf'):'#eab7cf',valid?1:.24);
  $('lightName').textContent=valid?(p.label==='mixed'?'各有颜色':LIGHT_NAME[p.label]):'待机光';
  $('lightState').textContent=valid?(LAMP.snapshot?.group?.people.length>1?'从画面左侧到右侧 · 分区渐变':LAMP.status==='stable'?'情绪已确认 · 柔和换色':'保留上一灯色 · 继续观察'):S.stale?'结果暂未更新 · 已回到待机光':raw?.uncertain?'判断不确定 · 保持待机光':'待机光 · 等待稳定表情';
  if(p?.label==='mixed'&&LAMP.snapshot?.group_mode==='cycle'){
    $('lightState').textContent=`整灯轮换 · 正在呈现${LABEL_ZH[LAMP.snapshot.output_label]||''}`;
  }
  const device=LAMP.snapshot?.device;
  $('lightConnection').dataset.status=device?.status||'connecting';
  $('lightConnection').textContent=device?.status==='connected'?`控制器已连接 · 配置 ${device.led_count} 颗灯珠`:device?.status==='disabled'?'屏幕预览 · 实体灯带未启用':device?.status==='disconnected'?'控制器未连接 · 正在重试':device?.status==='unavailable'?'灯光服务暂不可达':'正在连接控制器…';
  $('lightDeviceDetail').textContent=device?.message|| (device?.status==='connected'?`WLED ${device.version} · USB 在线；灯带接线和供电需现场确认`:'检查 USB 连接和 README 中的串口权限说明。');
  for(const chip of $('colorLegend').children)chip.classList.toggle('active',!!valid&&(chip.dataset.label===p.label||LAMP.snapshot?.group?.composition.some(c=>c.label===chip.dataset.label)));
  const symbol=$('weatherSymbol');
  if(symbol.dataset.icon!==name){symbol.dataset.icon=name;symbol.innerHTML=`<i data-lucide="${name||'users'}"></i>`;window.lucide?.createIcons();}
  drawWeather();
}
// Interpolate the backend trajectory locally; polling never creates a new target.
const reducedLight=matchMedia('(prefers-reduced-motion: reduce)');
const lightPaint={hex:'#eab7cf',power:.55,target:'',raf:0,last:0};
function setLightTarget(hex,power){
  const state=LAMP.snapshot;
  const track=state?.device.status!=='unavailable'?state?.transition:null;
  const target=track?`${state.instance}:${state.sample_ms}`:hex+':'+power;
  if(lightPaint.target===target)return;lightPaint.target=target;
  document.body.style.setProperty('--mood-accent',hex);
  cancelAnimationFrame(lightPaint.raf);
  const from=lightPaint.hex,fromPower=lightPaint.power,start=performance.now();
  const channels=h=>[1,3,5].map(i=>parseInt(h.slice(i,i+2),16));
  const a=track?track.from:channels(from),b=track?track.to:channels(hex);
  const duration=reducedLight.matches?0:track?.duration_ms||400;
  const elapsed=track?.elapsed_ms||0;
  const startPower=track?track.from_power*4:fromPower,endPower=track?track.to_power*4:power;
  function frame(now){
    const t=duration?Math.min(1,(elapsed+now-start)/duration):1,e=t*t*(3-2*t);
    lightPaint.hex='#'+a.map((v,i)=>Math.round(v+(b[i]-v)*e).toString(16).padStart(2,'0')).join('');
    lightPaint.power=Math.min(1,startPower+(endPower-startPower)*e);
    paintPixels(track,e,track?track.from_power+(track.to_power-track.from_power)*e:LAMP.snapshot?.power||.06);
    document.body.style.setProperty('--mood',lightPaint.hex);drawWeather();
    if(t<1)lightPaint.raf=requestAnimationFrame(frame);else lightPaint.raf=0;
  }
  lightPaint.raf=requestAnimationFrame(frame);
}
function drawWeather() {
  const canvas=$('weatherCanvas'), box=canvas.getBoundingClientRect(), dpr=Math.min(devicePixelRatio,2);
  if(!box.width)return;
  canvas.width=box.width*dpr;canvas.height=box.height*dpr;
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
  const w=box.width,h=box.height;
  const raw=S.phase==='running'&&!S.stale?S.lastPrimary:null;
  const p=LAMP.current;
  const valid=!!p;
  const standby=!valid,lit=lightPaint.power>.01;
  const hue=lightPaint.hex;
  const dark=document.documentElement.dataset.theme!=='light';
  const path=new Path2D();
  path.moveTo(w*.10,h*.77);
  path.bezierCurveTo(w*-.01,h*.45,w*.10,h*.15,w*.38,h*.13);
  path.bezierCurveTo(w*.68,h*.08,w*.99,h*.13,w*.92,h*.47);
  path.bezierCurveTo(w*.89,h*.71,w*.54,h*.77,w*.34,h*.83);
  path.bezierCurveTo(w*.14,h*.91,w*.44,h*1.02,w*.67,h*.91);
  const color=ctx.createLinearGradient(w*.08,h*.8,w*.91,h*.15);
  const composition=LAMP.snapshot?.group?.composition||[];
  if(composition.length>1){
    let offset=0;const total=composition.reduce((sum,c)=>sum+c.count,0);
    for(const c of composition){
      const start=offset;offset+=c.count/total;
      color.addColorStop(Math.min(start+.025,offset),MOOD_COLOR[c.label]);
      color.addColorStop(Math.max(start,offset-.025),MOOD_COLOR[c.label]);
    }
  }else{
    color.addColorStop(0,hue);
    color.addColorStop(.48,hue);
    color.addColorStop(1,hue);
  }
  const spread=ctx.createRadialGradient(w*.54,h*.54,0,w*.54,h*.54,w*.43);
  spread.addColorStop(0,hue+(lit?(dark?'22':'10'):'00'));spread.addColorStop(1,hue+'00');
  ctx.fillStyle=spread;ctx.fillRect(0,0,w,h);
  ctx.save();ctx.translate(w*.52,h*.93);ctx.scale(1,.095);
  const floor=ctx.createRadialGradient(0,0,0,0,0,w*.4);floor.addColorStop(0,hue+(lit?'60':'10'));floor.addColorStop(1,hue+'00');ctx.fillStyle=floor;ctx.fillRect(-w*.45,-w*.45,w*.9,w*.9);ctx.restore();
  const size=Math.max(7,Math.min(w*.024,15));ctx.lineCap='round';ctx.lineJoin='round';
  ctx.save();ctx.translate(0,5);ctx.strokeStyle=dark?'#100c1c80':'#6b547344';ctx.lineWidth=size+5;ctx.shadowBlur=10;ctx.shadowColor='#09071480';ctx.stroke(path);ctx.restore();
  ctx.strokeStyle=color;ctx.lineWidth=size;ctx.shadowColor=hue;ctx.shadowBlur=lit?25:0;ctx.globalAlpha=.32+.68*lightPaint.power;ctx.stroke(path);
  if(lit){ctx.lineWidth=size*.72;ctx.shadowBlur=12;ctx.stroke(path);}
  if(composition.length>1){ctx.shadowBlur=0;ctx.lineWidth=size;paintGroupCurve(ctx,w,h,composition);}
  if(lit){ctx.shadowBlur=0;ctx.strokeStyle=dark?'#fff2ea':'#fff9f2';ctx.lineWidth=size*.26;ctx.globalAlpha=.88*lightPaint.power;ctx.stroke(path);}
  ctx.globalAlpha=1;ctx.shadowBlur=0;
}
async function applyModelInfo(health) {
  currentModel=health.emotion_key||'official';
  activeLabels=health.labels||Object.keys(LABEL_ZH);
  $('modelSelect').value=currentModel;
  const info=health.models?.emotion;
  $('modelDescription').textContent=info ? `${info.name}，${activeLabels.length} 类。${info.providers?.join(' / ')||'SpacemiT Vision'}。${info.score_type==='logit'?'返回原始分数，不转换为百分比。':'使用 RGB 与作者规定的归一化。'}`:'模型信息暂不可用。';
  createDistribution();updateWeather();
}
$('modelSelect').addEventListener('change',async e=>{
  if(S.phase!=='idle'||changingModel){e.target.value=currentModel;return;}
  const key=e.target.value;changingModel=true;e.target.disabled=true;$('btnMain').disabled=true;
  $('modelStatus').textContent='正在加载并检查模型…';
  try{
    await lightSessionQueue;
    const r=await fetch('/api/model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:key}),signal:AbortSignal.timeout(30000)});
    const j=await r.json();if(!r.ok||!j.ok)throw new Error(j.error?.message||'模型切换失败');
    S.timeline=[];S.historyGap=true;renderTimeline(false);await applyModelInfo(j);$('modelStatus').textContent='模型已就绪，可以开始体验。';
  }catch(err){e.target.value=currentModel;$('modelStatus').textContent=`切换失败。${err.message}`;}
  finally{changingModel=false;e.target.disabled=false;$('btnMain').disabled=false;}
});
document.addEventListener('barometer-update',updateWeather);
new ResizeObserver(drawWeather).observe($('barometer'));
window.lucide?.createIcons();createDistribution();updateWeather();
fetch('/api/health').then(r=>r.json()).then(applyModelInfo).catch(()=>{$('modelDescription').textContent='服务连接失败，恢复后重新打开本页。';});
