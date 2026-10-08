"""Build a standalone, auditable HTML report from saved experiments; no network."""
from pathlib import Path
import base64, hashlib, html, io, json, math
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
OUT=Path(__file__).resolve().parent;REPO=OUT.parents[1];RESULTS=REPO/'results';evidence={}
def source(rel):
 p=RESULTS/rel;assert p.is_file(),rel
 evidence[rel]=hashlib.sha256(p.read_bytes()).hexdigest();return p

def j(rel):return json.loads(source(rel).read_text())
def csv(rel):return pd.read_csv(source(rel))
def esc(s):return html.escape(str(s))
def usd(v):return f'{"−" if v<0 else "+"}${abs(v):,.2f}'
def table(frame,money=(),pct=(),integer=()):
 out='<div class="table-wrap"><table><thead><tr>'+''.join('<th>'+esc(c)+'</th>' for c in frame.columns)+'</tr></thead><tbody>'
 for row in frame.to_dict('records'):
  out+='<tr>'
  for c,v in row.items():
   cls='';val=v
   if c in money and pd.notna(v):cls='num neg' if v<0 else 'num pos';val=usd(float(v))
   elif c in pct:cls='num';val=f'{v:,.2f}%'
   elif c in integer:cls='num';val=f'{int(v):,}'
   out+=f'<td class="{cls}">{esc(val)}</td>'
  out+='</tr>'
 return out+'</tbody></table></div>'
def image(p):return 'data:image/png;base64,'+base64.b64encode(p.read_bytes()).decode()
def figimage(fig):
 b=io.BytesIO();fig.savefig(b,format='png',dpi=145,bbox_inches='tight');plt.close(fig)
 return 'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()

families=[('Refresh × minimum distance × shorts','pepe-tardis-refresh-grid-20261008',48),
 ('Penetration p=0.5 / 1','pepe-tardis-penetration-20261008',24),
 ('Penetration p=0.8','pepe-tardis-penetration08-20261008',12),
 ('Reference price — touch fills','pepe-tardis-mid-vs-close-20261008',36),
 ('Reference price — p=0.9','pepe-tardis-mid-vs-close-p09-20261008',36),
 ('Feed latency — touch / p=0.9','pepe-tardis-latency-20261008',192)]
frames={};allrows=[]
for name,path,count in families:
 d=csv(path+'/results.csv');assert len(d)==count and d.validated.all();frames[path]=d
 for row in d.to_dict('records'):
  ref=row.get('reference','midpoint');p=row.get('penetration_probability',.9 if 'p09' in path else 0)
  if 'latency' in path:p=.9 if row['fill']=='p09' else 0
  allrows.append(dict(family=name,day=row['day'],refresh=int(row['refresh_seconds']),reference=ref,
   minimum='ON' if row.get('one_tick_distance',False) else 'OFF',shorts='ON' if row.get('shorts_enabled',False) else 'OFF',
   p=p,delay=int(row.get('delay_seconds',0)),pnl=row['pnl'],return_pct=row['return_pct'],fees=row['fees'],fills=int(row['fills']),
   drawdown=row['max_drawdown_pct'],eligible=int(row.get('fresh_decisions',0)),decisions=int(row.get('total_decisions',row.get('decisions',0))),
   source=path+'/results.csv'))
assert len(allrows)==348
G=frames[families[0][1]];P=frames[families[1][1]];P8=frames[families[2][1]];R0=frames[families[3][1]];R9=frames[families[4][1]];L=frames[families[5][1]]
# Include saved validation/provenance records alongside result hashes.
for _,path,_ in families:
 for name in ('experiment.json','parameters.json','probability-validation.json','comparison-validation.json','source-provenance.json','provenance.json'):
  if (RESULTS/path/name).exists():source(path+'/'+name)
coverage=csv('pepe-tardis-refresh-grid-20261008/coverage.csv')
source('pepe-tardis-refresh-grid-20261008/data-validation.json')
source('pepe-tardis-latency-20261008/coverage.json')
source('pepe-tardis-latency-20261008/crossing-diagnostic.csv')

# Five fill assumptions, all same midpoint/long-only/minimum-off setting.
prob=[]
for d,p in [(G[(~G.one_tick_distance)&(~G.shorts_enabled)],0),(P,None),(P8,None),(R9[R9.reference=='midpoint'],.9)]:
 for row in d.to_dict('records'):prob.append(dict(day=row['day'],refresh=row['refresh_seconds'],p=p if p is not None else row['penetration_probability'],pnl=row['pnl']))
prob=pd.DataFrame(prob);assert len(prob)==60
fig,ax=plt.subplots(figsize=(10,3.9));colors=['#166b81','#bc562b']
for (day,d),color in zip(prob[prob.refresh==20].groupby('day'),colors):
 d=d.sort_values('p');ax.plot(d.p,d.pnl,marker='o',label=day,color=color,lw=2)
