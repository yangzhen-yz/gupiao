# -*- coding: utf-8 -*-
# 防御型（低波动/抗跌）候选筛选 v2：基于本地 history_kline.db + 估值/基本面，结果用腾讯接口补名字
import os, sqlite3, csv, re, urllib.request

# 路径一律从 __file__ 推：写死本机绝对路径等于"只有这台机器能跑"，
# 还会把目录布局（含 Windows 用户名）留在版本库里。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # .../server
HIST = os.path.join(_ROOT, 'data', 'history_kline.db')
MAIN = os.path.join(_ROOT, 'gupiao.db')
OUT = os.path.join(_ROOT, 'analysis_out', 'defensive_screen_latest.csv')
os.makedirs(os.path.dirname(OUT), exist_ok=True)
START = '2025-10-15'   # 近 1 年窗口

try:
    import numpy as np
    def std(x): return float(np.std(x, ddof=1))
except Exception:
    def std(x):
        n=len(x); m=sum(x)/n
        return (sum((v-m)**2 for v in x)/(n-1))**0.5

def norm_code(s):
    s=s.strip().lower()
    return s[2:] if s[:2] in ('sh','sz') else s

val={}; fund={}; names={}
g=sqlite3.connect(MAIN)
for sym,pe,pep,pbp in g.execute('SELECT symbol,pe,pe_pct,pb_pct FROM stock_valuation'):
    val[norm_code(sym)]=(pe,pep,pbp)
for sym,roe,ny,eps in g.execute('SELECT symbol,roe,net_profit_yoy,eps FROM stock_fundamental'):
    fund[norm_code(sym)]=(roe,ny,eps)
for sym,nm in g.execute('SELECT symbol,name FROM stock_basic_info'):
    names[norm_code(sym)]=nm
g.close()

def analyze(data):
    closes=[d[4] for d in data]; highs=[d[2] for d in data]
    lows=[d[3] for d in data]; amts=[d[5] for d in data]
    n=len(closes)
    if n<180: return None
    rets=[closes[i]/closes[i-1]-1 for i in range(1,n)]
    av=std(rets)*(242**0.5)
    runmax=closes[0]; mdd=0.0
    for p in closes:
        runmax=max(runmax,p); mdd=min(mdd, p/runmax-1)
    avg_amp=sum((highs[i]-lows[i])/closes[i-1] for i in range(1,n))/(n-1)
    down=sum(1 for r in rets if r<0)/len(rets)
    worst=min(rets)
    ma20=sum(closes[-20:])/20; ma60=sum(closes[-60:])/60
    price=closes[-1]
    return dict(price=price,av=av,mdd=mdd,amp=avg_amp,down=down,worst=worst,
               ma20=ma20,ma60=ma60,avg_amt=sum(amts)/n,n=n)

# 流式遍历
stats=[]; buf_sym=None; buf=[]
cur=sqlite3.connect(HIST).cursor()
cur.execute("SELECT symbol,date,open,high,low,close,turnover FROM stock_daily WHERE date>=? ORDER BY symbol,date",(START,))
while True:
    r=cur.fetchone()
    if r is None:
        break
    if r[0]!=buf_sym:
        if buf:
            s=analyze(buf)
            if s: stats.append((buf_sym,s))
        buf_sym=r[0]; buf=[]
    buf.append(r[1:])
if buf:
    s=analyze(buf)
    if s: stats.append((buf_sym,s))

def passes(sym,s):
    if s['avg_amt'] < 200_000_000: return '流动性不足(<2亿/日)'
    if s['price'] < s['ma60']: return '低于MA60(下行趋势)'
    if s['price'] < 0.95*s['ma20']: return '低于MA20(弱势)'
    if s['mdd'] < -0.30: return f'近1年回撤过大(>{abs(s["mdd"])*100:.0f}%)'
    if s['worst'] < -0.095: return '年内曾跌停'
    if s['down'] > 0.55: return '下跌天数过半'
    if s['av'] > 0.45: return '波动过高'
    v=val.get(sym)
    if v and v[0] is not None and not (0<v[0]<60): return f'估值异常(PE={v[0]})'
    f=fund.get(sym)
    if f and f[0] is not None:
        if f[0]<5: return f'ROE偏低({f[0]})'
        if (f[1] or 0)<-10: return '净利同比下滑>10%'
    return None

