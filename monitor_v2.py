#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超级鹿鼎公 调仓监控脚本 v2.0
====================================
监控雪球网 + 微博帖子，检测到调仓信号时通过 钉钉/邮件 推送通知

部署方式:
  1. 本地/服务器: 设置cron定时运行 (推荐每30分钟一次)
  2. GitHub Actions: 使用 .github/workflows/monitor.yml (免费)
  3. 聚宽模拟交易策略: 使用run_daily/run_interval调度 (需VIP/积分)

环境变量配置 (选配，配了哪个就用哪个):

  企业微信推送 (推荐，免费无限制):
    WEWORK_WEBHOOK    - 企业微信群机器人Webhook URL

  钉钉推送 (备选):
    DINGTALK_WEBHOOK  - 钉钉群机器人Webhook URL
    DINGTALK_SECRET   - 钉钉机器人加签密钥 (可选)

  邮件推送 (备选):
    SMTP_HOST         - SMTP服务器 (默认: smtp.qq.com)
    SMTP_PORT         - SMTP端口 (默认: 465)
    SMTP_USER         - 发件邮箱
    SMTP_PASSWORD     - 邮箱授权码
    NOTIFY_EMAIL      - 收件邮箱 (默认同发件邮箱)

  微博配置:
    WEIBO_USER_ID     - 微博用户ID (默认自动搜索获取)
