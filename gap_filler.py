import os,re,sys,time,json
from datetime import datetime,timedelta
from urllib.parse import urljoin,urlparse
import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

C13=int(os.environ.get("GAP_FILLER_MAX_DAYS_BACK","10"))
C27=("linkedin.com","facebook.com","instagram.com","twitter.com","x.com","internshala.com","hellointern.in","unstop.com","naukri.com","glassdoor.com","indeed.com","youtube.com","wikipedia.org","justdial.com","crunchbase.com","google.com","ziprecruiter.com","tracxn.com","boardgamegeek.com","softonic.com","reddit.com","quora.com","medium.com","pinterest.com","apple.com","music.apple.com","spotify.com","app.spotify.com","play.google.com","apps.apple.com","amazon.com","amazon.in","flipkart.com","pharmeasy.in","1mg.com","netmeds.com","linktr.ee","bit.ly","tinyurl.com","zaubacorp.com","f6s.com","falconebiz.com","zoominfo.com","planetexim.net","find-and-update.company-information.service.gov.uk","company-information.service.gov.uk","indiamart.com","lawctopus.com","konfhub.com","usnews.com","ghar.tv","preqin.com","bebee.com","tohundguide.com","penguinrandomhouse.com","m.indiamart.com")
C7=os.environ.get("SCRAPERAPI_KEY","").strip()


def F17(r):
    if not r:
        return None
    r=str(r).strip()
    ff=("%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M","%Y-%m-%dT%H:%M:%S","%Y-%m-%d","%d/%m/%Y %H:%M:%S","%d/%m/%Y %H:%M","%d/%m/%Y %I:%M:%S %p","%d/%m/%Y","%m/%d/%Y %H:%M:%S","%m/%d/%Y %H:%M","%m/%d/%Y %I:%M:%S %p","%m/%d/%Y","%d %b %Y","%d %B %Y")
    z=[]
    for q in ff:
        try:
            z.append(datetime.strptime(r,q))
        except ValueError:
            continue
    try:
        w=datetime.fromisoformat(r.replace("Z",""))
        z.append(w.replace(tzinfo=None))
    except ValueError:
        pass
    if not z:
        return None
    k=datetime.now()+timedelta(days=1)
    v=[d for d in z if d<=k]
    if not v:
        return None
    return max(v)


C22=re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
C8=int(os.environ.get("GAP_FILLER_MAX_SCRAPERAPI_PAGES","200"))


class Ex(Exception):
    pass


C10={"blocked_pages":0,"scraperapi_pages":0,"scraperapi_emails":0}
C28=["https://www.googleapis.com/auth/spreadsheets"]


def F8(a,d=None):
    s=set()
    c=[]
    for e in a:
        x=e.lower().strip().strip(".").strip(",")
        for p in ("http://","https://"):
            if x.startswith(p):
                x=x[len(p):]
        if " " in x:
            continue
        if x in s:
            continue
        if any(b in x for b in C23):
            continue
        if x.endswith((".png",".jpg",".jpeg",".gif",".svg",".webp")):
            continue
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$",x):
            continue
        s.add(x)
        c.append(x)
    if d and c:
        m=[e for e in c if e.endswith("@"+d) or d in e.split("@")[-1]]
        if m:
            return m
        g=("gmail.com","outlook.com","yahoo.com","hotmail.com","rediffmail.com")
        n=[e for e in c if e.split("@")[-1] in g]
        return n if n else c
    return c


C4=int(os.environ.get("GAP_FILLER_MAX_PAGES_PER_SITE","8"))
C21={"User-Agent":("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")}


def F12(u):
    try:
        r=requests.get(u,headers=C21,timeout=10,allow_redirects=True)
        if r.status_code!=200:
            if r.status_code in C26:
                C10["blocked_pages"]+=1
                print(f"  [blocked] '{u}' refused direct access (status {r.status_code}).")
            return None,None
        return r.text,r.url
    except Exception:
        return None,None


C26=(401,403,406,429,503)
C9=70