ax.axhline(0,color='#8b97a4',lw=1);ax.set_xticks([0,.5,.8,.9,1]);ax.set_xlabel('Probability that an order requires one-tick penetration')
ax.set_ylabel('Net P&L (USD)');ax.set_title('20-second midpoint quoting: profits depend strongly on the fill assumption',loc='left',fontsize=12)
ax.legend(frameon=False);ax.spines[['top','right']].set_visible(False);ax.grid(axis='y',alpha=.18)
fill_chart=figimage(fig)

def pivot_table(d,cols,labels=None):
 t=d.pivot(index='refresh_seconds',columns=cols,values='pnl').sort_index()
 if labels:
  t=t.reindex(columns=list(labels));t.columns=list(labels.values())
 t.index=t.index.map({20:'20s',30:'30s',60:'60s',180:'3m',300:'5m',600:'10m'});t.index.name='Refresh'
 t=t.reset_index();return table(t,money=list(t.columns[1:]))

grid=''
labels={(False,True):'Long only · min ON',(False,False):'Long only · min OFF',(True,True):'Shorts ON · min ON',(True,False):'Shorts ON · min OFF'}
for day,d in G.groupby('day'):grid+=f'<h3>{day} · independent UTC day</h3>'+pivot_table(d,['shorts_enabled','one_tick_distance'],labels)
probtables=''
for day,d in prob.groupby('day'):
 t=d.pivot(index='refresh',columns='p',values='pnl').reindex(columns=[0,.5,.8,.9,1]);t.columns=['Touch (p=0)','p=0.5','p=0.8','p=0.9','p=1.0'];t.index=t.index.map({20:'20s',30:'30s',60:'60s',180:'3m',300:'5m',600:'10m'});t.index.name='Refresh';t=t.reset_index()
 probtables+=f'<h3>{day}</h3>'+table(t,money=list(t.columns[1:]))
reftables=''
for tag,d in [('Touch fills',R0),('p=0.9',R9)]:
 for day,x in d.groupby('day'):
  reftables+=f'<h3>{day} · {tag}</h3>'+pivot_table(x,'reference',{'midpoint':'Midpoint','latest-close':'Latest close (t−1s)','lagged-close':'Lagged close (t−2s)'})
latency=''
for fill in ('touch','p09'):
 for ref in ('midpoint','latest-close'):
  x=L[(L.fill==fill)&(L.reference==ref)].groupby(['refresh_seconds','delay_seconds']).pnl.mean().unstack().sort_index();x.columns=[f'+{c}s' for c in x.columns];x.index=x.index.map({20:'20s',30:'30s',60:'60s',180:'3m',300:'5m',600:'10m'});x.index.name='Refresh';x=x.reset_index()
  latency+=f'<h3>{"Touch fills" if fill=="touch" else "p=0.9"} · {ref} · mean daily P&L</h3>'+table(x,money=list(x.columns[1:]))

sparse=[]
for label,path,coverage_label in [
 ('Lagged candle close · 10m · penetration 1','pepe-tick-distance-3d-20261007T1501Z','Candle reference'),
 ('Midpoint · 10m · penetration 1','pepe-midpoint-3d-20261007','386 / 432 decisions'),
 ('Midpoint · 20s · penetration 1','pepe-midpoint-20s-3d-20261007','386 / 12,960 decisions'),
 ('Midpoint · 20s · touch fills','pepe-midpoint-20s-touch-3d-20261007','386 / 12,960 decisions')]:
 for x in j(path+'/comparison.json'):
  sparse.append({'Experiment':label,'Minimum':'ON' if x['variant']=='enforced' else 'OFF','Shorts':'ON' if x['shorts_enabled'] else 'OFF','Net P&L':x['net_pnl'],'Fills':x['fills'],'Coverage':coverage_label})
sparsetable=table(pd.DataFrame(sparse),money=['Net P&L'],integer=['Fills'])
week=j('mm-random30-weeks-20261007/summary.json');screen=j('mm-coin-screen-20261006T1739Z/screen-summary.json')
rotation=csv('mm-rolling12m-3m-20261006/comparison.csv');timing=pd.DataFrame(j('mm-random30-weeks-20261007/sync-timing-diagnostic.json'))
vol=csv('mm-vol30-decay-sweep-20261006T1739Z/eight-setting-summary.csv');vol=vol[vol.asset_group=='all'][['volatility_half_life_seconds','decay_seconds','instruments','profitable','median_return_pct','sum_pnl_independent_books']].sort_values(['volatility_half_life_seconds','decay_seconds'])
vol.columns=['Vol half-life (s)','Decay (s)','Instruments','Profitable','Median return','Sum of independent-book P&L']
legacy=[]
for name,label in [('latest24h-20261006T1739Z','Three-coin MM · latest 24h'),('latest6h-20261006T1836Z','Three-coin MM · latest 6h'),('latest3h-20261006T1846Z','Three-coin MM · latest 3h'),('prior24to48h-20261006T1842Z','Three-coin MM · prior 24–48h'),('pepe-only-latest7d-20261006T1921Z','PEPE only · 7 days')]:
 rows=j(name+'/comparison.json');x=next((r for r in rows if r['mode']=='mm-only'),None)
 if x:legacy.append({'Experiment':label,'Start UTC':x['start'][:16].replace('T',' '),'End UTC':x['end'][:16].replace('T',' '),'MM P&L (marked)':x['mm_pnl'],'MM return':x['mm_return_pct'],'MM fills':x['mm_fills']})
