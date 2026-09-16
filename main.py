from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
import pytesseract
import numpy as np
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


def ticket_score(a):
    a = list(dict.fromkeys(n for n in a if 1 <= n <= 90))
    if len(a) != 15:
        return -1
    cols = [0] * 9
    for n in a:
        cols[min(8, (n - 1) // 10)] += 1
    if any(c == 0 or c > 3 for c in cols):
        return -20
    return sum(1 for c in cols if c >= 1) * 10 - sum(abs(c - 2) for c in cols)


def clean_ticket(values):
    out = []
    for n in values:
        if 1 <= n <= 90 and n not in out:
            out.append(n)
    return out if len(out) == 15 else []


def remove_labels(text):
    # Ticket cards often contain "Ticket 57", "No. 57", etc. Remove the label number
    # before extracting the 15 actual ticket numbers.
    text = re.sub(r'(?i)\b(?:ticket|ticket\s*no\.?|no\.?|serial|sl\.?)[\s:#-]*\d{1,3}\b', ' ', text)
    return text


def recover_ticket(values):
    vals = [n for n in values if 1 <= n <= 90]
    vals = list(dict.fromkeys(vals))
    if len(vals) == 15:
        return vals
    # If a card has one or two extra numbers (usually its ticket ID), try small subsets
    # and keep only standard Tambola column distributions: each decade-column has 1-3.
    if 15 < len(vals) <= 18:
        from itertools import combinations
        best = None
        for pick in combinations(vals, 15):
            score = ticket_score(pick)
            if score >= 0 and (best is None or score > best[0]):
                best = (score, list(pick))
        return best[1] if best else []
    return []


def parse_dom(soup):
    out, seen = [], set()
    for el in soup.find_all(True):
        txt = ' '.join(el.stripped_strings)
        if not txt or len(txt) > 1200:
            continue
        a = recover_ticket(nums(remove_labels(txt)))
        if len(a) == 15:
            key = tuple(a)
            if key not in seen:
                seen.add(key)
                out.append({'flat': a, 'score': ticket_score(a), 'source': 'dom'})
        attrs = []
        for attr in ('alt', 'title', 'aria-label', 'data-ticket', 'data-numbers', 'value'):
            if el.has_attr(attr):
                attrs.append(str(el.get(attr)))
        a = recover_ticket(nums(remove_labels(' '.join(attrs))))
        if len(a) == 15:
            key = tuple(a)
            if key not in seen:
                seen.add(key)
                out.append({'flat': a, 'score': ticket_score(a), 'source': 'attribute'})
    return out


def ocr_values(im, psm):
    try:
        im = ImageOps.grayscale(im)
        im = ImageEnhance.Contrast(im).enhance(2.4)
        im = im.resize((im.width * 3, im.height * 3))
        im = im.filter(ImageFilter.SHARPEN)
        data = pytesseract.image_to_data(
            im,
            config=f'--psm {psm} -c tessedit_char_whitelist=0123456789',
            output_type=pytesseract.Output.DICT,
        )
    except Exception:
        return []
    words = []
    for i, raw in enumerate(data.get('text', [])):
        raw = (raw or '').strip()
        if not raw:
            continue
        found = nums(raw)
        if not found:
            continue
        try:
            x = int(data['left'][i]); y = int(data['top'][i])
            w = max(1, int(data['width'][i])); h = max(1, int(data['height'][i]))
        except Exception:
            continue
        for n in found:
            if 1 <= n <= 90:
                words.append((y + h // 2, x + w // 2, n, w, h))
    return words


def row_clusters(words):
    if not words:
        return []
    words = sorted(words, key=lambda z: (z[0], z[1]))
    lines = []
    for item in words:
        y = item[0]
        target = None
        for line in reversed(lines[-10:]):
            tolerance = max(18, min(45, item[4] * 0.9))
            if abs(line[0] - y) <= tolerance:
                target = line
                break
        if target is None:
            target = [y, []]
            lines.append(target)
        target[1].append(item)
    for line in lines:
        line[1].sort(key=lambda z: z[1])
    return lines


def build_ocr_tickets(words):
    lines = row_clusters(words)
    if not lines:
        return []
    found, seen = [], set()
    # A standard Tambola ticket has exactly 5 numbers on each of 3 rows.
    for i in range(len(lines) - 2):
        triple = lines[i:i + 3]
        ys = [x[0] for x in triple]
        if ys[2] - ys[0] > 420 or ys[1] - ys[0] < 12 or ys[2] - ys[1] < 12:
            continue
        counts = [len(x[1]) for x in triple]
        if any(c < 4 or c > 8 for c in counts):
            continue
        # Try to take five numbers from each row. If OCR added a ticket number,
        # keep the five-number selection whose x positions look most like a grid.
        choices = []
        for _, items in triple:
            if len(items) == 5:
                choices.append([items])
            else:
                from itertools import combinations
                choices.append([list(c) for c in combinations(items, 5)])
        if any(not c for c in choices):
            continue
        for a in choices[0][:30]:
            for b in choices[1][:30]:
                for c in choices[2][:30]:
                    picked = a + b + c
                    vals = [z[2] for z in picked]
                    if len(set(vals)) != 15:
                        continue
                    # A ticket should have numbers distributed across the 9 decade columns.
                    score = ticket_score(vals)
                    if score < 0:
                        continue
                    xs = sorted(z[1] for z in picked)
                    spread = xs[-1] - xs[0]
                    if spread < 80:
                        continue
                    key = tuple(vals)
                    if key not in seen:
                        seen.add(key)
                        found.append({'flat': vals, 'score': score + 5, 'source': 'ocr-grid'})
    return found


def cell_ocr(cell):
    candidates = []
    gray = ImageOps.grayscale(cell)
    gray = gray.resize((max(40, gray.width * 5), max(40, gray.height * 5)))
    for th in (None, 145, 165, 185, 200, 215, 230):
        im = gray if th is None else gray.point(lambda p: 0 if p < th else 255)
        for psm in (6, 7, 10, 13):
            try:
                txt = pytesseract.image_to_string(
                    im, config=f'--psm {psm} -c tessedit_char_whitelist=0123456789'
                ).strip()
            except Exception:
                continue
            for n in nums(txt):
                if 1 <= n <= 90:
                    candidates.append(n)
    if not candidates:
        return None
    two = [n for n in candidates if n >= 10]
    return max(set(two), key=two.count) if two else max(set(candidates), key=candidates.count)


def parse_green_grid(image_bytes):
    try:
        base = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    except Exception:
        return []
    try:
        a = np.asarray(base)
        r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
        green = (g > 125) & (g > r * 1.25) & (g > b * 1.02)
        row_counts = green.sum(axis=1)
        row_idx = np.where(row_counts > base.width * 0.18)[0]
        groups = []
        if len(row_idx):
            s = p = int(row_idx[0])
            for y in row_idx[1:]:
                y = int(y)
                if y > p + 2:
                    groups.append((s, p)); s = y
                p = y
            groups.append((s, p))
        out = []
        for i in range(len(groups) - 1):
            top, bottom = groups[i], groups[i + 1]
            height = bottom[1] - top[0]
            if height < 130 or height > 650:
                continue
            region = green[top[0]:bottom[1] + 1, :]
            col_counts = region.sum(axis=0)
            col_idx = np.where(col_counts > region.shape[0] * 0.40)[0]
            if len(col_idx) < 2:
                continue
            # Find the widest plausible pair of vertical borders.
            x0, x1 = int(col_idx[0]), int(col_idx[-1])
            if x1 - x0 < 220:
                continue
            y0, y1 = top[1] + 4, bottom[0] - 4
            ix0, ix1 = x0 + 5, x1 - 5
            iy0, iy1 = y0 + 2, y1 - 2
            grid = []
            for rr in range(3):
                row = []
                for cc in range(9):
                    xa = ix0 + (ix1 - ix0) * cc // 9 + 2
                    xb = ix0 + (ix1 - ix0) * (cc + 1) // 9 - 2
                    ya = iy0 + (iy1 - iy0) * rr // 3 + 2
                    yb = iy0 + (iy1 - iy0) * (rr + 1) // 3 - 2
                    row.append(cell_ocr(base.crop((xa, ya, xb, yb))))
                grid.append(row)
            flat = [n for row in grid for n in row if n is not None]
            if len(flat) == 15 and len(set(flat)) == 15:
                out.append({'flat': flat, 'grid': grid, 'score': ticket_score(flat) + 8, 'source': 'green-grid-ocr'})
        unique, seen = [], set()
        for t in out:
            key = tuple(t['flat'])
            if key not in seen:
                seen.add(key); unique.append(t)
        return unique
    except Exception:
        return []


def parse_ocr(image_bytes):
    try:
        base = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    except Exception:
        return []
    all_found = parse_green_grid(image_bytes)
    for psm in (6, 11, 12):
        all_found.extend(build_ocr_tickets(ocr_values(base, psm)))
    # OCR overlapping vertical slices so tickets that are small in a long page are larger.
    if base.height > 0:
        step = max(650, base.height // 10)
        for top in range(0, base.height, step):
            crop = base.crop((0, top, base.width, min(base.height, top + step + 320)))
            for psm in (6, 11):
                all_found.extend(build_ocr_tickets(ocr_values(crop, psm)))
    unique, seen = [], set()
    for t in all_found:
        a = clean_ticket(t.get('flat', []))
        if len(a) != 15:
            continue
        key = tuple(a)
        if key not in seen:
            seen.add(key); t['flat'] = a; unique.append(t)
    return unique


async def load(url):
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page(viewport={'width': 1800, 'height': 1400}, device_scale_factor=1)
        candidate_shots = []
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(6500 if host(url) == 'lumniwar.com' else 4500)
            for pat in ['CHECK AVAILABLE TICKET', 'TICKET FOR COMING GAME', 'AVAILABLE TICKET', 'COMING GAME']:
                try:
                    loc = page.get_by_text(re.compile(pat, re.I)).first
                    await loc.scroll_into_view_if_needed(timeout=1500)
                    await loc.click(timeout=2500)
                    await page.wait_for_timeout(3500)
                    break
                except Exception:
                    pass
            # Give JavaScript-rendered ticket lists time to finish, then expose the whole page.
            await page.wait_for_timeout(2500)
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await page.wait_for_timeout(1200)
            await page.evaluate('window.scrollTo(0, 0)')
            await page.wait_for_timeout(800)

            # Capture likely ticket-card elements individually. This is much more reliable than
            # OCRing a tiny ticket buried in a long page screenshot.
            candidates = await page.evaluate('''() => {
                const out=[];
                const re=/ticket|unsold|booked|available|coming|housie|tambola/i;
                for(const el of document.body.querySelectorAll('*')){
                    const r=el.getBoundingClientRect();
                    if(r.width<180 || r.height<70 || r.width>1500 || r.height>850) continue;
                    const st=getComputedStyle(el);
                    if(st.display==='none'||st.visibility==='hidden'||Number(st.opacity)===0) continue;
                    const txt=(el.innerText||'').trim();
                    const digits=(txt.match(/(?<!\\d)(?:[1-9]|[1-8]\\d|90)(?!\\d)/g)||[]).length;
                    const meta=((el.className||'')+' '+(el.id||'')+' '+txt.slice(0,180));
                    if(digits>=10 || (re.test(meta)&&digits>=3)) out.push({el,area:r.width*r.height,digits});
                }
                out.sort((a,b)=>a.area-b.area);
                return out.slice(0,80).map((x,i)=>{x.el.setAttribute('data-tambola-candidate',String(i+1));return i+1});
            }''')
            for n in candidates:
                try:
                    loc = page.locator(f'[data-tambola-candidate="{n}"]').first
                    box = await loc.bounding_box()
                    if not box or box['width'] < 180 or box['height'] < 70:
                        continue
                    candidate_shots.append(await loc.screenshot(type='png'))
                except Exception:
                    continue

            html = await page.content(); title = await page.title(); text = await page.locator('body').inner_text()
            shot = await page.screenshot(full_page=True, type='png')
        finally:
            await b.close()
    return title, html, text, shot, candidate_shots


@app.get('/api/scan')
async def scan(url: str = Query(...)):
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(400, 'Enter a full URL.')
    site = SITES.get(host(url), 'Generic Tambola')
    try:
        title, html, text, shot, candidate_shots = await load(url)
    except Exception as e:
        raise HTTPException(502, 'Could not read the public page: ' + str(e))
    soup = BeautifulSoup(html, 'html.parser')
    combined = parse_dom(soup)
    # OCR the individual candidate ticket/card elements first.
    for crop in candidate_shots:
        combined.extend(parse_green_grid(crop))
        for psm in (6, 11, 12):
            combined.extend(build_ocr_tickets(ocr_values(Image.open(io.BytesIO(crop)).convert('RGB'), psm)))
    combined.extend(parse_ocr(shot))

    unique, seen = [], set()
    for t in combined:
        a = clean_ticket(t.get('flat', []))
        if len(a) != 15 or len(set(a)) != 15:
            continue
        key = tuple(a)
        if key not in seen:
            seen.add(key); t['flat'] = a; unique.append(t)
    unique.sort(key=lambda x: x.get('score', 0), reverse=True)
    return {
        'url': url,
        'site': site,
        'title': title,
        'tickets': unique[:100],
        'selected_index': 0 if unique else None,
        'message': 'Tickets recovered from the public page.' if unique else 'No ticket grid was readable from the rendered public page.'
    }


@app.get('/api/called')
async def called(url: str = Query(...)):
    try:
        title, html, text, shot, candidate_shots = await load(url)
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
