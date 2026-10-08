'use strict';
const $ = id => document.getElementById(id);
const state = {config:null, live:null, result:null, job:null, liveChart:null, liveLoading:false, request:0};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite = value => typeof value === 'number' && Number.isFinite(value);
const fmt = (value, digits=2) => finite(value) ? value.toLocaleString('en-US',{maximumFractionDigits:digits,minimumFractionDigits:digits}) : '—';
const price = value => finite(value) ? value.toLocaleString('en-US',{maximumSignificantDigits:8}) : '—';
const quantity = value => finite(value) ? value.toLocaleString('en-US',{maximumFractionDigits:10}) : '—';
const money = value => finite(value) ? `${value<0?'−':''}$${fmt(Math.abs(value))}` : '—';
const dateTime = value => value ? new Date(value).toISOString().replace('T',' ').slice(0,19) : '—';
const shortDate = value => value ? new Date(value).toISOString().replace('T',' ').slice(5,16) : '—';
const tone = value => finite(value) ? (value>=0?'positive':'negative') : '';
async function api(path, options={}) {
  const response = await fetch(path, {cache:'no-store', ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function options(id, values, all=null) {
  const node = $(id), old = node.value;
  node.replaceChildren();
  if(all!==null) node.add(new Option(all,''));
  values.forEach(v => node.add(new Option(v,v)));
  if([...node.options].some(o=>o.value===old)) node.value=old;
}
function emptyChart(id, message) { $(id).replaceChildren(); const box=document.createElement('div'); box.className='empty'; box.textContent=message; $(id).append(box); }
function showMetrics(metrics=null) {
  const values = [
    ['Net P&L', money(metrics?.pnl), metrics ? `${fmt(metrics.total_return*100)}% total return`:'After simulated fees',metrics?.pnl],
    ['Final equity',money(metrics?.final_equity),'USDT',null],
    ['Sharpe',fmt(metrics?.sharpe),'Daily · annualized',null],
    ['Sortino',fmt(metrics?.sortino),'Daily · annualized',null],
    ['Calmar',fmt(metrics?.calmar),'Annual return / max DD',null],
    ['Max drawdown',finite(metrics?.max_drawdown)?fmt(metrics.max_drawdown*100)+'%':'—','All simulation bars',metrics?.max_drawdown],
  ];
  $('metrics').innerHTML=values.map(([label,value,note,v])=>`<div class="metric"><div class="label">${label}</div><strong class="${tone(v)}">${value}</strong><small>${note}</small></div>`).join('');
}
function chart(id, series, trades=[], settings={}) {
  const node=$(id); node.replaceChildren();
  const samples=series.filter(p=>finite(p[1])).map(p=>[new Date(p[0]).getTime(),p[1]]);
  const fills=trades.filter(t=>finite(t.price)).map(t=>({...t, timestamp:new Date(t.time).getTime()}));
  const all=[...samples,...fills.map(t=>[t.timestamp,t.price])].filter(p=>Number.isFinite(p[0]));
  if(!all.length) return emptyChart(id,settings.empty || 'No data in this window.');
  const width=Math.max(node.clientWidth,260), height=node.clientHeight || 245;
  const margin={left:16,right:78,top:20,bottom:32}, w=width-margin.left-margin.right, h=height-margin.top-margin.bottom;
  let minX=settings.start ? new Date(settings.start).getTime() : all.reduce((v,p)=>Math.min(v,p[0]),Infinity);
  let maxX=settings.end ? new Date(settings.end).getTime() : all.reduce((v,p)=>Math.max(v,p[0]),-Infinity);
  if(maxX<=minX) maxX=minX+1000;
  let minY=all.reduce((v,p)=>Math.min(v,p[1]),Infinity),maxY=all.reduce((v,p)=>Math.max(v,p[1]),-Infinity);
  const pad=Math.max((maxY-minY)*.12, Math.abs(maxY)*.0005,1e-12); minY-=pad;maxY+=pad;
  const x=value=>margin.left+(value-minX)/(maxX-minX)*w, y=value=>margin.top+(maxY-value)/(maxY-minY)*h;
  const ns='http://www.w3.org/2000/svg';
  function element(type,attrs={},text=null) {const el=document.createElementNS(ns,type);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,v));if(text!==null)el.textContent=text;return el;}
  const svg=element('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':settings.label || 'Price chart with executions'});
  svg.append(element('title',{},settings.label || 'Price history and executions'));
  for(let i=0;i<4;i++){
    const yy=margin.top+h*i/3, value=maxY-(maxY-minY)*i/3;
    svg.append(element('line',{x1:margin.left,x2:width-margin.right,y1:yy,y2:yy,stroke:'#26313e','stroke-dasharray':'3 5','stroke-opacity':'.65'}));
    svg.append(element('text',{x:width-margin.right+10,y:yy+3,fill:'#8090a3','font-size':9,'font-family':'monospace'},settings.percent?fmt(value*100,2)+'%':price(value)));
  }
  for(let i=0;i<4;i++){
    const xx=margin.left+w*i/3;
    svg.append(element('text',{x:xx,y:height-10,fill:'#728398','font-size':8,'text-anchor':i===0?'start':i===3?'end':'middle','font-family':'monospace'},shortDate(minX+(maxX-minX)*i/3)));
  }
  const visible=samples.filter(p=>p[0]>=minX && p[0]<=maxX);
  const path=visible.map((p,i)=>`${i?'L':'M'}${x(p[0]).toFixed(2)},${y(p[1]).toFixed(2)}`).join(' ');
  const color=settings.color || '#8ddbba';
  if(visible.length){
    svg.append(element('path',{d:`${path} L${x(visible.at(-1)[0])},${margin.top+h} L${x(visible[0][0])},${margin.top+h} Z`,fill:color,'fill-opacity':'.045'}));
    svg.append(element('path',{d:path,fill:'none',stroke:color,'stroke-width':1.5,'stroke-linejoin':'round'}));
  }
  const cross=element('line',{y1:margin.top,y2:height-margin.bottom,stroke:'#617a91','stroke-dasharray':'3 3',visibility:'hidden'});svg.append(cross);
  const tooltip=$('tooltip');
  function tip(event,text){tooltip.hidden=false;tooltip.textContent=text;tooltip.style.left=Math.max(4,Math.min(event.clientX+14,innerWidth-275))+'px';tooltip.style.top=Math.max(4,Math.min(event.clientY+14,innerHeight-140))+'px';}
  svg.addEventListener('pointermove',e=>{
    if(e.target.closest('[data-fill]'))return;
    if(!visible.length)return;
    const rect=svg.getBoundingClientRect(), cursor=(e.clientX-rect.left)/rect.width*width;
    const target=minX+(cursor-margin.left)/w*(maxX-minX);
    let lo=0,hi=visible.length-1;while(lo<hi){const mid=Math.floor((lo+hi)/2);if(visible[mid][0]<target)lo=mid+1;else hi=mid;}
    let index=lo;if(index>0 && Math.abs(visible[index-1][0]-target)<Math.abs(visible[index][0]-target))index--;
    const p=visible[index];cross.setAttribute('x1',x(p[0]));cross.setAttribute('x2',x(p[0]));cross.setAttribute('visibility','visible');
    tip(e,`${dateTime(p[0])} UTC\n${settings.percent?fmt(p[1]*100)+'%':price(p[1])}`);
  });
  fills.filter(t=>t.timestamp>=minX&&t.timestamp<=maxX).forEach(t=>{
    const buy=['BUY','COVER','SHORT_CLOSE'].includes(t.side),xx=x(t.timestamp),yy=y(t.price);
    const marker=element('path',{d:buy?`M${xx},${yy-5} l-4,8 h8 Z`:`M${xx},${yy+5} l-4,-8 h8 Z`,fill:buy?'#8ddbba':'#ed8290',stroke:'#101720','stroke-width':.8,'data-fill':'1',tabindex:'0'});
    const label=`${t.side} ${quantity(t.quantity)} ${t.symbol}\n${price(t.price)} USDT\n${dateTime(t.time)} UTC`;
    marker.append(element('title',{},label));marker.setAttribute('aria-label',label);
    marker.addEventListener('pointermove',e=>{e.stopPropagation();tip(e,label);});svg.append(marker);
  });
  svg.addEventListener('pointerleave',()=>{tooltip.hidden=true;cross.setAttribute('visibility','hidden');});
  node.append(svg);
}
function renderResearch() {
  const result=state.result;
  if(!result)return;
  const c=result.config;
  showMetrics(result.metrics);
  $('run-label').textContent=`${c.strategy.toUpperCase()} · ${dateTime(c.start)} → ${dateTime(c.end)} UTC · ${c.interval}`;
  chart('equity-chart',result.equity,[],{label:'Backtest equity in USDT'});
  $('drawdown-chart').hidden=false;
  chart('drawdown-chart',result.drawdown,[],{color:'#ed8290',percent:true,label:'Backtest drawdown'});
  $('metric-note').textContent=`${result.metrics.daily_observations} daily observations. ${result.notes.join(' ')}`;
  $('trade-count').textContent=`${result.trades.length.toLocaleString()} fills · ${result.quote_count.toLocaleString()} limit quotes`;
  for(const kind of ['quotes','trades']){const a=$(kind+'-export');a.hidden=false;a.href=`/api/backtests/${state.job}/${kind}.csv`;a.download=kind+'.csv';}
  updateSymbols(); renderTradeChart();
}
function updateSymbols(){
  const symbols=new Set(Object.keys(state.result?.prices || {}));
  if($('compare').checked && state.live)state.live.orders.filter(o=>o.strategy===$('live-strategy').value&&(!$('bot-filter').value||o.bot===$('bot-filter').value)).forEach(o=>symbols.add(o.symbol));
  if(!symbols.size)symbols.add('BTC');
  options('chart-symbol',[...symbols].sort());
}
function renderTradeChart(){
  const symbol=$('chart-symbol').value, r=state.result;
  if(!r){emptyChart('trade-chart','Run a backtest to view simulated executions.');return;}
  $('backtest-window').textContent=`${dateTime(r.config.start)} → ${dateTime(r.config.end)} UTC`;
  chart('trade-chart',r.prices[symbol] || [],r.trades.filter(t=>t.symbol===symbol),{color:'#74b6d9',label:`${symbol} backtest price and executed trades`,empty:`${symbol} was not in this backtest's symbol universe.`});
}
async function loadLiveChart(){
  const serial=++state.request;
  if(!$('compare').checked)return;
  state.liveChart=null;state.liveLoading=true;
  emptyChart('live-chart','Loading recorded executions…');$('live-chart-note').textContent='';
  try{
    if(!$('live-strategy').value)throw new Error('No live strategies available. Refresh the live data.');
    const params=new URLSearchParams({strategy:$('live-strategy').value,symbol:$('chart-symbol').value,bot:$('bot-filter').value});
    const data=await api('/api/executions?'+params);
    if(serial!==state.request)return;
    state.liveChart=data;renderLiveChart();
  }catch(e){if(serial===state.request){emptyChart('live-chart',e.message);$('live-window').textContent='Unavailable';}}
  finally{if(serial===state.request)state.liveLoading=false;}
}
function renderLiveChart(){
  const d=state.liveChart;if(!d)return;
  $('live-window').textContent=`${shortDate(d.start)} → ${shortDate(d.end)} UTC`;
  chart('live-chart',d.prices,d.trades,{start:d.start,end:d.end,color:'#8ddbba',label:`${d.symbol} actual live executions, recorded activity window`});
  $('live-chart-note').textContent=[d.warning,d.window_basis,d.timestamp_basis].filter(Boolean).join(' ');
}
function renderLive(){
  if(!state.live)return;
  const filter=o=>(!$('strategy-filter').value||o.strategy===$('strategy-filter').value)&&(!$('bot-filter').value||o.bot===$('bot-filter').value);
  const byStrategy=new Map();
  for(const item of (state.live.strategy_pnl || []).filter(filter)){
    if(!byStrategy.has(item.strategy))byStrategy.set(item.strategy,{strategy:item.strategy,accounts:new Set(),open_positions:0,realized_pnl:0,unrealized_pnl:0,total_pnl:0,complete:true});
    const summary=byStrategy.get(item.strategy);
    summary.accounts.add(item.bot);summary.open_positions+=item.open_positions;summary.complete=summary.complete&&item.complete;
    for(const field of ['realized_pnl','unrealized_pnl','total_pnl'])summary[field]=finite(summary[field])&&finite(item[field])?summary[field]+item[field]:null;
  }
  const summaries=[...byStrategy.values()];
  $('strategy-pnl-count').textContent=summaries.length;
  $('strategy-pnl-rows').innerHTML=summaries.length?summaries.map(s=>`<tr><td>${escapeHTML(s.strategy)}${!s.complete?'<small>Incomplete trade history</small>':!finite(s.unrealized_pnl)?'<small>Binance marks unavailable</small>':''}</td><td class="numeric">${s.accounts.size}</td><td class="numeric">${s.open_positions}</td><td class="numeric ${tone(s.realized_pnl)}">${money(s.realized_pnl)}</td><td class="numeric ${tone(s.unrealized_pnl)}">${money(s.unrealized_pnl)}</td><td class="numeric ${tone(s.total_pnl)}">${money(s.total_pnl)}</td></tr>`).join(''):'<tr><td colspan="6" class="table-empty">No recorded strategies for this selection.</td></tr>';
  const positions=state.live.positions.filter(filter);
  $('position-count').textContent=positions.length;
  $('position-rows').innerHTML=positions.length?positions.map(p=>`<tr><td>${escapeHTML(p.strategy)}<small>${escapeHTML(p.bot)}</small></td><td class="symbol">${escapeHTML(p.symbol)}<small>/ USDT${p.complete?'':' · incomplete history'}</small></td><td><span class="badge ${p.side}">${p.side}</span></td><td class="numeric">${quantity(p.quantity)}</td><td class="numeric">${price(p.entry)}</td><td class="numeric">${price(p.price)}</td><td class="numeric ${tone(p.pnl)}">${money(p.pnl)}</td></tr>`).join(''):'<tr><td colspan="7" class="table-empty">No recorded open positions for this selection.</td></tr>';
  const search=$('order-search').value.trim().toLowerCase();
  const orders=state.live.orders.filter(filter).filter(o=>(!$('status-filter').value||o.status===$('status-filter').value)&&(!search||`${o.symbol} ${o.exchange_order_id||''}`.toLowerCase().includes(search)));
  $('order-count').textContent=orders.length;
  $('order-rows').innerHTML=orders.length?orders.map(o=>`<tr><td class="numeric">${dateTime(o.time)}</td><td>${escapeHTML(o.strategy)}<small>${escapeHTML(o.bot)}</small></td><td class="symbol">${escapeHTML(o.symbol)}</td><td class="${['BUY','COVER'].includes(o.side)?'positive':'negative'}">${escapeHTML(o.side)}</td><td><span class="badge ${o.status}" title="${escapeHTML(o.raw_status)} / ${escapeHTML(o.exchange_status)}">${o.status}</span>${o.status==='cancelled'&&o.filled_quantity>0?'<small>Has recorded fills</small>':''}</td><td class="numeric">${quantity(o.quantity)}</td><td class="numeric">${quantity(o.filled_quantity)}</td><td class="numeric">${price(o.price)}</td><td class="numeric">${price(o.fill_price)}</td><td>${escapeHTML(o.order_type)}<small>#${escapeHTML(o.exchange_order_id||o.id.slice(0,8))}</small></td></tr>`).join(''):'<tr><td colspan="10" class="table-empty">No orders match these filters.</td></tr>';
}
let refreshing=false;
async function refreshLive(){
  if(refreshing)return;refreshing=true;$('refresh').disabled=true;
  try{
    const data=await api('/api/live');state.live=data;
    $('live-error').hidden=true;$('connection').textContent='Supabase connected';$('connection-dot').classList.add('connected');
    $('as-of').textContent=`Updated ${dateTime(data.as_of)} UTC · auto-refresh 30s`;
    $('mark-time').textContent=data.mark_time?`Binance marks · ${dateTime(data.mark_time)} UTC`:'Binance prices unavailable';
    options('strategy-filter',data.strategies,'All strategies'); options('bot-filter',data.bots,'All accounts');options('live-strategy',data.strategies);
    $('data-notes').hidden=!data.warnings.length;$('data-notes').innerHTML=data.warnings.map(w=>`<p>${escapeHTML(w)}</p>`).join('');
    renderLive();updateSymbols();renderTradeChart();if($('compare').checked)loadLiveChart();
  }catch(e){
    $('live-error').hidden=false;$('live-error').textContent=`${e.message}${state.live?' Displaying the last successful snapshot; it is stale.':''}`;
    $('connection').textContent='Live data unavailable';$('connection-dot').classList.remove('connected');
    if(!state.live){$('strategy-pnl-rows').innerHTML='<tr><td colspan="6" class="table-empty">Unable to load strategy P&L. Use Refresh to retry.</td></tr>';$('position-rows').innerHTML='<tr><td colspan="7" class="table-empty">Unable to load positions. Use Refresh to retry.</td></tr>';$('order-rows').innerHTML='<tr><td colspan="10" class="table-empty">Unable to load orders. Use Refresh to retry.</td></tr>';}
  }finally{refreshing=false;$('refresh').disabled=false;}
}
let mmSymbolsText='PEPE, BONK, 1000CHEEMS';
const mmAllocations=new Map([['PEPE','85'],['BONK','7.5'],['1000CHEEMS','7.5']]);
function updateMMAllocationTotal(){
  const total=[...$('mm-allocations').querySelectorAll('input')].reduce((sum,input)=>sum+Number(input.value),0);
  $('mm-allocation-total').textContent=`Total: ${fmt(total,4)}% / 100%. New symbols start at 0%.`;
}
function renderMMAllocations(){
  const symbols=[...new Set($('symbols').value.split(',').map(s=>s.trim().toUpperCase().replace(/\/(USDT|USD)$/,'')).filter(Boolean))];
  const valid=symbols.length>0 && symbols.length<=50 && symbols.every(s=>/^[A-Z0-9]{1,25}$/.test(s));
  $('symbols').setCustomValidity(valid?'':'Enter 1 to 50 coin tickers, separated by commas, e.g. BTC, ETH, PEPE.');
  $('mm-allocations').replaceChildren();
  for(const symbol of symbols.filter(s=>/^[A-Z0-9]{1,25}$/.test(s)).slice(0,50)){
    const label=document.createElement('label');label.textContent=`${symbol} allocation · %`;
    const input=document.createElement('input');
    Object.assign(input,{name:`allocation_${symbol}`,type:'number',min:'0',max:'100',step:'any',required:true,value:mmAllocations.get(symbol)??'0'});
    input.addEventListener('input',()=>{mmAllocations.set(symbol,input.value);updateMMAllocationTotal();});
    label.append(input);$('mm-allocations').append(label);
  }
  updateMMAllocationTotal();
}
$('symbols').addEventListener('input',()=>{
  if($('strategy').value==='mm-10m-fluctuation'){
    mmSymbolsText=$('symbols').value;renderMMAllocations();
  }
});
function changeStrategy(){
  const rxm=$('strategy').value==='rxm', mm=$('strategy').value==='mm-10m-fluctuation';
  $('rxm-fields').hidden=!rxm;$('rxm-preset').hidden=!rxm;$('ma-fields').hidden=rxm||mm;
  $('mm-fields').hidden=!mm;$('weight-parameters').hidden=mm;$('weight-execution-note').hidden=mm;
  document.querySelectorAll('#rxm-fields input, #rxm-preset select').forEach(input=>input.disabled=!rxm);
  document.querySelectorAll('#ma-fields input').forEach(input=>input.disabled=rxm||mm);
  document.querySelectorAll('#mm-fields input').forEach(input=>input.disabled=!mm);
  for(const name of ['limit_offset_bps','short_fee_bps','rebalance_band','lockin_return','lockin_scale']){
    const input=document.querySelector(`[name=${name}]`);input.disabled=mm;input.closest('label').hidden=mm;
  }
  const interval=document.querySelector('[name=interval]');
  for(const option of interval.options)option.disabled=mm?option.value!=='1s':option.value==='1s';
  interval.value=mm?'1s':'15m';
  const maker=document.querySelector('[name=maker_fee_bps]');maker.readOnly=mm;if(mm)maker.value=5;
  $('symbols').value=mm?mmSymbolsText:(rxm?state.config.rxm_symbols:state.config.defaults).join(', ');
  $('symbols').setCustomValidity('');
  if(mm)renderMMAllocations();
  $('symbol-help').textContent=mm?'Edit coin tickers, separated by commas, then set allocations below. Symbols at 0% are skipped.':'Coin tickers, separated by commas. RXM includes BTC as its benchmark.';
  document.querySelector('[name=lockin_return]').value=rxm?.06:0;
}
$('preset').addEventListener('change',()=>{
  const neutral=$('preset').value==='neutral';
  for(const [name,value] of Object.entries(neutral?{k:5,tilt:0,gross:.9,buffer:0,lockin_return:0}:{k:3,tilt:.3,gross:1,buffer:2,lockin_return:.06}))document.querySelector(`[name=${name}]`).value=value;
});
$('strategy').addEventListener('change',changeStrategy);
$('refresh').addEventListener('click',refreshLive);
for(const id of ['strategy-filter','status-filter'])$(id).addEventListener('change',renderLive);
$('bot-filter').addEventListener('change',()=>{renderLive();updateSymbols();renderTradeChart();loadLiveChart();});
$('order-search').addEventListener('input',renderLive);
$('chart-symbol').addEventListener('change',()=>{renderTradeChart();loadLiveChart();});
$('live-strategy').addEventListener('change',()=>{updateSymbols();renderTradeChart();loadLiveChart();});
$('compare').addEventListener('change',()=>{
  const compare=$('compare').checked;
  const matching=state.result?.config.strategy || $('strategy').value;
  if(compare && state.live?.strategies.includes(matching))$('live-strategy').value=matching;
  $('live-chart-pane').hidden=!compare;$('live-strategy-field').hidden=!compare;
  $('execution-charts').classList.toggle('comparing',compare);updateSymbols();renderTradeChart();loadLiveChart();
});
function changeRange(){
  const relative=$('range-mode').value==='relative';
  $('date-range').hidden=relative;$('relative-range').hidden=!relative;
  for(const id of ['start','end'])$(id).disabled=relative;
  for(const id of ['range-value','range-unit'])$(id).disabled=!relative;
  $('range-value').max=$('range-unit').value==='hours'?'2160':'129600';
}
$('range-mode').addEventListener('change',changeRange);
$('range-unit').addEventListener('change',changeRange);
$('backtest-form').addEventListener('submit',async e=>{
  e.preventDefault();if(!state.config)return;
  const data=Object.fromEntries(new FormData(e.target));data.symbols=data.symbols.split(',').map(s=>s.trim()).filter(Boolean);data.allow_short=e.target.elements.allow_short.checked;
  for(const [name,value] of Object.entries(data))if(!['strategy','preset','start','end','interval','symbols','allow_short'].includes(name))data[name]=Number(value);
  if($('range-mode').value==='relative')data[`last_${$('range-unit').value}`]=Number($('range-value').value);
  if(data.strategy==='mm-10m-fluctuation'){
    data.mm={allocations:{}};
    for(const [name,value] of Object.entries(data)){
      if(name.startsWith('allocation_')){data.mm.allocations[name.slice(11)]=value/100;delete data[name];}
      else if(name.startsWith('mm_')){data.mm[name.slice(3)]=value;delete data[name];}
    }
    data.mm.enforce_one_tick_distance=e.target.elements.mm_enforce_one_tick_distance.checked;
    data.liquidate_mm=e.target.elements.liquidate_mm.checked;
  }
  $('run').disabled=true;$('run-status').className='';$('run-status').textContent='Starting backtest…';
  try{
    const job=await api('/api/backtests',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':state.config.csrf},body:JSON.stringify(data)});
    sessionStorage.setItem('dashboard-job',job.id);await pollJob(job.id);
  }catch(e){$('run-status').textContent=e.message;$('run-status').className='failed';}
  finally{$('run').disabled=false;}
});
async function pollJob(id){
  while(true){
    const job=await api(`/api/backtests/${id}`);
    if(job.status==='complete'){state.job=id;state.result=job.result;renderResearch();$('run-status').textContent='Backtest complete. Quotes and trades are ready to download.';return;}
    if(job.status==='failed'){sessionStorage.removeItem('dashboard-job');throw new Error(job.error);}
    $('run-status').textContent=job.progress;await new Promise(resolve=>setTimeout(resolve,1200));
  }
}
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{renderResearch();renderLiveChart();},150);});
async function init(){
  showMetrics();const now=new Date();now.setUTCHours(0,0,0,0);$('end').value=now.toISOString().slice(0,10);$('end').max=$('end').value;
  $('start').value=new Date(now.getTime()-14*86400000).toISOString().slice(0,10);
  try{state.config=await api('/api/config');changeStrategy();}catch(e){$('run-status').textContent=e.message;$('run').disabled=true;}
  refreshLive();setInterval(()=>{if(!document.hidden)refreshLive();},30000);
  const saved=sessionStorage.getItem('dashboard-job');if(saved){$('run').disabled=true;try{await pollJob(saved);}catch(e){sessionStorage.removeItem('dashboard-job');$('run-status').textContent=e.message;}finally{$('run').disabled=false;}}
}
init();