def F13(u):
    if not C7:
        return None
    if C10["scraperapi_pages"]>=C8:
        return None
    C10["scraperapi_pages"]+=1
    n=C10["scraperapi_pages"]
    if n==C8:
        print(f"  [scraperapi] Reached the limit of {C8} ScraperAPI pages for this run. No more ScraperAPI use until the next run.")
    try:
        r=requests.get("https://api.scraperapi.com/",params={"api_key":C7,"url":u,"render":"true"},timeout=C9)
        if r.status_code!=200:
            print(f"  [scraperapi] Could not load '{u}' (status {r.status_code}). Page {n}/{C8}.")
            return None
        print(f"  [scraperapi] Loaded '{u}'. Page {n}/{C8}.")
        return r.text
    except Exception as e:
        print(f"  [scraperapi] Error loading '{u}': {e}. Page {n}/{C8}.")
        return None


C23=("sentry.io","wixpress.com","example.com","godaddy.com","yourdomain.com","domain.com","internshala.com","hellointern.in","sentry-next.wixpress.com","w3.org","schema.org")
C6=15000


def F9(h,d=None):
    o=BeautifulSoup(h,"html.parser")
    a=[x["href"].replace("mailto:","").split("?")[0] for x in o.find_all("a",href=True) if x["href"].lower().startswith("mailto:")]
    t=o.get_text(" ")
    b=C22.findall(t)
    return F8(a+b,d)


C25=("just a moment","attention required","access denied","checking your browser","verify you are human","are you a robot","request unsuccessful","pardon our interruption","enable javascript and cookies to continue","cf-browser-verification")


def F1():
    j=os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    i=json.loads(j)
    cr=service_account.Credentials.from_service_account_info(i,scopes=C28)
    return build("sheets","v4",credentials=cr)


C1=os.environ["GOOGLE_SHEET_ID"]
C2=os.environ.get("GOOGLE_SHEET_TAB_NAME","Sheet1")


def F3(se,cr,va,la):
    bo={"values":va}
    for at in range(1,C11+1):
        try:
            se.spreadsheets().values().update(spreadsheetId=C1,range=f"{C2}!{cr}",valueInputOption="USER_ENTERED",body=bo).execute()
            return True
        except Exception as e:
            print(f"  [warn] Sheets save failed for {la} (attempt {at}/{C11}): {e}",file=sys.stderr)
            if at<C11:
                time.sleep(C12)
    print(f"  [error] Could not save {la} after {C11} attempts. Continuing the run.",file=sys.stderr)
    return False


C11=3
C12=5
C3=os.environ["SERPER_API_KEY"]


def F7(nm):
    rs={}
    for at in range(1,3):
        try:
            rp=requests.post("https://google.serper.dev/search",headers={"X-API-KEY":C3,"Content-Type":"application/json"},json={"q":nm,"num":5},timeout=10)
            rp.raise_for_status()
            rs=rp.json()
            break
        except Exception as e:
            print(f"  [warn] Serper API error for '{nm}' (attempt {at}/2): {e}",file=sys.stderr)
            if at==2:
                return None
            time.sleep(3)
    for it in rs.get("organic",[]):
        lk=it.get("link","")
        if not any(bd in lk for bd in C27):
            return lk
    return None


C5=2
C24=("contact","about","reach","connect","touch","support","help","team","office","location")


def F10(h):
    if not h:
        return True
    try:
        t=BeautifulSoup(h,"html.parser").get_text(" ")
    except Exception:
        return True
    t=" ".join(t.split()).lower()
    if len(t)<200:
        return True
    if len(t)<1500 and any(m in t for m in C25):
        return True
    return False


def F14(u,br):
    pg=None
    try:
        pg=br.new_page(user_agent=C21["User-Agent"])
        pg.set_default_timeout(C6)
        pg.goto(u,wait_until="domcontentloaded")
        pg.wait_for_timeout(2000)
        h=pg.content()
        fu=pg.url
        pg.close()
        return h,fu
    except Exception as e:
        print(f"  [warn] Playwright error loading '{u}': {e}",file=sys.stderr)
        if pg:
            try:
                pg.close()
            except Exception:
                pass
        if "has been closed" in str(e) or "Target page, context or browser has been closed" in str(e):
            raise Ex(str(e))
        return None,None


