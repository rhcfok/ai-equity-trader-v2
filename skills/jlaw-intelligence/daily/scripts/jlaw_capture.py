#!/usr/bin/env python3
"""Capture slide frames + page text from the latest J Law 'Master Your Trade' lessons.

Uses the Kimi WebBridge daemon (user's real browser, member login) to open the
Kajabi category page, resolve each lesson's Wistia video, then captures HD frames
by seeking an off-screen <video> element and drawing to canvas (screenshots of the
player show a frozen poster frame - canvas is the reliable path).

Requires: WebBridge daemon at http://127.0.0.1:10086 (skill kimi-webbridge).
"""
import argparse, base64, json, os, re, sys, time, urllib.request

DAEMON = "http://127.0.0.1:10086/command"
SESSION = "jlaw-daily"
DEFAULT_CATEGORY = "https://www.jlawstock.com/products/master-your-trade/categories/2153346051"  # 大盤板塊方向部署


def cmd(action, args=None, timeout=120):
    body = json.dumps({"action": action, "args": args or {}, "session": SESSION}).encode()
    req = urllib.request.Request(DAEMON, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    if not d.get("ok"):
        raise RuntimeError(f"{action} failed: {str(d)[:300]}")
    return d["data"]


def evaluate(code, timeout=120):
    return cmd("evaluate", {"code": code}, timeout).get("value")


def fetch_json(url, timeout=30):
    return json.loads(urllib.request.urlopen(url, timeout=timeout).read().decode())


def lesson_links():
    """Dated lesson links on the category page, newest first, subtitle versions excluded."""
    raw = evaluate(
        "(()=>{const a=[...document.querySelectorAll('a')]"
        ".filter(x=>/^\\d{1,2} \\w{3} \\d{4}/.test((x.innerText||'').trim())&&!/字幕/.test(x.innerText));"
        "const seen=new Set();const out=[];"
        "for(const x of a){const h=x.href;if(seen.has(h))continue;seen.add(h);"
        "out.push({title:(x.innerText||'').trim().split('\\n')[0],href:x.href});}"
        "return JSON.stringify(out);})()")
    return json.loads(raw or "[]")


def wistia_hash():
    for _ in range(8):
        h = evaluate(
            "(()=>{const m=[...document.querySelectorAll('[class*=wistia]')]"
            ".map(e=>e.className).join(' ').match(/wistia_async_([a-z0-9]+)/i);return m?m[1]:null;})()")
        if h:
            return h
        time.sleep(3)
    return None


def pick_hd_url(assets):
    hd = [a for a in assets if a.get("type") == "hd_mp4_video" and a.get("width") == 1280]
    if not hd:
        hd = [a for a in assets if a.get("type") in ("hd_mp4_video", "mp4_video")]
    if not hd:
        raise RuntimeError("no mp4 asset in Wistia embed JSON")
    return sorted(hd, key=lambda a: a.get("width") or 0)[0]["url"]


def bring_to_front():
    """Background tabs throttle media loading (readyState stays 0). Page.bringToFront
    enables focus emulation: the tab renders as if frontmost WITHOUT stealing user focus."""
    try:
        cmd("cdp", {"method": "Page.bringToFront", "params": {}})
    except Exception as e:
        print(f"  bringToFront warn: {e}", file=sys.stderr)


def release_focus():
    try:
        cmd("cdp", {"method": "Emulation.setFocusEmulationEnabled", "params": {"enabled": False}})
    except Exception:
        pass


def capture_lesson(lesson, out_dir, interval, max_frames):
    tag = re.sub(r"[^0-9A-Za-z]+", "", lesson["title"])[:12] or "lesson"
    cmd("navigate", {"url": lesson["href"]})
    bring_to_front()
    time.sleep(6)

    # page text carries the stance block (e.g. 目前市場風險胃納：*Neutral*) and any notes
    txt = evaluate("(()=>{const m=document.querySelector('main')||document.body;"
                   "return (m.innerText||'').slice(0,4000);})()") or ""
    txt_path = os.path.join(out_dir, f"{tag}_page.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt)

    wid = wistia_hash()
    if not wid:
        return {"lesson": lesson["title"], "error": "no wistia video found"}
    media = fetch_json(f"https://fast.wistia.com/embed/medias/{wid}.json").get("media", {})
    dur = float(media.get("duration") or 0)
    url = pick_hd_url(media.get("assets", []))

    # off-screen HD video element; crossOrigin needed or canvas is tainted
    evaluate(f"""(()=>{{const old=document.getElementById('__hdv');if(old)old.remove();
const v=document.createElement('video');v.id='__hdv';v.crossOrigin='anonymous';v.muted=true;
v.preload='auto';v.src={json.dumps(url)};v.style.display='none';document.body.appendChild(v);return 1;}})()""")
    for _ in range(20):
        try:
            if float(evaluate("(()=>{const v=document.getElementById('__hdv');"
                              "return v&&v.readyState>=1?v.duration:0;})()") or 0) > 0:
                break
        except Exception:
            pass
        time.sleep(2)

    times = list(range(60, int(dur) - 20, interval)) if dur else []
    if max_frames:
        times = times[:max_frames]
    saved = []
    for t in times:
        code = f"""(async()=>{{const v=document.getElementById('__hdv');
await new Promise((res,rej)=>{{const to=setTimeout(()=>rej(new Error('seek timeout')),25000);
v.addEventListener('seeked',()=>{{clearTimeout(to);res();}},{{once:true}});v.currentTime={t};}});
const c=document.createElement('canvas');c.width=v.videoWidth;c.height=v.videoHeight;
c.getContext('2d').drawImage(v,0,0);return c.toDataURL('image/jpeg',0.78);}})()"""
        try:
            dataurl = evaluate(code)
        except Exception as e:
            print(f"  {tag} t={t} retry once after: {e}", file=sys.stderr)
            try:
                dataurl = evaluate(code)
            except Exception as e2:
                print(f"  {tag} t={t} ERR {e2}", file=sys.stderr)
                continue
        if not (isinstance(dataurl, str) and dataurl.startswith("data:")):
            continue
        raw = base64.b64decode(dataurl.split(",", 1)[1])
        out = os.path.join(out_dir, f"{tag}_t{t:04d}.jpg")
        with open(out, "wb") as f:
            f.write(raw)
        saved.append(out)
        print(f"  saved {out} ({len(raw)} B)")
    return {"lesson": lesson["title"], "url": lesson["href"], "wistia": wid,
            "duration": dur, "page_text": txt_path, "frames": saved}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default=DEFAULT_CATEGORY)
    ap.add_argument("--out", required=True, help="output dir for frames/text/manifest")
    ap.add_argument("--lessons", type=int, default=2, help="number of latest dated lessons")
    ap.add_argument("--interval", type=int, default=150, help="seconds between captured frames")
    ap.add_argument("--max-frames", type=int, default=0, help="cap frames per lesson (testing)")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    cmd("navigate", {"url": a.category, "newTab": True, "group_title": "JLaw daily"})
    bring_to_front()
    time.sleep(4)
    links = lesson_links()[: a.lessons]
    print(f"lessons: {[l['title'] for l in links]}")
    try:
        results = [capture_lesson(l, a.out, a.interval, a.max_frames) for l in links]
    finally:
        release_focus()
    manifest = os.path.join(a.out, "manifest.json")
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump({"category": a.category, "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "lessons": results}, f, ensure_ascii=False, indent=1)
    print(f"manifest: {manifest}")


if __name__ == "__main__":
    main()