penold=pd.DataFrame(j('pepe-only-latest1d2d-20261006T1924Z/penetration-comparison.json'))[['days','touch_pnl','penetration_pnl','exit_adjusted_pnl']]
penold.columns=['Days','Touch P&L (marked)','1-tick P&L (marked)','1-tick P&L after exit cost']
shortold=pd.DataFrame(j('pepe-short-1tick-20261006T1924Z/comparison.json'))[['days','shorts_enabled','net_pnl','marked_pnl_before_terminal_costs','fills']]
shortold.columns=['Days','Shorts enabled','P&L after exit cost','P&L before terminal cost','Fills']
last_hour=pd.DataFrame(j('pepe-last1h-1tick-20261007T0922Z/comparison.json'))
weeklytable=pd.DataFrame([{'Metric':'Profitable weeks','Result':f'{week["profitable_weeks"]} / {week["weeks"]}'},
 {'Metric':'Mean / median marked weekly return','Result':f'{week["mean_weekly_return_pct"]:.2f}% / {week["median_weekly_return_pct"]:.2f}%'},
 {'Metric':'Mean weekly return after terminal cost','Result':f'{week["exit_adjusted_mean_return_pct"]:.2f}%'},
 {'Metric':'Worst weekly P&L','Result':usd(week['worst_week']['mm_pnl'])+' (Oct 10–17, 2025)'},
 {'Metric':'Best weekly P&L','Result':usd(week['best_week']['mm_pnl'])+' (Sep 13–20, 2026)'},
 {'Metric':'Largest within-week drawdown','Result':f'{week["worst_week_max_drawdown_pct"]:.2f}%'}])
registry=table(pd.DataFrame([{'Study':n,'Run outputs':c,'Market sample':'Sep 1 & Oct 1, 2026'} for n,p,c in families]),integer=['Run outputs'])
# Compact, portable result export. No credentials or environment files are included.
(OUT/'tardis-results.csv').write_text(pd.DataFrame(allrows).to_csv(index=False))
source_manifest=json.dumps(evidence,indent=2);(OUT/'evidence-manifest.json').write_text(source_manifest)
latimg=image(source('pepe-tardis-latency-20261008/latency-comparison.png'))

