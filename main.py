from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from PIL import Image
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
    out = []
    for n in values:
        if 1 <= n <= 90 and n not in out:
            out.append(n)
    return out[:15]

def ticket_score(a):
    uniq = clean_ticket(a)
    if len(uniq) != 15:
        return -1
    cols = [0] * 9
    for n in uniq:
        cols[min(8, (n - 1) // 10)] += 1
    balance = sum(1 for c in cols if c >= 1)
    return balance * 10 - sum(abs(c - 2) for c in cols)

def parse_dom(soup):
    out, seen = [], set()
    # First inspect normal visible containers.
    for el in soup.find_all(['table', 'div', 'li', 'article', 'tr']):
        txt = ' '.join(el.stripped_strings)
        if len(txt) > 900:
            continue
        a = clean_ticket(nums(txt))
        if len(a) == 15:
            key = tuple(a)
            if key not in seen:
                seen.add(key)
                out.append({'flat': a, 'score': ticket_score(a), 'source': 'dom'})
    # Also inspect image alt/title and data attributes used by ticket cards.
    for el in soup.find_all(True):
        bits = []
        for attr in ('alt', 'title', 'aria-label', 'data-ticket', 'data-numbers', 'value'):
            if el.has_attr(attr):
                bits.append(str(el.get(attr)))
        a = clean_ticket(nums(' '.join(bits)))
        if len(a) == 15:
            key = tuple(a)
            if key not in seen:
                seen.add(key)
                out.append({'flat': a, 'score': ticket_score(a), 'source': 'attribute'})
    return out

def parse_ocr(image_bytes):
    """Recover image/canvas ticket rows. Looks for three nearby OCR lines containing 5 numbers each."""
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        data = pytesseract.image_to_data(im, config='--psm 6', output_type=pytesseract.Output.DICT)
    except Exception:
        return []

    rows = []
    for i, raw in enumerate(data.get('text', [])):
        raw = (raw or '').strip()
        if not raw:
            continue
        m = re.fullmatch(r'(?:[Oo])', raw)
        if m:
            raw = '0'
        found = nums(raw)
        if not found:
            # OCR often joins punctuation or a digit with a stray character.
            found = nums(re.sub(r'[^0-9]', ' ', raw))
        found = [n for n in found if 1 <= n <= 90]
        if not found:
            continue
        try:
            x, y = int(data['left'][i]), int(data['top'][i])
            h = max(1, int(data['height'][i]))
        except Exception:
            continue
        rows.append((y + h // 2, x, found[0]))

    # Merge words that are on the same visual row.
    rows.sort()
    lines = []
    for y, x, n in rows:
        target = None
        for line in reversed(lines[-4:]):
            if abs(line[0] - y) <= 18:
                target = line
                break
        if target is None:
            target = [y, []]
            lines.append(target)
        target[1].append((x, n))

    numeric_lines = []
    for y, items in lines:
        items.sort()
        vals = [n for _, n in items if 1 <= n <= 90]
        if 3 <= len(vals) <= 6:
            numeric_lines.append((y, vals))

    found_tickets, seen = [], set()
    for i in range(len(numeric_lines) - 2):
        a, b, c = numeric_lines[i:i+3]
        if c[0] - a[0] > 170:
            continue
        vals = clean_ticket(a[1] + b[1] + c[1])
        if len(vals) == 15:
            key = tuple(vals)
            if key not in seen:
                seen.add(key)
                found_tickets.append({'flat': vals, 'score': ticket_score(vals), 'source': 'ocr'})
    return found_tickets

async def load(url):
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page(viewport={'width': 1440, 'height': 1100}, device_scale_factor=1)
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(3000 if host(url) == 'lumniwar.com' else 2200)
            for pat in ['CHECK AVAILABLE TICKET', 'TICKET FOR COMING GAME', 'AVAILABLE TICKET']:
                try:
                    loc = page.get_by_text(re.compile(pat, re.I)).first
                    await loc.click(timeout=1800)
                    await page.wait_for_timeout(1800)
                    break
                except Exception:
                    pass
            # Lum Ni War can render ticket numbers as images/canvas after the control is opened.
            if host(url) == 'lumniwar.com':
                await page.wait_for_timeout(1800)
            html = await page.content()
            title = await page.title()
            text = await page.locator('body').inner_text()
            shot = await page.screenshot(full_page=True, type='png')
        finally:
            await b.close()
    return title, html, text, shot

@app.get('/api/scan')
async def scan(url: str = Query(...)):
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(400, 'Enter a full URL.')
    site = SITES.get(host(url), 'Generic Tambola')
    try:
        title, html, text, shot = await load(url)
    except Exception as e:
        raise HTTPException(502, 'Could not read the public page: ' + str(e))

    soup = BeautifulSoup(html, 'html.parser')
    tickets = parse_dom(soup)
    if not tickets:
        tickets = parse_ocr(shot)

    # Keep only plausible 15-number tickets and remove duplicates.
    unique, seen = [], set()
    for t in tickets:
        a = clean_ticket(t['flat'])
        if len(a) != 15 or len(set(a)) != 15:
            continue
        key = tuple(a)
        if key not in seen:
            seen.add(key)
            t['flat'] = a
            unique.append(t)
    unique.sort(key=lambda x: x['score'], reverse=True)
    return {
        'url': url,
        'site': site,
        'title': title,
        'tickets': unique[:100],
        'selected_index': 0 if unique else None,
        'message': 'Tickets recovered from the public page.' if unique else 'No ticket numbers were readable on this page yet.'
    }

@app.get('/api/called')
async def called(url: str = Query(...)):
    try:
        title, html, text, shot = await load(url)
    except Exception as e:
        raise HTTPException(502, str(e))
    found = []
    patterns = [
        r'(?:called|number\s*called|last\s*number|now\s*calling)\D{0,30}([1-9]|[1-8]\d|90)\b',
        r'\b([1-9]|[1-8]\d|90)\b\D{0,15}(?:called|number\s*called)'
    ]
    for p in patterns:
        found += [int(x) for x in re.findall(p, text, re.I)]
    return {'called': list(dict.fromkeys(found[-10:]))}

@app.get('/')
async def home():
    return FileResponse(os.path.join(BASE, 'index.html'))

@app.get('/{path:path}')
async def static(path: str):
    f = os.path.join(BASE, path)
    return FileResponse(f if os.path.isfile(f) else os.path.join(BASE, 'index.html'))
