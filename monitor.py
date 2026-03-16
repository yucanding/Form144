import feedparser
import time
import xml.etree.ElementTree as ET
from curl_cffi import requests
from datetime import datetime
import random
import os

# --- 配置从环境变量读取 ---
TOKEN = os.getenv("TG_BOT_TOKEN")
# 支持多个 ID，用逗号分隔，如: "123456,-100987654321"
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
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=payload, timeout=10)
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

        # 过滤
        if get_v("planAdoptionDate").strip(): return None
        nature = get_v("natureOfAcquisitionTransaction").lower()
        if any(kw in nature for kw in EXCLUDE_KEYWORDS): return None
        
        try:
            market_value = float(get_v("aggregateMarketValue") or 0)
        except: market_value = 0
        if market_value < 1000000: return None

        # 格式化
        dt = datetime.fromisoformat(pub_time_raw)
        pub_time_fmt = dt.strftime("%Y-%m-%d %H:%M:%S") + " ET"
        issuer = get_v("issuerName") or "未知"
        shares = float(get_v("noOfUnitsSold") or 0)
        outstanding = float(get_v("noOfUnitsOutstanding") or 0)
        sell_percent = (shares / outstanding * 100) if outstanding > 0 else 0
        ticker = get_ticker(issuer)
        seller = get_v("nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold") or "未知"
        rel = get_v("relationshipToIssuer") or "未知"

        # 消息模板
        msg = (
            f"🚨 <b>重大抛售预警</b>\n"
            f"🕒 发布时间: {pub_time_fmt}\n"
            f"🏢 发行公司: <b>${ticker}</b> ({issuer})\n"
            f"👤 卖家姓名: {seller} ({rel})\n"
            f"📊 拟卖股数: {shares:,.0f} 股\n"
            f"📉 抛售占比: <b>{sell_percent:.4f}%</b>\n"
            f"💰 拟卖总额: <b>${market_value:,.2f}</b>\n"
            f"📝 取得性质: {nature.upper()}\n"
            f"🔗 <a href='{display_url}'>点击查看公告</a>"
        )
        return msg
    except: return None

def run():
    # 1. 加载缓存
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            seen_ids = set(f.read().splitlines())
    else:
        seen_ids = set()

    # 2. 建立一个“当前运行中”的临时记录，防止同一次运行内重复
    current_batch_seen = set()

    try:
        resp = requests.get(FEED_URL, headers=SEC_HEADERS, impersonate="chrome120", timeout=30)
        feed = feedparser.parse(resp.content)
        new_ids = []
        
        for entry in feed.entries:
            acc_id = entry.link.split('/')[-2]
            
            # --- 核心修复：检查持久化缓存 OR 当前批次缓存 ---
            if acc_id in seen_ids or acc_id in current_batch_seen:
                continue
            
            # 立即标记为已看到，防止同一批次里的 Subject/Reporting 重复触发
            current_batch_seen.add(acc_id)
            
            xml_data, display_url = get_xml_data(entry.link)
            if xml_data:
                msg = check_and_parse(xml_data, display_url, entry.updated)
                if msg:
                    # 推送
                    for cid in CHAT_IDS:
                        if cid.strip(): send_telegram(msg, cid)
                    print(f"✅ 已推送: {acc_id}")
                    
                    # 只有真正符合过滤条件并推送到 TG 的，才记入 new_ids
                    # 如果你希望所有处理过的（无论是否符合金额条件）都以后不再处理，
                    # 这一行应该放在 if xml_data 之后
                    new_ids.append(acc_id)

        # 3. 更新持久化缓存文件
        if new_ids:
            with open(CACHE_FILE, "a") as f:
                for i in new_ids:
                    f.write(i + "\n")
            print(f"📊 本次新增 {len(new_ids)} 条记录到缓存")
                
    except Exception as e:
        print(f"🚨 运行异常: {e}")

if __name__ == "__main__":
    run()
