from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
import pytesseract
import io, os, re, urllib.parse

app = FastAPI()
BASE = os.path.dirname(__file__)

SITES = {
    'shillonghousiewin.com': 'Shillong Housie Win',
    'mylliemtambola01.com': 'Mylliem Tambola 01',
    'jollytombola.in': 'Jolly Tombola',
    'lumniwar.com': 'Lum Ni War',
    'khasitambolahousieonline2.com': 'Khasi Tambola Housie Online 2'
}

def host(u):
    return urllib.parse.urlparse(u).netloc.lower().split(':')[0].removeprefix('www.')

def nums(text):
    return [int(x) for x in re.findall(r'(?<!\d)(?:[1-9]|[1-8]\d|90)(?!\d)', text)]

def clean_ticket(values):
    out=[]
    for n in values:
        if 1 <= n <= 90 and n not in out:
            out.append(n)
    return out[:15]

def ticket_score(a):
    a=clean_ticket(a)
    if len(a)!=15:return -1
    cols=[0]*9
    for n in a: cols[min(8,(n-1)//10)]+=1
    return sum(1 for c in cols if c>=1)*10-sum(abs(c-2) for c in cols)

def parse_dom(soup):
    out=[]; seen=set()
    for el in soup.find_all(['table','div','li','article','tr']):
        txt=' '.join(el.stripped_strings)
        if len(txt)>900: continue
        a=clean_ticket(nums(txt))
        if len(a)==15 and len(set(a))==15:
            key=tuple(a)
            if key not in seen:
                seen.add(key); out.append({'flat':a,'score':ticket_score(a),'source':'dom'})
    for el in soup.find_all(True):
        bits=[]
        for attr in ('alt','title','aria-label','data-ticket','data-numbers','value'):
            if el.has_attr(attr): bits.append(str(el.get(attr)))
        a=clean_ticket(nums(' '.join(bits)))
        if len(a)==15 and len(set(a))==15:
            key=tuple(a)
            if key not in seen:
                seen.add(key); out.append({'flat':a,'score':ticket_score(a),'source':'attribute'})
    return out

def ocr_values(im, psm):
    try:
        im=ImageOps.grayscale(im)
        im=ImageEnhance.Contrast(im).enhance(2.2)
        im=im.resize((im.width*2,im.height*2))
        im=im.filter(ImageFilter.SHARPEN)
        data=pytesseract.image_to_data(im,config=f'--psm {psm} -c tessedit_char_whitelist=0123456789',output_type=pytesseract.Output.DICT)
    except Exception:
        return []
    words=[]
    for i,raw in enumerate(data.get('text',[])):
        raw=(raw or '').strip()
        if not raw: continue
        found=nums(raw)
        if not found: continue
        try:
            x=int(data['left'][i]); y=int(data['top'][i]); w=max(1,int(data['width'][i])); h=max(1,int(data['height'][i]))
        except Exception: continue
        for n in found: words.append((y+h//2,x,n,w,h))
    return words

def build_ocr_tickets(words):
    # Group OCR words into visual rows, then look for 3 adjacent rows forming a 15-number ticket.
    if not words:return []
    words.sort(key=lambda z:(z[0],z[1]))
    lines=[]
    for item in words:
        y=item[0]
        target=None
        for line in reversed(lines[-8:]):
            if abs(line[0]-y)<=28:
                target=line; break
        if target is None:
            target=[y,[]]; lines.append(target)
        target[1].append(item)
    numeric=[]
    for y,items in lines:
        items.sort(key=lambda z:z[1])
        vals=[n for _,_,n,_,_ in items if 1<=n<=90]
        if 4<=len(vals)<=8:
            numeric.append((y,vals))
    found=[];seen=set()
    for i in range(len(numeric)-2):
        a,b,c=numeric[i:i+3]
        if c[0]-a[0]>260: continue
        vals=clean_ticket(a[1]+b[1]+c[1])
        if len(vals)==15 and len(set(vals))==15:
            key=tuple(vals)
            if key not in seen:
                seen.add(key);found.append({'flat':vals,'score':ticket_score(vals),'source':'ocr'})
    return found

def parse_ocr(image_bytes):
    try: base=Image.open(io.BytesIO(image_bytes)).convert('RGB')
    except Exception:return []
    all_found=[]
    # Multiple OCR layouts are important because ticket cards can be compact or spread out.
    for psm in (6,11,12):
        all_found.extend(build_ocr_tickets(ocr_values(base,psm)))
    # Also OCR horizontal strips; this avoids unrelated page text merging into a ticket.
    if base.height>0:
        step=max(500,base.height//12)
        for top in range(0,base.height,step):
            crop=base.crop((0,top,base.width,min(base.height,top+step+250)))
            all_found.extend(build_ocr_tickets(ocr_values(crop,6)))
    unique=[];seen=set()
    for t in all_found:
        key=tuple(t['flat'])
        if key not in seen:
            seen.add(key);unique.append(t)
    return unique

async def load(url):
    async with async_playwright() as p:
        b=await p.chromium.launch(headless=True)
        page=await b.new_page(viewport={'width':1600,'height':1200},device_scale_factor=1)
        try:
            await page.goto(url,wait_until='domcontentloaded',timeout=30000)
            await page.wait_for_timeout(5000 if host(url)=='lumniwar.com' else 2500)
            for pat in ['CHECK AVAILABLE TICKET','TICKET FOR COMING GAME','AVAILABLE TICKET']:
                try:
                    loc=page.get_by_text(re.compile(pat,re.I)).first
                    await loc.scroll_into_view_if_needed(timeout=1200)
                    await loc.click(timeout=2200)
                    await page.wait_for_timeout(3000)
                    break
                except Exception: pass
            if host(url)=='lumniwar.com':
                await page.wait_for_timeout(3000)
                # Trigger lazy-loaded ticket content by scrolling through the page.
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await page.wait_for_timeout(1500)
                await page.evaluate('window.scrollTo(0, 0)')
                await page.wait_for_timeout(1000)
            html=await page.content(); title=await page.title(); text=await page.locator('body').inner_text()
            shot=await page.screenshot(full_page=True,type='png')
        finally:
            await b.close()
    return title,html,text,shot

@app.get('/api/scan')
async def scan(url:str=Query(...)):
    if not url.startswith(('http://','https://')): raise HTTPException(400,'Enter a full URL.')
    site=SITES.get(host(url),'Generic Tambola')
    try:title,html,text,shot=await load(url)
    except Exception as e: raise HTTPException(502,'Could not read the public page: '+str(e))
    soup=BeautifulSoup(html,'html.parser')
    tickets=parse_dom(soup)
    ocr=parse_ocr(shot)
    # Prefer actual DOM tickets, but include OCR tickets too when the site renders cards as images/canvas.
    combined=tickets+ocr
    unique=[];seen=set()
    for t in combined:
        a=clean_ticket(t.get('flat',[]))
        if len(a)!=15 or len(set(a))!=15: continue
        key=tuple(a)
        if key not in seen:
            seen.add(key);t['flat']=a;unique.append(t)
    unique.sort(key=lambda x:x['score'],reverse=True)
    return {'url':url,'site':site,'title':title,'tickets':unique[:100], 'selected_index':0 if unique else None,
            'message':'Tickets recovered from the public page.' if unique else 'No 15-number ticket was readable yet; the page may render tickets behind a login or in a protected component.'}

@app.get('/api/called')
async def called(url:str=Query(...)):
    try:title,html,text,shot=await load(url)
    except Exception as e: raise HTTPException(502,str(e))
    found=[]
    for p in [r'(?:called|number\s*called|last\s*number|now\s*calling)\D{0,30}([1-9]|[1-8]\d|90)\b',r'\b([1-9]|[1-8]\d|90)\b\D{0,15}(?:called|number\s*called)']:
        found += [int(x) for x in re.findall(p,text,re.I)]
    return {'called':list(dict.fromkeys(found[-10:]))}

@app.get('/')
async def home(): return FileResponse(os.path.join(BASE,'index.html'))

@app.get('/{path:path}')
async def static(path:str):
    f=os.path.join(BASE,path)
    return FileResponse(f if os.path.isfile(f) else os.path.join(BASE,'index.html'))
