from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import os,re,urllib.parse

app=FastAPI()
BASE=os.path.join(os.path.dirname(__file__),'..','frontend')

SITES={
 'shillonghousiewin.com':'Shillong Housie Win',
 'mylliemtambola01.com':'Mylliem Tambola 01',
 'jollytombola.in':'Jolly Tombola',
 'lumniwar.com':'Lum Ni War',
 'khasitambolahousieonline2.com':'Khasi Tambola Housie Online 2'
}
def host(u):
    return urllib.parse.urlparse(u).netloc.lower().split(':')[0].removeprefix('www.')
def nums(text):
    return [int(x) for x in re.findall(r'(?<!\d)(?:[1-9]|[1-8]\d|90)(?!\d)',text)]
def ticket_score(a):
    # Objective structural score, not a prediction of the winner.
    uniq=list(dict.fromkeys(a[:15]))
    if len(uniq)!=15:return -1
    cols=[0]*9
    for n in uniq:
        c=min(8,(n-1)//10); cols[c]+=1
    balance=sum(1 for c in cols if c>=1)
    return balance*10 - sum(abs(c-2) for c in cols)
def parse(soup):
    out=[]; seen=set()
    for el in soup.find_all(['table','div','li','article']):
        txt=' '.join(el.stripped_strings)
        if len(txt)>700: continue
        a=list(dict.fromkeys(nums(txt)))
        if len(a)>=15:
            key=tuple(a[:15])
            if key not in seen:
                seen.add(key); out.append({'flat':list(key),'score':ticket_score(a)})
        if len(out)>=100: break
    return out

async def load(url):
    async with async_playwright() as p:
        b=await p.chromium.launch(headless=True)
        page=await b.new_page()
        try:
            await page.goto(url,wait_until='domcontentloaded',timeout=30000)
            await page.wait_for_timeout(2500)
            # Common controls used by the supplied sites.
            for pat in ['CHECK AVAILABLE TICKET','TICKET FOR COMING GAME','AVAILABLE TICKET']:
                try:
                    await page.get_by_text(re.compile(pat,re.I)).first.click(timeout=1200)
                    await page.wait_for_timeout(1200); break
                except: pass
            html=await page.content()
            title=await page.title()
            # Publicly visible called numbers: conservative extraction from likely labels.
            text=await page.locator('body').inner_text()
        finally:
            await b.close()
    return title,html,text

@app.get('/api/scan')
async def scan(url:str=Query(...)):
    if not url.startswith(('http://','https://')): raise HTTPException(400,'Enter a full URL.')
    h=host(url); site=SITES.get(h,'Generic Tambola')
    try:title,html,text=await load(url)
    except Exception as e:raise HTTPException(502,'Could not read the public page: '+str(e))
    tickets=parse(BeautifulSoup(html,'html.parser'))
    tickets=[t for t in tickets if len(t['flat'])==15]
    tickets.sort(key=lambda x:x['score'],reverse=True)
    return {'url':url,'site':site,'title':title,'tickets':tickets,'selected_index':0 if tickets else None}

@app.get('/api/called')
async def called(url:str=Query(...)):
    try:title,html,text=await load(url)
    except Exception as e: raise HTTPException(502,str(e))
    # Only use numbers near explicit public caller labels; avoids treating ticket numbers as calls.
    found=[]
    patterns=[r'(?:called|number\s*called|last\s*number|now\s*calling)\D{0,30}([1-9]|[1-8]\d|90)\b',
              r'\b([1-9]|[1-8]\d|90)\b\D{0,15}(?:called|number\s*called)']
    for p in patterns:
        found += [int(x) for x in re.findall(p,text,re.I)]
    return {'called':list(dict.fromkeys(found[-10:]))}

@app.get('/')
async def home(): return FileResponse(os.path.join(BASE,'index.html'))
@app.get('/{path:path}')
async def static(path:str):
    f=os.path.join(BASE,path)
    return FileResponse(f if os.path.isfile(f) else os.path.join(BASE,'index.html'))
