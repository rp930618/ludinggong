#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超级鹿鼎公 调仓监控脚本
监控雪球网/微博帖子，检测到调仓关键词时发送邮件通知

使用方式:
  python monitor.py

环境变量配置:
  SMTP_HOST       - SMTP服务器 (默认: smtp.qq.com)
  SMTP_PORT       - SMTP端口 (默认: 465)
  SMTP_USER       - 发件邮箱
  SMTP_PASSWORD   - 邮箱授权码/密码
  NOTIFY_EMAIL    - 收件邮箱
  SENDER_NAME     - 发件人名称 (默认: 鹿鼎公监控)
"""

import os
import sys
import re
import json
import time
import hashlib
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ==================== 配置 ====================

# 监控对象
XUEQIU_USER_ID = "8790885129"  # 超级鹿鼎公 雪球ID
XUEQIU_NAME = "超级鹿鼎公"

# ==================== 关键词配置 ====================
# 权重说明: 高权重词(3分), 普通词(2分)

# 买入关键词
BUY_KEYWORDS_HIGH = ["加仓", "买入", "新建仓", "开仓", "建仓", "增持", "补仓"]
BUY_KEYWORDS_NORMAL = ["捞", "捡", "买了", "买点", "买了一点", "买了些", "入手"]
BUY_KEYWORDS_EN = ["add", "buy"]

# 卖出关键词
SELL_KEYWORDS_HIGH = ["减仓", "卖出", "清仓", "平仓", "减持", "兑现", "止盈", "止损"]
SELL_KEYWORDS_NORMAL = ["撤", "走了", "出了", "卖掉", "减持了", "卖单", "出完了"]
SELL_KEYWORDS_EN = ["sell", "clear"]

# 换仓关键词 (同时触发买入+卖出)
SWAP_KEYWORDS = ["换到", "换成", "换仓", "置换", "换内蒙", "换中海"]

# 所有买入关键词
ALL_BUY_KEYWORDS = BUY_KEYWORDS_HIGH + BUY_KEYWORDS_NORMAL + BUY_KEYWORDS_EN
# 所有卖出关键词
ALL_SELL_KEYWORDS = SELL_KEYWORDS_HIGH + SELL_KEYWORDS_NORMAL + SELL_KEYWORDS_EN

# 辅助确认词 (提高准确率)
CONFIRM_KEYWORDS = [
    "股", "万", "手", "仓位", "持仓", "比例", "PS图",
    "长江电力", "云铝", "神火", "中煤", "陕西煤业", "华能",
    "内蒙华电", "新城发展", "陕西能源", "中海油", "紫金",
    "游戏仓", "主仓", "中孚实业", "海狗", "中远海控", "小商品城",
    "国投电力", "淮北矿业", "广发证券", "腾讯控股", "宝丰能源",
]

# 排除词 (过滤掉非调仓内容)
EXCLUDE_KEYWORDS = [
    "祝大家", "新年快乐", "恭喜发财", "周末愉快", "早安", "晚安",
    "点赞", "转发", "评论", "抽奖", "红包", "广告", "推广",
]

# 状态文件路径
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://xueqiu.com/",
}

# 时区
TZ = timezone(timedelta(hours=8))


# ==================== 工具函数 ====================

def log(msg, level="INFO"):
    """打印日志"""
    timestamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}", flush=True)


def load_state():
    """加载已处理的状态"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"加载状态文件失败: {e}", "WARN")
    return {"processed_ids": [], "last_check": None}


def save_state(state):
    """保存状态"""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"保存状态文件失败: {e}", "WARN")


def fetch_json(url, timeout=15):
    """获取JSON数据"""
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except HTTPError as e:
        log(f"HTTP错误 {e.code}: {e.reason}", "ERROR")
        return None
    except URLError as e:
        log(f"URL错误: {e.reason}", "ERROR")
        return None
    except json.JSONDecodeError as e:
        log(f"JSON解析错误: {e}", "ERROR")
        return None
    except Exception as e:
        log(f"请求失败: {e}", "ERROR")
        return None


def fetch_page(url, timeout=15):
    """获取页面HTML"""
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        log(f"页面请求失败: {e}", "ERROR")
        return None


# ==================== 调仓检测 ====================