def F2(se):
    rr=se.spreadsheets().values().get(spreadsheetId=C1,range=f"{C2}!A1:G").execute()
    return rr.get("values",[])


C18="G"
C19=6
C20="Gap Filler Checked"


def F15(u,br,d):
    ph,pu=F12(u)
    em=F9(ph,d) if ph else []
    if em:
        return ph,pu or u,em
    bh,bu=F14(u,br)
    if bh:
        em=F9(bh,d)
        if em:
            return bh,bu or u,em
    bb,bbu=None,u
    if bh and not F10(bh):
        bb,bbu=bh,bu or u
    elif ph and not F10(ph):
        bb,bbu=ph,pu or u
    if bb is None:
        sh=F13(u)
        if sh and not F10(sh):
            em=F9(sh,d)
            if em:
                C10["scraperapi_emails"]+=1
                print(f"  [scraperapi] Email found through ScraperAPI on '{u}'.")
            return sh,u,em
        fb=bh or ph
        return fb,(bu or pu or u),[]
    return bb,bbu,[]


def F6(se,hr):
    cv=hr[C19] if len(hr)>C19 else ""
    if str(cv).strip():
        return
    F3(se,f"{C18}1",[[C20]],"header cell G1")


C29=("justdial.com",)


def F11(h,bu):
    o=BeautifulSoup(h,"html.parser")
    bd=urlparse(bu).netloc
    pl=[]
    ol=[]
    s=set()
    for a in o.find_all("a",href=True):
        hr=a["href"].strip()
        if not hr or hr.startswith(("mailto:","tel:","javascript:","#")):
            continue
        fu=urljoin(bu,hr)
        pa=urlparse(fu)
        if pa.netloc!=bd:
            continue
        if fu in s:
            continue
        s.add(fu)
        lt=(a.get_text(" ") or "").lower()
        hl=hr.lower()
        if any(kw in lt or kw in hl for kw in C24):
            pl.append(fu)
        else:
            ol.append(fu)
    return pl+ol


def F5(se,rn):
    td=datetime.now().strftime("%Y-%m-%d")
    return F3(se,f"{C18}{rn}",[[td]],f"row {rn} checked marker")


def F4(se,rn,w,e):
    return F3(se,f"B{rn}:C{rn}",[[w,e]],f"row {rn} website and email")


def F16(wu,br):
    if not wu:
        return None
    if any(bs in wu.lower() for bs in C29):
        print(f"  [skip] Site is in SKIP_SITES_ENTIRELY list, not attempting: {wu}")
        return None
    st=time.time()
    d=urlparse(wu).netloc.replace("www.","")
    h,fu,ca=F15(wu,br,d)
    if ca:
        return ca[0]
    if not h:
        return None
    lv=F11(h,fu)[:C4]
    for lk in lv:
        if (time.time()-st)>C16:
            print(f"  [skip] Time limit of {C16} seconds reached for this company site. Moving on.")
            return None
        time.sleep(C5)
        _,_,ca=F15(lk,br,d)
        if ca:
            return ca[0]
    return None


C16=int(os.environ.get("GAP_FILLER_MAX_SECONDS_PER_COMPANY","120"))


def F18(se,br,i,row):
    nm=row[0] if len(row)>0 else ""
    cw=row[1] if len(row)>1 else ""
    ce=row[2] if len(row)>2 else ""
    wm=cw in ("","N/A")
    em=ce in ("","Not found")
    if not nm or (not wm and not em):
        return "no_gaps"
    print(f"[Row {i}] Filling gaps for '{nm}' (website_missing={wm}, email_missing={em})")
    nw=cw
    ne=ce
    if wm:
        fw=F7(nm)
        if fw:
            nw=fw
            print(f"  [found] Website: {fw}")
        time.sleep(C5)
    wt=nw if nw not in ("","N/A") else None
    if em and wt:
        fe=F16(wt,br)
        if fe:
            ne=fe
            print(f"  [found] Email: {fe}")
        else:
            print(f"  [not found] No email found across site for '{nm}'")
    stt="no_change"
    if nw!=cw or ne!=ce:
        sv=F4(se,i,nw or "N/A",ne or "Not found")
        if sv:
            print(f"  [updated] Row {i} saved.")
            stt="updated"
    time.sleep(C5)
    return stt