body=f'''
<header class="hero"><div class="eyebrow">Research notebook / execution sensitivity</div><h1>Crypto market-making<br>scenario report</h1><p class="subtitle">PEPEUSDT experiments, execution assumptions, and what the results support.</p><div class="meta">Prepared 8 October 2026 · dates in UTC unless stated · USD-equivalent P&L</div><div class="actions"><button onclick="window.print()">Print / save as PDF</button><a href="#explorer">Explore all 348 Tardis runs ↓</a></div></header>
<nav aria-label="Report sections"><a href="#findings">Findings</a><a href="#setup">Model</a><a href="#data">Data</a><a href="#refresh">Refresh & shorts</a><a href="#fills">Fill assumptions</a><a href="#price-reference">Price reference</a><a href="#latency">Latency</a><a href="#sparse">Sparse tests</a><a href="#earlier">Earlier studies</a><a href="#explorer">Run explorer</a><a href="#limits">Interpretation</a></nav>
<main>
<section id="findings"><div class="kicker">01 / Findings</div><h2>Fill assumptions drive the apparent edge.</h2><div class="cards"><div><b>348</b><span>Saved Tardis run outputs<br>with overlapping controls</span></div><div><b>2 days</b><span>September 1 & October 1<br>not 348 independent samples</span></div><div><b>$70,000</b><span>Starting capital per daily run<br>PEPE-only allocation</span></div></div>
<p>The strongest modeled result was fast, long-only midpoint quoting without the one-tick minimum distance under touch fills. Requiring one-tick penetration on most orders largely removed that profitability. This is evidence of <strong>execution-model sensitivity</strong>, not a demonstrated live trading edge.</p>
<ul><li><strong>Refresh and shorts:</strong> 20-second, long-only, minimum OFF led the touch-fill grid on both days. Fast, tight quoting with venue-style shorts performed poorly.</li><li><strong>Penetration:</strong> at 20 seconds, midpoint long-only mean daily P&L fell from <strong>+$7,306</strong> under touch fills to <strong>−$673</strong> at p=0.9 and <strong>−$1,177</strong> at p=1.</li><li><strong>Reference price:</strong> midpoint versus close was mixed under touch fills. Latest close outperformed midpoint in all 12 day/refresh pairs at p=0.9, within the matched reference experiment.</li><li><strong>Latency:</strong> delays did not consistently hurt simulated P&L. Some delayed quotes were already marketable against the current book; the model did not implement their rejection or taker execution.</li></ul>
<div class="callout">The two dates were chosen because free Tardis samples were available. Signal parameters were not optimized per refresh interval. Results should not be generalized to other dates or treated as live-return forecasts.</div></section>
<section id="setup"><div class="kicker">02 / Experiment contract</div><h2>What was held fixed—and what changed</h2>{registry}
<p>“Run outputs” counts saved simulations, including repeated zero-delay and midpoint controls. Each daily run resets capital, inventory and orders. Two-day figures below are arithmetic means of independent daily P&Ls, not compounded returns.</p>
<table><tbody><tr><th>Capital / inventory</th><td>$70,000 PEPE budget; fixed startup lot worth $3,500; nominal gross inventory cap $49,000. Allocation is not 100% continuously invested.</td></tr><tr><th>Price / quantity precision</th><td>PEPE tick 0.00000001; integer quantity step; $1 minimum notional.</td></tr><tr><th>Signal settings for recent Tardis tests</th><td>20s forecast horizon; 10s signal decay; alpha EWMA half-life 10s; volatility EWMA half-life 30s; 1h candle warmup; one additional second of feature lag.</td></tr><tr><th>Refresh intervals</th><td>20s, 30s, 60s, 180s, 300s, 600s. Forecast/decay/EWMA settings stay fixed across intervals.</td></tr><tr><th>Fees and terminal treatment</th><td>Spot maker 5 bps; short opening/closing 10 bps; terminal shorts closed; remaining spot marked with a 10-bps exit allowance. Main Tardis tables include these modeled final costs.</td></tr><tr><th>Short mechanics</th><td>Collateralized venue-style shorts: short asks when no spot is held; local cover conditions execute at the next eligible candle open. Not symmetric maker-only two-sided MM.</td></tr><tr><th>What the simulator omits</th><td>Queue position, volume-constrained fills, own-order market impact, live API scheduling, and current-venue rejection/taker behavior for orders already crossing on arrival.</td></tr></tbody></table>
<h3>Three settings that must not be confused</h3><div class="definitions"><div><h4>Minimum quote distance</h4><p>When ON, each quote must be at least one tick from its reference before outward rounding. OFF retains tick rounding and the 2-bps minimum distance; it does not mean zero spread.</p></div><div><h4>Fill penetration</h4><p>A buy fills only if the candle low reaches the limit minus one tick; a sell requires the high to reach the limit plus one tick. Zero ticks allows touch fills.</p></div><div><h4>Penetration probability p</h4><p>The one-tick requirement is assigned once per order with probability p. Otherwise that order allows touch fills. This is <strong>not</strong> a probability of filling after penetration. Fractional-p runs use seed 20261008, one realization each.</p></div></div>
</section>
<section id="data"><div class="kicker">03 / Data and causal timing</div><h2>Full-day Tardis history versus sparse CoinAPI windows</h2>
<p>Tardis supplied <strong>197,371</strong> PEPEUSDT quote updates for September 1 and <strong>198,171</strong> for October 1. Binance one-second candles supplied signals, candle fill checks and valuation, with the previous hour used for warmup. Binance archive checksums matched.</p>
<p>Midpoints use the latest Tardis observation received before the decision, not a future quote. The two-second age rule passes roughly <strong>81–89%</strong> of refreshes in the no-added-delay grid. Older books suppress quoting. An unchanged quote older than two seconds does not itself prove an outage: the age cutoff is an explicit strategy assumption.</p>
{table(coverage[['day','refresh_seconds','decisions','fresh','skipped','fresh_pct']].rename(columns={'day':'Day','refresh_seconds':'Refresh (s)','decisions':'Decisions','fresh':'Eligible','skipped':'Skipped','fresh_pct':'Eligible %'}),pct=['Eligible %'],integer=['Refresh (s)','Decisions','Eligible','Skipped'])}
<p>Our earlier CoinAPI download contained only 5,724 records from two-second windows before 432 ten-minute decisions. It was sufficient for that sampling schedule but supported only 386 of 12,960 decisions after switching to a 20-second refresh.</p></section>
<section id="refresh"><div class="kicker">04 / Refresh × quote distance × shorts</div><h2>Touch-fill results</h2><p>Net P&L after trading fees and modeled terminal costs. Fill penetration is zero in every cell.</p>{grid}
<p>For example, on October 1 the 20-second minimum-OFF case produced +$6,252.71 long-only but −$9,115.69 with shorts. Fees rose from $7,863.97 to $14,094.89. This compares different inventory paths and venue-style short costs, not merely an extra symmetric quote.</p></section>
<section id="fills"><div class="kicker">05 / Fill-model sensitivity</div><h2>The fast-refresh result is fragile to penetration.</h2><p>All cells: midpoint reference, long-only, minimum quote distance OFF. “Touch” has zero penetration ticks. Other columns set one penetration tick with the stated per-order probability. Fractional-p runs are not Monte Carlo averages.</p><img class="chart" src="{fill_chart}" alt="20-second midpoint PnL declines strongly as penetration probability rises on both test dates">{probtables}
<div class="callout">p=1 is a conservative <em>trade-through execution scenario</em>, not a mathematical worst-case P&L bound. Fewer fills can remove losing entries as well as profitable exits.</div></section>
<section id="price-reference"><div class="kicker">06 / Midpoint versus close</div><h2>No reference price wins under every fill model.</h2><p>Quote-reference variants: latest midpoint, latest completed close at t−1s, and original lagged close at t−2s. All share the same signals, midpoint-based capacity/passivity checks and quote-age eligibility. Only the formula reference changes. This is not a wholesale replay of the original candle-only implementation.</p>{reftables}</section>
<section id="latency"><div class="kicker">07 / Market-data latency</div><h2>Delaying information is different from delaying an order.</h2>
<p>Added feed delays of 0, 1, 2 and 3 seconds were applied to midpoint, close, alpha and volatility. The latest completed close is t−delay−1s; features end at t−delay−2s. Thus +1s exposes close t−2, while +2s exposes close t−3. Startup anchors also use the latest close available under that delay.</p>
<p>The quote-age allowance is measured at the delayed feed cutoff: a D-second delay permits wall-clock quote age up to D+2s. Keeping a strict two-second wall-clock limit would instead suppress many delayed orders. Order-routing/activation delay remains zero, and cancellation latency is not modeled.</p>
<img class="chart" src="{latimg}" alt="Mean daily PnL for six quote refresh intervals and four added data delays, for midpoint and latest close under touch fills and p=0.9">
<details><summary>Exact latency comparison tables · mean daily P&L</summary>{latency}</details>
<p><strong>Why latency can appear beneficial here:</strong> stale prices alter rounding, inventory and eligible refreshes. The simulation also permits some quotes that already cross the current market to remain in the candle-fill model. At 20s refresh and +3s delay, the p=0.9 midpoint case submitted 295 already-marketable orders across both days; the close case submitted 75. Those need arrival-time rejection or taker treatment before making live-execution claims.</p>
<p>All zero-delay runs reproduce the earlier matching models and event ledgers exactly. Independent Python calculations verified delayed EWMA values and each penetration assignment.</p></section>
<section id="sparse"><div class="kicker">08 / Earlier three-day diagnostics</div><h2>Preserved for context—not a continuous 20-second test.</h2><p>Window: October 4, 15:01 UTC through October 7, 15:01 UTC. The initial candle-reference comparison produced identical results with minimum distance ON/OFF because its references lay on the tick grid. Actual midpoints introduced half-tick references and different rounded quotes.</p>
<div class="callout danger">The 20-second CoinAPI diagnostics had only 2.98% quote coverage. They typically quoted for 20 seconds, canceled, and waited until the next cached ten-minute window. Inventory stayed exposed between windows. Their profits must not rank continuous 20-second market-making strategies.</div>{sparsetable}
<p>The 10-minute midpoint test skipped 46/432 decisions under the freshness rule. Its comparison against the original candle-reference run also changes quote eligibility, so it is not a pure reference-only comparison.</p></section>
<section id="earlier"><div class="kicker">09 / Earlier studies in this research sequence</div><h2>Broader evidence, with different models and windows</h2><p>These are verified summaries of the preceding experiments. They use different configurations and evaluation windows from the recent Tardis tests. Marked P&L and exit-adjusted P&L are labeled separately; do not combine them into one portfolio track record.</p>
<h3>Short-window and PEPE-only replays</h3>{table(pd.DataFrame(legacy),money=['MM P&L (marked)'],pct=['MM return'],integer=['MM fills'])}
<h3>PEPE 1-day / 2-day: touch versus one-tick penetration</h3>{table(penold,money=list(penold.columns[1:]),integer=['Days'])}
<h3>PEPE venue-style shorts: same 1-day / 2-day windows, one-tick penetration</h3>{table(shortold,money=['P&L after exit cost','P&L before terminal cost'],integer=['Days','Fills'])}
<p>The separate October 7, 08:22–09:22 UTC one-hour test had one spot buy and no short trades. Long-only and short-enabled results were identical: −$10.20 before terminal cost, −$13.67 after the modeled exit.</p>
<h3>30 sampled, non-overlapping weeks</h3><p>Original fixed three-asset strategy: 85% PEPE, 7.5% BONK, 7.5% 1000CHEEMS within $70,000 MM capital, plus $30,000 idle at account level. Original 10-minute refresh, 30-second forecast and 5-minute volatility half-life. Seed 20261007; separate accounts and non-overlapping warmups/windows. Non-overlap does not establish statistical independence.</p>{table(weeklytable)}
<p>Historical engine source was frozen at commit 52f0de4 after an external reconciliation-code change was detected during that study. Affected mixed-version runs were excluded and the same dates rerun. Weekly outcomes were not selected by profitability.</p>
<h3>88-instrument screen and decay / volatility sweeps</h3><p>Initial screen: {screen['requested']} requested instruments, {screen['complete']} completed, {screen['failed']} unavailable (OMNI and TON); {screen['profitable']} profitable after fees versus {screen['positive_before_fees']} before fees. Median return: {screen['median_return_pct']:.3f}%. Subsequent 3-minute refresh/forecast sweeps compared 30/60/90/180-second decay and 30/300-second volatility half-lives.</p>{table(vol,money=['Sum of independent-book P&L'],pct=['Median return'],integer=['Vol half-life (s)','Decay (s)','Instruments','Profitable'])}
<p>Independent-book sums are screen aggregates, not the return of one funded portfolio. A shorter volatility half-life did not produce a consistent improvement across decay settings. A volatility-sized, matched-lot prototype and calibration were prepared; no completed performance screen is claimed for that prototype.</p>
<h3>Rolling 12-minute coin selection</h3><p>Ranked prior 12-minute performance, used a daily-equivalent 0.5% threshold, waited three minutes, traded selected coins and liquidated departing names. The common available set contained 86 instruments.</p>{table(rotation[['name','net_pnl','fees','rotation_fees','fills']].rename(columns={'name':'Variant','net_pnl':'Net P&L','fees':'Fees','rotation_fees':'Rotation fees','fills':'Fills'}),money=['Net P&L','Fees','Rotation fees'],integer=['Fills'])}
<h3>Reconciliation timing diagnostic</h3><p>For the October 10–17, 2025 week, removing API waits changed execution timing substantially. This supports a timing effect; it does not mean the read-only post-run accounting checks manufactured P&L.</p>{table(timing[['mode','net_pnl','fills','max_drawdown_pct','temporary_sync_mismatches']].rename(columns={'mode':'Variant','net_pnl':'Marked P&L','fills':'Fills','max_drawdown_pct':'Max drawdown','temporary_sync_mismatches':'Temporary sync mismatches'}),money=['Marked P&L'],pct=['Max drawdown'],integer=['Fills','Temporary sync mismatches'])}
</section>
<section id="explorer"><div class="kicker">10 / All recent runs</div><h2>Filter the 348 saved Tardis outputs</h2><p>Each row is one UTC-day simulation. The CSV download respects the current filters; repeated controls remain included. P&L includes modeled terminal costs.</p>
<div class="filters"><label>Study<select id="family"><option value="">All studies</option></select></label><label>Day<select id="day"><option value="">Both days</option><option>2026-09-01</option><option>2026-10-01</option></select></label><label>Reference<select id="reference"><option value="">All references</option><option>midpoint</option><option>latest-close</option><option>lagged-close</option></select></label><label>Penetration p<select id="probability"><option value="">All p</option><option>0</option><option>0.5</option><option>0.8</option><option>0.9</option><option>1</option></select></label><button id="download">Download filtered CSV</button></div>
<p id="count" aria-live="polite"></p><div class="table-wrap explorer"><table><thead><tr><th>Study</th><th>Day</th><th>Refresh</th><th>Reference</th><th>Min</th><th>Shorts</th><th>p</th><th>Delay</th><th>Net P&L</th><th>Fees</th><th>Fills</th><th>Max DD</th><th>Eligible</th></tr></thead><tbody id="runs"></tbody></table></div><noscript>Enable JavaScript for filtering. Static tables above contain the principal comparisons.</noscript></section>
<section id="limits"><div class="kicker">11 / Interpretation and next steps</div><h2>What these experiments establish</h2><ul><li>They establish that modeled outcomes are strongly sensitive to fill rules, quote rounding, short mechanics, and timing.</li><li>They do not establish a safe trading horizon, a guaranteed worst-case P&L, live profitability, or that stale data is beneficial.</li><li>Price horizon, quote refresh and inventory holding period are separate. Calling a 3- or 5-minute strategy “mid-frequency” does not change execution risk.</li></ul>
<h3>The proposed opposite-quote / trade-through rule</h3><p>For a buy at B, best ask ≤ B is an eligibility test; for a sell at A, best bid ≥ A is symmetric. Our existing penetration model instead checks candle trade extremes one tick beyond the limit. These are different events. A historical best bid falling below B can reflect cancellations; it does not prove that a hypothetical buy at B filled. A trade below an already-active bid is stronger evidence under price priority, but adding our quantity can change the historical path.</p>
<p>Before treating a conservative scenario as realistic, model exchange acceptance, cancellation latency, arrival-time post-only rejection or taker execution, partial fills and available volume. Keep p=1 as a stress scenario rather than a lower bound. Evaluate markouts and inventory attribution, then repeat across more dates and seeds.</p>
<h3>Report validation and reproducibility</h3><p>This report was built from saved result files without downloading new market data or changing production configuration. It checked expected row counts and saved validation flags, preserves complete recent-run data, and records SHA-256 hashes of source artifacts. Simulation validations include ledger reconciliation, causal quote/feature timestamps, fill checks, seed-draw checks, and zero-delay regressions where applicable. They test implementation consistency, not economic realism.</p>
<details><summary>Evidence files and SHA-256 hashes</summary><div class="table-wrap"><table><thead><tr><th>Source artifact (relative to results/)</th><th>SHA-256</th></tr></thead><tbody>{''.join('<tr><td><a href="../../results/'+esc(k)+'">'+esc(k)+'</a></td><td class="hash">'+v+'</td></tr>' for k,v in evidence.items())}</tbody></table></div></details>
<p class="small">External documentation: <a href="https://docs.tardis.dev/historical-data-details/binance">Tardis Binance coverage</a> · <a href="https://docs.tardis.dev/downloadable-csv-files/api">Tardis timestamps and free samples</a> · <a href="https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md">Binance order API</a>. Core tables, charts and explorer data are embedded; repository source links work when this report remains in its original directory.</p></section>
<footer>Prepared from the repository’s saved experiments · 8 October 2026 · Research results, not a live performance record.</footer></main>'''
css='''*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:70px}body{margin:0;background:#f3f5f8;color:#243347;font:15px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}h1,h2,h3,h4{line-height:1.18;color:#132b40}h1{font-size:clamp(36px,5vw,60px);letter-spacing:-2px;margin:16px 0}h2{font-size:29px;letter-spacing:-.6px;margin:8px 0 22px}h3{font-size:19px;margin:30px 0 13px}h4{margin:0 0 12px}p{max-width:100ch}a{color:#076b84}button{cursor:pointer;border:0;border-radius:6px;background:#126b80;color:white;padding:11px 16px;font:inherit;font-weight:600}button:hover{background:#08485b}.hero{background:#122e43;color:#dfebf0;padding:58px max(5vw,calc((100vw - 1180px)/2)) 48px}.hero h1{color:white}.eyebrow,.kicker{font-size:12px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase}.eyebrow{color:#78d2cb}.subtitle{font-size:20px;max-width:750px}.meta{font-size:13px;color:#b8cbd8}.actions{display:flex;align-items:center;gap:24px;margin-top:25px}.actions a{color:#b5e2e7}nav{position:sticky;top:0;background:#ffffffed;backdrop-filter:blur(9px);border-bottom:1px solid #dce4eb;display:flex;gap:19px;overflow-x:auto;padding:15px max(3vw,calc((100vw - 1180px)/2));z-index:3;white-space:nowrap;font-size:13px}nav a{text-decoration:none;color:#334b60}main{max-width:1240px;margin:0 auto;padding:30px}section{background:white;border:1px solid #dfe6ed;border-radius:12px;padding:34px 38px;margin:0 0 24px;box-shadow:0 3px 15px #172c4010}.kicker{color:#147283}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0}.cards>div{background:#eef5f7;padding:20px;border-radius:8px}.cards b{display:block;font-size:32px;color:#0b6177;line-height:1.25}.cards span{display:block;font-size:13px;margin-top:8px}.callout{border-left:4px solid #e0a343;background:#fff7e9;padding:17px 20px;margin:25px 0}.danger{border-color:#ba6543;background:#fff2ec}.definitions{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.definitions>div{padding:20px;border:1px solid #dae5ec;border-radius:8px}.definitions p{font-size:14px;margin-bottom:0}.table-wrap{overflow:auto;margin:14px 0 22px}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}th{background:#eff4f7;color:#395167;font-weight:650;text-align:left}th,td{padding:10px 12px;border-bottom:1px solid #e4eaf0;vertical-align:top}tbody tr:nth-child(even){background:#fafbfd}tbody tr:hover{background:#f0f7f8}.num{text-align:right;white-space:nowrap}.pos{color:#125d58}.neg{color:#a14532}.chart{display:block;max-width:100%;height:auto;border-radius:7px;border:1px solid #e3e9ef;margin:25px 0}summary{cursor:pointer;font-weight:600;color:#126b80;padding:14px 0}.filters{display:flex;gap:12px;flex-wrap:wrap;align-items:end}.filters label{display:grid;font-size:12px;font-weight:600;gap:5px}select{padding:10px;border:1px solid #cbd8e1;border-radius:5px;max-width:290px;background:white;color:#243347;font:inherit}.explorer{max-height:570px}.explorer th{position:sticky;top:0;z-index:1}.small{font-size:12px;color:#647386}.hash{font:10px/1.6 monospace;word-break:break-all;min-width:180px}footer{text-align:center;color:#748295;font-size:12px;padding:25px}li{margin-bottom:9px}#count{font-size:13px;color:#647386}@media(max-width:700px){main{padding:12px}section{padding:24px 18px}.cards,.definitions{grid-template-columns:1fr}.hero{padding:36px 24px}.actions{align-items:start;flex-direction:column;gap:12px}h2{font-size:25px}}@media print{@page{size:A4;margin:13mm}body{background:white;color:#111;font-size:10px}.hero{padding:16px 0;background:white;color:#222}.hero h1{color:#132b40;font-size:30px}.subtitle{font-size:14px}.meta{color:#555}.actions,nav,#explorer,.filters{display:none}main{padding:0;max-width:none}section{border:0;border-radius:0;box-shadow:none;padding:10px 0;margin:0 0 10px}h2{font-size:20px}h3{font-size:14px;break-after:avoid}h2,.kicker{break-after:avoid}table{font-size:8px}th,td{padding:5px}.table-wrap{overflow:visible}tr,.cards,.definitions,.chart{break-inside:avoid}.chart{max-height:225mm;object-fit:contain}.cards b{font-size:24px}.cards span,.definitions p{font-size:10px}.callout{padding:9px 12px}.hash{font-size:7px}a{text-decoration:none;color:inherit}summary{font-size:11px}}'''
script='''const data=DATA; const ids=['family','day','reference','probability'];let shown=[];
const family=document.getElementById('family');[...new Set(data.map(x=>x.family))].forEach(x=>{let o=document.createElement('option');o.value=x;o.textContent=x;family.append(o)});
const dollars=v=>(v<0?'−':'+')+'$'+Math.abs(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
function render(){const values=Object.fromEntries(ids.map(id=>[id,document.getElementById(id).value]));shown=data.filter(r=>(!values.family||r.family===values.family)&&(!values.day||r.day===values.day)&&(!values.reference||r.reference===values.reference)&&(!values.probability||r.p===Number(values.probability)));const body=document.getElementById('runs');body.replaceChildren();shown.forEach(r=>{const tr=document.createElement('tr');const vals=[r.family,r.day,r.refresh+'s',r.reference,r.minimum,r.shorts,r.p,r.delay+'s',dollars(r.pnl),dollars(r.fees),r.fills.toLocaleString(),r.drawdown.toFixed(2)+'%',r.eligible+'/'+r.decisions];vals.forEach((v,i)=>{let td=document.createElement('td');td.textContent=v;if(i>=8)td.className='num';if(i===8)td.classList.add(r.pnl<0?'neg':'pos');tr.append(td)});body.append(tr)});document.getElementById('count').textContent=shown.length+' run outputs shown. Repeated controls are included; do not sum them as portfolio P&L.';}
ids.forEach(id=>document.getElementById(id).addEventListener('change',render));render();
document.getElementById('download').addEventListener('click',()=>{const keys=Object.keys(data[0]);const quote=v=>'"'+String(v).replaceAll('"','""')+'"';const text=[keys.map(quote).join(','),...shown.map(r=>keys.map(k=>quote(r[k])).join(','))].join('\\r\\n');const u=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=u;a.download='mm-scenarios-filtered.csv';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)});
let opened=[];window.addEventListener('beforeprint',()=>{opened=[...document.querySelectorAll('details')].map(x=>x.open);document.querySelectorAll('details').forEach(x=>x.open=true)});window.addEventListener('afterprint',()=>document.querySelectorAll('details').forEach((x,i)=>x.open=opened[i]));'''.replace('DATA',json.dumps(allrows,allow_nan=False).replace('<','\\u003c'))
output='<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Crypto market-making scenario report · 8 October 2026</title><style>'+css+'</style></head><body>'+body+'<script>'+script+'</script></body></html>'
(OUT/'report.html').write_text(output)
# Re-save manifest after chart evidence was added.
(OUT/'evidence-manifest.json').write_text(json.dumps(evidence,indent=2))
(OUT/'report-validation.json').write_text(json.dumps(dict(tardis_rows=len(allrows),source_files=len(evidence),family_counts={n:c for n,p,c in families},
 standalone_charts=True,no_external_scripts=True,no_network_requests=True,html_sha256=hashlib.sha256(output.encode()).hexdigest()),indent=2))
print(f'Wrote {OUT/"report.html"} ({len(output):,} characters); {len(allrows)} recent run rows; {len(evidence)} source hashes.')
