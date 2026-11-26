const $=s=>document.getElementById(s); let ticket='';
$('copyLink').onclick=async()=>{
  const url=$('link').getAttribute('href');
  if(!url)return;
  try{
    await navigator.clipboard.writeText(url);
    if($('link').getAttribute('href')===url)$('msg').textContent='授权链接已复制。';
  }catch{
    if($('link').getAttribute('href')!==url)return;
    const input=$('manualLink');input.value=url;input.hidden=false;input.focus();input.select();
    $('msg').textContent='浏览器未允许自动复制，请手动复制选中的链接。';
  }
};
function reset(){$('manualLink').value='';$('manualLink').hidden=true;ticket='';$('flow').hidden=true;$('link').removeAttribute('href');$('code').value=$('secret').value=$('msg').textContent=''}
function mode(token){reset();$('authpane').hidden=token;$('tokenpane').hidden=!token;$('oauth').setAttribute('aria-pressed',!token);$('token').setAttribute('aria-pressed',token)}
async function state(){const slot=$('slot').value;$('state').textContent='读取中…';try{const r=await fetch(`/admin/${slot}/status`);const v=await r.json();if(slot===$('slot').value)$('state').textContent=v.configured?'已有凭据':'尚未绑定'}catch{if(slot===$('slot').value)$('state').textContent='状态不可用'}}
let submitting=false;async function run(op){const target=$('slot').value;if(!target||submitting)return;if(op!=='start' && !$(op==='finish'?'code':'secret').value.trim()){ $('msg').textContent='请先输入凭据。';return }if(op!=='start' && !confirm(`验证通过后将绑定 ${$('slot').value} 并替换该槽位的现有凭据，继续吗？`))return;
submitting=true;const buttons=document.querySelectorAll('button,select');buttons.forEach(b=>b.disabled=true);$('msg').textContent='正在联系 Antigravity…';try{const id=op==='finish'?'code':'secret',value=$(id).value.trim();$(id).value='';if(op==='start')reset();const r=await fetch(`/admin/${target}/${op}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticket,value})});if(!r.ok)throw Error(r.status===400?await r.text():'请求失败，请刷新页面后重试。');const v=await r.json();if(op==='start'){ticket=v.ticket;$('link').href=v.url;$('flow').hidden=false;$('msg').textContent='链接有效期为 5 分钟。'}else{reset();$('msg').textContent='账号已通过验证并保存。';await state();await loadAccounts($('slot').value)}}catch(e){$('msg').textContent=e.message}finally{submitting=false;buttons.forEach(b=>b.disabled=false)}}
$('slot').onchange=()=>{reset();state()};$('oauth').onclick=()=>mode(false);$('token').onclick=()=>mode(true);$('start').onclick=()=>run('start');$('finish').onclick=()=>run('finish');$('imp').onclick=()=>run('import');
function node(tag,text,cls){const el=document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el}
function card(slot,v){const el=node('article',null,'card'),head=node('div',null,'rowhead');head.append(node('h3',v.email||slot),node('span',v.error?'读取失败':v.configured?'已绑定':'待绑定','badge'));el.append(head);if(v.email)el.append(node('small',slot));
 if(v.rows?.length){const extra=node('details');let modelCount=0;el.append(node('small','以下是上游额度快照，不是逐请求扣费明细。未列出的模型未报告单独额度。'));v.rows.forEach(q=>{const isModel=q.kind==='model';if(isModel)modelCount++;const target=isModel?extra:el;const line=node('div',null,'meterline');line.append(node('span',q.kind==='bucket'?(q.label||q.name):q.name),node('strong',q.remaining==null?'未知':`${q.remaining}%`));target.append(line);if(isModel&&q.label&&q.label!==q.name)target.append(node('small','上游显示名：'+q.label));if(q.remaining!=null){const meter=node('progress');meter.max=100;meter.value=q.remaining;meter.setAttribute('aria-label',`${q.name} 剩余额度`);target.append(meter)}const ts=Date.parse(q.reset);if(Number.isFinite(ts))target.append(node('small','重置于 '+new Date(ts).toLocaleString()))});if(modelCount){extra.prepend(node('summary',`按上游模型 ID 查看（${modelCount} 项）`));el.append(extra);}el.append(node('small','查询于 '+new Date(v.updated*1000).toLocaleTimeString()))}
 else el.append(node('p',v.error||v.configured?'额度暂不可用，请稍后刷新。':'授权后将显示可读取的配额。'));
 const btn=node('button',v.configured?'重新绑定':'绑定账号');btn.onclick=()=>choose(slot);el.append(btn);return el}
function choose(slot){if(submitting)return;$('slot').value=slot;reset();state();$('enroll').scrollIntoView();$('slot').focus()}
let serial=0;
async function loadAccounts(preferred){const seq=++serial;try{const r=await fetch('/admin/accounts');if(!r.ok)throw Error();const list=await r.json();if(seq!==serial)return;const keep=preferred??$('slot').value;$('slot').replaceChildren(...list.map(s=>new Option(s,s)));if(list.includes(keep))$('slot').value=keep;else if(keep)reset();if(list.length)state();$('notice').textContent=`共 ${list.length} 个账号槽位 · 配额查询结果缓存 60 秒`;const tiles=list.map(s=>node('article',s+' · 读取中…','card'));$('grid').replaceChildren(...tiles);let next=0;await Promise.all(Array.from({length:Math.min(4,list.length)},async()=>{while(seq===serial&&next<list.length){const i=next++,s=list[i];let v;try{const r=await fetch(`/admin/${s}/quota`);if(!r.ok)throw Error();v=await r.json()}catch{v={error:true}}if(seq===serial)tiles[i].replaceWith(card(s,v))}}))}catch{if(seq===serial)$('notice').textContent='账号列表读取失败，请刷新重试。'}}
$('refresh').onclick=()=>loadAccounts();
$('add').onclick=async()=>{const btn=$('add');btn.disabled=true;try{const r=await fetch('/admin/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)throw Error(r.status===409?await r.text():'未能创建槽位');const {slot}=await r.json();reset();await loadAccounts(slot);choose(slot);$('msg').textContent='新槽位已创建，请选择绑定方式。'}catch(e){$('notice').textContent=e.message}finally{btn.disabled=false}};
loadAccounts();