C14=int(os.environ.get("GAP_FILLER_MAX_ROWS_PER_RUN","250"))
C15=int(os.environ.get("GAP_FILLER_MAX_RUN_MINUTES","50"))
C17=int(os.environ.get("GAP_FILLER_STOP_AFTER_OLD_ROWS","30"))


def F19():
    se=F1()
    rows=F2(se)
    if len(rows)<=1:
        print("No data rows found in sheet.")
        return
    F6(se,rows[0])
    dr=rows[1:]
    pc=0
    fc=0
    re_=0
    cs=0
    ac=0
    osk=0
    cor=0
    ir=list(enumerate(dr,start=2))
    ir.reverse()
    pd=[]
    for r in dr:
        d=F17(r[4] if len(r)>4 else "")
        if d is not None:
            pd.append(d)
    if pd:
        nd=max(pd)
        ns=datetime(nd.year,nd.month,nd.day)
        co=ns-timedelta(days=max(C13,1)-1)
        print(f"Newest company row is dated {nd.date()}. Only processing rows on or after {co.date()} ({C13} day(s) counting the newest day).")
    else:
        co=datetime.now()-timedelta(days=C13)
        print(f"Could not read any dates from the sheet. Falling back to cutoff {co.date()}.")
    print(f"Safety limits for this run: {C14} companies searched, {C15} minutes, {C16} seconds per company, stop after {C17} old rows in a row.")
    if C7:
        print(f"ScraperAPI backup is ON, up to {C8} pages this run.")
    else:
        print("ScraperAPI backup is OFF because SCRAPERAPI_KEY is not set.")
    rs=time.time()
    px=sync_playwright().start()
    br=px.chromium.launch(headless=True)
    try:
        for i,row in ir:
            if cs>=C14:
                print(f"Reached the limit of {C14} companies searched for this run. Remaining rows will be picked up on the next run.")
                break
            if (time.time()-rs)>C15*60:
                print(f"Reached the time limit of {C15} minutes for this run. Remaining rows will be picked up on the next run.")
                break
            re_+=1
            sdr=row[4] if len(row)>4 else ""
            sd=F17(sdr)
            if sd is not None and sd<co:
                osk+=1
                cor+=1
                if cor>=C17:
                    print(f"[Row {i}] Found {C17} rows in a row older than the cutoff {co.date()}. Stopping run here.")
                    break
                continue
            cor=0
            cv=row[C19] if len(row)>C19 else ""
            if str(cv).strip():
                ac+=1
                continue
            rz=None
            try:
                rz=F18(se,br,i,row)
            except Ex:
                print(f"  [warn] Browser crashed on row {i}. Restarting browser and retrying this row.",file=sys.stderr)
                try:
                    br.close()
                except Exception:
                    pass
                br=px.chromium.launch(headless=True)
                try:
                    rz=F18(se,br,i,row)
                except Exception as e:
                    print(f"  [error] Row {i} failed again after browser restart, skipping: {e}",file=sys.stderr)
                    fc+=1
            except Exception as e:
                print(f"  [error] Unexpected error on row {i} ('{row[0] if row else ''}'), skipping: {e}",file=sys.stderr)
                fc+=1
            if rz in ("updated","no_change"):
                cs+=1
                if rz=="updated":
                    pc+=1
                F5(se,i)
    finally:
        try:
            br.close()
        except Exception:
            pass
        try:
            px.stop()
        except Exception:
            pass
    em_=round((time.time()-rs)/60,1)
    print(f"\nDone in {em_} minutes. Examined {re_} rows. Searched {cs} companies. Filled gaps for {pc} companies. Skipped {ac} rows already checked earlier and {osk} rows older than the cutoff. {fc} companies skipped due to errors.")
    print(f"Blocked pages seen: {C10['blocked_pages']}. ScraperAPI pages used: {C10['scraperapi_pages']}/{C8}. Emails found through ScraperAPI: {C10['scraperapi_emails']}.")


if __name__=="__main__":
    F19()