def detect_trade_signal(text, title=""):
    """
    检测文本中是否包含调仓信号
    返回: (is_signal, signal_type, confidence, matched_keywords)

    权重规则:
    - 高权重买入/卖出词: 3分
    - 普通买入/卖出词: 2分
    - 换仓词: 同时触发买入+卖出，各3分
    - 辅助确认词: 1分
    - 股票名+数字组合模式: +5分
    - 股数单位(万股/手): +3分

    置信度阈值: >=4 才触发信号
    """
    if not text:
        return False, None, 0, []

    full_text = title + " " + text

    # 排除非调仓内容 (如果全是排除词，直接返回)
    exclude_count = sum(1 for ex in EXCLUDE_KEYWORDS if ex in full_text)
    if exclude_count >= 3 and len(full_text) < 100:
        return False, None, 0, []

    # ===== 排除模式1：历史回顾/年度总结 =====
    # 检测文本中是否包含多个不同年份（如14年、15年...25年）
    # 年度总结/历史回顾会包含大量往年操作描述，导致历史调仓词被误匹配
    year_matches = set(re.findall(r'(1[4-9]|2[0-6])\s*年', full_text))
    if len(year_matches) >= 5:
        return False, None, 0, []

    # ===== 排除模式2：市场评论（讨论"他们"的行为） =====
    # 如"为什么'他们'要卖这些股票"——讨论国家队/市场操作，非个人调仓
    they_count = full_text.count("他们")
    if "为什么" in title and ("卖" in title or "买" in title) and they_count >= 2:
        return False, None, 0, []

    # ===== 排除模式3：公司基本面吐槽（非本人操作） =====
    # 如"陕煤换了管理层""张尧退出十大股东（减持至少2100万股）"
    # 特征：包含"管理层""分红""负债""产销量"等公司基本面词，且无"游戏仓""我"等个人操作词
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

    # 检测高权重买入信号
    for k in BUY_KEYWORDS_HIGH:
        if k in full_text:
            matched.append(k)
            has_buy = True
            confidence += 3

    # 检测普通买入信号
    for k in BUY_KEYWORDS_NORMAL:
        if k in full_text:
            matched.append(k)
            has_buy = True
            confidence += 2

    # 检测英文买入信号
    for k in BUY_KEYWORDS_EN:
        if k in full_text.lower():
            matched.append(k)
            has_buy = True
            confidence += 2

    # 检测高权重卖出信号
    for k in SELL_KEYWORDS_HIGH:
        if k in full_text:
            matched.append(k)
            has_sell = True
            confidence += 3

    # 检测普通卖出信号
    for k in SELL_KEYWORDS_NORMAL:
        if k in full_text:
            matched.append(k)
            has_sell = True
            confidence += 2

    # 检测英文卖出信号
    for k in SELL_KEYWORDS_EN:
        if k in full_text.lower():
            matched.append(k)
            has_sell = True
            confidence += 2

    # 检测换仓信号 (同时算买入+卖出)
    for k in SWAP_KEYWORDS:
        if k in full_text:
            matched.append(k)
            has_buy = True
            has_sell = True
            confidence += 3

    # 确定信号类型
    if has_buy and has_sell:
        signal_type = "MIX"
    elif has_buy:
        signal_type = "BUY"
    elif has_sell:
        signal_type = "SELL"
    else:
        return False, None, 0, []

    # 检测辅助确认词 (提高可信度)
    confirm_count = 0
    for k in CONFIRM_KEYWORDS:
        if k in full_text:
            confirm_count += 1
    confidence += min(confirm_count, 5)  # 最多加5分

    # 检测具体股票名 + 数字组合 (如 "加仓云铝 1.5万股")
    stock_pattern = re.compile(
        r'(加仓|减仓|买入|卖出|清仓|新建仓|开仓|减持)\s*[\u4e00-\u9fa5A-Za-z]+\s*\d+'
    )
    if stock_pattern.search(full_text):
        confidence += 5

    # 检测"股数"相关
    if re.search(r'\d+\s*(股|手|万)', full_text):
        confidence += 3

    # 检测"全部换到"这种明确的换仓表达
    if re.search(r'全部\s*(换到|换成|换为)', full_text):
        confidence += 4

    # 检测"新开仓了+股票名"模式
    if re.search(r'新开仓了\s*[\u4e00-\u9fa5A-Za-z]+', full_text):
        confidence += 3

    # 检测"策略声明"模式 (如"考虑减仓""高位止盈""高了卖低了买")
    strategy_words = ["考虑", "计划", "未来", "策略", "准备"]
    strategy_count = sum(1 for w in strategy_words if w in full_text)
    if strategy_count >= 1 and confidence >= 2:
        confidence += 2

    # 置信度阈值 (需要>=4)
    is_signal = confidence >= 4

    return is_signal, signal_type, confidence, list(set(matched))


