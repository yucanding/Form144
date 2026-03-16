import feedparser
import time
import xml.etree.ElementTree as ET
from curl_cffi import requests
from datetime import datetime
import random
import os

# --- 配置区 ---
TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_IDS = os.getenv("TG_CHAT_IDS", "").split(",")
CACHE_FILE = "seen_ids.txt"

SEC_HEADERS = {
    "User-Agent": "Academic Research Bot (fywang@umich.edu)",
    "Accept": "application/xml,text/html",
    "Host": "www.sec.gov"
}

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=144&count=100&output=atom"

EXCLUDE_KEYWORDS = ["restricted stock", "stock option exercise", "rsu", "option exercise", "performance share", "vesting", "ltip", "consideration", "award", "compensation"]

def send_telegram(message, target_id):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": target_id.strip(),
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True 
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"向 {target_id} 发送失败: {e}")

def get_ticker(company_name):
    try:
        clean_name = company_name.split(',')[0].split(' INC')[0].split(' CORP')[0]
        search_url = f"https://query2.finance.yahoo.com/v1/finance/search?q={clean_name}&quotesCount=1&newsCount=0"
        resp = requests.get(search_url, headers=YAHOO_HEADERS, impersonate="chrome120", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('quotes'): return data['quotes'][0]['symbol']
    except: pass
    return "N/A"

def get_xml_data(index_url):
    try:
        parent_url = index_url.rsplit('/', 1)[0]
        raw_xml_url = parent_url + '/primary_doc.xml'
        display_xml_url = parent_url + '/xsl144X01/primary_doc.xml'
        time.sleep(random.uniform(0.5, 0.8)) 
        resp = requests.get(raw_xml_url, headers=SEC_HEADERS, impersonate="chrome120", timeout=15)
        if resp.status_code == 200 and b"<?xml" in resp.content[:100]:
            return resp.content, display_xml_url
    except: pass
    return None, None

def check_and_parse(xml_content, display_url, pub_time_raw):
    try:
        root = ET.fromstring(xml_content)
        def get_v(tag):
            node = root.find(f".//{{*}}{tag}")
            return node.text if node is not None else ""

        if get_v("planAdoptionDate").strip(): return None
        nature = get_v("natureOfAcquisitionTransaction").lower()
        if any(kw in nature for kw in EXCLUDE_KEYWORDS): return None
        
        try:
            market_value = float(get_v("aggregateMarketValue") or 0)
        except: market_value = 0
        if market_value < 1000000: return None

        dt = datetime.fromisoformat(pub_time_raw)
        pub_time_fmt = dt.strftime("%Y-%m-%d %H:%M:%S") + " ET"
        issuer = get_v("issuerName") or "未知"
        shares = float(get_v("noOfUnitsSold") or 0)
        outstanding = float(get_v("noOfUnitsOutstanding") or 0)
        sell_percent = (shares / outstanding * 100) if outstanding > 0 else 0
        ticker = get_ticker(issuer)
        seller = get_v("nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold") or "未知"
        rel = get_v("relationshipToIssuer") or "未知"

        # 预警单条消息模板
        item_msg = (
            f"🚨 <b>重大抛售预警</b>\n"
            f"🕒 发布时间: {pub_time_fmt}\n"
            f"🏢 发行公司: ${ticker} ({issuer})\n"
            f"👤 卖家姓名: {seller} ({rel})\n"
            f"📊 拟卖股数: {shares:,.0f} 股\n"
            f"📉 抛售占比: {sell_percent:.4f}%\n"
            f"💰 拟卖总额: ${market_value:,.2f}\n"
            f"📝 取得性质: {nature.upper()}\n"
            f"🔗 <a href='{display_url}'>点击查看公告</a>"
        )
        return item_msg
    except: return None

def run():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            seen_ids = set(f.read().splitlines())
    else:
        seen_ids = set()

    current_batch_seen = set()
    hit_messages = [] 
    new_ids = []

    try:
        resp = requests.get(FEED_URL, headers=SEC_HEADERS, impersonate="chrome120", timeout=30)
        feed = feedparser.parse(resp.content)
        
        for entry in feed.entries:
            acc_id = entry.link.split('/')[-2]
            if acc_id in seen_ids or acc_id in current_batch_seen: continue
            
            current_batch_seen.add(acc_id)
            xml_data, display_url = get_xml_data(entry.link)
            
            if xml_data:
                msg_content = check_and_parse(xml_data, display_url, entry.updated)
                if msg_content:
                    hit_messages.append(msg_content)
                new_ids.append(acc_id)

        # --- 修改后的汇总编号逻辑 ---
        if hit_messages:
            # 1. 为每条信息增加编号
            numbered_messages = [f"{i}. {msg}" for i, msg in enumerate(hit_messages, 1)]
            
            separator = "\n" + "—" * 20 + "\n"
            final_body = separator.join(numbered_messages)
            
            final_message = f"{final_body}\n\n#Form4 #InsiderTrading"
            
            # 超长处理逻辑
            if len(final_message) > 4000:
                for i, single_msg in enumerate(numbered_messages, 1):
                    # 确保最后一条带 Hashtag
                    send_text = single_msg + (f"\n\n#Form4 #InsiderTrading" if i == len(numbered_messages) else "")
                    for cid in CHAT_IDS:
                        if cid.strip(): send_telegram(send_text, cid)
            else:
                # 正常合并发送
                for cid in CHAT_IDS:
                    if cid.strip(): send_telegram(final_message, cid)
            
            print(f"📊 本次运行汇总推送了 {len(hit_messages)} 条信号")

        if new_ids:
            with open(CACHE_FILE, "a") as f:
                for i in new_ids: f.write(i + "\n")
                
    except Exception as e:
        print(f"🚨 运行异常: {e}")

if __name__ == "__main__":
    run()