"""

import os
import sys
import re
import json
import time
import hmac
import hashlib
import base64
import urllib.parse
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ==================== 配置 ====================

# 监控对象
XUEQIU_USER_ID = "8790885129"
XUEQIU_NAME = "超级鹿鼎公"
WEIBO_NAME = "挖地瓜的超级鹿鼎公"
WEIBO_USER_ID = os.environ.get("WEIBO_USER_ID", "")

# ==================== 关键词配置 ====================

BUY_KEYWORDS_HIGH = ["加仓", "买入", "新建仓", "开仓", "建仓", "增持", "补仓"]
BUY_KEYWORDS_NORMAL = ["捞", "捡", "买了", "买点", "买了一点", "买了些", "入手"]
BUY_KEYWORDS_EN = ["add", "buy"]

SELL_KEYWORDS_HIGH = ["减仓", "卖出", "清仓", "平仓", "减持", "兑现", "止盈", "止损"]
SELL_KEYWORDS_NORMAL = ["撤", "走了", "出了", "卖掉", "减持了", "卖单", "出完了"]
SELL_KEYWORDS_EN = ["sell", "clear"]

SWAP_KEYWORDS = ["换到", "换成", "换仓", "置换", "换内蒙", "换中海"]

ALL_BUY_KEYWORDS = BUY_KEYWORDS_HIGH + BUY_KEYWORDS_NORMAL + BUY_KEYWORDS_EN
ALL_SELL_KEYWORDS = SELL_KEYWORDS_HIGH + SELL_KEYWORDS_NORMAL + SELL_KEYWORDS_EN

CONFIRM_KEYWORDS = [
    "股", "万", "手", "仓位", "持仓", "比例", "PS图",
    "长江电力", "云铝", "神火", "中煤", "陕西煤业", "华能",
    "内蒙华电", "新城发展", "陕西能源", "中海油", "紫金",
    "游戏仓", "主仓", "中孚实业", "海狗", "中远海控", "小商品城",
    "国投电力", "淮北矿业", "广发证券", "腾讯控股", "宝丰能源",
    "长电", "猛电", "蒙电", "门窗",
]

EXCLUDE_KEYWORDS = [
    "祝大家", "新年快乐", "恭喜发财", "周末愉快", "早安", "晚安",
    "点赞", "转发", "评论", "抽奖", "红包", "广告", "推广",
]

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://xueqiu.com/",
    "Cookie": os.environ.get("XUEQIU_COOKIE", ""),
}

TZ = timezone(timedelta(hours=8))


# ==================== 工具函数 ====================

def log(msg, level="INFO"):
    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"加载状态文件失败: {e}", "WARN")
    return {"processed_ids": [], "last_check": None}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"保存状态文件失败: {e}", "WARN")


def fetch_json(url, timeout=15):
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"请求失败 {url}: {e}", "ERROR")
        return None


def fetch_page(url, timeout=15):
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        log(f"页面请求失败: {e}", "ERROR")
        return None


# ==================== 调仓检测 (与验证版本完全一致) ====================

def detect_trade_signal(text, title=""):
    if not text:
        return False, None, 0, []

    full_text = title + " " + text

    exclude_count = sum(1 for ex in EXCLUDE_KEYWORDS if ex in full_text)
    if exclude_count >= 3 and len(full_text) < 100:
        return False, None, 0, []

    year_matches = set(re.findall(r'(1[4-9]|2[0-6])\s*年', full_text))
    if len(year_matches) >= 5:
        return False, None, 0, []

    they_count = full_text.count("他们")
    if "为什么" in title and ("卖" in title or "买" in title) and they_count >= 2:
        return False, None, 0, []

    fundamental_words = ["管理层", "分红", "负债", "产销量", "利润", "烂起来", "果然烂"]
    fundamental_count = sum(1 for w in fundamental_words if w in full_text)
    personal_words = ["游戏仓", "我买", "我卖", "我清", "我减", "我加", "我的仓位"]
    personal_count = sum(1 for w in personal_words if w in full_text)
    if fundamental_count >= 2 and personal_count == 0 and len(full_text) < 200:
        return False, None, 0, []

    matched = []
    signal_type = None
    confidence = 0
    has_buy = False
    has_sell = False

    for k in BUY_KEYWORDS_HIGH:
        if k in full_text:
            matched.append(k); has_buy = True; confidence += 3
    for k in BUY_KEYWORDS_NORMAL:
        if k in full_text:
            matched.append(k); has_buy = True; confidence += 2
    for k in BUY_KEYWORDS_EN:
        if k in full_text.lower():
            matched.append(k); has_buy = True; confidence += 2
    for k in SELL_KEYWORDS_HIGH:
        if k in full_text:
            matched.append(k); has_sell = True; confidence += 3
    for k in SELL_KEYWORDS_NORMAL:
        if k in full_text:
            matched.append(k); has_sell = True; confidence += 2
    for k in SELL_KEYWORDS_EN:
        if k in full_text.lower():
            matched.append(k); has_sell = True; confidence += 2
    for k in SWAP_KEYWORDS:
        if k in full_text:
            matched.append(k); has_buy = True; has_sell = True; confidence += 3

    if has_buy and has_sell:
        signal_type = "MIX"
    elif has_buy:
        signal_type = "BUY"
    elif has_sell:
        signal_type = "SELL"
    else:
        return False, None, 0, []

    confirm_count = sum(1 for k in CONFIRM_KEYWORDS if k in full_text)
    confidence += min(confirm_count, 5)

    if re.search(r'(加仓|减仓|买入|卖出|清仓|新建仓|开仓|减持)\s*[\u4e00-\u9fa5A-Za-z]+\s*\d+', full_text):
        confidence += 5
    if re.search(r'\d+\s*(股|手|万)', full_text):
        confidence += 3
    if re.search(r'全部\s*(换到|换成|换为)', full_text):
        confidence += 4
    if re.search(r'新开仓了\s*[\u4e00-\u9fa5A-Za-z]+', full_text):
        confidence += 3

    strategy_words = ["考虑", "计划", "未来", "策略", "准备"]
    strategy_count = sum(1 for w in strategy_words if w in full_text)
    if strategy_count >= 1 and confidence >= 2:
        confidence += 2

    return confidence >= 4, signal_type, confidence, list(set(matched))


def classify_signal_type(signal_type):
    return {"BUY": "买入/加仓", "SELL": "卖出/减仓", "MIX": "调仓(买卖均有)"}.get(signal_type, "未知")


# ==================== 雪球数据获取 ====================

def fetch_xueqiu_posts(user_id, count=10):
    """获取雪球用户最新帖子（API优先，HTML备选）"""
    url = f"https://xueqiu.com/v4/statuses/user_timeline.json?page=1&user_id={user_id}"
    data = fetch_json(url)

    if data and "statuses" in data:
        posts = []
        for item in data.get("statuses", [])[:count]:
            posts.append({
                "id": str(item.get("id", "")),
                "title": item.get("title", ""),
                "content": item.get("text", ""),
                "created_at": item.get("created_at", 0),
                "source": "雪球",
                "url": f"https://xueqiu.com/{user_id}/{item.get('id', '')}",
            })
        return posts

    return fetch_xueqiu_html(user_id, count)


def fetch_xueqiu_html(user_id, count=10):
    """备选：从雪球专栏页面解析"""
    url = f"https://xueqiu.com/{user_id}/column"
    html = fetch_page(url)
    if not html:
        return []

    posts = []
    pattern = re.compile(r'href="/(\d+)/(\d+)"[^>]*>([^<]{5,80})</a>')

    seen = set()
    for m in pattern.finditer(html):
        uid, aid, text = m.groups()
        if uid != user_id:
            continue
        if aid in seen:
            continue
        seen.add(aid)
        posts.append({
            "id": aid,
            "title": text.strip(),
            "content": "",
            "created_at": 0,
            "source": "雪球",
            "url": f"https://xueqiu.com/{user_id}/{aid}",
        })
        if len(posts) >= count:
            break

    return posts


# ==================== 微博数据获取 ====================

def fetch_weibo_posts(count=10):
    """获取微博最新帖子（通过微博移动版API）"""
    global WEIBO_USER_ID

    if not WEIBO_USER_ID:
        # 通过搜索获取用户ID
        search_url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={WEIBO_NAME}&containerid=100103type%3D1%26q%3D{urllib.parse.quote(WEIBO_NAME)}"
        data = fetch_json(search_url)
        if data and "data" in data and "cards" in data["data"]:
            for card in data["data"]["cards"]:
                if "mblog" in card:
                    WEIBO_USER_ID = str(card["mblog"].get("user", {}).get("id", ""))
                    if WEIBO_USER_ID:
                        log(f"获取到微博用户ID: {WEIBO_USER_ID}")
                        break

    if not WEIBO_USER_ID:
        log("未获取到微博用户ID，跳过微博监控", "WARN")
        return []

    url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={WEIBO_USER_ID}"
    data = fetch_json(url)

    if not data or "data" not in data:
        log(f"微博API请求失败", "WARN")
        return []

    posts = []
    cards = data.get("data", {}).get("cards", [])
    for card in cards:
        if "mblog" not in card:
            continue
        mblog = card["mblog"]
        mid = str(mblog.get("mid", mblog.get("id", "")))
        text = mblog.get("text", "")
        created_at = mblog.get("created_at", "")

        # 清理HTML标签和微博表情
        text = re.sub(r'<[^>]+>', '', text)
        text = text.replace('\n', ' ').strip()

        posts.append({
            "id": mid,
            "title": "",
            "content": text,
            "created_at": 0,
            "source": "微博",
            "url": f"https://m.weibo.cn/detail/{mid}",
        })
        if len(posts) >= count:
            break

    return posts


# ==================== 推送通知 ====================

def send_dingtalk(title, content, is_buy=False, is_sell=False):
    """发送钉钉机器人通知"""
    webhook = os.environ.get("DINGTALK_WEBHOOK", "")
    if not webhook:
        return False

    secret = os.environ.get("DINGTALK_SECRET", "")

    # 标记
    if is_buy:
        mark = "🟢 买入"
    elif is_sell:
        mark = "🔴 卖出"
    else:
        mark = "🟡 调仓"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"## {mark} {title}\n\n{content}\n\n"
                   f"---\n> 时间: {datetime.now(TZ).strftime('%H:%M:%S')}\n"
                   f"> 来源: 鹿鼎公调仓监控 v2.0"
        },
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # 加签
    if secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        webhook = f"{webhook}&timestamp={timestamp}&sign={sign}"

    req = Request(webhook, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("errcode") == 0:
                log("钉钉推送成功", "INFO")
                return True
            else:
                log(f"钉钉推送失败: {result}", "ERROR")
                return False
    except Exception as e:
        log(f"钉钉推送异常: {e}", "ERROR")
        return False


def send_wework(title, content, is_buy=False, is_sell=False):
    """发送企业微信机器人通知（纯文字，微信可直接查看）"""
    webhook = os.environ.get("WEWORK_WEBHOOK", "")
    if not webhook:
        return False

    # 标记
    if is_buy:
        mark = "【买入】"
    elif is_sell:
        mark = "【卖出】"
    else:
        mark = "【调仓】"

    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    full_text = (
        f"{mark} {title}\n"
        f"{'=' * 30}\n"
        f"{content}\n"
        f"{'=' * 30}\n"
        f"时间: {ts}\n"
        f"来源: 鹿鼎公调仓监控 v2.0"
    )

    payload = {
        "msgtype": "text",
        "text": {"content": full_text},
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(webhook, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("errcode") == 0:
                log("企业微信推送成功", "INFO")
                return True
            else:
                log(f"企业微信推送失败: {result}", "ERROR")
                return False
    except Exception as e:
        log(f"企业微信推送异常: {e}", "ERROR")
        return False


def send_email(subject, body_html):
    """发送邮件通知"""
    smtp_host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    notify_email = os.environ.get("NOTIFY_EMAIL", smtp_user)

    if not smtp_user or not smtp_password:
        log("SMTP未配置，跳过邮件发送", "WARN")
        return False

    msg = MIMEText(body_html, "html", "utf-8")
    msg["From"] = Header(f"鹿鼎公监控 <{smtp_user}>", "utf-8")
    msg["To"] = Header(notify_email, "utf-8")
    msg["Subject"] = Header(subject, "utf-8")

    try:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [notify_email], msg.as_string())
        server.quit()
        log(f"邮件推送成功 -> {notify_email}", "INFO")
        return True
    except Exception as e:
        log(f"邮件推送失败: {e}", "ERROR")
        return False


def notify_signal(post, signal_type, confidence, matched_keywords):
    """统一推送通知（企业微信 + 钉钉 + 邮件）"""
    signal_label = classify_signal_type(signal_type)
    is_buy = signal_type == "BUY"
    is_sell = signal_type == "SELL"

    # 清理内容
    content = post.get("content", "")
    content = re.sub(r'<[^>]+>', '', content)
    content = content[:300] + ("..." if len(content) > 300 else "")

    title_text = post.get("title", "新帖")[:30] if post.get("title") else "微博动态"
    source = post.get("source", "未知")
    url = post.get("url", "")
    keywords_str = "、".join(matched_keywords[:8])

    # === 企业微信（主要推送） ===
    wework_title = f"【鹿鼎公{signal_label}】{title_text}"
    wework_content = (
        f"来源: {source} | 置信度: {confidence}\n\n"
        f"内容: {content}\n\n"
        f"命中关键词: {keywords_str}\n"
    )
    if url:
        wework_content += f"原文: {url}\n"
    send_wework(wework_title, wework_content, is_buy, is_sell)

    # === 钉钉（备选推送） ===
    ding_title = f"【鹿鼎公{signal_label}】{title_text}"
    ding_content = (
        f"**来源:** {source} | **置信度:** {confidence}\n\n"
        f"{content}\n\n"
        f"**命中关键词:** {keywords_str}\n"
    )
    if url:
        ding_content += f"[查看原文]({url})\n"
    send_dingtalk(ding_title, ding_content, is_buy, is_sell)

    # === 邮件（备选推送） ===
    email_subject = f"【鹿鼎公{signal_label}】{title_text} (置信度{confidence})"
    email_body = f"""
    <html><body style="font-family:Microsoft YaHei,sans-serif;line-height:1.6;color:#333;max-width:600px;margin:0 auto">
    <div style="background:{'#1e6f5c' if is_buy else '#c0392b' if is_sell else '#e65100'};color:#fff;padding:15px;text-align:center;border-radius:8px 8px 0 0">
      <h2 style="margin:0">🔔 超级鹿鼎公 调仓信号</h2>
    </div>
    <div style="padding:20px;background:#f9f9f9">
      <p><strong>信号类型:</strong> {signal_label}</p>
      <p><strong>置信度:</strong> {confidence}</p>
      <p><strong>来源:</strong> {source}</p>
      {"<p><strong>标题:</strong> " + title_text + "</p>" if post.get("title") else ""}
      <div style="background:#fff;padding:15px;border-left:4px solid #1e6f5c;margin:15px 0">{content}</div>
      <p><strong>命中关键词:</strong> {keywords_str}</p>
      {f'<p><a href="{url}" style="color:#1e6f5c">查看原文</a></p>' if url else ''}
    </div>
    <div style="text-align:center;padding:15px;color:#999;font-size:12px">
      鹿鼎公调仓监控 v2.0 | {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}
    </div>
    </body></html>
    """
    send_email(email_subject, email_body)


# ==================== 主流程 ====================

def check_source(fetch_func, source_name, processed_ids):
    """检查单个数据源，返回 (信号列表, 所有帖子ID列表)"""
    try:
        posts = fetch_func(count=10)
    except Exception as e:
        log(f"{source_name}数据获取异常: {e}", "ERROR")
        return [], []

    if not posts:
        log(f"{source_name}: 未获取到帖子", "WARN")
        return [], []

    log(f"{source_name}: 获取到 {len(posts)} 条帖子")
    signals = []
    all_ids = []

    for post in posts:
        post_id = post.get("id", "")
        if not post_id:
            continue
        all_ids.append(post_id)

        # 跳过已处理的帖子
        if post_id in processed_ids:
            log(f"   [{source_name}] 帖子{post_id} 已处理，跳过")
            continue

        title = post.get("title", "")
        content = post.get("content", "")

        is_signal, signal_type, confidence, matched = detect_trade_signal(content, title)

        if is_signal:
            log(f"🚨 [{source_name}] 检测到{classify_signal_type(signal_type)}信号! "
                f"置信度:{confidence} 命中:{matched}", "ALERT")
            signals.append({
                "post": post,
                "signal_type": signal_type,
                "confidence": confidence,
                "matched": matched,
            })
        else:
            log(f"   [{source_name}] 帖子{post_id} 无信号 (置信度:{confidence})")

    return signals, all_ids


def main():
    log("=" * 60)
    log("超级鹿鼎公 调仓监控 v2.0 启动")
    log("=" * 60)

    state = load_state()
    processed_ids = set(state.get("processed_ids", []))
    log(f"已处理记录数: {len(processed_ids)}")

    # 检查雪球
    log("--- 检查雪球 ---")
    xueqiu_signals, xueqiu_ids = check_source(
        lambda count=10: fetch_xueqiu_posts(XUEQIU_USER_ID, count),
        "雪球",
        processed_ids,
    )

    # 检查微博
    log("--- 检查微博 ---")
    weibo_signals, weibo_ids = check_source(
        lambda count=10: fetch_weibo_posts(count),
        "微博",
        processed_ids,
    )

    all_signals = xueqiu_signals + weibo_signals

    # 发送通知
    if all_signals:
        log(f"共检测到 {len(all_signals)} 条调仓信号!", "ALERT")

        for sig in all_signals:
            notify_signal(sig["post"], sig["signal_type"], sig["confidence"], sig["matched"])
            time.sleep(2)

        log("所有通知已发送", "ALERT")
    else:
        log("本次检查未发现新的调仓信号")

    # 保存状态（标记所有已扫描帖子为已处理）
    for pid in xueqiu_ids + weibo_ids:
        processed_ids.add(pid)
    state["processed_ids"] = list(processed_ids)[-1000:]
    state["last_check"] = datetime.now(TZ).isoformat()
    state["total_signals"] = state.get("total_signals", 0) + len(all_signals)
    save_state(state)

    log(f"累计检测信号数: {state.get('total_signals', 0)}")
    log("监控完成")
    log("=" * 60)


if __name__ == "__main__":
    main()