def classify_signal_type(signal_type):
    """分类信号类型为中文"""
    mapping = {
        "BUY": "买入/加仓",
        "SELL": "卖出/减仓",
        "MIX": "调仓(买卖均有)",
    }
    return mapping.get(signal_type, "未知")


# ==================== 雪球监控 ====================

def fetch_xueqiu_posts(user_id, count=10):
    """
    获取雪球用户最新帖子
    雪球API: https://xueqiu.com/v4/statuses/user_timeline.json?page=1&user_id=xxx
    """
    url = f"https://xueqiu.com/v4/statuses/user_timeline.json?page=1&user_id={user_id}"
    data = fetch_json(url)

    if not data or "statuses" not in data:
        # 备选: 用雪球个人主页HTML解析
        return fetch_xueqiu_posts_fallback(user_id, count)

    posts = []
    for item in data.get("statuses", [])[:count]:
        post = {
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "content": item.get("text", ""),
            "description": item.get("description", ""),
            "created_at": item.get("created_at", 0),
            "source": "雪球",
            "url": f"https://xueqiu.com/{user_id}/{item.get('id', '')}",
        }
        posts.append(post)

    return posts


def fetch_xueqiu_posts_fallback(user_id, count=10):
    """
    备选方案: 从雪球个人主页HTML解析最新帖子
    """
    url = f"https://xueqiu.com/u/{user_id}"
    html = fetch_page(url)

    if not html:
        return []

    posts = []

    # 方法1: 从页面中的JSON数据提取
    # 雪球页面会嵌入用户的文章数据在script标签中
    import re

    # 尝试提取文章列表JSON
    article_pattern = re.compile(
        r'"id":(\d+),"title":"([^"]*)".*?"description":"([^"]*)".*?"created_at":(\d+)',
        re.DOTALL
    )

    matches = article_pattern.findall(html)
    seen_ids = set()

    for match in matches[:count]:
        article_id, title, desc, created_at = match
        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)

        # 清理转义字符
        title = title.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"')
        desc = desc.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"')

        posts.append({
            "id": article_id,
            "title": title,
            "content": desc,
            "description": desc,
            "created_at": int(created_at) // 1000 if len(created_at) > 10 else int(created_at),
            "source": "雪球",
            "url": f"https://xueqiu.com/{user_id}/{article_id}",
        })

    # 方法2: 如果上面没匹配到，尝试提取内容中的文章链接
    if not posts:
        link_pattern = re.compile(
            r'href="/\d+/\d+"[^>]*>([^<]+)</a>.*?<div[^>]*>(.*?)</div>',
            re.DOTALL
        )
        # 简化处理，提取最近的几条
        for m in re.finditer(r'article_time[^>]*>([^<]+)</span>', html):
            pass  # 占位

    return posts


# ==================== 邮件发送 ====================

def send_email(subject, body_html):
    """
    发送邮件通知
    """
    smtp_host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    notify_email = os.environ.get("NOTIFY_EMAIL", smtp_user)
    sender_name = os.environ.get("SENDER_NAME", "鹿鼎公监控")

    if not smtp_user or not smtp_password:
        log("SMTP未配置，跳过邮件发送", "WARN")
        log(f"邮件内容:\n标题: {subject}\n{body_html[:500]}...", "INFO")
        return False

    if not notify_email:
        notify_email = smtp_user

    msg = MIMEText(body_html, "html", "utf-8")
    msg["From"] = Header(f"{sender_name} <{smtp_user}>", "utf-8")
    msg["To"] = Header(notify_email, "utf-8")
    msg["Subject"] = Header(subject, "utf-8")

    try:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [notify_email], msg.as_string())
        server.quit()
        log(f"邮件已发送至 {notify_email}", "INFO")
        return True
    except Exception as e:
        log(f"邮件发送失败: {e}", "ERROR")
        return False