passed=[]
for sym,s in stats:
    if passes(sym,s) is None:
        s['sym']=sym; passed.append(s)

def score(s):
    v=1-min(s['av']/0.45,1)
    dd=1-min((-s['mdd'])/0.30,1)
    tr=max(0,min((s['price']/s['ma60']-1)/0.10+0.5,1))
    vs=0.5; vv=val.get(s['sym'])
    if vv and vv[1] is not None: vs=1-min(vv[1]/100,1)
    fs=0.5; ff=fund.get(s['sym'])
    if ff and ff[0] is not None: fs=min(ff[0]/20,1)*0.5+min((ff[1] or 0)/30,1)*0.5
    return 0.32*v+0.28*dd+0.20*tr+0.12*vs+0.08*fs

for s in passed: s['_score']=score(s)
passed.sort(key=lambda x:x['_score'],reverse=True)

# 腾讯 smartbox 补名字（全部候选），并剔除 ST/*ST 风险警示股
def fetch_name(code):
    try:
        url=f'https://smartbox.gtimg.cn/s3/?v=2&q={code}&t=gp'
        req=urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        data=urllib.request.urlopen(req, timeout=5).read().decode('gbk','ignore')
        m=re.search(r'v_hint="([^"]+)"', data)
        if m:
            p=m.group(1).split('~')
            if len(p)>=3:
                nm=p[2]
                if nm.isascii():   # smartbox 返回 \uXXXX 转义，需还原
                    try: nm=nm.encode('utf-8').decode('unicode_escape')
                    except Exception: pass
                return nm
    except Exception:
        return ''
    return ''

for s in passed:
    nm=names.get(s['sym']) or fetch_name(s['sym']) or ''
    names[s['sym']]=nm
    s['_name']=nm
# 剔除 ST / *ST（风险警示，可能大跌）
passed=[s for s in passed if not s['_name'].startswith(('ST','*ST','S','N '))]
passed.sort(key=lambda x:x['_score'],reverse=True)

TOP=passed[:25]
print(f'扫描 {len(stats)} 只（近1年数据充足）；通过防御过滤并剔除ST后 {len(passed)} 只；展示 Top {len(TOP)}\n')
hdr=['排名','代码','名称','现价','年化波动%','最大回撤%','下跌天数%','最差单日%','距MA60%','PE','估值分位%','ROE%','净利同比%','防御分']
print(' | '.join(hdr))
with open(OUT,'w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f); w.writerow(hdr)
    for i,s in enumerate(TOP,1):
        vv=val.get(s['sym']); ff=fund.get(s['sym'])
        pe=f"{vv[0]:.1f}" if vv and vv[0] is not None else '-'
        pep=f"{vv[1]:.0f}" if vv and vv[1] is not None else '-'
        roe=f"{ff[0]:.1f}" if ff and ff[0] is not None else '-'
        ny=f"{ff[1]:.1f}" if ff and ff[1] is not None else '-'
        row=[i,s['sym'],s['_name'],f"{s['price']:.2f}",f"{s['av']*100:.1f}",
             f"{s['mdd']*100:.1f}",f"{s['down']*100:.0f}",f"{s['worst']*100:.1f}",
             f"{(s['price']/s['ma60']-1)*100:.1f}",pe,pep,roe,ny,f"{s['_score']:.3f}"]
        w.writerow(row); print(' | '.join(str(x) for x in row))
print(f'\n已保存: {OUT}')
