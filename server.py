"""Run: pip install -r requirements.txt && python server.py"""
import json, re, os, threading, time
from datetime import datetime, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.request import Request, urlopen
from bs4 import BeautifulSoup

ROOT=Path(__file__).parent
SOURCE='https://www.kenangainvestors.com.my/fund-list'
# Only unambiguous exact names; a missing fund is reported, never guessed.
NAMES={
'Kenanga Growth Fund':'Kenanga Growth Fund',
'Kenanga Syariah Growth Fund':'Kenanga Syariah Growth Fund',
'Kenanga Shariah Growth Opportunities Fund':'Kenanga Shariah Growth Opportunities Fund',
'Kenanga Blue Chip Fund':'Kenanga Blue Chip Fund',
'Kenanga Premier Fund':'Kenanga Premier Fund',
'Kenanga Islamic Balanced Fund':'Kenanga Islamic Balanced Fund',
'Kenanga Managed Growth Fund':'Kenanga Managed Growth Fund',
'Kenanga Growth Fund Series 2 (MYR)':'Kenanga Growth Fund Series 2 (MYR)',
'Kenanga SyariahEXTRA Fund':'Kenanga SyariahEXTRA Fund',
'Kenanga Malaysian Inc Fund':'Kenanga Malaysian Inc Fund',
'Kenanga Islamic Asia Pacific (ex Japan) Total Return Fund':'Kenanga Islamic Asia Pacific (ex Japan) Total Return Fund',
'Kenanga Global Islamic Fund':'Kenanga Global Islamic Fund',
'Halogen Shariah Crypto Titans Fund':'Halogen Shariah Crypto Titans Fund',
'Kenanga ASnitaBOND Fund':'Kenanga ASnitaBOND Fund',
}
def norm(s):
 s=re.sub(r'\s+',' ',s).strip();return re.sub(r'\s+(?:\*+|\^+)$','',s).strip().casefold()
def parse_nav(html):
 soup=BeautifulSoup(html,'html.parser'); out={}; wanted={norm(v):k for k,v in NAMES.items()}
 for tr in soup.select('tr'):
  cells=tr.find_all(['td','th'],recursive=False)
  if len(cells)<3:continue
  name=norm(cells[0].get_text(' ',strip=True))
  if name not in wanted:continue
  date_text=cells[1].get_text(' ',strip=True)
  nav_text=cells[2].get_text(' ',strip=True)
  m=re.search(r'(?<!\d)(\d+\.\d{3,6})(?!\d)',nav_text)
  if not m:continue
  try:date=datetime.strptime(date_text,'%d %b %Y').strftime('%Y-%m-%d')
  except ValueError:continue
  price=float(m.group(1))
  if not 0<price<1000:continue
  out[wanted[name]]={'name':wanted[name],'nav':price,'date':date}
 return out

cache={'fetched_at':None,'funds':{},'error':None}
lock=threading.Lock()
def refresh(force=False):
 with lock:
  if not force and cache['fetched_at'] and time.time()-cache.get('_time',0)<3600:return dict(cache)
  try:
   req=Request(SOURCE,headers={'User-Agent':'Mozilla/5.0 (compatible; KenangaNAVDashboard/1.0)'})
   with urlopen(req,timeout=25) as r: html=r.read(3_000_000).decode('utf-8','replace')
   funds=parse_nav(html)
   if not funds:raise RuntimeError('No matching NAV rows found; source page structure may have changed.')
   cache.update({'funds':funds,'fetched_at':datetime.now(timezone.utc).isoformat(),'error':None,'_time':time.time()})
  except Exception as e:cache['error']=str(e)
  return dict(cache)
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT),**kwargs)
 def do_GET(self):
  if self.path.startswith('/api/nav'):
   # Always attempt retrieval when refresh is clicked; no fabricated prices on failure.
   data=refresh(force=True)
   payload=json.dumps({'source':SOURCE,'fetched_at':data['fetched_at'],'funds':list(data['funds'].values()),'error':data['error'],'missing':[k for k in NAMES if k not in data['funds']]},ensure_ascii=False).encode()
   self.send_response(200 if not data['error'] else 503);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
  if self.path=='/':self.path='/index.html'
  return super().do_GET()
if __name__=='__main__':
 port=int(os.environ.get('PORT','8000'))
 print(f'Open http://localhost:{port} in your browser')
 ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()