def build_email_content(post, signal_type, confidence, matched_keywords):
    """构建邮件HTML内容"""
    signal_label = classify_signal_type(signal_type)
    created_time = datetime.fromtimestamp(post.get("created_at", 0), TZ).strftime("%Y-%m-%d %H:%M:%S")

    # 清理HTML标签
    content = post.get("content", "") or post.get("description", "")
    content = re.sub(r'<[^>]+>', '', content)
    content = content.replace('\n', '<br/>')

    title = post.get("title", "")
    if title:
        title = re.sub(r'<[^>]+>', '', title)

    keywords_str = "、".join(matched_keywords[:10])

    html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: 'Microsoft YaHei', sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; }}
            .header {{ background: #1e6f5c; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
            .header h2 {{ margin: 0; font-size: 18px; }}
            .badge {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: bold; margin-left: 10px; }}
            .badge-buy {{ background: #e8f5e9; color: #1e6f5c; }}
            .badge-sell {{ background: #ffebee; color: #c0392b; }}
            .badge-mix {{ background: #fff3e0; color: #e65100; }}
            .content {{ padding: 20px; background: #f9f9f9; }}
            .meta {{ color: #666; font-size: 12px; margin-bottom: 15px; }}
            .post-content {{ background: white; padding: 15px; border-radius: 6px; border-left: 4px solid #1e6f5c; margin: 15px 0; }}
            .keywords {{ background: #f0f0f0; padding: 10px; border-radius: 4px; font-size: 12px; margin-top: 15px; }}
            .footer {{ text-align: center; padding: 15px; color: #999; font-size: 12px; }}
            .btn {{ display: inline-block; padding: 10px 20px; background: #1e6f5c; color: white; text-decoration: none; border-radius: 4px; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h2>🔔 超级鹿鼎公 调仓信号提醒</h2>
        </div>
        <div class="content">
            <p>
                <strong>信号类型:</strong>
                <span class="badge badge-{signal_type.lower()}">{signal_label}</span>
            </p>
            <p><strong>置信度:</strong> {confidence}/20</p>
            <div class="meta">
                来源: {post.get('source', '未知')} | 时间: {created_time}<br/>
                <a href="{post.get('url', '#')}" class="btn">查看原文</a>
            </div>
            {"<h3>" + title + "</h3>" if title else ""}
            <div class="post-content">
                {content[:800]}{"..." if len(content) > 800 else ""}
            </div>
            <div class="keywords">
                <strong>命中关键词:</strong> {keywords_str}
            </div>
        </div>
        <div class="footer">
            鹿鼎公调仓监控系统 | {datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")}
        </div>
    </body>
    </html>
    """
    return html


# ==================== 主流程 ====================

def main():
    log("=" * 50)
    log("超级鹿鼎公 调仓监控启动")
    log("=" * 50)

    # 加载状态
    state = load_state()
    processed_ids = set(state.get("processed_ids", []))

    log(f"已处理记录数: {len(processed_ids)}")

    # 获取最新帖子
    log(f"正在获取 {XUEQIU_NAME} 的最新帖子...")
    posts = fetch_xueqiu_posts(XUEQIU_USER_ID, count=15)

    if not posts:
        log("未获取到任何帖子", "WARN")
        sys.exit(1)

    log(f"获取到 {len(posts)} 条帖子")

    new_signals = []

    for post in posts:
        post_id = post.get("id", "")
        if not post_id:
            continue

        # 跳过已处理的
        if post_id in processed_ids:
            continue

        title = post.get("title", "")
        content = post.get("content", "") or post.get("description", "")
        full_text = title + " " + content

        # 检测调仓信号
        is_signal, signal_type, confidence, matched = detect_trade_signal(content, title)

        log(f"帖子 [{post_id}] [{post.get('source')}] 置信度: {confidence}")

        if is_signal:
            log(f"🚨 检测到调仓信号! 类型: {classify_signal_type(signal_type)}, 置信度: {confidence}", "ALERT")
            log(f"   命中词: {matched}", "ALERT")
            log(f"   标题: {title[:60]}...", "ALERT")

            new_signals.append({
                "post": post,
                "signal_type": signal_type,
                "confidence": confidence,
                "matched": matched,
            })
        else:
            log(f"   无调仓信号 (置信度{confidence}不足)")

        # 标记为已处理
        processed_ids.add(post_id)

    # 发送通知
    if new_signals:
        log(f"共检测到 {len(new_signals)} 条调仓信号，正在发送邮件...", "ALERT")

        for sig in new_signals:
            post = sig["post"]
            signal_type = sig["signal_type"]
            confidence = sig["confidence"]
            matched = sig["matched"]

            subject = f"【鹿鼎公调仓】{classify_signal_type(signal_type)} - {post.get('title', '新帖')[:30]}"
            body = build_email_content(post, signal_type, confidence, matched)

            send_email(subject, body)
            time.sleep(1)  # 避免发送过快
    else:
        log("本次检查未发现新的调仓信号")

    # 保存状态
    state["processed_ids"] = list(processed_ids)[-500:]  # 保留最近500条
    state["last_check"] = datetime.now(TZ).isoformat()
    save_state(state)

    log("监控完成")
    log("=" * 50)


if __name__ == "__main__":
    main()
